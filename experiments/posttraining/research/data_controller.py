"""Real native expert episodes on independent registered synthetic fixtures."""

from copy import deepcopy
import json
from pathlib import Path
import time
from uuid import uuid4

import requests
from apps.retail_demo.db import connect
from psycopg.types.json import Jsonb

from ..dev_controller import Services, free_port, save
from ..episode_evidence import CanonicalJournal, audit_trajectory
from ..episode_state import EpisodeDatabase
from .completion_wait import post_native
from .data_audit import business_report
from .data_user import DataUser


class TrainingDatabase(EpisodeDatabase):
    def materialize(self):
        initial = super().materialize()
        with connect(self.url) as c:
            c.execute("UPDATE parlant_sessions SET doc=jsonb_set(doc,'{metadata,posttraining_split}',%s) WHERE id=%s",
                      (Jsonb(self.task["split"]), self.task["initial_db_fixture"]["trusted_context"]["session_id"]))
        return initial


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

        for child in reversed(services.children):
            ResearchResources.stop_child(child)
        for stream in services.logs:
            stream.close()
        client.close()
        if not completed:
            save(directory / "STOP_EVIDENCE.json", {"events": CanonicalJournal(directory).read(),
                                                    "final_db": db.project()})


def collect(config, tasks):
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
        if summary["estimated_or_reserved_usd"] >= 7.8 or summary["unknown_usage_requests"]:
            raise RuntimeError("DATA_BUDGET_STOP: preserve evaluation and unknown-use reservations")
        db = TrainingDatabase(config["admin_dsn"], "ptdev_" + uuid4().hex[:16], task, path)
        try:
            db.materialize()
            result = run_episode(db, config)
        finally:
            db.cleanup()
        with output.open("a") as out:
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            out.flush()
