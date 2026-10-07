"""Two explicitly authorized units only; no full runner and no fail-file deletion."""
import argparse
import asyncio
import copy
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from psycopg import sql
from .common import config, activate, save, phase, service, database_evidence, assert_isolated, digest
from .refund_v2 import score
from ..eval_s1.runner import Conversation, metadata
from ..eval_s1.protocol import visible_confirmation
from ..db import connect, initialize
from ..settings import load_settings

WHITELIST = ("r2_R1", "r2_R6")
ORIGINAL_OP = "6b182d02-d47e-430a-8dbe-33bc6bb4fd37"


def check_freeze():
    c = config()
    root = Path(c["original_result_dir"])
    for path, h in json.loads((root / "protocol_source_hashes.json").read_text()).items():
        assert digest(path) == h, "Original APP-S1 source/config changed: " + path
    audit = Path(c["audit_dir"])
    freeze = json.loads((audit / "COMPLETION_EXECUTION_FREEZE.json").read_text())
    for path, h in freeze["completion_source_hashes"].items():
        assert digest(path) == h, "Completion recovery/scoring source changed"
    assert digest(audit / "BUDGET_AUTHORIZATION.json") == freeze["authorization_sha256"]


def fresh_r6():
    """Use a new database, preserving the recovered R1 database in full."""
    c = config()
    assert c["database"] == "app_s1"
    target = "app_s1_completion_r6"
    with connect(load_settings().database_url) as conn:
        conn.autocommit = True
        assert not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (target,)).fetchone(), "R6 database already exists; do not reset/re-run"
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(target), sql.Identifier(c["user"])))
    c["database"] = target
    private = Path(c["runtime_dir"]) / "private.json"
    save(private, c)
    private.chmod(0o600)
    activate()
    initialize(load_settings().database_url)
    # Reuse the identical frozen fixture implementation. Only its evaluation
    # isolation assertion is replaced for this named, empty test database.
    from ..eval_s1 import fixtures
    with connect(load_settings().database_url) as conn:
        assert conn.execute("SELECT count(*) AS n FROM orders").fetchone()["n"] == 0
    fixtures.assert_isolated = assert_isolated
    fixtures.reset()
    current = database_evidence()
    original = json.loads((Path(c["original_result_dir"]) / "tasks/r1_R6/initial_database.json").read_text())
    business = ("customers", "orders", "order_items", "return_operations", "return_requests", "operation_logs")
    # The insert-only historical submitted fixture has a new default expiry,
    # irrelevant to a submitted request. Do not change database clocks/expiry.
    diffs = {t: {k: [a.get(k), b.get(k)] for a, b in zip(original[t], current[t])
                      for k in a if a.get(k) != b.get(k)} for t in business}
    assert all(not differences or (table == "return_operations" and set(differences) == {"expires_at"})
               for table, differences in diffs.items()), "R6 initial business facts differ"
    assert all(len(current[t]) == len(original[t]) for t in business)
    save(Path(c["result_dir"]) / "R6_FIXTURE_IDENTICAL.json", {"database": target,
         "same_business_facts_tables": list(business), "fixture_anchor": c["fixture_anchor"],
         "explicit_generated_timestamp_differences": diffs, "historical_submitted_fixture_expiry_not_used": True,
         "R1_database_preserved": True})


async def run(unit):
    assert unit in WHITELIST
    activate()
    check_freeze()
    assert_isolated()
    c = config()
    root, old = Path(c["result_dir"]), Path(c["original_result_dir"])
    folder = root / "completion_tasks" / unit
    assert not (folder / "attempt.json").exists(), "Existing attempt needs explicit record audit; never rerun"
    assert not (root / "STOP.json").exists(), "Completion paused"
    if unit == "r2_R6":
        assert (root / "completion_tasks/r2_R1/final_database.json").exists(), "R1 must be recorded first"
        fresh_r6()
        c = config()
    planned = next(x for x in json.loads((old / "plan.json").read_text()) if x["attempt_id"] == unit)
    if unit == "r2_R1":
        task = copy.deepcopy(json.loads((old / "tasks/r2_R1/attempt.json").read_text()))
        historical_error = task.pop("error")
        task["historical_interruption"] = historical_error
        task["original_attempt_path"] = str(old / "tasks/r2_R1/attempt.json")
        task["recovery_segment"] = {"started_utc": datetime.now(timezone.utc).isoformat(), "original_turns": len(task["turns"]),
                                    "old_operation_id": ORIGINAL_OP, "reason": "authorized_budget_completion"}
        initial = json.loads((old / "tasks/r2_R1/initial_database.json").read_text())
    else:
        task = {**planned, "turns": [], "recovery_count": 0, "canary": False, "first_execution": True}
        initial = None
    phase("completion_startup", "startup")
    with service("apps.retail_demo.mcp_server", root / "services" / unit), service("apps.retail_demo.eval_s1_completion.launcher", root / "services" / unit):
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8920", timeout=25) as web:
            await metadata(web, "completion_" + unit)
            if unit == "r2_R6":
                created = await web.post("/sessions?allow_greeting=false", json={"agent_id": "retail-persistent-demo", "customer_id": "demo-alice", "title": "APP-S1 completion r2_R6"})
                created.raise_for_status()
                task["session_id"] = created.json()["id"]
                initial = database_evidence()
            else:
                snapshot = await web.post(f"/demo/sessions/{task['session_id']}/snapshot", timeout=70)
                snapshot.raise_for_status()
                save(folder / "before_recovery_snapshot.json", snapshot.json())
                original_events = json.loads((old / "tasks/r2_R1/final_snapshot.json").read_text())["events"]
                assert snapshot.json()["events"] == original_events, "Original conversation changed"
            save(folder / "initial_database.json", initial)
            save(folder / "live_before_segment.json", database_evidence())
            save(folder / "attempt.json", task)
            convo = Conversation(web, task, folder)
            start = time.monotonic()
            try:
                if unit == "r2_R6":
                    await asyncio.wait_for(convo.perform(), 720)
                else:
                    state = await web.get(f"/demo/sessions/{task['session_id']}/operation-result", params={"operation_id": ORIGINAL_OP})
                    state.raise_for_status()
                    result = state.json()
                    save(folder / "readonly_original_operation_result.json", result)
                    assert result["ok"] and result["data"]["operation_id"] == ORIGINAL_OP
                    actual = result["data"]
                    if actual["request"]:
                        task["recovery_segment"]["branch"] = "already_submitted_readonly_recovery"
                        await convo.send("刚才退货成功了吗？请查询原操作状态，不要重新提交。")
                    elif actual["state"] == "expired_not_submitted":
                        task["recovery_segment"]["branch"] = "expired_requires_new_real_confirmation"
                        # These are new recovery interactions, not replayed old turns.
                        await convo.send("刚才退货成功了吗？请查询原操作状态，不要重新提交。")
                        await convo.send("原操作已过期且尚未提交。我的目标仍是退订单 DEMO-1001 的 DEMO-1001-HEADSET 中1件，原因是尺寸不合适。请重新检查资格并准备新的内容，展示金额和完整新口令，等我重新确认，不要使用过期授权提交。")
                        phrase = await convo.offer(task["scenario"])
                        assert ORIGINAL_OP not in phrase, "Expired old offer cannot authorize submission"
                        task["copied_confirmation"] = phrase
                        task["recovery_segment"]["new_visible_operation_id"] = phrase.split()[1]
                        await convo.send(phrase)
                    elif actual["state"] == "confirmed_not_submitted" and actual["retry_allowed"]:
                        task["recovery_segment"]["branch"] = "valid_confirmed_continue_original_via_engine"
                        await convo.send("此前已提供有效的完整确认，请核查并完成原操作 " + ORIGINAL_OP + " 的提交，只使用该操作的后端已确认快照，不重新准备其他操作。")
                    else:
                        raise RuntimeError("unsafe_to_resume_original_operation:" + actual["state"])
            except Exception as exc:
                task["error"] = {"type": type(exc).__name__, "code": str(exc) if isinstance(exc, (RuntimeError, TimeoutError)) else "see completion-only log"}
            task["seconds"] = time.monotonic() - start
            task["segment_ended_utc"] = datetime.now(timezone.utc).isoformat()
            task["completion_semantics"] = "first recorded complete recovery result; historical interruption retained; not uninterrupted original protocol"
            save(folder / "attempt.json", task)
            snap = await web.post(f"/demo/sessions/{task['session_id']}/snapshot", timeout=70)
            if snap.status_code != 200:
                save(root / "STOP.json", {"reason": "cannot_prove_processing_completed", "unit": unit, "status": snap.status_code})
                raise RuntimeError("Snapshot barrier unavailable; no score")
            save(folder / "final_snapshot.json", snap.json())
            final = database_evidence()
            save(folder / "final_database.json", final)
            original, revised, observations = score(task, initial, final, "S1")
            save(folder / "score_old.json", original)
            save(folder / "score_v2.json", revised)
            save(folder / "refund_observations.json", observations)
            save(folder / "lineage.json", {"logical_unit": unit, "original_attempt": str(old / "tasks" / unit),
                 "completion_attempt": str(folder / "attempt.json"), "first_complete_result_only": True,
                 "new_interactions": len(task["turns"]) - (2 if unit == "r2_R1" else 0),
                 "original_turns_replayed": 0, "old_confirmation_never_reused_if_expired": True,
                 "operation_link": task.get("recovery_segment"), "normal_retry": 0})
            if original["actual_wrong_writes"]:
                save(root / "STOP.json", {"reason": "actual_wrong_writes", "unit": unit, "evidence": original["actual_wrong_writes"]})
            print(json.dumps({"unit": unit, "old_status": original["status"], "v2_status": revised["status"], "errors": revised["errors"], "turns": len(task["turns"]), "segment_seconds": task["seconds"]}, ensure_ascii=False), flush=True)
    phase("completion_closing", "closing")
    save(folder / "database_after_process_exit.json", database_evidence())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("unit", choices=WHITELIST)
    args = parser.parse_args()
    asyncio.run(run(args.unit))
