"""Audit saved tau2 and Parlant traces without running any models."""

import argparse
import ast
from collections import Counter
import json
from pathlib import Path


def normalized(value):
    # Parlant's event serializer represents list arguments as Python strings.
    if isinstance(value, str) and value.startswith("["):
        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, list):
                return parsed
        except (SyntaxError, ValueError):
            pass
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalized(item) for item in value]
    return value


def canonical(name, arguments, result):
    return json.dumps(
        [name, normalized(arguments), normalized(result)],
        sort_keys=True, ensure_ascii=False,
    )


def audit_task(data):
    simulation = data.get("simulation")
    if simulation is None:
        return {"task_id": data["task_id"], "classification": data["classification"], "trace_complete": False}

    messages = simulation["messages"]
    calls = [call for message in messages if message["role"] == "assistant"
             for call in message.get("tool_calls") or []]
    results = [message for message in messages if message["role"] == "tool"]
    result_by_id = {message["id"]: message["content"] for message in results}
    official = Counter()
    for call in calls:
        content = result_by_id.get(call["id"])
        try:
            result = json.loads(content)
        except (TypeError, ValueError):
            result = content
        official[canonical(call["name"], call["arguments"], result)] += 1

    parlant_calls = [call for event in data["parlant_events"] if event["kind"] == "tool"
                     for call in event["data"]["tool_calls"]]
    parlant = Counter(canonical(call["tool_id"].split(":")[-1], call["arguments"],
                                call["result"]["data"]) for call in parlant_calls)
    tool_names = [call["name"] for call in calls]
    write_names = {
        "cancel_pending_order", "modify_pending_order_address", "modify_pending_order_items",
        "modify_pending_order_payment", "return_delivered_order_items",
        "exchange_delivered_order_items", "transfer_to_human_agents",
    }
    write_calls = [call for call in calls if call["name"] in write_names]
    write_signatures = [canonical(call["name"], call["arguments"], None)
                        for call in write_calls]
    reward = simulation.get("reward_info") or {}
    output = {
        "task_id": data["task_id"],
        "classification": data["classification"],
        "termination_reason": simulation.get("termination_reason"),
        "reward": reward.get("reward"),
        "reward_breakdown": reward.get("reward_breakdown"),
        "db_check": reward.get("db_check"),
        "nl_assertions": reward.get("nl_assertions"),
        "communicate_checks": reward.get("communicate_checks"),
        "elapsed_seconds": data["elapsed_seconds"],
        "usage": data["usage"],
        "official_tool_calls": len(calls),
        "official_tool_results": len(results),
        "parlant_tool_events": len(parlant_calls),
        "tool_names": tool_names,
        "write_tool_names": [name for name in tool_names if name in write_names],
        "no_duplicate_write_calls": len(write_signatures) == len(set(write_signatures)),
        "all_tool_ids_unique": len({call["id"] for call in calls}) == len(calls),
        "all_tool_results_matched": len(results) == len(calls) == len(result_by_id)
        and set(result_by_id) == {call["id"] for call in calls},
        "parlant_official_tool_multisets_match": official == parlant,
        "trace_complete": True,
    }
    observations = data.get("observations")
    if observations is not None:
        tickets = [row for row in observations if row["kind"] == "bridge_ticket_created"]
        fulfilled = [row for row in observations if row["kind"] == "official_result_fulfilled"]
        returned = [row for row in observations if row["kind"] == "bridge_result_returned"]
        started = [row for row in observations if row["kind"] == "parlant_call_started"]
        ticket_ids = [row["ticket_id"] for row in tickets]
        call_ids = [row["official_call_id"] for row in tickets]
        parlant_ids = [row["parlant_call_id"] for row in tickets]
        event_position = {(row["kind"], row.get("ticket_id")): index for index, row in enumerate(observations)
                          if row["kind"] in {"bridge_ticket_created", "official_result_fulfilled", "bridge_result_returned"}}
        output["identity_mapping"] = {
            "ticket_count": len(tickets),
            "parlant_call_count": len(started),
            "parlant_calls_equal_tickets": Counter(row["parlant_call_id"] for row in started) == Counter(parlant_ids),
            "all_parlant_ids_observed": all(value != "unavailable" for value in parlant_ids),
            "unique_ticket_ids": len(set(ticket_ids)) == len(tickets),
            "unique_parlant_ids": len(set(parlant_ids)) == len(tickets),
            "official_ids_equal_trace": set(call_ids) == {call["id"] for call in calls},
            "one_result_per_ticket": Counter(row["ticket_id"] for row in fulfilled)
                == Counter(ticket_ids) == Counter(row["ticket_id"] for row in returned),
            "result_dependency_order": all(
                event_position.get(("bridge_ticket_created", ticket_id), 10**9)
                < event_position.get(("official_result_fulfilled", ticket_id), -1)
                < event_position.get(("bridge_result_returned", ticket_id), -1)
                for ticket_id in ticket_ids
            ),
            "mappings": [{key: row.get(key) for key in ("parlant_call_id", "ticket_id", "official_call_id", "batch", "iteration", "tool_name")}
                         for row in tickets],
        }
        positions = {call["id"]: index for index, message in enumerate(messages)
                     if message["role"] == "assistant" for call in message.get("tool_calls") or []}
        result_positions = {message["id"]: index for index, message in enumerate(messages) if message["role"] == "tool"}
        output["identity_mapping"]["official_call_before_result"] = all(
            positions.get(call_id, 10**9) < result_positions.get(call_id, -1) for call_id in call_ids
        )
        turns = [row for row in observations if row["kind"] == "customer_turn_complete"]
        response_phases = [row for row in observations if row["kind"] == "response_generation"]
        prompt_coverages = [row for row in observations if row["kind"] == "response_prompt_coverage"]
        output["full_response_seconds"] = [row["full_response_seconds"] for row in turns]
        output["full_response_seconds_total"] = sum(row["full_response_seconds"] for row in turns)
        output["response_generation_seconds_total"] = sum(row["seconds"] for row in response_phases)
        output["final_response_prompt_coverage"] = prompt_coverages[-1] if prompt_coverages else "unavailable"
        output["model_calls_observed"] = {
            role: sum(row["kind"] == "model_call" and row.get("role") == role for row in observations)
            for role in ("customer_parlant", "simulated_user", "judge")
        }
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("result_dir", type=Path)
    args = parser.parse_args()
    selection = json.loads((Path(__file__).parent / "selection.json").read_text())
    files = [args.result_dir / f"task_{task_id}.json" for task_id in selection["task_ids"]]
    if any(not path.exists() for path in files):
        raise SystemExit("All five fixed task results are required")
    tasks = [audit_task(json.loads(path.read_text())) for path in files]
    output = {
        "task_ids": selection["task_ids"],
        "tasks": tasks,
        "all_trace_checks_pass": all(
            task.get("trace_complete") and task.get("all_tool_ids_unique")
            and task.get("all_tool_results_matched")
            and task.get("parlant_official_tool_multisets_match")
            and task.get("no_duplicate_write_calls")
            and ("identity_mapping" not in task or all(value for key, value in task["identity_mapping"].items()
                                                 if key not in {"ticket_count", "parlant_call_count", "mappings"}))
            for task in tasks
        ),
    }
    (args.result_dir / "audit.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"all_trace_checks_pass": output["all_trace_checks_pass"],
                      "tasks": [(x["task_id"], x["classification"], x["reward"])
                                for x in tasks]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
