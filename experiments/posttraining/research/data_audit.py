"""Business and lineage validation independent of the label-producing expert."""

from copy import deepcopy
import json

from parlant.core.engines.alpha.tool_calling.single_tool_batch import SingleToolBatchSchema
from .data_validation import verify_sample


def expected_parameters(task):
    # Privileged fixture access is confined to final validation, never generation.
    fixture = task["initial_db_fixture"]["tables"]
    fields = deepcopy(task["visible_goal_fields"])
    for script in task["user_followups"]:
        fields.update(script.get("sets", {}))
    if fields.get("order_selector") == "latest_delivered":
        delivered = [o for o in fixture["orders"] if o["status"] == "delivered"]
        latest = max(o["delivered_at"]["seconds_from_episode_start"] for o in delivered)
        choices = [o for o in delivered if o["delivered_at"]["seconds_from_episode_start"] == latest]
        if len(choices) != 1:
            raise ValueError("Ambiguous validation target")
        fields["order_id"] = choices[0]["id"]
    rows = [i for i in fixture["order_items"] if i["order_id"] == fields["order_id"] and
            (i["id"] == fields.get("item_id") or
             ("item_id" not in fields and i["product_name"] == fields.get("product_name")))]
    if len(rows) != 1:
        raise ValueError("Ambiguous validation line")
    fields["item_id"] = rows[0]["id"]
    if fields.get("quantity_selector") == "all_available":
        fields["quantity"] = rows[0]["quantity"] - rows[0]["reserved_return_quantity"]
    return {k: fields[k] for k in ("order_id", "item_id", "quantity", "reason")}


def business_report(task, final_db, events):
    wanted = expected_parameters(task)
    requests = final_db["return_requests"]
    operations = final_db["return_operations"]
    confirmations = {e["operation_id"]: e for e in events if e["kind"] == "confirmation_recorded"}
    displayed = {e["operation_id"] for e in events if e["kind"] == "snapshot_displayed"}
    def matches(row):
        return all(row.get(k) == v for k, v in wanted.items())
    errors = []
    user_messages = {e["event_id"]: e["text"] for e in events if e["kind"] == "user_message"}
    updates = [e for e in events if e["kind"] == "user_update"]
    for script in task["user_followups"]:
        if "sets" in script and not any(e["fields"] == script["sets"]
                and user_messages.get(e["user_event_id"]) == script["message"] for e in updates):
            errors.append("unconsumed_registered_user_input")
    fixture = task["initial_db_fixture"]["tables"]
    line = next(i for i in fixture["order_items"] if i["id"] == wanted["item_id"])
    for request in requests:
        if not matches(request) or request["operation_id"] not in confirmations or request["operation_id"] not in displayed:
            errors.append("unauthorized_or_wrong_request")
        if (request.get("customer_id") != task["initial_db_fixture"]["trusted_context"]["customer_id"]
                or request["amount_cents"] != wanted["quantity"] * line["unit_price_cents"]):
            errors.append("ownership_or_amount_mismatch")
    if len(requests) > 1:
        errors.append("duplicate_request")
    if task["data_terminal_goal"] == "preview_without_request":
        success = not requests and any(matches(o) and o["id"] in displayed for o in operations)
        if requests:
            errors.append("preview_only_write")
    else:
        success = len(requests) == 1 and matches(requests[0]) and not errors
    reserved = sum(r["quantity"] for r in requests)
    actual = sum(i["reserved_return_quantity"] for i in final_db["order_items"])
    if reserved != actual:
        errors.append("reservation_mismatch")
    for item in final_db["order_items"]:
        expected_reserved = sum(r["quantity"] for r in requests if r["item_id"] == item["id"])
        if item["reserved_return_quantity"] != expected_reserved:
            errors.append("reservation_mismatch")
    if len(operations) != 1 or any(not matches(o) or o["quoted_unit_price_cents"] != line["unit_price_cents"]
                                  for o in operations):
        errors.append("missing_wrong_or_duplicate_preview")
    for event in events:
        if event["kind"] == "tool_result" and event["result"].get("ok") is not True:
            errors.append("backend_rejection")
    return {"passed": bool(success and not errors), "goal_reached": bool(success), "errors": errors,
            "expected_parameters": wanted, "independent_of_expert": True,
            "counts_as_baseline": False}


def verify_lineage(task, policy, expert, events):
    if policy["generation_origin"] != "synthetic_rule_expert" or policy["parse_status"] != "passed":
        raise ValueError("Not a parsed independent expert target")
    parsed = SingleToolBatchSchema.model_validate(expert["target_json"])
    actual = SingleToolBatchSchema.model_validate_json(policy["raw_model_output"])
    if parsed != actual or parsed.model_dump(mode="json") != policy["parsed_schema_output"]:
        raise ValueError("Target differs from actual native parse")
    if expert["rendered_prompt"] != policy["rendered_prompt"]:
        raise ValueError("Actual native/model prompt differs")
    proposals = [e for e in events if e["kind"] == "policy_proposal" and e["policy_step_id"] == policy["policy_step_id"]]
    if len(proposals) != 1 or proposals[0]["split"] != task["split"]:
        raise ValueError("Missing or cross-split proposal")
    before = [e for e in events if e["sequence"] < proposals[0]["sequence"]]
    user_events = {e["event_id"]: e for e in before if e["kind"] == "user_message"}
    observations = {r["tool_result_id"]: r for r in policy["relevant_tool_observations"]}
    results = {e["tool_result_id"]: e for e in before if e["kind"] == "tool_result"}
    for fact in expert["preceding_visible_facts"]:
        fid = fact["event_id"]
        if fact["source_kind"] == "native_user_message":
            if fid not in user_events or fid not in policy["conversation_history_refs"]:
                raise ValueError("Future or unbound user evidence")
            if fact["source_message"] != user_events[fid]["text"]:
                raise ValueError("User evidence was modified")
            for field, span in fact["extraction_spans"].items():
                if str(fact["data"][field]) != fact["source_message"][span[0]:span[1]].strip():
                    raise ValueError("User argument lacks exact source span")
        elif fact["source_kind"] == "native_tool_result":
            if fid not in observations or fid not in results:
                raise ValueError("Future or unbound tool fact")
            if fact["data"] != observations[fid]["result"]["data"]["data"]:
                raise ValueError("Tool evidence was modified")
        else:
            raise ValueError("Unknown evidence source")
    return True


def verify_visible_action(policy, expert, events):
    """Constraint verifier; never invokes choose() or uses future task fields."""
    fields = {}
    messages = []
    tools = []
    for fact in expert["preceding_visible_facts"]:
        if fact["source_kind"] == "native_user_message":
            fields.update(fact["data"])
            messages.append(fact["source_message"])
        else:
            tools.append(fact)
    text = "\n".join(messages)
    if "order_id" not in fields and "最新已送达" in text:
        orders = [o for f in tools if f["tool_name"] == "list_my_orders" for o in f["data"].get("orders", [])
                  if o["status"] == "delivered"]
        if orders:
            latest = max(o["delivered_at"] for o in orders)
            unique = [o for o in orders if o["delivered_at"] == latest]
            if len(unique) == 1:
                fields["order_id"] = unique[0]["id"]
    details = [f["data"] for f in tools if f["tool_name"] == "get_order_details"
               and f["data"]["order"]["id"] == fields.get("order_id")]
    if details:
        matches = [i for i in details[-1]["items"] if i["id"] == fields.get("item_id") or
                   ("item_id" not in fields and i["product_name"] == fields.get("product_name"))]
        if len(matches) == 1:
            fields["item_id"] = matches[0]["id"]
            if "全部可退" in text:
                fields["quantity"] = matches[0]["available_return_quantity"]
    previews = [f["data"] for f in tools if f["tool_name"] == "check_return_eligibility"
                and all(k in fields and f["data"].get(k) == fields[k]
                        for k in ("order_id", "item_id", "quantity", "reason"))]
    complete = any(f["tool_name"] == "submit_confirmed_return" for f in tools)
    permitted = set()
    if not complete:
        if "order_id" not in fields and "最新已送达" in text and not any(f["tool_name"] == "list_my_orders" for f in tools):
            permitted.add("list_my_orders")
        if "order_id" in fields and not details:
            permitted.add("get_order_details")
        if details and not previews and all(k in fields for k in ("order_id", "item_id", "quantity", "reason")):
            selected = [i for i in details[-1]["items"] if i["id"] == fields["item_id"]]
            if (len(selected) == 1 and selected[0]["returnable"] and details[-1]["order"]["status"] == "delivered"
                    and isinstance(fields["quantity"], int) and not isinstance(fields["quantity"], bool)
                    and 0 < fields["quantity"] <= selected[0]["available_return_quantity"]):
                permitted.add("check_return_eligibility")
        preview_only = any(cue in text for cue in ("只授权生成退货预览", "仅为我生成", "只做预览", "仍然只预览"))
        if previews and not preview_only:
            sequence = next(e["sequence"] for e in events if e["kind"] == "policy_proposal"
                            and e["policy_step_id"] == policy["policy_step_id"])
            if any(e["kind"] == "confirmation_recorded" and e["operation_id"] == previews[-1]["operation_id"]
                   and e["sequence"] < sequence for e in events):
                permitted.add("submit_confirmed_return")
    should_call = policy["candidate_tool_name"] in permitted
    if (policy["proposed_action"] == "CALL") != should_call:
        raise ValueError("Step action violates independently checked current visible constraints")
    return True


def admit_sample(task, policy, expert, events, business, registry):
    if not business["passed"]:
        raise ValueError("Unverified whole episode cannot enter supervision")
    verify_lineage(task, policy, expert, events)
    verify_visible_action(policy, expert, events)
    gate = policy["actual_gate_result"] or {}
    gate_ids = [c["tool_call_id"] for c in gate.get("accepted_calls", [])]
    executed = [c["tool_call_id"] for c in policy["actual_executed_calls"]]
    result_ids = [r["tool_call_id"] for r in policy["tool_results"]]
    if any(not isinstance(r["result"].get("data"), dict) or r["result"]["data"].get("ok") is not True
           for r in policy["tool_results"]):
        raise ValueError("Rejected action is not positive supervision")
    argument_evidence, paths = {}, {}
    for argument, (ref, path) in expert["argument_refs"].items():
        argument_evidence[argument] = [ref]
        paths[argument] = {ref: path}
    sample = {k: task[k] for k in ("episode_id", "task_id", "split", "family_id", "capability_family",
                                   "causal_template_group", "template_fingerprint", "source", "source_type")}
    sample.update(sample_id=policy["policy_step_id"], policy_step_id=policy["policy_step_id"],
                  schema_name="SingleToolBatchSchema", candidate_tool=policy["candidate_tool_name"],
                  model_revision=policy["model_revision"], prompt_sha256=policy["prompt_hash"],
                  prompt_token_ids=expert["prompt_token_ids"], target_json=expert["target_json"],
                  argument_evidence=argument_evidence, argument_paths=paths,
                  action=policy["proposed_action"], hard_no_call=expert["hard_no_call"],
                  joint_round=policy["engine_iteration"], verification={
                      "schema": True, "visible_evidence": True, "native_gate": True,
                      "business_predicates": True})
    lineage = dict(expert, counts_as_baseline=False, gate_call_ids=gate_ids,
                   executed_call_ids=executed, result_call_ids=result_ids,
                   backend_rejection=False, wrong_action=False)
    sample["dedup_hash"] = verify_sample(sample, lineage, registry)
    # Independent confirmation constraint: no accepted submit without its
    # actual earlier user event. Expert text is never a confirmation proof.
    for call in gate.get("accepted_calls", []):
        if call["tool_name"].endswith(":submit_confirmed_return"):
            pid_sequence = next(e["sequence"] for e in events if e["kind"] == "policy_proposal"
                                and e["policy_step_id"] == policy["policy_step_id"])
            if not any(e["kind"] == "confirmation_recorded" and e["operation_id"] == call["arguments"]["operation_id"]
                       and e["sequence"] < pid_sequence for e in events):
                raise ValueError("Submit lacks a prior actual native confirmation")
    return sample


def load_latest(directory):
    rows = {}
    for path in directory.joinpath("trajectories").glob("policy-*.jsonl"):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            rows[row["policy_step_id"]] = row
    return rows
