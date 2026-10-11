"""Formal DEV episodes: visible user reactions, real tools, native scoring."""

from copy import deepcopy
import json
from pathlib import Path
import time
from uuid import uuid4

import requests

from ..budget_protocol import verify_execution_bundle
from ..dev_controller import Services, free_port, save
from ..episode_state import EpisodeDatabase
from ..episode_evidence import CanonicalJournal, audit_trajectory
from ..scorer import score_episode
from ..dev_protocol import sha
from .user_controller import ScriptedUser


def run_episode(db, shared):
    task, directory = db.task, db.directory
    context = task["initial_db_fixture"]["trusted_context"]
    sid = context["session_id"]
    user = ScriptedUser(
        task["conversation_opening"],
        task["visible_goal_fields"],
        task["user_followups"],
        task["family"],
    )
    config = dict(
        shared,
        database_url=db.url,
        clock_anchor=db.anchor.isoformat(),
        directory=str(directory),
        episode_id=task["task_id"],
        session_id=sid,
        customer_id=context["customer_id"],
        mcp_port=free_port(),
        app_port=free_port(),
        tool_port=free_port(),
        max_policy_decisions=30,
        mode="formal_qwen",
    )
    save(directory / "APP_CONFIG.json", config)
    (directory / "APP_CONFIG.json").chmod(0o600)
    save(directory / "TASK.json", task)
    services = Services(config)
    client = requests.Session()
    client.trust_env = False
    url = f"http://127.0.0.1:{config['app_port']}"

    def post(path, body=None):
        response = client.post(url + path, json=body or {}, timeout=600)
        response.raise_for_status()
        return response.json()

    def events_with_visible_phrases():
        events = deepcopy(CanonicalJournal(directory).read())
        path = directory / "VISIBLE_PHRASES.jsonl"
        phrases = (
            {
                r["native_event_id"]: r["phrase"]
                for r in map(json.loads, path.read_text().splitlines())
            }
            if path.exists()
            else {}
        )
        for e in events:
            if e["kind"] == "snapshot_displayed":
                e["visible_confirmation_phrase"] = phrases.get(e["native_event_id"])
        return events

    start = time.monotonic()
    snapshots = []
    turns = 0
    pending = {"message": user.opening, "confirm": False, "script_sets": None}
    infrastructure_ok = False
    try:
        services.start("experiments.posttraining.episode_mcp", config["mcp_port"])
        services.start("experiments.posttraining.research.episode_app", config["app_port"])
        deadline = time.monotonic() + 120
        while "Ready to start:" not in (directory / "episode_app.log").read_text():
            if time.monotonic() > deadline or services.children[-1].poll() is not None:
                raise RuntimeError("APP setup did not complete")
            time.sleep(0.2)
        consumed_results = set()
        failure_type = None
        while pending and turns < task["episode_budget"]["max_user_turns"]:
            state = {
                "message": pending["message"],
                "script_sets": pending.get("script_sets"),
                "next_script": user.scripts[0] if user.scripts else None,
            }
            save(directory / "USER_STATE.json", state)
            if pending["confirm"]:
                post(f"/demo/sessions/{sid}/confirm", {"message": pending["message"]})
                post(f"/posttraining/sessions/{sid}/process")
            else:
                post(
                    f"/sessions/{sid}/events",
                    {"kind": "message", "source": "customer", "message": pending["message"]},
                )
            turns += 1
            while True:
                snapshot = post(f"/demo/sessions/{sid}/snapshot")
                snapshots.append(snapshot)
                save(directory / "APP_SNAPSHOTS.json", snapshots)
                # Audit after actual processing; privileged DB facts never
                # select a model action or scripted confirmation.
                live_trace = {
                    "task_id": task["task_id"],
                    "scoring_version": task["scoring_version"],
                    "initial_fixture_hash": sha(task["initial_db_fixture"]),
                    "evidence_source": "isolated_controller",
                    "events": CanonicalJournal(directory).read(),
                    "final_db": db.project(),
                }
                live_score = score_episode(task, live_trace)
                if any(
                    e.get("reason") == "forbidden_final_db_change"
                    for e in live_score["critical_evidence"]
                ):
                    db.save("SAFETY_STOP.json", live_trace)
                    raise RuntimeError(
                        "Actual forbidden business write; stop the entire experiment"
                    )
                if (directory / "APP_ERRORS.json").exists():
                    raise RuntimeError("Native infrastructure error; no result stitching")
                events = events_with_visible_phrases()
                pending = user.react(events)
                if pending:
                    break
                proposals = [
                    e for e in events if e["kind"] in ("policy_proposal", "policy_failure")
                ]
                results = {e["tool_result_id"] for e in events if e["kind"] == "tool_result"}
                # Only wait/continue while a frozen visible-user trigger is
                # pending and new real observations exist. Never pick a next tool.
                fresh = results - consumed_results
                if user.scripts and fresh and len(proposals) + 6 <= 30:
                    consumed_results.update(fresh)
                    post(f"/posttraining/sessions/{sid}/process")
                    continue
                if user.scripts:
                    failure_type = "USER_TRIGGER_UNREACHED"
                if (directory / "BUDGET_STOP.json").exists():
                    failure_type = "DECISION_BUDGET"
                break
        evidence = {
            "task_id": task["task_id"],
            "scoring_version": task["scoring_version"],
            "initial_fixture_hash": sha(task["initial_db_fixture"]),
            "evidence_source": "isolated_controller",
            "events": CanonicalJournal(directory).read(),
            "final_db": db.project(),
        }
        db.save("EPISODE_EVIDENCE.json", evidence)
        score = score_episode(task, evidence)
        assert score == score_episode(task, evidence)
        db.save("SCORE.json", score)
        post(f"/posttraining/sessions/{sid}/finish")
        audit = audit_trajectory(directory, task["task_id"])
        if audit["policy_decisions"] > 30 or audit["tool_results"] > 18:
            raise RuntimeError("Frozen native budget exceeded")
        db.save("TRAJECTORY_AUDIT.json", audit)
        infrastructure_ok = True
        return {
            "task_id": task["task_id"],
            "family": task["family"],
            "scoreable": True,
            "score": score,
            "audit": audit,
            "failure_type": failure_type,
            "latency_seconds": time.monotonic() - start,
        }
    finally:
        services.stop()
        client.close()
        if not infrastructure_ok:
            db.save(
                "STOP_EVIDENCE.json",
                {
                    "task_id": task["task_id"],
                    "events": CanonicalJournal(directory).read(),
                    "final_db": db.project(),
                },
            )


def run_batch(config):
    # Refuse arbitrary loopback DSNs: it must match the owned cluster marker.
    marker = json.loads(Path(config["owned_cluster_marker"]).read_text())
    if config["admin_dsn"] != marker["admin_dsn"]:
        raise ValueError("Episode DSN differs from owned isolated cluster")
    import psycopg

    with psycopg.connect(config["admin_dsn"]) as conn:
        actual = conn.execute("SHOW data_directory").fetchone()[0]
    if Path(actual).resolve() != Path(marker["data_directory"]).resolve():
        raise ValueError("Refusing existing demo/production PostgreSQL")
    tasks = verify_execution_bundle(Path(config["bundle"]))
    directory = Path(config["output_directory"])
    directory.mkdir(parents=True, exist_ok=True)
    result_file = directory / "PER_TASK.jsonl"
    completed = (
        {r["task_id"] for r in map(json.loads, result_file.read_text().splitlines())}
        if result_file.exists()
        else set()
    )
    for task in tasks:
        if task["task_id"] in completed:
            continue
        # A partial episode requires an explicitly audited recovery, never a
        # silent reset or a normal-failure retry.
        d = directory / task["task_id"]
        if d.exists():
            raise RuntimeError(
                "Partial episode retained; explicit infrastructure recovery review required"
            )
        db = EpisodeDatabase(config["admin_dsn"], "ptdev_" + uuid4().hex[:16], task, d)
        try:
            db.materialize()
            result = run_episode(db, config)
        finally:
            db.cleanup()
        with result_file.open("a") as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            stream.flush()
