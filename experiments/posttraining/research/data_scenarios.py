"""Independently authored V2 scenarios; no DEV generator/answers imported."""

from pathlib import Path
import hashlib
import json
import random

from .split_policy import check_episode, check_texts

PRODUCTS = ["桦木托盘", "软垫腕托", "棉布笔袋", "磁吸书签", "硅胶隔热垫", "折叠桌垫"]
REASONS = ["边角有明显擦痕", "缝线松开", "表面有气味", "实际尺寸不合适", "配色与需要不符"]


def scenario(group, index):
    split = group["split"]
    seed = (810000 if split == "train" else 920000) + index
    rng = random.Random(seed)
    eid = ("PTTRAIN-" if split == "train" else "PTVALID-") + str(seed)
    customer = eid + "-OWNER"
    order = "ORD-" + str(seed) + "-A"
    item = "ITEM-" + str(seed) + "-A"
    product = rng.choice(PRODUCTS)
    reason = rng.choice(REASONS)
    inventory = rng.randint(2, 6)
    quantity = rng.randint(1, inventory)

    def ordrow(oid, days):
        return {
            "id": oid,
            "customer_id": customer,
            "status": "delivered",
            "currency": "CNY",
            "created_at": {"seconds_from_episode_start": -days * 86400 - 172800},
            "delivered_at": {"seconds_from_episode_start": -days * 86400},
        }

    def line(i, oid, name):
        return {
            "id": i,
            "order_id": oid,
            "product_name": name,
            "variant": "单一规格",
            "quantity": inventory,
            "reserved_return_quantity": 0,
            "unit_price_cents": rng.randint(12, 90) * 100,
            "returnable": True,
        }

    tables = {
        "customers": [{"id": customer, "name": "独立合成研究访客"}],
        "orders": [ordrow(order, 3)],
        "order_items": [line(item, order, product)],
        "return_operations": [],
        "return_requests": [],
        "session_operations": [],
        "session_current_operations": [],
        "operation_logs": [],
        "return_outbox": [],
    }
    name = group["causal_template_group"]
    visible = {"order_id": order, "item_id": item, "quantity": quantity, "reason": reason}
    followups = []
    preview_only = name.startswith("VALID.") or "preview_only" in name
    if name == "TRAIN.preview_only_complete_intent":
        opening = f"我只授权生成退货预览，不授权确认或提交。订单：{order}；明细：{item}；数量：{quantity}；原因：{reason}。请先核对明细，再展示预览，之后停下。"
    elif name == "TRAIN.unique_product_name_return":
        visible.pop("item_id")
        visible["product_name"] = product
        opening = f"订单：{order}。我只退这笔订单里名称准确为“{product}”的唯一明细；数量：{quantity}；原因：{reason}。请从订单结果识别明细编号，生成预览供我确认，确认后提交。"
        tables["order_items"].append(line("ITEM-" + str(seed) + "-B", order, "独立附赠收纳绳"))
    elif name == "TRAIN.latest_delivered_order_return":
        visible.pop("order_id")
        visible.pop("item_id")
        visible["product_name"] = product
        visible["order_selector"] = "latest_delivered"
        opening = f"我要退我最新已送达订单中名称准确为“{product}”的唯一明细，其他订单不要操作。数量：{quantity}；原因：{reason}。先用订单清单按送达时间定位，再生成预览给我确认，确认后提交。"
        tables["orders"].append(ordrow("ORD-" + str(seed) + "-B", 12))
        tables["order_items"].append(
            line("ITEM-" + str(seed) + "-B", "ORD-" + str(seed) + "-B", "独立附赠收纳绳")
        )
    elif name == "TRAIN.all_available_single_line_return":
        quantity = inventory
        visible.pop("quantity")
        visible["quantity_selector"] = "all_available"
        opening = f"订单：{order}；明细：{item}。我想退这条明细当前全部可退的数量，请以查询到的available_return_quantity为准，不猜数字。原因：{reason}。核对后生成预览，等我确认再提交。"
    elif name == "VALID.preview_reason_clarification":
        visible.pop("reason")
        opening = f"仅为我生成一份可核对的预览，绝不提交。订单：{order}；明细：{item}；数量：{quantity}。我还没有说明原因，需要你问清楚再准备。"
        followups = [
            {
                "trigger": "clarification_requested",
                "field": "reason",
                "sets": {"reason": reason},
                "message": f"补充原因：{reason}。仍然只预览，请勿提交。",
            }
        ]
    elif name == "VALID.preview_quantity_after_order_read":
        visible.pop("quantity")
        opening = f"先查订单：{order}；明细：{item}，原因：{reason}。数量我要看到订单查询结果以后再告诉你；先不要准备。之后只做预览，禁止确认或提交。"
        followups = [
            {
                "trigger": "order_read_visible",
                "sets": {"quantity": quantity},
                "message": f"订单明细已经看到了，补充数量：{quantity}。现在可以准备预览，但不要提交。",
            }
        ]
    else:
        raise ValueError("Unregistered causal template")
    if not preview_only:
        followups.append(
            {
                "trigger": "latest_snapshot_displayed",
                "kind": "exact_confirmation",
                "condition": "matches actual resolved visible user intent; native human event",
            }
        )
    else:
        visible["submission_allowed"] = False
    task = {
        "task_id": eid,
        "episode_id": eid,
        "split": split,
        "source": "independently_authored_public_business_rules",
        "source_type": "synthetic_rule_expert",
        "family_id": name,
        "family": group["capability_family"],
        "capability_family": group["capability_family"],
        "causal_template_group": name,
        "template_fingerprint": group["normalized_fingerprint"],
        "seed": seed,
        "conversation_opening": opening,
        "visible_goal_fields": visible,
        "user_followups": followups,
        "initial_db_fixture": {
            "format": "retail-logical-fixture.v1",
            "clock": "episode_start_utc",
            "trusted_context": {"customer_id": customer, "session_id": eid + "-SESSION"},
            "trusted_history": [],
            "tables": tables,
        },
        "episode_budget": {"max_policy_decisions": 30, "max_user_turns": 12, "max_tool_calls": 18},
        "data_terminal_goal": "preview_without_request"
        if preview_only
        else "one_confirmed_request",
        "scoring_version": "training.business.validation.v1",
    }
    check_episode(task, [group])
    return task


def generate(policy_path, output, dev_audit_tasks):
    policy = json.loads(Path(policy_path).read_text())
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    groups = [g for g in policy["groups"] if g["split"] != "DEV"]
    tasks = []
    counts = {"train": 0, "sft_valid": 0}
    for group in groups:
        n = 50 if group["split"] == "train" else 20
        for _ in range(n):
            counts[group["split"]] += 1
            tasks.append(scenario(group, counts[group["split"]]))
    texts = []
    for t in dev_audit_tasks + tasks:
        texts.append(
            {
                "split": t["split"],
                "scenario_texts": [t["conversation_opening"]]
                + [s["message"] for s in t["user_followups"] if "message" in s],
            }
        )
    check_texts(texts)
    path = output / "SCENARIOS.jsonl"
    path.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks))
    registry = dict(
        policy,
        episode_ownership={t["episode_id"]: t["split"] for t in tasks},
        episode_template_ownership={t["episode_id"]: t["causal_template_group"] for t in tasks},
    )
    (output / "SPLIT_REGISTRY.json").write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n"
    )
    manifest = {
        "status": "SCENARIOS_FROZEN_NOT_COLLECTED",
        "counts": counts,
        "scenario_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "policy_sha256": hashlib.sha256(Path(policy_path).read_bytes()).hexdigest(),
        "source_type": "synthetic_rule_expert",
        "source": "independently_authored_public_business_rules",
        "cross_split_normalized_text_check": "PASS",
        "dev_copied": False,
        "holdout_generated": False,
        "execution_order": [t["episode_id"] for t in tasks],
    }
    (output / "SCENARIO_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest
