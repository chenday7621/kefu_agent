"""Read-only accounting of proposals, native dispatch, wire calls and DB effects.

This module never changes scoring or supplies supervision. MCP middleware IDs
are counts only: they are deliberately not matched to policy IDs by time.
"""

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def response_flags(snapshots, order_ids, operation_ids):
    """Conservative text flags, not a judge or an exhaustive provider error rate."""
    seen, flags = set(), []
    for snapshot in snapshots:
        for event in snapshot.get("events", []):
            if event.get("source") != "ai_agent" or event.get("kind") != "message":
                continue
            event_id = event["id"]
            if event_id in seen:
                continue
            seen.add(event_id)
            message = event.get("data", {}).get("message", "")
            display_text = message.replace("**", "").replace("`", "")
            for order_id in sorted(order_ids - operation_ids):
                # Merely mentioning an order is valid. Only an explicit
                # operation label or confirmation instruction qualifies.
                identifier = re.escape(order_id)
                patterns = [
                    rf"(?:operation[_ ]?id|操作(?:编号|ID|id))\s*[:：=]\s*[`\"' ]*{identifier}(?![\w-])",
                    rf"确认退货\s*[`\"' ]*{identifier}(?![\w-])",
                ]
                if any(re.search(p, display_text, re.IGNORECASE) for p in patterns):
                    flags.append({"event_id": event_id, "type": "order_id_presented_as_operation_id",
                                  "identifier": order_id,
                                  "message_sha256": hashlib.sha256(message.encode()).hexdigest()})
    return flags


def account(task, evidence, latest_steps, wire, snapshots, score, cleanup):
    initial, final = task["initial_db_fixture"]["tables"], evidence["final_db"]
    visible = deepcopy(task["visible_goal_fields"])
    by_call, proposal_by_policy = {}, {}
    for step in latest_steps.values():
        for call in step.get("actual_executed_calls", []):
            cid = call["tool_call_id"]
            if cid in by_call:
                raise ValueError("Duplicate native dispatch ID in latest policy records")
            by_call[cid] = call
    policy_counts, dispatch_counts = Counter(), Counter()
    errors, session_rejections, preview_errors = Counter(), [], []
    result_ids, preview_ids = set(), set()
    for event in evidence["events"]:
        kind = event["kind"]
        if kind == "user_update":
            visible.update(event["fields"])
        elif kind == "policy_proposal":
            proposal_by_policy[event["policy_step_id"]] = event
            policy_counts.update(call["tool_name"].split(":")[-1]
                                 for call in event["proposed_calls"] if call.get("is_applicable", True))
        elif kind == "tool_result":
            cid, rid = event["tool_call_id"], event["tool_result_id"]
            if cid not in by_call or rid in result_ids:
                raise ValueError("Tool result without its unique explicit native dispatch")
            result_ids.add(rid)
            name = by_call[cid]["tool_name"].split(":")[-1]
            dispatch_counts[name] += 1
            payload = event["result"]
            if not payload["ok"]:
                detail = str(event.get("raw_payload", {}).get("metadata", {}).get("error_details", ""))
                error = payload.get("error")
                code = error.get("code", "UNKNOWN_TOOL_ERROR") if isinstance(error, dict) else str(error or "UNKNOWN_TOOL_ERROR")
                if "SESSION_FORBIDDEN" in detail:
                    code = "SESSION_FORBIDDEN"
                errors[code] += 1
                if code == "SESSION_FORBIDDEN":
                    session_rejections.append({"policy_step_id": event["policy_step_id"],
                                               "tool_call_id": cid, "tool_result_id": rid, "tool_name": name})
            elif name == "check_return_eligibility":
                operation = payload["data"]["operation"]
                preview_ids.add(operation["id"])
                fields = ("order_id", "item_id", "quantity", "reason")
                if any(key not in visible or operation.get(key) != visible[key] for key in fields):
                    preview_errors.append({"operation_id": operation["id"],
                                           "policy_step_id": event["policy_step_id"],
                                           "tool_call_id": cid, "tool_result_id": rid,
                                           "type": "prepared_preview_mismatches_current_visible_intent"})
    if len(result_ids) != len(by_call):
        raise ValueError("Native dispatch has no explicit final tool result")
    initial_requests = {row["id"] for row in initial["return_requests"]}
    initial_outbox = {row["id"] for row in initial["return_outbox"]}
    new_requests = [row["id"] for row in final["return_requests"] if row["id"] not in initial_requests]
    new_outbox = [row for row in final["return_outbox"] if row["id"] not in initial_outbox]
    before_items = {row["id"]: row for row in initial["order_items"]}
    reservations = {row["id"]: row["reserved_return_quantity"] - before_items[row["id"]]["reserved_return_quantity"]
                    for row in final["order_items"]
                    if row["reserved_return_quantity"] != before_items[row["id"]]["reserved_return_quantity"]}
    flags = response_flags(snapshots, {r["id"] for r in final["orders"]},
                           {r["id"] for r in final["return_operations"]})
    critical_ids = {row["policy_step_id"] for row in score["critical_evidence"] if row.get("policy_step_id")}
    if not critical_ids.issubset(proposal_by_policy):
        raise ValueError("Scored critical policy ID lacks its original proposal")
    return {"task_id": task["task_id"], "family": task["family"],
            "proposal_calls_by_tool": dict(policy_counts), "critical_proposal_steps": len(critical_ids),
            "native_dispatch_calls_by_tool": dict(dispatch_counts), "native_dispatch_calls": len(by_call),
            "actual_mcp_requests_by_tool": dict(Counter(row["tool_name"] for row in wire)),
            "actual_mcp_requests": len(wire), "mcp_ids_used_for_causal_attribution": False,
            "session_guard_rejections": session_rejections, "tool_error_results_by_code": dict(errors),
            "new_prepared_previews": len(preview_ids), "incorrect_previews": preview_errors,
            "new_formal_request_ids": new_requests, "reservation_quantity_changes": reservations,
            "new_outbox_rows": len(new_outbox),
            "new_outbox_status_counts": dict(Counter(row.get("status", "unknown") for row in new_outbox)),
            "response_provider_flags": flags,
            "response_provider_flag_scope": "Exact order-ID-as-operation-ID text flags only; not exhaustive; no causal blame allocation",
            "forbidden_final_db_change": any(row.get("reason") == "forbidden_final_db_change" for row in score["critical_evidence"]),
            "database_dropped": cleanup["database_dropped"]}


def summarize(data):
    result = {key: sum(row[key] for row in data)
              for key in ("critical_proposal_steps", "native_dispatch_calls", "actual_mcp_requests",
                          "new_prepared_previews", "new_outbox_rows")}
    for key in ("proposal_calls_by_tool", "native_dispatch_calls_by_tool", "actual_mcp_requests_by_tool",
                "tool_error_results_by_code", "new_outbox_status_counts"):
        counts = Counter()
        for row in data:
            counts.update(row[key])
        result[key] = dict(counts)
    for source, target in (("session_guard_rejections", "session_guard_rejection_count"),
                           ("incorrect_previews", "incorrect_preview_count"),
                           ("new_formal_request_ids", "new_formal_request_count"),
                           ("response_provider_flags", "response_provider_flag_count")):
        result[target] = sum(len(row[source]) for row in data)
    result.update(completed=len(data), databases_dropped=sum(row["database_dropped"] for row in data),
                  mcp_ids_used_for_causal_attribution=False,
                  response_provider_flag_scope="Conservative observable flags, not an exhaustive error rate or causal allocation")
    return result


def audit_rows(rows):
    results, references = [], {}
    for row in rows:
        directory = Path(row["attempt_directory"])
        latest = {}
        paths = list(directory.glob("trajectories/*.jsonl"))
        for path in paths:
            for step in read_rows(path):
                latest[step["policy_step_id"]] = step
        names = ("TASK.json", "EPISODE_EVIDENCE.json", "APP_SNAPSHOTS.json", "SCORE.json", "CLEANUP.json")
        task, evidence, snapshots, score, cleanup = [json.loads((directory / name).read_text()) for name in names]
        wire_path = directory / "MCP_INVOCATIONS.jsonl"
        results.append(account(task, evidence, latest, read_rows(wire_path), snapshots, score, cleanup))
        for path in paths + [directory / name for name in names] + [wire_path]:
            if path.exists():
                references[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"totals": summarize(results), "per_task": results, "source_sha256": references}
