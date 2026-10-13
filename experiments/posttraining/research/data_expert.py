"""TRAIN-only public result binding; leaves frozen B0/expert source untouched.

The native prompt props use string section keys; observations are admitted only
when that exact result is also present in the actual model-visible prompt.
Preview fields are flat in the public APP tool result, not a invented envelope.
"""

from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import patch

from ..trajectory import FRAME, redact
from . import expert
from .expert import parse_user_messages, resolve_intent, VISIBLE, VisibleRuleBackend
from .boundary import OnceSingleToolBatch, OnceBatcher


class DataVisibleContextError(RuntimeError):
    """Native visible evidence could not be read or explicitly attributed."""


def model_visible_payload(value):
    # The actual policy sanitizes native prompt text, rather than a standalone
    # JSON mapping. Use that same existing text sanitizer on a native payload;
    # no observations are inferred or repaired by this privacy operation.
    if not isinstance(value, dict):
        return redact(value)
    prefix = "TOOL OBSERVATION\n"
    sanitized = redact(prefix + json.dumps(value, ensure_ascii=False))
    try:
        return json.loads(sanitized[len(prefix):])
    except json.JSONDecodeError as error:
        raise DataVisibleContextError("Model-visible payload invalid after sanitization") from error


def model_visible_frame(frame):
    """Derived input references; original executed ToolResults remain untouched."""
    observations = deepcopy(frame.observations)
    for observed in observations:
        observed["result"]["data"] = model_visible_payload(observed["result"].get("data", {}))
    return replace(frame, observations=observations)


def native_visible_calls(prompt):
    """Read only the native history/staged sections, without text inference."""
    from parlant.core.engines.alpha.prompt_builder import BuiltInSection

    sections = prompt.props
    calls = []
    present = False
    for name, field in (
        (BuiltInSection.INTERACTION_HISTORY, "interaction_events"),
        (BuiltInSection.STAGED_EVENTS, "staged_events_as_dict"),
    ):
        if name not in sections:
            continue
        present = True
        section = sections[name]
        # Native empty history has no interaction_events; it is not a read error.
        if field not in section:
            if section or name == BuiltInSection.STAGED_EVENTS:
                raise DataVisibleContextError("Native visible section field missing")
            continue
        encoded_events = section[field]
        if not isinstance(encoded_events, list):
            raise DataVisibleContextError("Native visible events must be a list")
        for encoded in encoded_events:
            if not isinstance(encoded, str):
                raise DataVisibleContextError("Native visible event must be encoded JSON")
            try:
                event = json.loads(encoded)
            except json.JSONDecodeError as error:
                raise DataVisibleContextError("Native visible event JSON is invalid") from error
            if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
                raise DataVisibleContextError("Native visible event data is invalid")
            tool_calls = event["data"].get("tool_calls", [])
            if not isinstance(tool_calls, list) or any(
                not isinstance(c, dict) or not all(k in c for k in ("tool_id", "arguments", "result"))
                for c in tool_calls
            ):
                raise DataVisibleContextError("Native visible tool call is invalid")
            calls.extend(dict(c, result=model_visible_payload(c["result"])) for c in tool_calls)
    return calls, present


def native_visible_facts(prompt, frame, trajectory):
    """Bind actual visible payloads to this episode's explicit result/call IDs."""
    calls, present = native_visible_calls(prompt)
    if frame.observations and not present:
        raise DataVisibleContextError("Native visible sections missing with preceding results")
    facts = {}
    matched = set()
    for result in frame.observations:
        call_id, result_id = result["tool_call_id"], result["tool_result_id"]
        try:
            pid = trajectory.call_parents[call_id]
            step = trajectory.steps[pid]
        except KeyError as error:
            raise DataVisibleContextError("Preceding result has no policy/call binding") from error
        if step["episode_id"] != frame.episode_id:
            raise DataVisibleContextError("Cross-episode visible result")
        payload = model_visible_payload(result["result"].get("data", {}))
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            continue
        indices = [i for i, c in enumerate(calls)
                   if c["tool_id"].split(":")[-1] == step["candidate_tool_name"]
                   and redact(c["result"]) == payload]
        if not indices:
            # A result not present in this native prompt is not expert input.
            continue
        matched.update(indices)
        fact = {"event_id": result_id, "data": payload["data"],
                "tool_name": step["candidate_tool_name"], "tool_call_id": call_id,
                "parent_policy_step_id": pid, "source_kind": "native_tool_result"}
        if result_id in facts and facts[result_id] != fact:
            raise DataVisibleContextError("Conflicting visible ToolResult binding")
        facts[result_id] = fact
    if any(isinstance(c["result"], dict) and c["result"].get("ok") is True
           and i not in matched for i, c in enumerate(calls)):
        raise DataVisibleContextError("Visible successful tool result lacks explicit attribution")
    return list(facts.values())


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
        frame = FRAME.get()
        facts = native_visible_facts(prompt, frame, self.trajectory)
        frame_token = FRAME.set(model_visible_frame(frame))
        token = VISIBLE.set(
            {"user_messages": user, "agent_messages": agents, "tool_facts": deepcopy(facts)}
        )
        try:
            return await super()._run_consequential_tool_inference(prompt, tool_id, temperature)
        finally:
            VISIBLE.reset(token)
            FRAME.reset(frame_token)


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
