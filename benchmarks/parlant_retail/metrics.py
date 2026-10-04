"""Derive comparable, model-free measures from saved V0/V0.1 trajectories."""

import argparse
from datetime import datetime
import json
from pathlib import Path


def event_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def customer_to_ready_seconds(events: list[dict]) -> list[float]:
    durations = []
    start = None
    for event in sorted(events, key=lambda row: row["offset"]):
        if event["kind"] == "message" and event["source"] == "customer":
            start = event_time(event["creation_utc"])
        elif event["kind"] == "status" and event["data"].get("status") == "ready" and start:
            durations.append((event_time(event["creation_utc"]) - start).total_seconds())
            start = None
    return durations


def collect(raw: dict) -> dict:
    simulation = raw.get("simulation") or {}
    messages = simulation.get("messages") or []
    calls = [(index, call) for index, message in enumerate(messages) if message["role"] == "assistant"
             for call in message.get("tool_calls") or []]
    results = {message["id"]: message["content"] for message in messages if message["role"] == "tool"}
    user_order_list_indices = []
    product_results = []
    for index, call in calls:
        content = results.get(call["id"])
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            data = content
        if call["name"] == "get_user_details" and isinstance(data, dict) and data.get("orders"):
            user_order_list_indices.append(index)
        if call["name"] == "get_product_details" and isinstance(data, dict) and isinstance(data.get("variants"), dict):
            variants = data["variants"].values()
            product_results.append({
                "variants_total": len(data["variants"]),
                "available_true": sum(v.get("available") is True for v in variants if isinstance(v, dict)),
                "available_field_count": sum("available" in v for v in data["variants"].values() if isinstance(v, dict)),
            })
    durations = customer_to_ready_seconds(raw.get("parlant_events") or [])
    reward = simulation.get("reward_info") or {}
    usage = raw.get("usage") or {}
    observations = raw.get("observations") or []
    model_rows = [row for row in observations if row.get("kind") == "model_call"]
    return {
        "task_id": raw["task_id"], "classification": raw["classification"],
        "reward": reward.get("reward"), "reward_breakdown": reward.get("reward_breakdown"),
        "termination_reason": simulation.get("termination_reason"),
        "tool_calls": len(calls),
        "tool_names": [call["name"] for _, call in calls],
        "directory_calls": sum(call["name"] == "list_all_product_types" for _, call in calls),
        "repeated_directory_calls": max(0, sum(call["name"] == "list_all_product_types" for _, call in calls) - 1),
        "user_details_with_orders": len(user_order_list_indices),
        "order_details_after_user_list": sum(call["name"] == "get_order_details" and any(index > earlier for earlier in user_order_list_indices)
                                             for index, call in calls),
        "product_result_summaries": product_results,
        "elapsed_seconds": raw.get("elapsed_seconds"),
        "customer_to_ready_seconds": durations,
        "customer_to_ready_total_seconds": sum(durations),
        "usage": usage,
        "observed_model_calls": {role: sum(row.get("role") == role for row in model_rows)
                                 for role in ("customer_parlant", "simulated_user", "judge")} if observations else "unavailable",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("result_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    files = [args.result_dir / f"task_{task_id}.json" for task_id in range(5)]
    if any(not path.is_file() for path in files):
        raise SystemExit("Five fixed task results are required")
    output = [collect(json.loads(path.read_text())) for path in files]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    for row in output:
        print(row["task_id"], row["reward"], row["tool_calls"], row["directory_calls"], row["customer_to_ready_total_seconds"])


if __name__ == "__main__":
    main()
