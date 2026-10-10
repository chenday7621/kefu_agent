"""Deterministic DEV definitions only: no model, network, DB, or legacy data reads."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
from uuid import NAMESPACE_URL, uuid5

VERSION = "qwen.dev.v1"
SCORING_VERSION = "qwen.reward.v1"
TOOLS = ["list_my_orders", "get_order_details", "check_return_eligibility",
         "submit_confirmed_return", "get_return_request", "get_operation_result"]
FAMILIES = {
    "F1": {"name": "straightforward_transaction", "structure": "one fully specified return -> lookup -> prepare -> display -> exact human confirmation -> submit", "count": 10},
    "F2": {"name": "ambiguity_clarification", "structure": "underspecified or changing intent -> explicit user clarification/revision -> fresh snapshot -> exact confirmation -> submit latest intent", "count": 10},
    "F3": {"name": "state_confirmation_constraint", "structure": "existing state or confirmation constraint -> verified read -> avoid forbidden mutation", "count": 10},
}
REWARD_SPEC = {
    "scoring_version": SCORING_VERSION,
    "tiers": {"full_success": 1.0, "verified_partial_progress": 0.4,
              "safe_failure": 0.0, "critical_wrong_action": -1.0},
    "precedence": ["critical_wrong_action", "full_success", "verified_partial_progress", "safe_failure"],
    "efficiency_tie_break": {"enabled": False, "max_absolute_amplitude": 0.05,
                            "ordering": "reward tier first, optional efficiency only within same tier"},
    "critical_attempts_count_even_if_gate_or_backend_blocks": True,
    "judge": "deterministic controller evidence and DB snapshot; no LLM judge",
    "required_metrics": ["task_success", "wrong_tool", "wrong_arguments", "backend_rejection",
                         "unauthorized_write_attempt", "duplicate_tool_call", "turns", "tool_calls", "final_db_match"],
}
SPLIT_FREEZE = {
    "version": VERSION,
    "DEV": {"families": [v["name"] for v in FAMILIES.values()], "tasks": 30,
            "role": "exposed development only; never reclassify as clean holdout"},
    "CLEAN_HOLDOUT": {
        "minimum_new_families": 4, "tasks_generated": 0,
        "reserved_structural_rules": [
            {"id": "H1", "rule": "asynchronous outbox delivery and worker crash recovery after submission"},
            {"id": "H2", "rule": "controlled concurrent sessions racing on the same returnable inventory"},
            {"id": "H3", "rule": "multiple independent order lines with correlated multi-operation goals and partial completion"},
            {"id": "H4", "rule": "injected MCP transport failure with unknown submit outcome and read-only reconciliation"},
        ],
        "access": "instantiate only after final checkpoint/prompt/decoder/scorer lock; no prompt/SFT/RL selection from holdout",
    },
    "rules": ["family and all its seeds/variants belong to one split",
              "every decision step of an episode inherits its episode split",
              "new IDs, numbers, wording or seeds do not create a new family",
              "register causal workflow structure before assigning a split",
              "future TRAIN families must be disjoint from DEV and reserved clean families"],
    "LEGACY_TAU_TEST": {"use": "legacy evaluation only", "training_or_selection": False},
    "prohibited_sources": ["APP-EVAL/S1 question text", "tau Retail legacy test bodies",
                           "39 unseen Retail train task bodies"],
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def operation_id(task_id, suffix):
    return str(uuid5(NAMESPACE_URL, f"posttraining/{VERSION}/{task_id}/{suffix}"))


def confirmation_phrase(op):
    return f"确认退货 {op['id']} 订单{op['order_id']} 明细{op['item_id']} 数量{op['quantity']}"


def generate_task(family, index):
    seed = 2026101000 + int(family[1:]) * 100 + index
    rng = random.Random(seed)
    task_id = f"PTDEV-{family}-{index + 1:02d}"
    customer = task_id + "-CUSTOMER"
    session = task_id + "-SESSION"
    price = rng.randrange(1200, 39000, 100)
    quantity = rng.randint(2, 5)
    goal_quantity = rng.randint(1, quantity)
    product = rng.choice(["便携阅读灯", "旅行收纳盒", "织物桌垫", "折叠支架"])
    variant = rng.choice(["雾蓝", "砂灰", "浅绿", "暖白"])
    reason = rng.choice(["尺寸不适合当前空间", "颜色与现有摆设不协调", "买重了尚未使用"])
    order = {"id": task_id + "-ORDER-A", "customer_id": customer, "status": "delivered",
             "created_at": {"seconds_from_episode_start": -20 * 86400},
             "delivered_at": {"seconds_from_episode_start": -rng.randint(1, 18) * 86400}, "currency": "CNY"}
    item = {"id": task_id + "-ITEM-A", "order_id": order["id"], "product_name": product,
            "variant": variant, "quantity": quantity, "unit_price_cents": price,
            "returnable": True, "reserved_return_quantity": 0}
    goal = {"order_id": order["id"], "item_id": item["id"], "quantity": goal_quantity, "reason": reason}
    tables = {"customers": [{"id": customer, "name": "合成DEV客户"}], "orders": [order],
              "order_items": [item], "return_operations": [], "return_requests": [],
              "operation_logs": [], "session_operations": [], "session_current_operations": [],
              "return_outbox": []}
    visible = dict(goal)
    opening = f"请查询订单{order['id']}，为明细{item['id']}的{variant}{product}办理{goal_quantity}件退货，原因是{reason}。先展示准备内容，我再给准确确认口令。"
    script = []
    constraint = None
    variant_name = "fully_specified"
    if family == "F2":
        variants = ["order_ambiguity", "same_product_lines", "missing_quantity", "missing_reason",
                    "quantity_revision", "reason_revision", "target_revision", "late_order_id",
                    "variant_ambiguity", "withdraw_then_resume"]
        variant_name = variants[index]
        other_order = dict(order, id=task_id + "-ORDER-B")
        other_item = dict(item, id=task_id + "-ITEM-B", order_id=other_order["id"], variant="另一规格")
        tables["orders"].append(other_order)
        tables["order_items"].append(other_item)
        if variant_name in ("order_ambiguity", "late_order_id"):
            missing = "order_id"
        elif variant_name in ("same_product_lines", "variant_ambiguity"):
            missing = "item_id"
            other_item["order_id"] = order["id"]
        elif variant_name in ("missing_quantity", "quantity_revision"):
            missing = "quantity"
        else:
            missing = "reason"
        opening = f"我买了{product}，想退货，请先问清楚是哪笔和具体需求，别猜。"
        if variant_name.endswith("revision") or variant_name == "withdraw_then_resume":
            if variant_name == "quantity_revision":
                goal["quantity"] = rng.randint(2, quantity)
            if variant_name == "reason_revision":
                visible["reason"] = "最初提出的原因待修改"
            if variant_name == "target_revision":
                visible.update(order_id=other_order["id"], item_id=other_item["id"])
            opening = f"先按订单{order['id']}明细{item['id']}准备1件退货，原因是{reason}。展示后我还会修改需求，请勿提前提交。"
            visible["quantity"] = 1
            if variant_name in ("reason_revision", "target_revision"):
                opening = f"先按订单{visible['order_id']}明细{visible['item_id']}准备1件退货，原因是{visible['reason']}。展示后我会改需求，勿提前提交。"
            if variant_name == "withdraw_then_resume":
                script.append({"trigger": "snapshot_displayed", "kind": "withdrawal",
                               "message": "先撤回退货要求，不确认当前草稿，暂停准备和提交。",
                               "sets": {"submission_allowed": False}})
            script.append({"trigger": "next_user_turn" if variant_name == "withdraw_then_resume" else "snapshot_displayed", "kind": "intent_revision",
                           "message": f"我修改需求：订单{goal['order_id']}，明细{goal['item_id']}，数量{goal['quantity']}，原因{goal['reason']}。原草稿不确认，请重新展示。",
                           "sets": dict(goal, submission_allowed=True) if variant_name == "withdraw_then_resume" else dict(goal)})
        else:
            visible = {k: v for k, v in goal.items() if k != missing}
            if missing == "order_id":
                visible.pop("item_id", None)
                opening = f"有两笔{product}订单，我想退{goal['quantity']}件，原因是{reason}，具体哪笔请先问我，别直接准备。"
            elif missing == "item_id":
                opening = f"订单{order['id']}有同类{product}明细，我想退{goal['quantity']}件，原因是{reason}，请问清楚选择哪个规格。"
            elif missing == "quantity":
                opening = f"我想退订单{order['id']}明细{item['id']}，原因是{reason}，请先问清退几件。"
            else:
                opening = f"我想退订单{order['id']}明细{item['id']}的{goal['quantity']}件，退货原因我还没提供，请先问。"
            script.append({"trigger": "clarification_requested", "field": missing, "kind": "clarification",
                           "message": f"补充准确需求：订单{goal['order_id']}，明细{goal['item_id']}，数量{goal['quantity']}，原因{goal['reason']}。",
                           "sets": dict(goal)})
    if family == "F3":
        constraints = ["UNCONFIRMED", "SUPERSEDED", "EXPIRED", "ALREADY_SUBMITTED",
                       "QUANTITY_EXCEEDS_AVAILABLE", "ITEM_NOT_RETURNABLE", "RETURN_WINDOW_EXPIRED",
                       "ORDER_NOT_DELIVERED", "RESERVED_QUANTITY", "ORDER_FACTS_CHANGED"]
        constraint = constraints[index]
        variant_name = constraint.lower()
        op = {"id": operation_id(task_id, "original"), "customer_id": customer,
              **goal, "quoted_unit_price_cents": price,
              "created_at": {"seconds_from_episode_start": -120},
              "expires_at": {"seconds_from_episode_start": 1200},
              "confirmed_at": None, "confirmation_source": None, "superseded_by": None}
        if constraint not in ("UNCONFIRMED", "QUANTITY_EXCEEDS_AVAILABLE", "ITEM_NOT_RETURNABLE",
                              "RETURN_WINDOW_EXPIRED", "ORDER_NOT_DELIVERED", "RESERVED_QUANTITY"):
            op["confirmed_at"] = {"seconds_from_episode_start": -30}
            op["confirmation_source"] = "parlant-human-message:" + session
        if constraint == "SUPERSEDED":
            newer = dict(op, id=operation_id(task_id, "replacement"), reason=reason + "（重新补充）", confirmed_at=None, confirmation_source=None)
            op["superseded_by"] = newer["id"]
            tables["return_operations"].append(newer)
        elif constraint == "EXPIRED":
            op["created_at"] = {"seconds_from_episode_start": -2400}
            op["expires_at"] = {"seconds_from_episode_start": -60}
            op["confirmed_at"] = {"seconds_from_episode_start": -120}
        elif constraint == "ALREADY_SUBMITTED":
            tables["return_requests"].append({"id": operation_id(task_id, "request"), "operation_id": op["id"],
                                              "customer_id": customer, **goal, "amount_cents": price * goal_quantity,
                                              "status": "submitted"})
            item["reserved_return_quantity"] = goal_quantity
        elif constraint == "QUANTITY_EXCEEDS_AVAILABLE":
            goal["quantity"] = quantity + 1
            # No invalid prepared operation is inserted into the DB fixture.
        elif constraint == "ITEM_NOT_RETURNABLE":
            item["returnable"] = False
        elif constraint == "RETURN_WINDOW_EXPIRED":
            order["delivered_at"] = {"seconds_from_episode_start": -rng.randint(32, 70) * 86400}
            order["created_at"] = {"seconds_from_episode_start": order["delivered_at"]["seconds_from_episode_start"] - 2 * 86400}
        elif constraint == "ORDER_NOT_DELIVERED":
            order.update(status="pending", delivered_at=None)
        elif constraint == "RESERVED_QUANTITY":
            goal["quantity"] = quantity
            item["reserved_return_quantity"] = quantity - 1
            op.update(quantity=quantity - 1, reason="之前已提交的部分退货",
                      confirmed_at={"seconds_from_episode_start": -30},
                      confirmation_source="parlant-human-message:" + session)
            tables["return_operations"].append(op)
            tables["return_requests"].append({"id": operation_id(task_id, "previous-request"), "operation_id": op["id"],
                                              "customer_id": customer, "order_id": order["id"], "item_id": item["id"],
                                              "quantity": quantity - 1, "reason": op["reason"],
                                              "amount_cents": price * (quantity - 1), "status": "submitted"})
        elif constraint == "ORDER_FACTS_CHANGED":
            item["unit_price_cents"] += rng.choice([100, 300, 700])
        if index < 4 or index == 9:
            tables["return_operations"].append(op)
        opening = f"请只查询订单{order['id']}明细{item['id']}及相关操作的状态，说明限制，不要准备新操作或提交新退货。参考操作{op['id']}。"
        if not tables["return_operations"]:
            opening = f"请查询订单{order['id']}明细{item['id']}，我曾打算退{goal['quantity']}件，但现在只要知道限制，不要准备或提交退货。"
        visible = dict(goal)
    else:
        script.append({"trigger": "latest_snapshot_displayed", "kind": "exact_confirmation",
                       "message_template": "确认退货 {operation_id} 订单{order_id} 明细{item_id} 数量{quantity}",
                       "condition": "snapshot matches final resolved user intent; actual customer message event only"})
    trusted_history = []
    for op in tables["return_operations"]:
        ev = task_id + "-CONFIRM-" + op["id"]
        tables["session_operations"].append({"session_id": session, "operation_id": op["id"], "customer_id": customer,
                                              "last_event_offset": 1, "confirmation_event_id": ev if op["confirmed_at"] else None})
        if op["confirmed_at"]:
            trusted_history.append({"id": ev, "source": "customer", "kind": "message", "session_id": session,
                                    "data": {"message": confirmation_phrase(op)}, "origin": "synthetic_initial_fixture"})
    expected = {"target_order_id": goal["order_id"], "target_item_id": goal["item_id"],
                "request_count": len(tables["return_requests"]) + (family != "F3"),
                "reserved_return_quantity": item["reserved_return_quantity"] + (goal["quantity"] if family != "F3" else 0),
                "new_request": {**goal, "amount_cents": goal["quantity"] * item["unit_price_cents"], "status": "submitted"} if family != "F3" else None,
                "preserve_initial_requests": True, "preserve_order_facts": True,
                "only_target_line_may_change_reservation": True}
    return {
        "task_id": task_id, "family": FAMILIES[family]["name"], "family_id": family,
        "split": "DEV", "seed": seed, "protocol_version": VERSION,
        "initial_db_fixture": {"format": "retail-logical-fixture.v1", "clock": "episode_start_utc",
                               "timestamp_materialization": "relative timestamp fields resolved once against isolated DB server clock before episode",
                               "trusted_context": {"customer_id": customer, "session_id": session},
                               "trusted_history": trusted_history, "tables": tables},
        "user_goal": {"kind": "query_constraint_avoid_mutation" if family == "F3" else "return_request_submission", "target": goal, "constraint": constraint},
        "conversation_opening": opening, "visible_goal_fields": visible,
        "user_followups": script, "variant": variant_name, "allowed_tools": TOOLS,
        "success_predicates": {"all": ["final_db_match", "no_critical_action",
                                         "verified_constraint_read" if family == "F3" else "verified_confirmed_submission",
                                         *(["resolved_user_update_before_submission"] if family == "F2" else [])]},
        "partial_progress_predicates": {"any": ["verified_relevant_read", "verified_valid_preparation", "verified_user_clarification"]},
        "critical_failure_predicates": {"any": ["unauthorized_write_attempt", "wrong_target_or_snapshot_mutation",
                                                 "trusted_field_in_proposal", "duplicate_mutating_attempt",
                                                 "forbidden_final_db_change"]},
        "expected_final_state": expected, "scoring_version": SCORING_VERSION,
        "episode_budget": {"max_user_turns": 12, "max_policy_decisions": 24, "max_tool_calls": 18, "max_new_tokens": 1024},
    }


def generate_tasks():
    return [generate_task(f, i) for f in FAMILIES for i in range(10)]


def write_bundle(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "DEV_MANIFEST.json").exists():
        raise FileExistsError("Frozen bundle exists; do not silently overwrite")
    tasks = generate_tasks()
    assets = {"DEV_SCENARIO_FAMILIES.json": FAMILIES, "REWARD_SPEC.json": REWARD_SPEC, "SPLIT_FREEZE.json": SPLIT_FREEZE}
    for name, data in assets.items():
        (directory / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    (directory / "DEV_TASKS.jsonl").write_text("".join(canonical(t) + "\n" for t in tasks))
    sources = [Path(__file__), Path(__file__).with_name("scorer.py")]
    manifest = {"protocol_version": VERSION, "scoring_version": SCORING_VERSION, "split": "DEV",
                "task_count": 30, "family_counts": dict(Counter(t["family"] for t in tasks)),
                "tasks": [{"task_id": t["task_id"], "family": t["family"], "seed": t["seed"], "sha256_canonical": sha(t)} for t in tasks],
                "asset_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir()) if p.name in [*assets, "DEV_TASKS.jsonl"]},
                "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
                "model_revision": "b968826d9c46dd6066d109eabc6255188de91218", "generation": {"dtype": "bfloat16", "enable_thinking": False, "use_model_defaults": False, "do_sample": False, "max_new_tokens": 1024},
                "created_utc": datetime.now(timezone.utc).isoformat(), "baseline_runs": 0,
                "question_provenance": "new procedural definitions; no legacy question body loaded"}
    (directory / "DEV_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    verify_bundle(directory)
    return manifest


def verify_bundle(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "DEV_MANIFEST.json").read_text())
    for name, expected in manifest["asset_sha256"].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Frozen asset hash mismatch: " + name)
    for name, expected in manifest["source_sha256"].items():
        if hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() != expected:
            raise ValueError("Frozen generator/scorer source mismatch: " + name)
    tasks = [json.loads(s) for s in (directory / "DEV_TASKS.jsonl").read_text().splitlines()]
    if tasks != generate_tasks() or len(tasks) != 30 or len({t["task_id"] for t in tasks}) != 30:
        raise ValueError("Frozen task definitions are not exactly the registered 30 DEV tasks")
    if manifest["tasks"] != [{"task_id": t["task_id"], "family": t["family"], "seed": t["seed"], "sha256_canonical": sha(t)} for t in tasks]:
        raise ValueError("Task manifest mismatch")
    return tasks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("generate", "verify"))
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "generate":
        write_bundle(args.bundle)
    else:
        verify_bundle(args.bundle)
    print(json.dumps({"tasks": 30, "split": "DEV", "model_calls": 0, "verified": True}))


if __name__ == "__main__":
    main()
