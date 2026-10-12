"""DATA-only owned PG initialization and cleanup; no policy/APP changes."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from apps.retail_demo.db import connect, initialize
from apps.retail_demo.settings import APP
from ..dev_controller import save
from ..dev_protocol import sha
from ..episode_state import EpisodeDatabase, native_history_documents
from ..trajectory import redact
from .resources import ResearchResources


class DataStateUnknown(RuntimeError):
    """No initialization retry or deletion can be justified from this state."""


class DataResources(ResearchResources):
    def close(self):
        if self.pg_running:
            with psycopg.connect(self.admin_dsn, autocommit=True, application_name="ptdata:cluster-close") as conn:
                if Path(conn.execute("SHOW data_directory").fetchone()[0]).resolve() != self.pg_data.resolve():
                    raise DataStateUnknown("DATA shutdown cluster identity mismatch")
                user = conn.execute("SELECT current_user").fetchone()[0]
                clients = conn.execute("SELECT datname,usename,application_name FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid()").fetchall()
                if any(owner != user or not app.startswith("ptdata:") for _, owner, app in clients):
                    save(self.runtime / "logs/DATA_SHUTDOWN_SCOPE_REFUSED.json", {"foreign_clients": clients})
                    raise DataStateUnknown("DATA cluster has foreign clients; shutdown refused")
        super().close()


def error_record(error):
    return {"type": type(error).__name__, "message": redact(str(error))}


def record_errors(directory, primary, cleanup, filename="DATA_LIFECYCLE_ERRORS.json"):
    save(Path(directory) / filename, {"primary": error_record(primary) if primary else None,
                                    "cleanup": [error_record(e) for e in cleanup]})
    if cleanup:
        if primary is not None:
            primary.add_note("Separate DATA cleanup errors retained in " + filename)
        else:
            raise cleanup[0]


class TrainingDatabase(EpisodeDatabase):
    """Native fixture operations, with bounded pre-episode state reconciliation."""

    def __init__(self, admin_dsn, name, task, directory, *, owned_marker, run_id):
        params = psycopg.conninfo.conninfo_to_dict(admin_dsn)
        if params.get("host") != "127.0.0.1" or not name.startswith("ptdev_") or not name.isidentifier():
            raise DataStateUnknown("Not an owned local episode database")
        self.admin_dsn, self.name, self.task = admin_dsn, name, task
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.marker = json.loads(Path(owned_marker).read_text())
        if self.marker["owner_run_id"] != run_id or self.marker["admin_dsn"] != admin_dsn:
            raise DataStateUnknown("DATA cluster ownership mismatch")
        self.application_name = "ptdata:" + name
        self.url = psycopg.conninfo.make_conninfo(admin_dsn, dbname=name, application_name=self.application_name)
        self.materialized = False
        self.anchor = None
        self.creation_started = False
        save(self.directory / "DATA_DB_OWNER.json", {"database": name, "run_id": run_id,
             "fixture_hash": sha(task["initial_db_fixture"]), "application_name": self.application_name,
             "cluster_directory": self.marker["data_directory"]})

    def verify_owned_cluster(self):
        with psycopg.connect(self.admin_dsn, autocommit=True, application_name=self.application_name + ":admin") as conn:
            actual = conn.execute("SHOW data_directory").fetchone()[0]
            if Path(actual).resolve() != Path(self.marker["data_directory"]).resolve():
                raise DataStateUnknown("Connected PostgreSQL is not this run's cluster")
            return conn.execute("SELECT current_user").fetchone()[0]

    def assert_pre_episode(self):
        if any((self.directory / name).exists() for name in (
            "DATA_NATIVE_STAGE_STARTED.json", "APP_CONFIG.json", "USER_STATE.json", "EXPERT_RECORDS.jsonl",
        )):
            raise DataStateUnknown("Native/user/expert/MCP stage started; initialization retry forbidden")

    def inspect(self):
        owner = self.verify_owned_cluster()
        with psycopg.connect(self.admin_dsn, autocommit=True, application_name=self.application_name + ":admin") as conn:
            deadline = time.monotonic() + 5
            while True:
                # CREATE DATABASE runs on postgres, not the destination DB.
                # Drain that exact owned admin connection too before deciding
                # whether a timed-out creation committed or may still commit.
                sessions = conn.execute("SELECT application_name,usename,backend_type FROM pg_stat_activity WHERE (datname=%s OR application_name=%s) AND pid<>pg_backend_pid()", (self.name, self.application_name + ":admin")).fetchall()
                clients = [(app, user) for app, user, kind in sessions if kind == "client backend"]
                if any(app not in (self.application_name, self.application_name + ":admin") or user != owner for app, user in clients):
                    raise DataStateUnknown("Foreign episode connection; refuse retry/termination")
                if not clients:
                    break
                if time.monotonic() > deadline:
                    raise DataStateUnknown("Prior initialization connection remains; commit state unknown")
                time.sleep(0.1)
            row = conn.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (self.name,)).fetchone()
            if row is None:
                return {"exists": False, "migrations": [], "counts": {}}
            if row[0] != owner:
                raise DataStateUnknown("Episode database has a different owner")
        with connect(self.url) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            exists = conn.execute("SELECT to_regclass('retail_demo.schema_migrations') AS name").fetchone()["name"]
            migrations = conn.execute("SELECT version,sha256 FROM schema_migrations ORDER BY version").fetchall() if exists else []
            expected = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (APP / "migrations").glob("*.sql")}
            if any(r["version"] not in expected or expected[r["version"]] != r["sha256"] for r in migrations):
                raise DataStateUnknown("Unexpected or modified migration record")
            versions = [r["version"] for r in migrations]
            if versions != sorted(expected)[:len(versions)]:
                raise DataStateUnknown("Partial migrations are not a known prefix")
            counts = {}
            for table in (*self.task["initial_db_fixture"]["tables"], "parlant_sessions", "parlant_events"):
                if conn.execute("SELECT to_regclass(%s) AS name", ("retail_demo." + table,)).fetchone()["name"]:
                    counts[table] = conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))).fetchone()["n"]
        return {"exists": True, "migrations": migrations, "counts": counts,
                "migrations_complete": len(migrations) == len(expected)}

    def initialize_and_materialize(self):
        for attempt in range(3):
            self.assert_pre_episode()
            state = None
            try:
                # A successful read checks readiness and exact run ownership.
                state = self.inspect()
                save(self.directory / f"DATA_INIT_ATTEMPT_{attempt}.json", {"attempt": attempt,
                     "reconciliation": state, "stage_started": False, "status": "checked"})
                if state["exists"] and not self.creation_started:
                    raise DataStateUnknown("Database existed before this DATA creation intent")
                if not state["exists"]:
                    self.creation_started = True
                    with psycopg.connect(self.admin_dsn, autocommit=True,
                                         application_name=self.application_name + ":admin") as conn:
                        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.name)))
                    state = self.inspect()
                if not state["migrations_complete"]:
                    if any(state["counts"].values()):
                        raise DataStateUnknown("Partial migration with existing fixture rows")
                    # Native initialize checks hashes and applies missing migrations
                    # in its original transaction. APP connect_timeout stays 5s.
                    initialize(self.url)
                if self.anchor is None:
                    with connect(self.url) as conn:
                        self.anchor = conn.execute("SELECT now() AS t").fetchone()["t"]
                    save(self.directory / "DATA_CLOCK_ANCHOR.json", {"clock_anchor": self.anchor.isoformat()})
                state = self.inspect()
                counts = state["counts"]
                if not any(counts.values()):
                    initial = super().materialize()
                else:
                    # A prior fixture commit may have completed before a metadata
                    # read timed out. Verify it; never replay inserts or move its clock.
                    wanted = self.task["initial_db_fixture"]
                    expected_counts = {t: len(rows) for t, rows in wanted["tables"].items()}
                    expected_counts.update(parlant_sessions=1, parlant_events=len(wanted["trusted_history"]))
                    if counts != expected_counts:
                        raise DataStateUnknown("Partial or unexpected fixture initialization")
                    with connect(self.url) as conn:
                        sessions = conn.execute("SELECT id,doc FROM parlant_sessions").fetchall()
                        context = wanted["trusted_context"]
                        if (len(sessions) != 1 or sessions[0]["id"] != context["session_id"]
                                or sessions[0]["doc"]["customer_id"] != context["customer_id"]
                                or sessions[0]["doc"]["title"] != self.task["task_id"]):
                            raise DataStateUnknown("Initial native session identity mismatch")
                        events = conn.execute("SELECT id,doc FROM parlant_events").fetchall()
                        if {row["id"]: row["doc"] for row in events} != native_history_documents(wanted, self.anchor):
                            raise DataStateUnknown("Unexpected native user/tool event before initialization retry")
                    initial = self.project()
                    for table in ("customers", "orders", "order_items", "return_operations", "return_requests"):
                        if sorted(initial[table], key=lambda r: r["id"]) != sorted(wanted["tables"][table], key=lambda r: r["id"]):
                            raise DataStateUnknown("Committed initialization differs from frozen fixture")
                    self.materialized = True
                    self.save("INITIAL_PROJECTION.json", initial)
                    self.save("MATERIALIZATION.json", {"task_id": self.task["task_id"], "fixture_hash": sha(wanted),
                        "database": self.name, "clock_anchor": self.anchor.isoformat(), "native_migrations": self.migrations(),
                        "synthetic_initial_history": True, "guards_disabled": False, "projection_hash": sha(initial),
                        "initialization_commit_reconciled_not_reinserted": True})
                with connect(self.url) as conn:
                    conn.execute("UPDATE parlant_sessions SET doc=jsonb_set(doc,'{metadata,posttraining_split}',%s) WHERE id=%s",
                                 (Jsonb(self.task["split"]), self.task["initial_db_fixture"]["trusted_context"]["session_id"]))
                save(self.directory / f"DATA_INIT_ATTEMPT_{attempt}.json", {"attempt": attempt, "status": "ready",
                     "reconciliation": state, "stage_started": False, "clock_anchor": self.anchor.isoformat()})
                return initial
            except BaseException as error:
                save(self.directory / f"DATA_INIT_ATTEMPT_{attempt}.json", {"attempt": attempt, "status": "failed",
                     "reconciliation": state, "stage_started": False, "error": error_record(error)})
                if not isinstance(error, (psycopg.errors.ConnectionTimeout, psycopg.errors.ConnectionFailure)) or attempt == 2:
                    raise
                self.assert_pre_episode()
                # Reconciliation on the next attempt must succeed before any
                # CREATE/migration/write. Unknown state pauses instead of guessing.
                time.sleep((1, 2)[attempt])

    def backup_failed_initialization(self):
        """Keep real partial state before deleting a failed, pre-native fixture."""
        self.assert_pre_episode()
        self.verify_owned_cluster()
        if not self.creation_started:
            return
        from .resources import PG_ROOT
        path = self.directory / "DATA_FAILED_INITIALIZATION.dump"
        environment = dict(os.environ, LD_LIBRARY_PATH=str(PG_ROOT / "usr/lib/x86_64-linux-gnu")
                           + ":" + os.environ.get("LD_LIBRARY_PATH", ""))
        result = subprocess.run([str(PG_ROOT / "usr/lib/postgresql/16/bin/pg_dump"),
                 "--dbname", self.url, "--format=custom", "--file", str(path)],
                 env=environment, capture_output=True, timeout=30)
        if result.returncode:
            save(self.directory / "DATA_BACKUP_ERROR.json", {"returncode": result.returncode,
                 "stderr": redact(result.stderr.decode(errors="replace"))})
            raise DataStateUnknown("Partial initialization backup failed; database retained")
        path.chmod(0o600)
        save(self.directory / "DATA_BACKUP.json", {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
             "bytes": path.stat().st_size, "initialization_only": True})

    def cleanup(self):
        owner = self.verify_owned_cluster()
        with psycopg.connect(self.admin_dsn, autocommit=True,
                             application_name=self.application_name + ":admin") as conn:
            row = conn.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (self.name,)).fetchone()
            if row is None:
                save(self.directory / "CLEANUP.json", {"database_dropped": True, "database": self.name, "already_absent": True})
                return
            if row[0] != owner:
                raise DataStateUnknown("Cleanup database owner mismatch")
            if not self.creation_started:
                raise DataStateUnknown("Cleanup requires this DATA database creation intent")
            sessions = conn.execute("SELECT pid,application_name,usename,backend_type FROM pg_stat_activity WHERE datname=%s", (self.name,)).fetchall()
            if any(kind == "client backend" and (app != self.application_name or user != owner)
                   for _, app, user, kind in sessions):
                raise DataStateUnknown("Foreign client in episode database; do not terminate")
            for pid, app, user, kind in sessions:
                if kind == "client backend":
                    conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
            deadline = time.monotonic() + 5
            while conn.execute("SELECT 1 FROM pg_stat_activity WHERE datname=%s AND backend_type='client backend'", (self.name,)).fetchone():
                if time.monotonic() > deadline:
                    raise DataStateUnknown("Owned episode connections did not close within cleanup bound")
                time.sleep(0.1)
            conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(self.name)))
            if conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (self.name,)).fetchone():
                raise DataStateUnknown("Episode database survived cleanup")
        save(self.directory / "CLEANUP.json", {"database_dropped": True, "database": self.name,
             "owned_connections_terminated": [pid for pid, _, _, kind in sessions if kind == "client backend"]})
