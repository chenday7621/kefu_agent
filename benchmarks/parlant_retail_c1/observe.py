"""Read-only V0.1 hooks for Parlant phases and bridge call correlation."""

from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalized(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("["):
        import ast
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


def argument_digest(name: str, arguments: Any) -> str:
    raw = json.dumps([name, normalized(arguments)], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def result_summary(name: str, data: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {"tool": name, "result_type": type(data).__name__}
    if not isinstance(data, dict):
        summary["success_marker"] = data == "Transfer successful" if name == "transfer_to_human_agents" else None
        return summary
    summary["fields"] = sorted(data.keys())
    for field in ("orders", "items", "payment_methods"):
        if field in data and isinstance(data[field], (dict, list)):
            summary[f"{field}_count"] = len(data[field])
    if "variants" in data and isinstance(data["variants"], dict):
        variants = list(data["variants"].values())
        summary["variants_total"] = len(variants)
        summary["available_true"] = sum(v.get("available") is True for v in variants if isinstance(v, dict))
        summary["available_field_count"] = sum("available" in v for v in variants if isinstance(v, dict))
    if "status" in data:
        summary["status"] = data["status"]
    return summary


def event_tool_results(events) -> list[tuple[str, Any]]:
    output = []
    for event in events:
        kind = getattr(event, "kind", None)
        if getattr(kind, "value", kind) != "tool":
            continue
        for call in (event.data or {}).get("tool_calls", []):
            output.append((str(call.get("tool_id", "")).split(":")[-1], call.get("result", {}).get("data")))
    return output


class Observer:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        # Context propagates into Parlant's async processing task. A session
        # remains mapped to its original attempt even after the next task starts.
        self._scope: ContextVar[tuple[str, str, str | None] | None] = ContextVar(
            "retail_observer_scope", default=None
        )
        self.sessions: dict[str, tuple[str, str]] = {}
        self.turns: dict[tuple[str, str], int] = defaultdict(int)
        self.phases: dict[tuple[str, str], str] = {("startup", "startup"): "startup"}
        self.records: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.pending_parlant: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.call_batches: dict[str, dict] = {}
        self.journals: dict[tuple[str, str], Path] = {}

    def set_startup_journal(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.journals[("startup", "startup")] = path

    def set_unattributed_journal(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.journals[("unattributed", "unattributed")] = path

    def begin_attempt(self, task_id: str, attempt_id: str, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            key = (task_id, attempt_id)
            if key in self.journals:
                raise RuntimeError(f"Duplicate observation attempt: {key}")
            self.journals[key] = path
            self.phases[key] = "task"
        return self._scope.set((task_id, attempt_id, None))

    def end_attempt(self, token) -> None:
        self._scope.reset(token)

    def register_session(self, session: str) -> None:
        scope = self._scope.get()
        if scope is None:
            raise RuntimeError("Cannot register a session outside an attempt")
        with self.lock:
            if session in self.sessions:
                raise RuntimeError(f"Duplicate observation session: {session}")
            self.sessions[session] = scope[:2]
        self._scope.set((scope[0], scope[1], session))

    def bind_session(self, session: str):
        with self.lock:
            key = self.sessions.get(session, ("unattributed", "unattributed"))
        return self._scope.set((key[0], key[1], session))

    def unbind_session(self, token) -> None:
        self._scope.reset(token)

    def set_phase(self, task_id: str, attempt_id: str, phase: str) -> None:
        with self.lock:
            self.phases[(task_id, attempt_id)] = phase

    def next_turn(self) -> int:
        context = self.context()
        key = (context["task_id"], context["attempt_id"])
        with self.lock:
            self.turns[key] += 1
            return self.turns[key]

    def context(self, session: str | None = None) -> dict:
        with self.lock:
            if session is not None:
                key = self.sessions.get(session)
                if key is None:
                    key = ("unattributed", "unattributed")
                session_id = session
            else:
                scope = self._scope.get()
                key = scope[:2] if scope is not None else ("startup", "startup")
                session_id = scope[2] if scope is not None else None
            return {"task_id": key[0], "attempt_id": key[1],
                    "session_id": session_id or "unavailable",
                    "turn": self.turns.get(key) or "unavailable",
                    "phase": self.phases.get(key, "unavailable")}

    def record(self, kind: str, *, session: str | None = None, **fields) -> None:
        context = self.context(session)
        row = {"kind": kind, "observed_utc": utc_now(), **context, **fields}
        with self.lock:
            key = (context["task_id"], context["attempt_id"])
            self.records[key].append(row)
            if path := self.journals.get(key):
                payload = (json.dumps(row, ensure_ascii=False, default=str) + "\n").encode()
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    while payload:
                        payload = payload[os.write(descriptor, payload):]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

    def task_records(self, task_id: str, attempt_id: str) -> list[dict]:
        with self.lock:
            return json.loads(json.dumps(self.records[(task_id, attempt_id)], ensure_ascii=False, default=str))

    def register_parlant_call(self, session: str, call_id: str, name: str, arguments: Any) -> None:
        row = {"parlant_call_id": call_id, "name": name, "argument_sha256": argument_digest(name, arguments),
               "batch": self.call_batches.get(call_id, {}).get("batch", "unavailable"),
               "iteration": self.call_batches.get(call_id, {}).get("iteration", "unavailable")}
        with self.lock:
            self.pending_parlant[(session, name)].append(row)
        self.record("parlant_call_started", session=session, **row)

    def claim_parlant_call(self, session: str, name: str, arguments: Any) -> dict:
        digest = argument_digest(name, arguments)
        with self.lock:
            queue = self.pending_parlant.get((session, name), [])
            for index, row in enumerate(queue):
                if row["argument_sha256"] == digest:
                    return queue.pop(index)
        return {"parlant_call_id": "unavailable", "name": name, "argument_sha256": digest,
                "batch": "unavailable", "iteration": "unavailable"}


def install_parlant_hooks(observer: Observer) -> None:
    from parlant.core.engines.alpha.engine import AlphaEngine
    from parlant.core.engines.alpha.message_generator import MessageGenerator
    from parlant.core.engines.alpha.tool_calling.tool_caller import ToolCaller

    original_process = AlphaEngine.process

    async def tracked_process(self, context, event_emitter):
        token = observer.bind_session(str(context.session_id))
        try:
            return await original_process(self, context, event_emitter)
        finally:
            observer.unbind_session(token)

    AlphaEngine.process = tracked_process

    for method_name, label in (("_run_initial_preparation_iteration", "initial"),
                               ("_run_additional_preparation_iteration", "additional")):
        original = getattr(AlphaEngine, method_name)

        async def tracked(self, context, *args, _original=original, _label=label, **kwargs):
            result = await _original(self, context, *args, **kwargs)
            state = result.state
            insights = state.tool_insights
            staged_results = [result_summary(n, d) for n, d in event_tool_results(context.state.tool_events)]
            observer.record(
                "preparation_iteration", session=str(context.session.id),
                trace_id=context.tracer.trace_id, iteration_phase=_label,
                iteration=len(context.state.iterations) + 1,
                matched_rule_ids=[str(m.guideline.id) for m in state.matched_guidelines],
                resolved_rule_ids=[str(m.guideline.id) for m in state.resolved_guidelines],
                tool_candidates=sorted({t.to_string() for ts in context.state.tool_enabled_guideline_matches.values() for t in ts}),
                executed_tools=[t.to_string() for t in state.executed_tools],
                missing_parameters=[{"parameter": x.parameter, "description": x.description} for x in insights.missing_data],
                invalid_parameters=[{"parameter": x.parameter, "description": x.description} for x in insights.invalid_data],
                staged_result_summaries=staged_results,
                new_tool_result_summaries=staged_results[-len(state.executed_tools):] if state.executed_tools else [],
            )
            return result

        setattr(AlphaEngine, method_name, tracked)

    original_infer = ToolCaller.infer_tool_calls

    async def tracked_infer(self, context):
        result = await original_infer(self, context)
        for batch_index, calls in enumerate(result.batches):
            for call in calls:
                observer.call_batches[str(call.id)] = {"batch": batch_index, "iteration": "unavailable"}
        observer.record(
            "tool_inference", session=str(context.session_id),
            tool_candidates=sorted({t.to_string() for ts in context.tool_enabled_guideline_matches.values() for t in ts}),
            matched_tool_rule_ids=[str(m.guideline.id) for m in context.tool_enabled_guideline_matches],
            batches=len(result.batches), batch_call_ids=[[str(c.id) for c in b] for b in result.batches],
            missing_parameters=[{"parameter": x.parameter, "description": x.description} for x in result.insights.missing_data],
            evaluations=[{"tool": t.to_string(), "decision": e.value} for t, e in result.insights.evaluations],
        )
        return result

    ToolCaller.infer_tool_calls = tracked_infer

    original_run_tool = ToolCaller._run_tool

    async def tracked_run_tool(self, context, tool_call, tool_id):
        observer.register_parlant_call(str(context.session_id), str(tool_call.id), tool_id.tool_name, tool_call.arguments)
        return await original_run_tool(self, context, tool_call, tool_id)

    ToolCaller._run_tool = tracked_run_tool

    original_build = MessageGenerator._build_prompt

    def tracked_build(self, *args, **kwargs):
        prompt = original_build(self, *args, **kwargs)
        built = prompt.build() if hasattr(prompt, "build") else str(prompt)
        all_results = event_tool_results(kwargs.get("interaction_history", [])) + event_tool_results(kwargs.get("staged_tool_events", []))
        order_markers = [d.get("order_id") for n, d in all_results if n == "get_order_details" and isinstance(d, dict) and d.get("order_id")]
        product_markers = [d.get("product_id") for n, d in all_results if n == "get_product_details" and isinstance(d, dict) and d.get("product_id")]
        observer.record(
            "response_prompt_coverage", session=str(kwargs["session"].id),
            trace_id=self._tracer.trace_id, prompt_sha256=hashlib.sha256(built.encode()).hexdigest(),
            order_results=len(order_markers), order_ids_present=sum(value in built for value in order_markers),
            product_results=len(product_markers), product_ids_present=sum(value in built for value in product_markers),
            available_field_present="available" in built if product_markers else "unavailable",
            result_summaries=[result_summary(n, d) for n, d in all_results],
        )
        return prompt

    MessageGenerator._build_prompt = tracked_build

    original_response = MessageGenerator.generate_response

    async def tracked_response(self, context, *args, **kwargs):
        started = time.monotonic()
        start_utc = utc_now()
        try:
            result = await original_response(self, context, *args, **kwargs)
        except BaseException as exc:
            observer.record("response_generation", session=str(context.session.id), trace_id=context.tracer.trace_id,
                            start_utc=start_utc, end_utc=utc_now(), seconds=time.monotonic()-started,
                            success=False, error_type=type(exc).__name__)
            raise
        observer.record("response_generation", session=str(context.session.id), trace_id=context.tracer.trace_id,
                        start_utc=start_utc, end_utc=utc_now(), seconds=time.monotonic()-started,
                        success=True, compositions=len(result))
        return result

    MessageGenerator.generate_response = tracked_response
