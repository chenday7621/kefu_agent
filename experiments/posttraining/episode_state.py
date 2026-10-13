"""Independent, guarded PostgreSQL DEV fixture materialization and projection.

This module never connects to the APP's configured database. Its caller owns a
new cluster and passes a local DSN plus a randomly named episode database.
"""

from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from apps.retail_demo.db import connect, initialize
from parlant.core.sessions import SessionDocumentStore

from .dev_protocol import sha


def normalize(value, anchor):
    if isinstance(value, datetime):
        return {"seconds_from_episode_start": round((value - anchor).total_seconds(), 6)}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: normalize(v, anchor) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(v, anchor) for v in value]
    return value


def resolve(value, anchor):
    if isinstance(value, dict) and set(value) == {"seconds_from_episode_start"}:
        return anchor + timedelta(seconds=value["seconds_from_episode_start"])
    if isinstance(value, dict):
        return {k: resolve(v, anchor) for k, v in value.items()}
    return value


def insert(conn, table, row):
    conn.execute(sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, row)),
        sql.SQL(",").join(sql.Placeholder() for _ in row)), list(row.values()))


def native_history_documents(fixture, anchor):
    """Add native presentation metadata to trusted, immutable fixture messages.

    Participant.id is optional in MessageEventData. Keep the hidden customer
    identity out of the payload; display_name comes from the owned fixture row.
    Unsupported roles fail closed instead of inventing an agent/confirmation.
    """
    context = fixture["trusted_context"]
    customers = [r for r in fixture["tables"]["customers"]
                 if r["id"] == context["customer_id"]]
    if len(customers) != 1 or not isinstance(customers[0]["name"], str):
        raise ValueError("History requires exactly one trusted customer name")
    participant = {"display_name": customers[0]["name"]}
    documents = {}
    for offset, original in enumerate(fixture["trusted_history"]):
        raw = deepcopy(original)
        if (raw["session_id"] != context["session_id"] or raw["kind"] != "message"
                or raw["source"] not in ("customer", "customer_ui")
                or raw["origin"] != "synthetic_initial_fixture"
                or not isinstance(raw["data"].get("message"), str)):
            raise ValueError("Unsupported or untrusted initial history event")
        if raw["id"] in documents:
            raise ValueError("Duplicate initial history event id")
        if "participant" in raw["data"] and raw["data"]["participant"] != participant:
            raise ValueError("History participant differs from trusted fixture role")
        raw["data"].setdefault("participant", deepcopy(participant))
        doc = {"version": SessionDocumentStore.VERSION.to_string(),
               "creation_utc": anchor.isoformat(), "offset": offset,
               "trace_id": "synthetic-initial-fixture", "deleted": False,
               "metadata": {"fixture_origin": raw["origin"]},
               **{k: raw[k] for k in ("id", "session_id", "kind", "source", "data")}}
        documents[doc["id"]] = doc
    return documents


class EpisodeDatabase:
    def __init__(self, admin_dsn, name, task, directory):
        if not name.startswith("ptdev_") or not name.isidentifier():
            raise ValueError("Refusing a non-episode database name")
        params = psycopg.conninfo.conninfo_to_dict(admin_dsn)
        if params.get("host") not in ("127.0.0.1", "localhost"):
            raise ValueError("DEV database must be the owned local cluster")
        self.admin_dsn, self.name, self.task = admin_dsn, name, task
        self.url = psycopg.conninfo.make_conninfo(admin_dsn, dbname=name)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            initialize(self.url)
            with connect(self.url) as conn:
                self.anchor = conn.execute("SELECT now() AS t").fetchone()["t"]
        except BaseException:
            self.cleanup()
            raise
        self.materialized = False

    def materialize(self):
        task, anchor = self.task, self.anchor
        fixture = task["initial_db_fixture"]
        tables = deepcopy(fixture["tables"])
        context = fixture["trusted_context"]
        session = {"id": context["session_id"],
                   "version": SessionDocumentStore.VERSION.to_string(),
                   "creation_utc": anchor.isoformat(), "customer_id": context["customer_id"],
                   "agent_id": "retail-persistent-demo", "mode": "auto",
                   "title": task["task_id"], "consumption_offsets": {"client": 0},
                   "agent_states": [], "metadata": {"posttraining_split": "DEV"}, "labels": []}
        with connect(self.url) as conn:
            for table in ("customers", "orders", "order_items"):
                for row in tables[table]:
                    if table == "order_items":
                        reserved_by_requests = sum(r["quantity"] for r in tables["return_requests"]
                                                   if r["item_id"] == row["id"])
                        row["reserved_return_quantity"] -= reserved_by_requests
                    insert(conn, table, resolve(row, anchor))
            insert(conn, "parlant_sessions", {"id": session["id"], "doc": Jsonb(session)})
            # Historical fixture events are explicitly synthetic, never episode confirmations.
            documents = native_history_documents(fixture, anchor)
            for doc in documents.values():
                insert(conn, "parlant_events", {"id": doc["id"], "doc": Jsonb(doc)})
            conn.execute("UPDATE parlant_sessions SET next_offset=%s WHERE id=%s",
                         (len(documents), session["id"]))
            # Resolve forward supersession FK after all immutable snapshots exist.
            for row in tables["return_operations"]:
                insert(conn, "return_operations", resolve(dict(row, superseded_by=None), anchor))
            for row in tables["return_operations"]:
                if row["superseded_by"]:
                    conn.execute("UPDATE return_operations SET superseded_by=%s WHERE id=%s",
                                 (row["superseded_by"], row["id"]))
            # Prior requests precede session bindings: native triggers reserve once,
            # and do not backfill notifications for historical requests.
            for row in tables["return_requests"]:
                insert(conn, "return_requests", row)
            for row in tables["session_operations"]:
                entry = dict(row)
                eid = entry.get("confirmation_event_id")
                entry["confirmation_event"] = Jsonb(documents[eid]) if eid else None
                request = next((r for r in tables["return_requests"] if r["operation_id"] == entry["operation_id"]), None)
                entry["request_id"] = request["id"] if request else None
                insert(conn, "session_operations", entry)
        self.materialized = True
        actual = self.project()
        # Every declared immutable fact and initial request is exact. Derived
        # current-operation pointers are generated by unchanged native triggers.
        for table in ("customers", "orders", "order_items", "return_operations", "return_requests"):
            wanted = fixture["tables"][table]
            if sorted(actual[table], key=lambda r: r["id"]) != sorted(wanted, key=lambda r: r["id"]):
                raise ValueError("Materialized fixture differs: " + table)
        self.save("INITIAL_PROJECTION.json", actual)
        self.save("MATERIALIZATION.json", {"task_id": task["task_id"], "fixture_hash": sha(fixture),
                  "database": self.name, "clock_anchor": anchor.isoformat(),
                  "native_migrations": self.migrations(), "synthetic_initial_history": True,
                  "guards_disabled": False, "projection_hash": sha(actual)})
        return actual

    def migrations(self):
        with connect(self.url) as conn:
            return conn.execute("SELECT version,sha256 FROM schema_migrations ORDER BY version").fetchall()

    def operation(self, operation_id):
        with connect(self.url) as conn:
            row = conn.execute("SELECT * FROM return_operations WHERE id=%s", (operation_id,)).fetchone()
        if row is None:
            raise ValueError("Native result references absent operation")
        return normalize(row, self.anchor)

    def project(self):
        initial = self.task["initial_db_fixture"]["tables"]
        result = {}
        with connect(self.url) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            for table, rows in initial.items():
                actual = conn.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))).fetchall()
                if table in ("customers", "orders", "order_items", "return_operations", "return_requests"):
                    # Stable columns frozen in this protocol; no volatile request timestamps.
                    columns = set(rows[0]) if rows else {
                        "return_operations": {"id", "customer_id", "order_id", "item_id", "quantity", "reason",
                                              "quoted_unit_price_cents", "created_at", "expires_at", "confirmed_at",
                                              "confirmation_source", "superseded_by"},
                        "return_requests": {"id", "operation_id", "customer_id", "order_id", "item_id", "quantity",
                                            "reason", "amount_cents", "status"},
                    }.get(table, set())
                    projected = [{k: r[k] for k in columns} for r in actual]
                    # Fixture row order is part of the frozen projection contract.
                    order = {r["id"]: i for i, r in enumerate(rows)}
                    projected.sort(key=lambda r: (order.get(str(r["id"]), len(order)), str(r["id"])))
                else:
                    projected = actual
                result[table] = normalize(projected, self.anchor)
        return result

    def save(self, name, data):
        (self.directory / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def cleanup(self):
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(self.name)))
            present = conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (self.name,)).fetchone()
            if present:
                raise RuntimeError("Episode database survived cleanup")
        self.save("CLEANUP.json", {"database_dropped": True, "database": self.name})
