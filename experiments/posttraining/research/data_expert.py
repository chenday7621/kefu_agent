"""TRAIN-only public result binding; leaves frozen B0/expert source untouched.

The native prompt props use string section keys; observations are admitted only
when that exact result is also present in the actual model-visible prompt.
Preview fields are flat in the public APP tool result, not a invented envelope.
"""

from copy import deepcopy
import json
from unittest.mock import patch

from ..trajectory import FRAME, redact
from . import expert
from .expert import parse_user_messages, resolve_intent, VISIBLE, VisibleRuleBackend
from .boundary import OnceSingleToolBatch, OnceBatcher

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
        preview = fact["data"]
        if all(
            preview.get(k) == resolved.get(k) for k in ("order_id", "item_id", "quantity", "reason")
        ):
            matching.append(fact)
    confirmed = None
    if matching and not resolved.get("preview_only"):
        fact = matching[-1]
        op = fact["data"]["operation_id"]
        phrase = fact["data"].get("confirmation_phrase")
        shown = isinstance(phrase, str) and any(phrase in a["message"] for a in view["agent_messages"])
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


class DataRuleBackend(VisibleRuleBackend):
    async def complete(self, request):
        # Base completion has no await/yield: the scope cannot interleave with
        # another candidate. Only this isolated TRAIN child uses this backend.
        with patch.object(expert, "choose", choose):
            return await super().complete(request)


class DataSingleToolBatch(OnceSingleToolBatch):
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
            for values in sections.get(f"__{name.name}__", {}).values():
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
            if not isinstance(payload, dict) or payload.get("ok") is not True:
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


class DataBatcher(OnceBatcher):
    def _create_single_tool_batch(self, candidate_tool, context):
        if not candidate_tool[1].consequential:
            return super()._create_single_tool_batch(candidate_tool, context)
        return DataSingleToolBatch(
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
