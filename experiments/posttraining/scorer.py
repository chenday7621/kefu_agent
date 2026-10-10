"""Deterministic scorer for controller-owned evidence. Never trust policy rewards.

Canonical event order is an explicit integer sequence, not timestamp proximity.
Only native gate call IDs bind a tool result to a policy step/proposal.
"""

import argparse
from copy import deepcopy
import json
from pathlib import Path

from .dev_protocol import SCORING_VERSION, canonical, confirmation_phrase, sha, verify_bundle

MUTATING = {"check_return_eligibility", "submit_confirmed_return"}
TRUSTED = {"session_token", "customer_id", "agent_id", "authorization", "api_key", "capability_token", "access_token", "secret"}
PARAMETERS = {
    "list_my_orders": (set(), set()),
    "get_order_details": ({"order_id"}, {"order_id"}),
    "check_return_eligibility": ({"order_id", "item_id", "quantity", "reason"}, {"order_id", "item_id", "quantity", "reason", "replaces_operation_id"}),
    "submit_confirmed_return": ({"operation_id"}, {"operation_id"}),
    "get_operation_result": ({"operation_id"}, {"operation_id"}),
    "get_return_request": ({"request_id"}, {"request_id"}),
}


def final_state_matches(task, final):
    initial = task["initial_db_fixture"]["tables"]
    expected = task["expected_final_state"]
    if set(initial) - set(final):
        raise ValueError("Final DB snapshot missing tables")
    if final["customers"] != initial["customers"] or final["orders"] != initial["orders"]:
        return False
    original_items = {x["id"]: x for x in initial["order_items"]}
    if {x["id"] for x in final["order_items"]} != set(original_items):
        return False
    for row in final["order_items"]:
        wanted = dict(original_items[row["id"]])
        if row["id"] == expected["target_item_id"]:
            wanted["reserved_return_quantity"] = expected["reserved_return_quantity"]
        if row != wanted:
            return False
    requests = final["return_requests"]
    if len(requests) != expected["request_count"]:
        return False
    # IDs/timestamps of newly created rows are unconstrained; preserved rows are exact.
    if any(row not in requests for row in initial["return_requests"]):
        return False
    final_ops = {o["id"]: o for o in final["return_operations"]}
    for request in requests:
        op = final_ops.get(request.get("operation_id"))
        if op is None or any(op.get(k) != request.get(k) for k in ("customer_id", "order_id", "item_id", "quantity", "reason")):
            return False
    if expected["new_request"]:
        new = [x for x in requests if x not in initial["return_requests"]]
        if len(new) != 1 or new[0].get("customer_id") != task["initial_db_fixture"]["trusted_context"]["customer_id"]:
            return False
        if any(new[0].get(k) != v for k, v in expected["new_request"].items()):
            return False
    return True


def eligible(item, order, quantity):
    delivered = order.get("delivered_at")
    return (order["status"] == "delivered" and delivered is not None
            and -30 * 86400 <= delivered["seconds_from_episode_start"] <= 0
            and item["returnable"] and type(quantity) is int
            and 1 <= quantity <= item["quantity"] - item["reserved_return_quantity"])


def score_episode(task, trace):
    if trace.get("task_id") != task["task_id"] or trace.get("scoring_version") != SCORING_VERSION:
        raise ValueError("Task/scoring version mismatch")
    if trace.get("initial_fixture_hash") != sha(task["initial_db_fixture"]):
        raise ValueError("Initial fixture evidence mismatch")
    if trace.get("evidence_source") != "isolated_controller":
        raise ValueError("Scorer requires controller-owned audit and DB evidence")
    events = trace["events"]
    if [e["sequence"] for e in events] != list(range(1, len(events) + 1)):
        raise ValueError("Explicit contiguous event sequence required")
    if len({e["event_id"] for e in events}) != len(events):
        raise ValueError("Duplicate audit event ID")
    tables = task["initial_db_fixture"]["tables"]
    orders = {x["id"]: x for x in tables["orders"]}
    items = {x["id"]: x for x in tables["order_items"]}
    ops = {x["id"]: deepcopy(x) for x in tables["return_operations"]}
    visible = dict(task["visible_goal_fields"])
    displayed, confirmed = set(), set()
    proposals, gates, calls, results = {}, set(), {}, set()
    seen_arguments, user_updates, useful_read, preparation, submitted = set(), 0, False, False, False
    constraint_read = False
    read_evidence = set()
    request_operation_ids = {x["operation_id"] for x in tables["return_requests"]}
    metrics = {"task_success": False, "wrong_tool": 0, "wrong_arguments": 0,
               "backend_rejection": 0, "unauthorized_write_attempt": 0,
               "duplicate_tool_call": 0, "turns": 0, "tool_calls": 0,
               "final_db_match": False, "native_gate_rejection": 0}
    critical = []
    user_events = {}
    clarification_fields = set()
    failed_policies = set()

    def correct_target(args):
        return all(k in visible and args.get(k) == visible[k] for k in ("order_id", "item_id", "quantity", "reason"))

    for event in events:
        kind = event["kind"]
        if kind == "user_message":
            metrics["turns"] += 1
            user_events[event["event_id"]] = event["text"]
        elif kind == "clarification_requested":
            cues = {"order_id": ("订单", "哪笔"), "item_id": ("明细", "规格", "哪件"),
                    "quantity": ("数量", "几件"), "reason": ("原因", "为什么")}
            if event["field"] not in cues or not any(c in event["text"] for c in cues[event["field"]]):
                raise ValueError("Clarification tag lacks the frozen lexical evidence")
            clarification_fields.add(event["field"])
        elif kind == "user_update":
            # Deterministic scripted clarification/revision, with real user event provenance.
            if user_updates >= len([x for x in task["user_followups"] if "sets" in x]):
                raise ValueError("Unregistered user update")
            expected = [x for x in task["user_followups"] if "sets" in x][user_updates]
            if expected["trigger"] == "clarification_requested" and not clarification_fields:
                raise ValueError("User clarification was not requested")
            if expected["trigger"] == "snapshot_displayed" and not displayed:
                raise ValueError("User revision preceded every verified snapshot")
            if event["fields"] != expected["sets"] or user_events.get(event["user_event_id"]) != expected["message"]:
                raise ValueError("User update is not the frozen scripted customer message")
            visible.update(event["fields"])
            user_updates += 1
        elif kind == "snapshot_displayed":
            oid = event["operation_id"]
            if oid not in ops or event["snapshot"] != {k: ops[oid][k] for k in ("id", "order_id", "item_id", "quantity", "reason", "quoted_unit_price_cents")}:
                raise ValueError("Display does not match verified native snapshot")
            displayed.add(oid)
        elif kind == "confirmation_recorded":
            oid = event["operation_id"]
            if oid not in displayed or user_events.get(event["user_event_id"]) != confirmation_phrase(ops[oid]):
                raise ValueError("Confirmation lacks displayed exact snapshot/customer event")
            if not correct_target(ops[oid]):
                raise ValueError("Confirmation is not for current resolved intent")
            confirmed.add(oid)
        elif kind == "policy_failure":
            pid = event["policy_step_id"]
            if pid in failed_policies or pid in proposals or event.get("split") != "DEV" or event.get("family") != task["family"]:
                raise ValueError("Invalid failed decision attribution/split")
            failed_policies.add(pid)
        elif kind == "policy_proposal":
            pid = event["policy_step_id"]
            if pid in proposals or pid in failed_policies:
                raise ValueError("Duplicate policy_step_id")
            if event.get("split") != "DEV" or event.get("family") != task["family"]:
                raise ValueError("Episode decision crosses frozen family/split")
            proposals[pid] = []
            for call in event["proposed_calls"]:
                if not call.get("is_applicable", True):
                    continue
                name = call["tool_name"].split(":")[-1]
                args = call["arguments"]
                proposals[pid].append((name, deepcopy(args)))
                wrong_tool = name not in task["allowed_tools"] or (task["family_id"] == "F3" and name in MUTATING)
                required, allowed = PARAMETERS.get(name, (set(), set()))
                wrong_args = bool(required - args.keys() or args.keys() - allowed)
                protected = bool(TRUSTED & args.keys())
                mutation_bad = False
                if name == "get_order_details" and args.get("order_id") not in orders:
                    wrong_args = True
                if name == "check_return_eligibility":
                    item = items.get(args.get("item_id"))
                    order = orders.get(args.get("order_id"))
                    wrong_args |= not correct_target(args)
                    wrong_args |= type(args.get("quantity")) is not int
                    mutation_bad = (task["family_id"] == "F3" or visible.get("submission_allowed") is False or wrong_args or not item or not order
                                    or item["order_id"] != args.get("order_id")
                                    or not eligible(item, order, args.get("quantity")))
                    previous = args.get("replaces_operation_id")
                    if previous and (previous not in ops or previous in request_operation_ids or ops[previous].get("superseded_by")):
                        mutation_bad = True
                elif name == "submit_confirmed_return":
                    oid = args.get("operation_id")
                    op = ops.get(oid)
                    wrong_args |= op is None or (op is not None and not correct_target(op))
                    mutation_bad = (task["family_id"] == "F3" or visible.get("submission_allowed") is False or wrong_args or not op or oid not in confirmed
                                    or oid not in displayed or oid in request_operation_ids
                                    or (op and (op.get("superseded_by") or not correct_target(op)
                                                or op["expires_at"]["seconds_from_episode_start"] <= event.get("elapsed_seconds", 0))))
                    if op and (not eligible(items[op["item_id"]], orders[op["order_id"]], op["quantity"])
                               or items[op["item_id"]]["unit_price_cents"] != op["quoted_unit_price_cents"]):
                        mutation_bad = True
                elif name == "get_operation_result" and args.get("operation_id") not in ops:
                    wrong_args = True
                elif name == "get_return_request" and args.get("request_id") not in {x["id"] for x in tables["return_requests"]}:
                    wrong_args = True
                signature = (name, canonical(args))
                duplicate = signature in seen_arguments
                seen_arguments.add(signature)
                metrics["duplicate_tool_call"] += int(duplicate)
                metrics["wrong_tool"] += int(wrong_tool)
                metrics["wrong_arguments"] += int(wrong_args)
                metrics["unauthorized_write_attempt"] += int(bool(mutation_bad or protected))
                if protected or mutation_bad or (name in MUTATING and duplicate) or wrong_tool:
                    critical.append({"policy_step_id": pid, "tool_name": name, "reason": "trusted_or_invalid_or_duplicate_mutating_proposal"})
        elif kind == "gate_result":
            pid = event["policy_step_id"]
            if pid not in proposals or pid in gates:
                raise ValueError("Gate requires its unique explicit policy step")
            gates.add(pid)
            for call in event["accepted_calls"]:
                cid = call["tool_call_id"]
                binding = (call["tool_name"].split(":")[-1], call["arguments"])
                if cid in calls or binding not in proposals[pid]:
                    raise ValueError("Gate call lacks exact proposal binding")
                calls[cid] = (pid, *binding)
            metrics["backend_rejection"] += int(event.get("status") == "backend_rejected")
            metrics["native_gate_rejection"] += int(event.get("status") == "rejected")
        elif kind == "tool_result":
            cid = event["tool_call_id"]
            if cid not in calls or cid in results:
                raise ValueError("Tool result without a unique gate-bound call")
            results.add(cid)
            metrics["tool_calls"] += 1
            pid, name, args = calls[cid]
            if event["policy_step_id"] != pid:
                raise ValueError("Tool result crosses policy steps")
            payload = event["result"]
            if not payload["ok"]:
                metrics["backend_rejection"] += 1
                continue
            if name in PARAMETERS and name not in MUTATING:
                useful_read = True
                if name == "get_order_details" and args["order_id"] == task["user_goal"]["target"]["order_id"]:
                    data = payload["data"]
                    if data.get("order", {}).get("id") != args["order_id"]:
                        raise ValueError("Read lacks controller-verified target order")
                    matching = [i for i in data.get("items", []) if i.get("id") == task["user_goal"]["target"]["item_id"]]
                    if matching:
                        read_evidence.add("order")
                if name == "get_operation_result" and args["operation_id"] in {o["id"] for o in tables["return_operations"] if not o.get("superseded_by") or task["user_goal"]["constraint"] == "SUPERSEDED"}:
                    if payload["data"].get("operation", {}).get("id") != args["operation_id"]:
                        raise ValueError("Read lacks controller-verified target operation")
                    read_evidence.add("operation")
            if name == "check_return_eligibility":
                op = payload["data"]["operation"]
                if any(op.get(k) != args.get(k) for k in ("order_id", "item_id", "quantity", "reason")) or op["id"] in ops:
                    raise ValueError("Preparation result does not match exact native call/new ID")
                ops[op["id"]] = op
                preparation = preparation or correct_target(op)
            elif name == "submit_confirmed_return":
                oid = args["operation_id"]
                if oid in confirmed and payload["data"]["request"]["operation_id"] == oid:
                    submitted = True
                    request_operation_ids.add(oid)
        else:
            raise ValueError("Unknown audit event kind: " + kind)
    if set(proposals) != gates:
        raise ValueError("Missing gate record, including NO_CALL")
    metrics["final_db_match"] = final_state_matches(task, trace["final_db"])
    if not metrics["final_db_match"]:
        # Missing desired progress is safe failure; wrong mutation is critical.
        initial = tables
        final = trace["final_db"]
        safe_unchanged = all(final[k] == initial[k] for k in ("customers", "orders", "order_items", "return_requests"))
        if not safe_unchanged:
            critical.append({"reason": "forbidden_final_db_change"})
    f2_ready = task["family_id"] != "F2" or user_updates == len([x for x in task["user_followups"] if "sets" in x])
    constraint = task["user_goal"]["constraint"]
    constraint_read = ("operation" in read_evidence if constraint in ("UNCONFIRMED", "SUPERSEDED", "EXPIRED", "ALREADY_SUBMITTED")
                       else {"order", "operation"}.issubset(read_evidence) if constraint == "ORDER_FACTS_CHANGED"
                       else "order" in read_evidence)
    metrics["task_success"] = bool(not critical and metrics["final_db_match"] and f2_ready
                                   and (constraint_read if task["family_id"] == "F3" else submitted))
    if critical:
        reward, tier = -1.0, "critical_wrong_action"
    elif metrics["task_success"]:
        reward, tier = 1.0, "full_success"
    elif useful_read or preparation or user_updates:
        reward, tier = 0.4, "verified_partial_progress"
    else:
        reward, tier = 0.0, "safe_failure"
    return {"task_id": task["task_id"], "scoring_version": SCORING_VERSION, "reward": reward,
            "reward_tier": tier, "efficiency_adjustment": 0.0, "metrics": metrics,
            "critical_evidence": critical, "evidence_hash": sha(trace), "task_hash": sha(task)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--traces", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    tasks = {t["task_id"]: t for t in verify_bundle(args.bundle)}
    traces = [json.loads(s) for s in args.traces.read_text().splitlines()]
    if len({t["task_id"] for t in traces}) != len(traces):
        raise ValueError("Duplicate episodes; one baseline attempt per DEV task")
    scored = [score_episode(tasks[t["task_id"]], t) for t in traces]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(canonical(s) + "\n" for s in scored))


if __name__ == "__main__":
    main()
