"""Read-only V0.2 audit; never edits task JSON or historical results."""

import argparse
from collections import Counter
import json
from pathlib import Path

from observe import argument_digest, normalized
from v02_guard import TOOL_POLICY, WRITE_TOOLS


def parse_result(content):
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return content


def audit_task(path: Path) -> dict:
    data = json.loads(path.read_text())
    output = {"task_id": data["task_id"], "classification": data["classification"],
              "elapsed_seconds": data.get("elapsed_seconds"), "usage": data.get("usage")}
    simulation = data.get("simulation")
    if not simulation:
        output["trace_complete"] = False
        output["error"] = data.get("error")
        return output
    reward = simulation.get("reward_info") or {}
    output.update({"reward": reward.get("reward"), "reward_breakdown": reward.get("reward_breakdown"),
                   "termination_reason": simulation.get("termination_reason")})
    messages = simulation["messages"]
    official_calls = [(i, c) for i, m in enumerate(messages) if m["role"] == "assistant"
                      for c in m.get("tool_calls") or []]
    official_results = [(i, m) for i, m in enumerate(messages) if m["role"] == "tool"]
    call_by_id = {c["id"]: (i, c) for i, c in official_calls}
    result_by_id = {m["id"]: (i, m) for i, m in official_results}
    attempts = [e for e in data.get("guard_events", []) if e["kind"] == "attempt"]
    forwarded = [e for e in attempts if e["decision"] == "forwarded_to_official"]
    blocked = [e for e in attempts if e["decision"] == "blocked"]
    reused = [e for e in attempts if e["decision"] == "cached_reuse"]
    observed = data.get("observations", [])
    tickets = [e for e in observed if e["kind"] == "bridge_ticket_created"]
    fulfilled = [e for e in observed if e["kind"] == "official_result_fulfilled"]
    returned = [e for e in observed if e["kind"] == "bridge_result_returned"]
    started = [e for e in observed if e["kind"] == "parlant_call_started"]
    ids = [c["id"] for _, c in official_calls]
    forwarded_ids = [e["official_call_id"] for e in forwarded]
    ticket_ids = [e["ticket_id"] for e in tickets]
    positions = {(e["kind"], e.get("ticket_id")): index for index, e in enumerate(observed)
                 if e["kind"] in {"bridge_ticket_created", "official_result_fulfilled", "bridge_result_returned"}}
    checks = {
        "official_calls_unique_and_matched": len(ids) == len(set(ids)) and set(ids) == set(result_by_id)
            and all(call_by_id[cid][0] < result_by_id[cid][0] for cid in ids),
        "forwarded_only_has_official_ids": set(forwarded_ids) == set(ids) and len(forwarded_ids) == len(ids)
            and all(e["ticket_id"] and e["official_call_id"] for e in forwarded)
            and all(e["official_call_id"] is None and e["ticket_id"] is None for e in blocked + reused),
        "ticket_result_one_to_one": Counter(ticket_ids) == Counter(e["ticket_id"] for e in fulfilled)
            == Counter(e["ticket_id"] for e in returned) and set(ticket_ids) == set(e["ticket_id"] for e in forwarded),
        "result_dependency_order": all(positions.get(("bridge_ticket_created", tid), 10**9)
            < positions.get(("official_result_fulfilled", tid), -1)
            < positions.get(("bridge_result_returned", tid), -1) for tid in ticket_ids),
        "parlant_attempt_count": len(attempts) == len(started),
        "cached_source_was_forwarded": all(e.get("evidence", {}).get("source_official_call_id") in ids for e in reused),
        "blocked_has_no_official_call": all(e["argument_sha256"] for e in blocked),
    }
    # The official call/result match applies only to forwarded attempts. The
    # Parlant trace additionally contains local blocked and cached tool events.
    output["mapping_checks"] = checks
    output["trace_complete"] = all(checks.values())
    output["attempt_counts"] = dict(Counter(e["decision"] for e in attempts))
    output["blocked_reasons"] = dict(Counter(e["reason"] for e in blocked))
    output["official_tool_calls"] = len(official_calls)
    output["official_write_calls"] = [c["name"] for _, c in official_calls if c["name"] in WRITE_TOOLS]
    output["official_rejections"] = [{"official_call_id": m["id"], "tool_name": call_by_id[m["id"]][1]["name"]}
                                     for _, m in official_results if isinstance(m.get("content"), str)
                                     and m["content"].startswith("Error:") and m["id"] in call_by_id]
    output["customer_response_seconds_total"] = sum(float(e["full_response_seconds"])
                                                    for e in observed if e["kind"] == "customer_turn_complete")
    output["canned_prompt_coverage"] = [{k: e.get(k) for k in ("trace_id", "prompt_sha256",
                                          "derived_fact_marker_present", "derived_fact_source_ids_present",
                                          "official_success_marker_present")}
                                        for e in observed if e["kind"] == "canned_draft_prompt_coverage"]
    output["reply_corrections"] = [e for e in observed if e["kind"] == "reply_pre_emission_check" and e.get("corrections")]
    output["derived_product_facts"] = [e for e in data.get("guard_events", []) if e["kind"] == "derived_product_fact"]
    output["authentication_source_ids"] = sorted({e.get("evidence", {}).get("auth_official_call_id")
                                                  for e in forwarded if e.get("evidence", {}).get("auth_official_call_id")})
    output["forwarded_precondition_violations"] = [e for e in forwarded if TOOL_POLICY[e["tool_name"]][1]
        and (not e.get("evidence", {}).get("auth_official_call_id") or
             (TOOL_POLICY[e["tool_name"]][0] in {"order_read", "order_write"}
              and not e.get("evidence", {}).get("order_list_official_call_id")) or
             (e["tool_name"] in WRITE_TOOLS and e["tool_name"] != "transfer_to_human_agents"
              and not e.get("evidence", {}).get("confirmation_message_sha256")))]
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("result_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    files = sorted(args.result_dir.glob("task_*.json"), key=lambda p: int(p.stem.split("_")[-1]))
    report = {"tasks": [audit_task(p) for p in files], "task_count": len(files)}
    report["all_trace_checks_pass"] = bool(files) and all(x.get("trace_complete") for x in report["tasks"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"task_count": len(files), "all_trace_checks_pass": report["all_trace_checks_pass"]}))


if __name__ == "__main__":
    main()
