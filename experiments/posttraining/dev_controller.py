"""Sequential, model-free DEV controller. Never treats oracle traces as baseline.

The baseline safety barrier is a complete 30-task real-service dry run. This
entrance intentionally cannot load a model or fall back to an external provider.
"""

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4

import requests

from .dev_protocol import confirmation_phrase, sha
from .budget_protocol import f3_tools, verify_execution_bundle
from .episode_evidence import CanonicalJournal, audit_trajectory
from .episode_state import EpisodeDatabase
from .scorer import score_episode


def save(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def validate_completion(score, budget_stopped):
    """A scored task failure is complete; a protocol budget stop is not."""
    if budget_stopped and not score["metrics"]["task_success"]:
        raise RuntimeError("Frozen decision budget stopped a nonterminal episode")


class Services:
    def __init__(self, config):
        self.config, self.children, self.logs = config, [], []

    def start(self, module, port):
        directory = Path(self.config["directory"])
        log = (directory / (module.rsplit(".", 1)[1] + ".log")).open("w")
        self.logs.append(log)
        env = dict(os.environ, CUDA_VISIBLE_DEVICES="", DEMO_DISABLE_LLM="1",
                   HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false",
                   PARLANT_HOME=str(directory / "parlant"), TMPDIR=str(directory / "tmp"))
        (directory / "tmp").mkdir(exist_ok=True)
        child = subprocess.Popen([sys.executable, "-m", module, "--config", str(directory / "APP_CONFIG.json")],
                                 stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        self.children.append(child)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError(module + " exited; inspect its NAS log")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.2)
        raise TimeoutError(module + " did not bind owned port")

    def stop(self):
        for child in reversed(self.children):
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGINT)
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGTERM)
                    child.wait(timeout=10)
        for log in self.logs:
            log.close()


def run_episode(db):
    task, directory = db.task, db.directory
    sid = task["initial_db_fixture"]["trusted_context"]["session_id"]
    visible = deepcopy(task["visible_goal_fields"])
    scripts = deepcopy(task["user_followups"])
    state = {"visible": visible, "message": task["conversation_opening"],
             "next_script": scripts[0] if scripts else None, "script_sets": None}
    config = {"database_url": db.url, "clock_anchor": db.anchor.isoformat(),
              "directory": str(directory), "mcp_port": free_port(), "app_port": free_port(), "tool_port": free_port(),
              "mode": "oracle", "qwen_generations": 0, "deepseek_calls": 0}
    save(directory / "APP_CONFIG.json", config)
    save(directory / "TASK.json", task)
    save(directory / "USER_STATE.json", state)
    services = Services(config)
    client = requests.Session()
    client.trust_env = False
    base = f"http://127.0.0.1:{config['app_port']}"

    def post(path, body=None):
        response = client.post(base + path, json=body or {}, timeout=120)
        if response.status_code >= 400:
            raise RuntimeError(f"APP endpoint {path.rsplit('/', 1)[-1]} status {response.status_code}: {response.text[:500]}")
        return response.json()

    started = time.monotonic()
    try:
        services.start("experiments.posttraining.episode_mcp", config["mcp_port"])
        services.start("experiments.posttraining.episode_app", config["app_port"])
        # Wait for guidelines and real discovery, not merely the listening socket.
        deadline = time.monotonic() + 120
        log = directory / "episode_app.log"
        while "Ready to start:" not in log.read_text():
            if time.monotonic() > deadline or services.children[-1].poll() is not None:
                raise RuntimeError("APP setup not completed")
            time.sleep(0.2)
        journal = CanonicalJournal(directory)
        snapshots = []
        turns = 0
        boundary_results = 0
        while turns < task["episode_budget"]["max_user_turns"]:
            save(directory / "USER_STATE.json", state)
            if state.get("confirmed_operation_id"):
                post(f"/demo/sessions/{sid}/confirm", {"message": state["message"]})
                post(f"/posttraining/sessions/{sid}/process")
            else:
                post(f"/sessions/{sid}/events", {"kind": "message", "source": "customer", "message": state["message"]})
            snapshot = post(f"/demo/sessions/{sid}/snapshot")
            snapshots.append(snapshot)
            save(directory / "APP_SNAPSHOTS.json", snapshots)
            turns += 1
            if (directory / "APP_ERRORS.json").exists():
                raise RuntimeError("Native APP processing failed; see APP_ERRORS.json and process log")
            events = journal.read()
            # The unchanged APP can finish a preparation cycle after a read
            # without evaluating the next candidate. Resume the SAME actual
            # customer event only if a fresh native result was persisted.
            # No extra scripted message, guessed tool result, or prompt is added.
            def resume_after_results():
                nonlocal boundary_results, snapshot, events
                results = [e for e in events if e["kind"] == "tool_result"]
                proposals = [e for e in events if e["kind"] in ("policy_proposal", "policy_failure")]
                if len(results) <= boundary_results or len(proposals) + 6 > task["episode_budget"]["max_policy_decisions"]:
                    return False
                boundary_results = len(results)
                with (directory / "PROCESSING_CONTINUATIONS.jsonl").open("a") as out:
                    out.write(json.dumps({"session_id": sid,
                                          "parent_policy_step_ids": [e["policy_step_id"] for e in proposals],
                                          "native_result_ids": [e["tool_result_id"] for e in results],
                                          "reason": "fresh_native_result_same_customer_turn"}) + "\n")
                post(f"/posttraining/sessions/{sid}/process")
                snapshot = post(f"/demo/sessions/{sid}/snapshot")
                snapshots.append(snapshot)
                save(directory / "APP_SNAPSHOTS.json", snapshots)
                if (directory / "APP_ERRORS.json").exists():
                    raise RuntimeError("APP continuation failed; native error evidence retained")
                events = journal.read()
                return True

            if scripts:
                script = scripts[0]
                trigger = script["trigger"]
                shows = [e for e in events if e["kind"] == "snapshot_displayed"]
                if trigger == "clarification_requested":
                    reached = any(e["kind"] == "clarification_requested" and e["field"] == script["field"] for e in events)
                elif trigger == "snapshot_displayed":
                    reached = bool(shows)
                elif trigger == "latest_snapshot_displayed":
                    reached = bool(shows) and all(shows[-1]["snapshot"].get(k) == task["user_goal"]["target"][k]
                                                  for k in ("order_id", "item_id", "quantity", "reason"))
                elif trigger == "next_user_turn":
                    reached = True
                else:
                    raise ValueError("Unknown frozen user followup trigger: " + trigger)
                while not reached and resume_after_results():
                    shows = [e for e in events if e["kind"] == "snapshot_displayed"]
                    if trigger == "clarification_requested":
                        reached = any(e["kind"] == "clarification_requested" and e["field"] == script["field"] for e in events)
                    elif trigger == "snapshot_displayed":
                        reached = bool(shows)
                    elif trigger == "latest_snapshot_displayed":
                        reached = bool(shows) and all(shows[-1]["snapshot"].get(k) == task["user_goal"]["target"][k]
                                                      for k in ("order_id", "item_id", "quantity", "reason"))
                if not reached:
                    raise RuntimeError("Frozen scripted trigger not reached: " + trigger)
                if "sets" in script:
                    visible.update(script["sets"])
                    state = {"visible": visible, "message": script["message"], "script_sets": script["sets"],
                             "displayed_operation_id": shows[-1]["operation_id"] if shows else None}
                else:
                    matching = [s for s in shows if all(s["snapshot"].get(k) == task["user_goal"]["target"][k]
                                                      for k in ("order_id", "item_id", "quantity", "reason"))]
                    if not matching:
                        raise RuntimeError("No displayed snapshot matching frozen final intent")
                    op = matching[-1]["snapshot"]
                    state = {"visible": visible, "message": confirmation_phrase(op),
                             "script_sets": None, "confirmed_operation_id": op["id"]}
                scripts.pop(0)
                state["next_script"] = scripts[0] if scripts else None
            else:
                if task["family_id"] == "F3":
                    # Stop once every predeclared read/probe has a gate-bound
                    # result. A fresh last result alone is not a reason to add
                    # another all-NO_CALL round after terminal evidence.
                    def terminal_evidence_present():
                        calls = {c["tool_call_id"]: c["tool_name"].split(":")[-1]
                                 for e in events if e["kind"] == "gate_result"
                                 for c in e["accepted_calls"]}
                        returned = {calls[e["tool_call_id"]] for e in events if e["kind"] == "tool_result"}
                        return set(f3_tools(task)).issubset(returned)
                    while not terminal_evidence_present() and resume_after_results():
                        pass
                break
        if scripts:
            raise RuntimeError("User turn budget exhausted before frozen followups")
        final = db.project()
        trace = {"task_id": task["task_id"], "scoring_version": task["scoring_version"],
                 "initial_fixture_hash": sha(task["initial_db_fixture"]), "evidence_source": "isolated_controller",
                 "events": journal.read(), "final_db": final, "generation_origin": "oracle_infrastructure_only"}
        db.save("EPISODE_EVIDENCE.json", trace)
        scored = score_episode(task, trace)
        if scored != score_episode(task, trace):
            raise RuntimeError("Scorer not reproducible")
        db.save("SCORE.json", scored)
        post(f"/posttraining/sessions/{sid}/finish")
        audit = audit_trajectory(directory, task["task_id"])
        db.save("TRAJECTORY_AUDIT.json", audit)
        if audit["tool_results"] > task["episode_budget"]["max_tool_calls"]:
            raise RuntimeError("Oracle exceeded frozen native-tool budget")
        validate_completion(scored, (directory / "BUDGET_STOP.json").exists())
        return {"task_id": task["task_id"], "passed": True, "score": scored,
                "audit": audit, "elapsed_seconds": time.monotonic() - started}
    finally:
        services.stop()
        client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--admin-dsn", required=True)
    parser.add_argument("--materialize-only", action="store_true")
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    if not runtime.is_relative_to(Path("/nas/chenyi/posttraining-qwen")):
        raise ValueError("All new runtime evidence must be on NAS")
    tasks = verify_execution_bundle(args.bundle)
    runtime.mkdir(parents=True, exist_ok=True)
    results = []
    # Unique directories preserve original attempts and every setup failure.
    run = runtime / ("fixture-probe-" if args.materialize_only else "oracle-")
    run = run.with_name(run.name + uuid4().hex[:12])
    run.mkdir()
    summary = {"status": "DRY_RUN_BLOCKED", "passed": 0, "total": 30,
               "qwen_generations": 0, "deepseek_calls": 0, "baseline_episodes": 0,
               "mode": "materialization_probe" if args.materialize_only else "real_pg_mcp_oracle",
               "runtime": str(run), "results": results}
    try:
        for task in tasks:
            db = None
            result = {"task_id": task["task_id"], "family": task["family"], "passed": False}
            try:
                if shutil.disk_usage(runtime).free < 2 * 1024**3:
                    raise RuntimeError("NAS free space below the 2 GiB evidence reserve")
                db = EpisodeDatabase(args.admin_dsn, "ptdev_" + uuid4().hex[:16], task, run / task["task_id"])
                db.materialize()
                result.update({"materialized": True})
                if not args.materialize_only:
                    result.update(run_episode(db))
                else:
                    result["passed"] = True
            except BaseException as exc:
                result["error"] = type(exc).__name__ + ": " + str(exc)
                raise
            finally:
                if db:
                    # Preserve authoritative partial evidence before dropping a
                    # failed episode. Never turn a protocol stop into success.
                    try:
                        if not result["passed"] and not args.materialize_only:
                            db.save("STOP_EVIDENCE.json", {
                                "task_id": task["task_id"], "error": result.get("error"),
                                "events": CanonicalJournal(db.directory).read(),
                                "final_db": db.project(), "generation_origin": "oracle_infrastructure_only",
                            })
                            try:
                                db.save("STOP_TRAJECTORY_AUDIT.json", audit_trajectory(db.directory, task["task_id"]))
                            except Exception as audit_error:
                                db.save("STOP_TRAJECTORY_AUDIT.json", {"passed": False, "error": str(audit_error)})
                    finally:
                        db.cleanup()
                        result["cleanup_passed"] = True
                results.append(result)
                summary["passed"] = sum(r["passed"] for r in results)
                save(args.review / ("MATERIALIZATION_RESULTS.json" if args.materialize_only else "DRY_RUN_RESULTS.json"), summary)
                print(json.dumps(result, ensure_ascii=False), flush=True)
        summary["status"] = "MATERIALIZATION_PASS" if args.materialize_only else "READY_FOR_QWEN_DEV_RUN"
    except BaseException as exc:
        summary["blocked_reason"] = type(exc).__name__ + ": " + str(exc)
    save(args.review / ("MATERIALIZATION_RESULTS.json" if args.materialize_only else "DRY_RUN_RESULTS.json"), summary)
    save(run / "RUN_SUMMARY.json", summary)
    print(json.dumps({"status": summary["status"], "passed": summary["passed"], "reason": summary.get("blocked_reason")}))
    return 0 if summary["passed"] == 30 else 1


if __name__ == "__main__":
    raise SystemExit(main())
