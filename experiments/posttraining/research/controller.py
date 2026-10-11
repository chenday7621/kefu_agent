"""Formal DEV episodes: visible user reactions, real tools, native scoring."""

from copy import deepcopy
import json
from pathlib import Path
import time
from uuid import uuid4

import requests
import subprocess

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
        from .completion_wait import post_native

        def observed_wait():
            with (directory / "SNAPSHOT_WAIT.jsonl").open("a") as out:
                out.write(
                    json.dumps(
                        {
                            "status": "native_processing_pending",
                            "canonical_events_observed": len(CanonicalJournal(directory).read()),
                        }
                    )
                    + "\n"
                )

        return post_native(client, url, path, body, observed_wait)

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
                if (directory / "TOOL_BUDGET_STOP.json").exists():
                    pending = None
                    failure_type = "NATIVE_TOOL_BUDGET"
                    break
                events = events_with_visible_phrases()
                results = {e["tool_result_id"] for e in events if e["kind"] == "tool_result"}
                pending = user.react(events)
                if pending:
                    consumed_results.update(results)
                    break
                proposals = [
                    e for e in events if e["kind"] in ("policy_proposal", "policy_failure")
                ]
                # Continue after new actual observations, including after the
                # final confirmation. Never select the next candidate/action.
                fresh = results - consumed_results
                if fresh and len(proposals) + 6 <= 30:
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
        latest = {}
        for path in (directory / "trajectories").glob("policy-*.jsonl"):
            for line in path.read_text().splitlines():
                row = json.loads(line)
                latest[row["policy_step_id"]] = row
        generation_file = Path(config["generation_directory"]) / "GENERATIONS.jsonl"
        generations = (
            [
                r
                for r in map(json.loads, generation_file.read_text().splitlines())
                if r["policy_step_id"] in latest
            ]
            if generation_file.exists()
            else []
        )
        if len(generations) != len(latest):
            raise RuntimeError("Model generations lack exact episode policy attribution")
        model_metrics = {
            "generation_origin": "local_qwen",
            "policy_decisions": len(latest),
            "joint_rounds": len({r["engine_iteration"] for r in latest.values()}),
            "parse_failures": sum(r["parse_status"] == "failed" for r in latest.values()),
            "prompt_tokens": sum(r["input_tokens"] for r in generations),
            "output_tokens": sum(r["output_tokens"] for r in generations),
            "peak_gpu_allocated_bytes": max(
                (r["peak_allocated_bytes"] for r in generations), default=None
            ),
            "peak_gpu_reserved_bytes": max(
                (r["peak_reserved_bytes"] for r in generations), default=None
            ),
            "model_generation_seconds": sum(r["latency_seconds"] for r in generations),
        }
        db.save("MODEL_METRICS.json", model_metrics)
        infrastructure_ok = True
        return {
            "task_id": task["task_id"],
            "attempt_directory": str(directory),
            "family": task["family"],
            "scoreable": True,
            "score": score,
            "audit": audit,
            "failure_type": failure_type,
            "latency_seconds": time.monotonic() - start,
            "model_metrics": model_metrics,
        }
    finally:
        try:
            services.stop()
        except TimeoutError:
            from .resources import ResearchResources

            for child in services.children:
                ResearchResources.stop_child(child)
        except subprocess.TimeoutExpired:
            from .resources import ResearchResources

            for child in services.children:
                ResearchResources.stop_child(child)
        for stream in services.logs:
            if not stream.closed:
                stream.close()
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
        d = directory / config.get("episode_attempt_directories", {}).get(
            task["task_id"], task["task_id"]
        )
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
