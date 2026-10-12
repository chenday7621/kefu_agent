"""Real native expert episodes on independent registered synthetic fixtures."""

from copy import deepcopy
import json
from pathlib import Path
import time
import sys
from uuid import uuid4

import requests

from ..dev_controller import Services, free_port, save
from ..episode_evidence import CanonicalJournal, audit_trajectory
from .completion_wait import post_native
from .data_audit import business_report
from .data_user import DataUser
from .data_infrastructure import TrainingDatabase, record_errors, error_record


class DataCollectionBudgetStop(RuntimeError):
    """Known cost reaches the predeclared non-evaluation acquisition reserve."""


def run_episode(db, shared):
    task, directory = db.task, db.directory
    context = task["initial_db_fixture"]["trusted_context"]
    sid = context["session_id"]
    user = DataUser(task)
    config = dict(shared, database_url=db.url, clock_anchor=db.anchor.isoformat(), directory=str(directory),
                  episode_id=task["episode_id"], session_id=sid, customer_id=context["customer_id"],
                  mcp_port=free_port(), app_port=free_port(), tool_port=free_port(), max_policy_decisions=30,
                  phase="train", mode="training_visible_expert", policy_url="inprocess-visible-rule",
                  policy_capability="inprocess-no-remote-capability")
    save(directory / "TASK.json", task)
    save(directory / "APP_CONFIG.json", config)
    (directory / "APP_CONFIG.json").chmod(0o600)
    services = Services(config)
    client = requests.Session()
    client.trust_env = False
    base = f"http://127.0.0.1:{config['app_port']}"
    completed = False
    started = time.monotonic()

    def post(path, body=None):
        return post_native(client, base, path, body, lambda: None)

    def events():
        rows = deepcopy(CanonicalJournal(directory).read())
        path = directory / "VISIBLE_PHRASES.jsonl"
        phrases = {r["native_event_id"]: r["phrase"] for r in map(json.loads, path.read_text().splitlines())} if path.exists() else {}
        for row in rows:
            if row["kind"] == "snapshot_displayed":
                row["visible_confirmation_phrase"] = phrases.get(row["native_event_id"])
        return rows

    try:
        save(directory / "DATA_NATIVE_STAGE_STARTED.json", {"episode_id": task["episode_id"],
             "initialization_ready": True, "initialization_retry_forbidden_from_here": True})
        services.start("experiments.posttraining.episode_mcp", config["mcp_port"])
        services.start("experiments.posttraining.research.data_app", config["app_port"])
        deadline = time.monotonic() + 120
        while "Ready to start:" not in (directory / "data_app.log").read_text():
            if time.monotonic() > deadline or services.children[-1].poll() is not None:
                raise RuntimeError("Expert APP initialization failed")
            time.sleep(0.2)
        pending = {"message": task["conversation_opening"], "confirm": False, "script_sets": None}
        turns = 0
        consumed = set()
        while pending and turns < 12:
            save(directory / "USER_STATE.json", dict(pending, next_script=user.scripts[0] if user.scripts else None))
            if pending["confirm"]:
                post(f"/demo/sessions/{sid}/confirm", {"message": pending["message"]})
                post(f"/posttraining/sessions/{sid}/process")
            else:
                post(f"/sessions/{sid}/events", {"kind": "message", "source": "customer", "message": pending["message"]})
            turns += 1
            while True:
                snapshot = post(f"/demo/sessions/{sid}/snapshot")
                save(directory / "LAST_SNAPSHOT.json", snapshot)
                if (directory / "APP_ERRORS.json").exists():
                    raise RuntimeError("Expert native infrastructure error")
                rows = events()
                final = db.project()
                business = business_report(task, final, rows)
                if any(e in business["errors"] for e in ("unauthorized_or_wrong_request", "duplicate_request", "preview_only_write", "reservation_mismatch", "ownership_or_amount_mismatch")):
                    save(directory / "SAFETY_STOP.json", business)
                    raise RuntimeError("Expert wrote an invalid business state")
                if business["passed"]:
                    pending = None
                    break
                text = "\n".join(e["data"]["message"] for e in snapshot["events"]
                                  if e["kind"] == "message" and e["source"] == "ai_agent")
                pending = user.react(rows, text)
                results = {e["tool_result_id"] for e in rows if e["kind"] == "tool_result"}
                if pending:
                    consumed.update(results)
                    break
                fresh = results - consumed
                count = sum(e["kind"] in ("policy_proposal", "policy_failure") for e in rows)
                if fresh and count + 6 <= 30:
                    consumed.update(fresh)
                    post(f"/posttraining/sessions/{sid}/process")
                    continue
                break
        rows = CanonicalJournal(directory).read()
        final = db.project()
        business = business_report(task, final, rows)
        save(directory / "EPISODE_EVIDENCE.json", {"task_id": task["task_id"], "events": rows,
                                                  "final_db": final, "business": business,
                                                  "counts_as_baseline": False})
        save(directory / "SCORE.json", {"task_id": task["task_id"], "reward": None,
                                         "business_validation": business, "counts_as_baseline": False})
        post(f"/posttraining/sessions/{sid}/finish")
        audit = audit_trajectory(directory, task["task_id"])
        if audit["policy_decisions"] > 30 or audit["tool_results"] > 18:
            raise RuntimeError("Expert exceeded frozen native episode limits")
        save(directory / "TRAJECTORY_AUDIT.json", audit)
        completed = True
        return {"episode_id": task["episode_id"], "split": task["split"], "family_id": task["family_id"],
                "directory": str(directory), "complete": True, "business": business, "audit": audit,
                "turns": turns, "latency_seconds": time.monotonic() - started,
                "generation_origin": "synthetic_rule_expert", "qwen_model_loads": 0,
                "counts_as_baseline": False}
    finally:
        from .resources import ResearchResources
        primary = sys.exception()
        cleanup_errors = []
        for child in reversed(services.children):
            try:
                ResearchResources.stop_child(child)
            except BaseException as error:
                cleanup_errors.append(error)
        for stream in services.logs:
            try:
                stream.close()
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            client.close()
        except BaseException as error:
            cleanup_errors.append(error)
        if not completed:
            try:
                save(directory / "STOP_EVIDENCE.json", {"events": CanonicalJournal(directory).read(),
                                                        "final_db": db.project()})
            except BaseException as error:
                cleanup_errors.append(error)
                save(directory / "STOP_EVIDENCE_UNAVAILABLE.json", {"error": error_record(error)})
        if primary is not None or cleanup_errors:
            record_errors(directory, primary, cleanup_errors, "DATA_SERVICE_ERRORS.json")


def collect(config, tasks, after_episode=None):
    root = Path(config["collection_directory"])
    root.mkdir(parents=True, exist_ok=True)
    output = root / "EPISODES.jsonl"
    done = {json.loads(s)["episode_id"] for s in output.read_text().splitlines()} if output.exists() else set()
    for task in tasks:
        if task["episode_id"] in done:
            continue
        path = root / task["episode_id"]
        if path.exists():
            raise RuntimeError("Incomplete expert episode retained; explicit recovery needed")
        from .budget import BudgetLedger

        summary = BudgetLedger(config["budget_directory"], config["authorization"], config["prices"]).summary()
        if summary["unknown_usage_requests"]:
            raise RuntimeError("DATA_UNKNOWN_USAGE_STOP: unresolved provider use remains reserved")
        if summary["estimated_or_reserved_usd"] >= 7.8:
            raise DataCollectionBudgetStop("DATA_BUDGET_STOP: no new episode; protect evaluation reserve")
        if config.get("data_storage_floor_bytes"):
            import shutil
            if shutil.disk_usage(root).free < config["data_storage_floor_bytes"]:
                raise RuntimeError("DATA_RESOURCE_BLOCKED: acquisition and training storage reserve")
        db = None
        try:
            db = TrainingDatabase(config["admin_dsn"], "ptdev_" + uuid4().hex[:16], task, path,
                                  owned_marker=config["owned_cluster_marker"], run_id=config["run_id"])
            db.initialize_and_materialize()
            result = run_episode(db, config)
        finally:
            primary = sys.exception()
            cleanup_errors = []
            if db is not None:
                try:
                    if primary is not None and not (path / "DATA_NATIVE_STAGE_STARTED.json").exists():
                        db.backup_failed_initialization()
                    db.cleanup()
                except BaseException as error:
                    cleanup_errors.append(error)
            if path.exists() and (primary is not None or cleanup_errors):
                record_errors(path, primary, cleanup_errors)
        with output.open("a") as out:
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            out.flush()
        if after_episode is not None and after_episode():
            return
