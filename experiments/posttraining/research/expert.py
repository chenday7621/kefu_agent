"""Auditable grammar expert over current native-visible facts, never DB/task gold."""

from contextvars import ContextVar
from copy import deepcopy
import json
from pathlib import Path
import re

from parlant.core.nlp.generation_info import UsageInfo
from ..trajectory import FRAME, CANDIDATE, redact
from .boundary import LAST_REQUEST, OnceSingleToolBatch, OnceBatcher

VISIBLE = ContextVar("training_native_visible", default=None)


def parse_user_messages(messages):
    intent = {}
    facts = []
    rules = {
        "order_id": r"订单[：:]\s*(ORD-[A-Za-z0-9-]+)",
        "item_id": r"明细[：:]\s*(ITEM-[A-Za-z0-9-]+)",
        "quantity": r"数量[：:]\s*(\d+)",
        "reason": r"原因[：:]\s*([^。；\n]+)",
        "product_name": r"名称准确为“([^”]+)”",
    }
    for event in messages:
        text = event["message"]
        data = {}
        spans = {}
        for key, pattern in rules.items():
            match = re.search(pattern, text)
            if match:
                value = int(match.group(1)) if key == "quantity" else match.group(1).strip()
                intent[key] = value
                data[key] = value
                spans[key] = list(match.span(1))
        if "最新已送达" in text:
            intent["order_selector"] = "latest_delivered"
        if "全部可退" in text:
            intent["quantity_selector"] = "all_available"
        if (
            "只授权生成退货预览" in text
            or "仅为我生成" in text
            or "只做预览" in text
            or "仍然只预览" in text
        ):
            intent["preview_only"] = True
        if "数量我要看到订单查询结果以后再告诉你" in text:
            intent["wait_for_quantity"] = True
        if "quantity" in data:
            intent.pop("wait_for_quantity", None)
        facts.append(
            {
                "event_id": event["event_id"],
                "data": data,
                "source_kind": "native_user_message",
                "source_message": text,
                "extraction_spans": spans,
            }
        )
    return intent, facts


def resolve_intent(intent, tool_facts):
    result = deepcopy(intent)
    refs = {}
    orders = []
    details = []
    previews = []
    submitted = []
    for fact in tool_facts:
        name = fact["tool_name"]
        data = fact["data"]
        if name == "list_my_orders":
            orders.extend((fact, i, o) for i, o in enumerate(data.get("orders", [])))
        elif name == "get_order_details":
            details.append(fact)
        elif name == "check_return_eligibility":
            previews.append(fact)
        elif name == "submit_confirmed_return":
            submitted.append(fact)
    if "order_id" not in result and result.get("order_selector") == "latest_delivered":
        candidates = [x for x in orders if x[2]["status"] == "delivered"]
        if candidates:
            latest = max(o["delivered_at"] for _, _, o in candidates)
            choices = [x for x in candidates if x[2]["delivered_at"] == latest]
            if len(choices) == 1:
                fact, i, order = choices[0]
                result["order_id"] = order["id"]
                refs["order_id"] = (fact["event_id"], ["orders", i, "id"])
    matches = [f for f in details if f["data"].get("order", {}).get("id") == result.get("order_id")]
    if matches:
        fact = matches[-1]
        items = fact["data"].get("items", [])
        choices = [
            (i, item)
            for i, item in enumerate(items)
            if item["id"] == result.get("item_id")
            or ("item_id" not in result and item["product_name"] == result.get("product_name"))
        ]
        if len(choices) == 1:
            i, item = choices[0]
            result["item_id"] = item["id"]
            refs["item_id"] = (fact["event_id"], ["items", i, "id"])
            if result.get("quantity_selector") == "all_available":
                result["quantity"] = item["available_return_quantity"]
                refs["quantity"] = (fact["event_id"], ["items", i, "available_return_quantity"])
            result["read_line"] = True
            result["line_eligible"] = bool(
                item["returnable"]
                and fact["data"]["order"]["status"] == "delivered"
                and isinstance(result.get("quantity"), int)
                and 1 <= result["quantity"] <= item["available_return_quantity"]
            )
    return result, refs, matches, previews, submitted


def choose(view, candidate):
    intent, user_facts = parse_user_messages(view["user_messages"])
    resolved, derived, details, previews, submitted = resolve_intent(intent, view["tool_facts"])
    argument_refs = {}
    for fact in user_facts:
        for key in fact["data"]:
            argument_refs[key] = (fact["event_id"], [key])
    argument_refs.update(derived)
    matching = []
    for fact in previews:
        preview = fact["data"].get("preview", {})
        if all(
            preview.get(k) == resolved.get(k) for k in ("order_id", "item_id", "quantity", "reason")
        ):
            matching.append(fact)
    confirmed = None
    if matching and not resolved.get("preview_only"):
        fact = matching[-1]
        op = fact["data"]["operation_id"]
        p = fact["data"]["preview"]
        phrase = f"确认退货 {op} 订单{p['order_id']} 明细{p['item_id']} 数量{p['quantity']}"
        shown = any(phrase in a["message"] for a in view["agent_messages"])
        actual = any(m["message"].strip() == phrase for m in view["user_messages"])
        if shown and actual:
            confirmed = op
            argument_refs["operation_id"] = (fact["event_id"], ["operation_id"])
    args = None
    rationale = "Current visible facts do not justify this candidate."
    hard = False
    if not submitted:
        if (
            "order_id" not in resolved
            and resolved.get("order_selector") == "latest_delivered"
            and not any(f["tool_name"] == "list_my_orders" for f in view["tool_facts"])
        ):
            if candidate == "list_my_orders":
                args = {}
                rationale = "Read orders to resolve the explicit latest-delivered selection rule."
        elif "order_id" in resolved and not details:
            if candidate == "get_order_details":
                args = {"order_id": resolved["order_id"]}
                rationale = "Read the user-specified or visibly selected order."
        elif details and not matching:
            if candidate == "check_return_eligibility":
                ready = (
                    all(k in resolved for k in ("order_id", "item_id", "quantity", "reason"))
                    and resolved.get("line_eligible")
                    and not resolved.get("wait_for_quantity")
                )
                if ready:
                    args = {k: resolved[k] for k in ("order_id", "item_id", "quantity", "reason")}
                    rationale = "Visible order facts and user parameters support one preview."
                else:
                    hard = True
                    rationale = (
                        "Wait for missing user parameters or eligibility; guessing is forbidden."
                    )
        elif matching and confirmed and candidate == "submit_confirmed_return":
            args = {"operation_id": confirmed}
            rationale = "Latest visible matching preview has an actual exact user confirmation."
    if candidate == "submit_confirmed_return" and args is None:
        hard = True
        rationale = "No fresh exact confirmation, preview-only permission, or already completed; no submission."
    if candidate == "check_return_eligibility" and (matching or submitted):
        hard = True
        rationale = "The current preview or request already exists; do not create a duplicate."
    return args, rationale, hard, user_facts + view["tool_facts"], argument_refs


class VisibleRuleBackend:
    origin = "synthetic_rule_expert"

    def __init__(self, tokenizer, directory):
        self.tokenizer = tokenizer
        self.directory = Path(directory)
        self.records = {}

    async def complete(self, request):
        LAST_REQUEST.set(request)
        view = VISIBLE.get()
        if view is None:
            raise RuntimeError("Explicit native-visible expert context required")
        tool = CANDIDATE.get()[1]
        args, reason, hard, facts, refs = choose(view, tool.name)
        evaluations = []
        for key, value in (args or {}).items():
            evaluations.append(
                {
                    "parameter_name": key,
                    "acceptable_source_for_this_argument_according_to_its_tool_definition": "Actual user message or preceding visible tool observation.",
                    "evaluate_is_it_provided_by_an_acceptable_source": "The exact value is supported by an explicit visible fact.",
                    "evaluate_was_it_already_provided_and_should_it_be_provided_again": "Needed for this bound candidate only.",
                    "evaluate_is_it_potentially_problematic_to_guess_what_the_value_is_if_it_isnt_provided": "Guessing is forbidden.",
                    "is_optional": False,
                    "valid_invalid_or_missing": "valid",
                    "value_as_string": str(value),
                }
            )
        target = {
            "name": tool.name,
            "last_customer_message": view["user_messages"][-1]["message"],
            "most_recent_customer_inquiry_or_need": "User request as expressed in the visible conversation.",
            "most_recent_customer_inquiry_or_need_was_already_resolved": False,
            "subtleties_to_be_aware_of": "Respect explicit permission and observed confirmation; do not guess.",
            "tool_calls_for_candidate_tool": [
                {
                    "applicability_rationale": reason,
                    "is_applicable": args is not None,
                    "argument_evaluations": evaluations,
                    "same_call_is_already_staged": False,
                    "relevant_subtleties": reason,
                }
            ],
        }
        raw = json.dumps(target, ensure_ascii=False, separators=(",", ":"))
        pid = request["policy_step_id"]
        prompt_ids = self.tokenizer.encode(request["rendered_prompt"], add_special_tokens=False)
        self.records[pid] = {
            "input_tokens": len(prompt_ids),
            "output_tokens": len(self.tokenizer.encode(raw, add_special_tokens=False)),
        }
        row = {
            "policy_step_id": pid,
            "target_json": target,
            "prompt_token_ids": prompt_ids,
            "rendered_prompt": request["rendered_prompt"],
            "preceding_visible_facts": facts,
            "argument_refs": {k: refs[k] for k in (args or {})},
            "hard_no_call": hard,
            "source_type": self.origin,
            "expert_reads_db_or_task": False,
        }
        with (self.directory / "EXPERT_RECORDS.jsonl").open("a") as out:
            out.write(json.dumps(redact(row), ensure_ascii=False) + "\n")
        return raw

    def usage_for(self, pid):
        return UsageInfo(**self.records[pid], extra={"synthetic_rule_expert": 1})


class ExpertSingleToolBatch(OnceSingleToolBatch):
    async def _run_consequential_tool_inference(self, prompt, tool_id, temperature):
        from parlant.core.sessions import EventKind, EventSource

        user = []
        agents = []
        for e in self._context.interaction_history:
            if e.kind == EventKind.MESSAGE:
                row = {"event_id": str(e.id), "message": redact(e.data["message"])}
                if e.source in (EventSource.CUSTOMER, EventSource.CUSTOMER_UI):
                    user.append(row)
                elif e.source == EventSource.AI_AGENT:
                    agents.append(row)
        # Only observations also present in the actual native prompt are exposed.
        from parlant.core.engines.alpha.prompt_builder import BuiltInSection

        sections = prompt.props
        visible_calls = []
        for name in (BuiltInSection.INTERACTION_HISTORY, BuiltInSection.STAGED_EVENTS):
            for values in sections.get(name, {}).values():
                if not isinstance(values, list):
                    continue
                for encoded in values:
                    if not isinstance(encoded, str):
                        continue
                    try:
                        event = json.loads(encoded)
                    except json.JSONDecodeError:
                        continue
                    visible_calls.extend(event.get("data", {}).get("tool_calls", []))
        facts = []
        frame = FRAME.get()
        for result in frame.observations:
            payload = result["result"].get("data", {})
            if not payload.get("ok"):
                continue
            pid = self.trajectory.call_parents[result["tool_call_id"]]
            name = self.trajectory.steps[pid]["candidate_tool_name"]
            if not any(
                c["tool_id"].split(":")[-1] == name and redact(c["result"]) == payload
                for c in visible_calls
            ):
                continue
            facts.append(
                {
                    "event_id": result["tool_result_id"],
                    "data": payload["data"],
                    "tool_name": name,
                    "tool_call_id": result["tool_call_id"],
                    "parent_policy_step_id": pid,
                    "source_kind": "native_tool_result",
                }
            )
        token = VISIBLE.set(
            {"user_messages": user, "agent_messages": agents, "tool_facts": deepcopy(facts)}
        )
        try:
            return await super()._run_consequential_tool_inference(prompt, tool_id, temperature)
        finally:
            VISIBLE.reset(token)


class ExpertBatcher(OnceBatcher):
    def _create_single_tool_batch(self, candidate_tool, context):
        if not candidate_tool[1].consequential:
            return super()._create_single_tool_batch(candidate_tool, context)
        return ExpertSingleToolBatch(
            logger=self._logger,
            meter=self._meter,
            optimization_policy=self._optimization_policy,
            service_registry=self._service_registry,
            consequential_schema_generator=self._single_tool_schematic_generator,
            non_consequential_schema_generator=self._simple_tool_schematic_generator,
            candidate_tool=candidate_tool,
            context=context,
            trajectory=self.trajectory,
        )
