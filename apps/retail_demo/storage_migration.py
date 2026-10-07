"""Stopped-writer backup, insert-only import, verification, switch and rollback.

No automatic restore of business tables. Rollback exports CURRENT PostgreSQL
native stores to separate local files; local rollback is explicitly read-only.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
import shutil
import socket
import subprocess
from psycopg import sql, AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .db import connect, initialize
from .settings import ROOT, APP, load_settings
from .postgres_documents import TABLES

FILES = {name: name + ".json" for name in TABLES}


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()


def stopped(s):
    for port in (s.parlant_port, s.mcp_port, s.tool_port):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                raise RuntimeError(
                    "Stop demo Parlant/MCP before backup, migration, switch or rollback"
                )
        except OSError:
            pass


def business_snapshot(url):
    with connect(url) as c:
        return {
            table: digest(
                c.execute(
                    sql.SQL("SELECT * FROM {} ORDER BY id").format(sql.Identifier(table))
                ).fetchall()
            )
            for table in (
                "customers",
                "orders",
                "order_items",
                "return_operations",
                "return_requests",
                "operation_logs",
            )
        }


def backup(s):
    target = (
        ROOT
        / "runtime-data/retail-demo/backups"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    )
    target.mkdir(parents=True, mode=0o700)
    hashes = {}
    for name in FILES.values():
        source = s.parlant_home / name
        if not source.exists():
            raise RuntimeError("Required native local store file missing: " + name)
        shutil.copy2(source, target / name)
        (target / name).chmod(0o600)
        hashes[name] = hashlib.sha256((target / name).read_bytes()).hexdigest()
    command = [
        "docker",
        "compose",
        "--env-file",
        str(APP / ".env"),
        "-f",
        str(APP / "compose.yaml"),
        "exec",
        "-T",
        "db",
        "pg_dump",
        "-U",
        "retail_demo",
        "-d",
        "retail_demo",
        "-Fc",
    ]
    with (target / "database.dump").open("wb") as stream:
        subprocess.run(command, stdout=stream, check=True, timeout=60)
    (target / "database.dump").chmod(0o600)
    hashes["database.dump"] = hashlib.sha256((target / "database.dump").read_bytes()).hexdigest()
    manifest = {
        "sha256": hashes,
        "business_before": business_snapshot(s.database_url),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "mode": s.session_storage,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (target / "manifest.json").chmod(0o600)
    print("Stopped-writer backup:", target.relative_to(ROOT))
    return target


def read_backup(folder):
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text())
    for name, expected in manifest["sha256"].items():
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Backup hash mismatch: " + name)
    return {namespace: json.loads((folder / name).read_text()) for namespace, name in FILES.items()}


async def import_local(s, folder):
    payload = read_backup(folder)
    before = business_snapshot(s.database_url)
    from .postgres_stores import capture_prepared

    async with await AsyncConnection.connect(
        s.database_url,
        row_factory=dict_row,
        options="-c search_path=retail_demo,public",
        connect_timeout=5,
    ) as c:
        inserted = 0
        skipped = 0
        for namespace, collections in payload.items():
            if set(collections) != set(TABLES[namespace]):
                raise RuntimeError("Unexpected or missing local collections: " + namespace)
            for collection, documents in collections.items():
                table = TABLES[namespace][collection]
                for doc in documents:
                    source_hash = digest(doc)
                    ledger = await (
                        await c.execute(
                            "SELECT source_sha256 FROM parlant_imported_documents WHERE table_name=%s AND document_id=%s",
                            (table, str(doc["id"])),
                        )
                    ).fetchone()
                    if ledger:
                        if ledger["source_sha256"] != source_hash:
                            raise RuntimeError(
                                "Different source document reused an imported ID; no overwrite allowed"
                            )
                        skipped += 1
                        continue
                    row = await (
                        await c.execute(
                            sql.SQL("SELECT doc FROM {} WHERE id=%s").format(sql.Identifier(table)),
                            (str(doc["id"]),),
                        )
                    ).fetchone()
                    if row and row["doc"] != doc:
                        raise RuntimeError(
                            "PostgreSQL/local ID content conflict; nothing was overwritten"
                        )
                    if not row:
                        await c.execute(
                            sql.SQL("INSERT INTO {}(id,doc) VALUES (%s,%s)").format(
                                sql.Identifier(table)
                            ),
                            (str(doc["id"]), Jsonb(doc)),
                        )
                        inserted += 1
                    await c.execute(
                        "INSERT INTO parlant_imported_documents VALUES (%s,%s,%s)",
                        (table, str(doc["id"]), source_hash),
                    )
        await c.execute(
            """UPDATE parlant_sessions s SET next_offset=GREATEST(s.next_offset,COALESCE((SELECT max(event_offset)+1 FROM parlant_events WHERE session_id=s.id),0))"""
        )
        sessions = {doc["id"]: doc for doc in payload["sessions"]["sessions"]}
        for event in sorted(
            payload["sessions"]["events"], key=lambda d: (d["session_id"], d["offset"])
        ):
            session = sessions[event["session_id"]]
            if event["kind"] == "tool":
                await capture_prepared(c, event, session["customer_id"])
            if (
                event["kind"] == "message"
                and event["source"] == "customer"
                and not event["deleted"]
            ):
                text = event.get("data", {}).get("message", "").strip()
                parts = text.split()
                if len(parts) != 5 or parts[0] != "确认退货":
                    continue
                # Import association only from a matching EXISTING human confirmation
                # log. Never promote an old unconfirmed customer message to authority.
                if not re.fullmatch(r"[0-9a-fA-F-]{36}", parts[1]):
                    continue
                row = await (
                    await c.execute(
                        """SELECT o.*,r.id AS request_id FROM return_operations o
                    LEFT JOIN return_requests r ON r.operation_id=o.id
                    WHERE o.id=%s AND o.customer_id=%s AND o.confirmed_at IS NOT NULL
                    AND o.confirmation_source=%s AND EXISTS(SELECT 1 FROM operation_logs l WHERE l.operation_id=o.id AND l.action='confirmed' AND l.details->>'confirmation_text'=%s)""",
                        (
                            parts[1],
                            session["customer_id"],
                            "parlant-human-message:" + session["id"],
                            text,
                        ),
                    )
                ).fetchone()
                if not row:
                    continue
                from .business import confirmation_phrase

                if text != confirmation_phrase(row):
                    raise RuntimeError("Legacy confirmation facts mismatch")
                await c.execute(
                    """INSERT INTO session_operations(session_id,operation_id,customer_id,last_event_offset,confirmation_event_id,confirmation_event,request_id)
                    VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(session_id,operation_id) DO UPDATE SET
                    confirmation_event_id=COALESCE(session_operations.confirmation_event_id,EXCLUDED.confirmation_event_id),
                    confirmation_event=COALESCE(session_operations.confirmation_event,EXCLUDED.confirmation_event),
                    last_event_offset=GREATEST(session_operations.last_event_offset,EXCLUDED.last_event_offset),request_id=COALESCE(session_operations.request_id,EXCLUDED.request_id)""",
                    (
                        session["id"],
                        row["id"],
                        session["customer_id"],
                        event["offset"],
                        event["id"],
                        Jsonb(event),
                        row["request_id"],
                    ),
                )
        await c.execute(
            """UPDATE session_operations b SET request_id=r.id FROM return_requests r WHERE b.operation_id=r.operation_id AND b.customer_id=r.customer_id AND b.request_id IS NULL"""
        )
    assert before == business_snapshot(s.database_url), (
        "Business records changed during store import"
    )
    report = {
        "inserted_documents": inserted,
        "already_imported_skipped": skipped,
        "business_records_unchanged": True,
        "source_backup": str(Path(folder).resolve().relative_to(ROOT)),
    }
    print(json.dumps(report, indent=2))
    return report


def verify(s, folder, allow_post_import_changes=False):
    payload = read_backup(folder)
    counts = {}
    differences = []
    with connect(s.database_url) as c:
        for namespace, collections in payload.items():
            for collection, docs in collections.items():
                table = TABLES[namespace][collection]
                counts[table] = len(docs)
                for doc in docs:
                    row = c.execute(
                        sql.SQL("SELECT doc FROM {} WHERE id=%s").format(sql.Identifier(table)),
                        (str(doc["id"]),),
                    ).fetchone()
                    if not row or row["doc"] != doc:
                        differences.append({"table": table, "id": doc["id"]})
        for sid in [d["id"] for d in payload["sessions"]["sessions"]]:
            original = sorted(
                [d for d in payload["sessions"]["events"] if d["session_id"] == sid],
                key=lambda d: d["offset"],
            )
            actual = [
                r["doc"]
                for r in c.execute(
                    "SELECT doc FROM parlant_events WHERE session_id=%s ORDER BY event_offset",
                    (sid,),
                ).fetchall()
            ]
            if original != actual:
                differences.append(
                    {"session_id": sid, "event_count_order_content_difference": True}
                )
    report = {
        "source_backup": str(Path(folder).resolve().relative_to(ROOT)),
        "counts": counts,
        "id_content_order_differences": differences,
        "passed": not differences or allow_post_import_changes,
        "post_import_changes_preserved": allow_post_import_changes and bool(differences),
    }
    (Path(folder) / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    initial = Path(folder) / "verification_initial.json"
    if not differences and not initial.exists():
        initial.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if differences and not allow_post_import_changes:
        raise RuntimeError("Migration verification differs; no automatic switch")
    return report


def mode(s, value, rollback_home=None):
    path = s.parlant_home / "storage_mode.json"
    tmp = path.with_suffix(".tmp")
    from dotenv import dotenv_values
    import os

    override = os.environ.get("DEMO_SESSION_STORAGE") or dotenv_values(APP / ".env").get(
        "DEMO_SESSION_STORAGE"
    )
    if override and override != value:
        raise RuntimeError(
            "DEMO_SESSION_STORAGE overrides the switch; unset it or select the requested mode explicitly"
        )
    payload = {"mode": value, **({"rollback_home": str(rollback_home)} if rollback_home else {})}
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)


def rollback(s):
    # Export current PG state, never restore an old snapshot over newer user data.
    target = (
        ROOT
        / "runtime-data/retail-demo/rollback_exports"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    )
    target.mkdir(parents=True, mode=0o700)
    with connect(s.database_url) as c:
        for namespace, collections in TABLES.items():
            data = {
                name: [
                    r["doc"]
                    for r in c.execute(
                        sql.SQL("SELECT doc FROM {} ORDER BY id").format(sql.Identifier(table))
                    ).fetchall()
                ]
                for name, table in collections.items()
            }
            dest = target / FILES[namespace]
            dest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            dest.chmod(0o600)
    mode(s, "local_rollback", target)
    print("Read-only local rollback export:", target.relative_to(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["backup", "import-local", "verify", "switch", "rollback", "resume-postgres"],
    )
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    s = load_settings()
    stopped(s)
    with connect(s.database_url) as lock:
        if not lock.execute("SELECT pg_try_advisory_lock(8810,3) AS locked").fetchone()["locked"]:
            raise RuntimeError("A Parlant instance is still writing")
        if args.command == "backup":
            backup(s)
            return
        if args.command in ("import-local", "verify", "switch") and not args.backup:
            raise SystemExit("--backup is required")
        if args.command == "import-local":
            initialize(s.database_url)
            imported = asyncio.run(import_local(s, args.backup))
            verify(
                s,
                args.backup,
                allow_post_import_changes=imported["inserted_documents"] == 0
                and imported["already_imported_skipped"] > 0,
            )
        elif args.command == "verify":
            verify(s, args.backup)
        elif args.command == "switch":
            verify(s, args.backup)
            mode(s, "postgres")
            print("PostgreSQL selected; start the demo")
        elif args.command == "rollback":
            rollback(s)
        elif args.command == "resume-postgres":
            mode(s, "postgres")
            print("PostgreSQL selected; exported rollback files are no longer written")


if __name__ == "__main__":
    main()
