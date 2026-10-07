"""One result per original logical unit; no best-of or scorer changes."""
import argparse
import copy
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .offline import read, save, sha
from ..eval_s1.launcher import guard_cost
from ..eval_v1.protocol import REQUEST_ID
from ..eval_v1.evidence_diff import key


def latency(values):
    values = sorted(values)
    return {"n": len(values), "median_seconds": ((values[(len(values)-1)//2] + values[len(values)//2]) / 2 if values else None),
            "p95_nearest_rank_seconds": values[math.ceil(len(values)*.95)-1] if values else None}


def aggregate(calls):
    tokens = {k: 0 for k in ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")}
    missing = {k: [] for k in tokens}
    known, reserves = 0, 0
    phases = defaultdict(list)
    for c in calls:
        phases[c.get("category", "unavailable")].append(c)
        if c.get("usage"):
            known += guard_cost(c)
            for k in tokens:
                if c["usage"].get(k) is None:
                    missing[k].append(c["invocation_id"])
                else:
                    tokens[k] += c["usage"][k]
        else:
            reserves += c["reserved_upper_usd"]
            for k in tokens:
                missing[k].append(c["invocation_id"])
    unknown = [c["invocation_id"] for c in calls if not c.get("usage")]
    return {"unique_dispatches": len(calls), "unique_response_ids": len({c["response_id"] for c in calls if c.get("response_id")}),
            "statuses": dict(Counter(c["status"] for c in calls)), "known_token_subtotals": tokens,
            "missing_usage_by_field": missing, "unknown_dispatches": unknown,
            "known_guard_usd": known, "retained_unknown_reserve_usd": reserves, "guard_bound_usd": known + reserves,
            "by_phase": {k: {"dispatches": len(v), "known_guard_usd": sum(guard_cost(c) for c in v if c.get("usage")),
                              "unknown_dispatches": sum(not c.get("usage") for c in v)} for k, v in phases.items()},
            "returned_models": dict(Counter(c.get("returned_model", "unavailable") for c in calls)),
            "actual_bill": "unavailable", "unknown_usage_not_zero": True, "physical_HTTP_requests_and_retries": "unavailable"}


def views(rows, field):
    statuses = Counter(r[field]["status"] for r in rows)
    return {"planned": 16, "selected_logical_units": len(rows), "score_statuses": dict(statuses),
            "pass_per_plan": statuses["pass"] / 16,
            "execution_statuses": dict(Counter(r["execution_status"] for r in rows)),
            "by_repeat": {str(n): {"planned": 8, "scores": dict(Counter(r[field]["status"] for r in rows if r["repeat"] == n)),
                                    "execution": dict(Counter(r["execution_status"] for r in rows if r["repeat"] == n))}
                          for n in (1, 2)}}


def run(audit, results):
    audit, results = Path(audit), Path(results)
    frozen = read(results / "S1_score_comparison.json")
    selected = []
    checks = []
    lineage = []
    for row in frozen:
        unit = row["logical_unit"]
        new = results / "completion_tasks" / unit
        row = copy.deepcopy(row)
        if unit in ("r2_R1", "r2_R6"):
            assert (new / "score_old.json").exists() and (new / "score_v2.json").exists()
            task = read(new / "attempt.json")
            row.update(old_score=read(new / "score_old.json"), new_score=read(new / "score_v2.json"),
                       selected_attempt=str(new / "attempt.json"),
                       execution_status="completed_after_interruption" if unit == "r2_R1" else "completed",
                       original_interruption_retained=unit == "r2_R1", session_id=task["session_id"])
            evidence = new
            lineage.append(read(new / "lineage.json"))
        else:
            assert row["execution_status"] == "completed"
            row["selected_attempt"] = row["original_evidence"] + "/attempt.json"
            evidence = Path(row["original_evidence"])
        initial, final = read(evidence / "initial_database.json"), read(evidence / "final_database.json")
        target = row["goal"]
        requests = [r for r in final["return_requests"] if r["id"] != REQUEST_ID]
        failures = []
        details = []
        for req in requests:
            op = next(o for o in final["return_operations"] if o["id"] == req["operation_id"])
            fields = ("customer_id", "order_id", "item_id", "quantity", "reason")
            same = all(req[k] == op[k] for k in fields)
            goal = all(req[k] == target[k] for k in ("order_id", "item_id", "quantity", "reason"))
            outbox = [o for o in final["return_outbox"] if o["operation_id"] == op["id"]]
            receipts = [r["doc"] for r in final["parlant_events"]
                        if (r["doc"].get("metadata") or {}).get("outbox_id") in {o["id"] for o in outbox}]
            qty = next(i["reserved_return_quantity"] for i in final["order_items"] if i["id"] == req["item_id"])
            unique = len(requests) == 1 and len(outbox) == len(receipts) == 1 and qty == req["quantity"]
            confirmation = next(b for b in final["session_operations"] if b["operation_id"] == op["id"])
            actual = confirmation.get("confirmation_event") or {}
            confirmed = actual.get("source") == "customer" and actual.get("session_id") == row["session_id"]
            details.append({"request_id": req["id"], "operation_id": op["id"], "snapshot_exact": same,
                            "matches_goal": goal, "request_outbox_receipt_unique": unique,
                            "actual_customer_confirmation": confirmed, "request": req,
                            "outbox": outbox, "receipt_event_ids": [r["id"] for r in receipts]})
            if not all((same, goal, unique, confirmed)):
                failures.append("snapshot_or_goal_or_unique_or_confirmation")
        if len(requests) != 1:
            failures.append("request_count")
        checks.append({"unit": unit, "passed": not failures, "errors": failures, "details": details})
        row["score_status_old"] = row["old_score"]["status"]
        row["score_status_v2"] = row["new_score"]["status"]
        selected.append(row)
        if unit in ("r2_R1", "r2_R6"):
            delta = {}
            for table in initial:
                a = {key(table, x): x for x in initial[table]}
                b = {key(table, x): x for x in final[table]}
                delta[table] = {"added_keys": sorted(b.keys() - a.keys()), "removed_keys": sorted(a.keys() - b.keys()),
                                "changed": {k: [f for f in b[k] if b[k][f] != a[k].get(f)] for k in a.keys() & b.keys() if a[k] != b[k]}}
            save(new / "database_diff.json", delta)
    save(results / "selected_16_logical_units.json", selected)
    save(audit / "selected_16_logical_units.json", selected)
    save(audit / "snapshot_and_uniqueness_checks.json", checks)
    save(audit / "recovery_links.json", lineage)
    source = Path("results/app_s1_confirmed_snapshot_20261006_165858")
    old_calls = [read(p) for p in (source / "calls").glob("*.json")]
    new_calls = [read(p) for p in (results / "calls").glob("*.json")]
    assert not ({c["invocation_id"] for c in old_calls} & {c["invocation_id"] for c in new_calls})
    usage = {"prior": aggregate(old_calls), "incremental": aggregate(new_calls), "cumulative": aggregate(old_calls + new_calls),
             "incremental_cap_usd": 1, "cumulative_cap_usd": 3,
             "known_guard_mean_over_16_logical_units": aggregate(old_calls + new_calls)["known_guard_usd"] / 16,
             "known_guard_per_success_old_scorer": aggregate(old_calls + new_calls)["known_guard_usd"] / sum(r["old_score"]["status"] == "pass" for r in selected),
             "known_guard_per_success_v2": aggregate(old_calls + new_calls)["known_guard_usd"] / sum(r["new_score"]["status"] == "pass" for r in selected)}
    for name in ("prior", "incremental", "cumulative"):
        for phase in ("startup", "formal", "exception", "closing"):
            usage[name]["by_phase"].setdefault(phase, {"dispatches": 0, "known_guard_usd": 0, "unknown_dispatches": 0})
    assert usage["incremental"]["guard_bound_usd"] <= 1 and usage["cumulative"]["guard_bound_usd"] <= 3
    assert usage["prior"]["unknown_dispatches"] == usage["cumulative"]["unknown_dispatches"]
    for unit in ("r2_R1", "r2_R6"):
        usage.setdefault("by_incremental_unit", {})[unit] = aggregate([c for c in new_calls if c["attempt_id"] == unit])
    save(results / "usage_completion.json", usage)
    save(audit / "usage_completion.json", usage)
    conflict_calls = []
    for p in (results / "mcp_calls").glob("*.json"):
        c = read(p)
        data = c.get("result", {}).get("data", {})
        if isinstance(data, dict) and (data.get("error") or {}).get("code") == "IDEMPOTENCY_CONFLICT":
            conflict_calls.append(c["id"])
    task_times = [read(results / "completion_tasks" / u / "attempt.json")["seconds"] for u in ("r2_R1", "r2_R6")]
    # Exclude the original two turns of recovered R1 from new-turn latency.
    turns = read(results / "completion_tasks/r2_R1/attempt.json")["turns"][2:] + read(results / "completion_tasks/r2_R6/attempt.json")["turns"]
    r1 = read(results / "completion_tasks/r2_R1/attempt.json")
    original = read(source / "tasks/r2_R1/attempt.json")
    receipts = []
    for unit in ("r2_R1", "r2_R6"):
        final = read(results / "completion_tasks" / unit / "final_database.json")
        receipts += [(datetime.fromisoformat(o["completed_at"]) - datetime.fromisoformat(o["created_at"])).total_seconds() for o in final["return_outbox"] if o["status"] == "delivered"]
    metrics = {"status": "COMPLETED", "original_frozen": read(results / "offline_metrics.json")["S1"]["original_frozen"],
               "completed_with_old_scorer": views(selected, "old_score"), "completed_with_same_refund_v2": views(selected, "new_score"),
               "snapshot_exact_and_unique_goal_applications": {"passed": sum(c["passed"] for c in checks), "n": 16},
               "actual_wrong_writes": sum(len(r["old_score"]["actual_wrong_writes"]) for r in selected),
               "wrong_tool_attempts": sum(len(r["old_score"]["wrong_tool_attempts"]) for r in selected),
               "backend_rejections": sum(len(r["old_score"]["backend_rejections"]) for r in selected),
               "incremental_IDEMPOTENCY_CONFLICT_calls": conflict_calls, "historical_S1_IDEMPOTENCY_CONFLICT": 0,
               "duplicate_request_or_outbox_or_canonical_receipt_findings": [c for c in checks if not c["passed"]],
               "new_response_type_counts": dict(Counter(typ for t in turns for typ in t["response_types"])),
               "historical_interruption_preserved": {"unit": "r2_R1", "error": original["error"], "old_score": read(source / "tasks/r2_R1/score.json")},
               "recovery_interaction_and_time": {"unit": "r2_R1", "new_user_messages": 3,
                   "old_user_messages_replayed": 0, "original_seconds": original["seconds"], "new_segment_seconds": r1["seconds"],
                   "gap_seconds_since_last_original_event": (datetime.fromisoformat(r1["recovery_segment"]["started_utc"]) - datetime.fromisoformat(original["turns"][-1]["post_body"]["creation_utc"])).total_seconds(),
                   "expiry_reconfirmation": True, "not_uninterrupted_or_contemporaneous_AB": True},
               "latency": {"new_segments": latency(task_times), "new_answered_turns": latency([t["seconds"] for t in turns]),
                           "new_MCP_calls": latency([read(p)["seconds"] for p in (results / "mcp_calls").glob("*.json")]),
                           "new_commit_to_receipt": latency(receipts)},
               "fee_estimate_is_bill": False, "model_factual_accuracy": "unreviewed", "deployment_executed": False}
    save(results / "completion_metrics.json", metrics)
    save(audit / "completion_metrics.json", metrics)
    print(json.dumps({"old_scores": metrics["completed_with_old_scorer"]["score_statuses"], "v2_scores": metrics["completed_with_same_refund_v2"]["score_statuses"], "new_guard_usd": usage["incremental"]["guard_bound_usd"], "cumulative_bound_usd": usage["cumulative"]["guard_bound_usd"], "cumulative_unknown_usage": len(usage["cumulative"]["unknown_dispatches"]), "snapshot_unique": metrics["snapshot_exact_and_unique_goal_applications"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--results", required=True)
    args = parser.parse_args()
    run(args.audit, args.results)
