"""Consume a failed parse once, without native re-generation or fake success."""

from contextvars import ContextVar
from time import perf_counter
import json

from parlant.core.nlp.generation_info import GenerationInfo, UsageInfo
from ..integration import (
    ObservedSingleToolBatch,
    ObservedBatcher,
    ObservedToolCaller,
    AttributedEvaluations,
)
from ..policy import QwenPolicyProvider, TrustedValueLeak, visible_tool_schema
from parlant.core.tools import validate_tool_arguments, ToolExecutionError
from parlant.core.engines.alpha.tool_calling.tool_caller import ToolCallEvaluation
from ..trajectory import FRAME, CANDIDATE, TRUSTED

LAST_REQUEST = ContextVar("research_exact_request", default=None)


TrustedFieldLeak = TrustedValueLeak  # Compatibility name; never inferred from an argument name.


class NativeToolBudget(RuntimeError):
    pass


class BoundedToolCaller(ObservedToolCaller):
    def __init__(self, *args, directory, maximum=18):
        super().__init__(*args)
        self.directory, self.maximum, self.started = directory, maximum, 0

    async def _run_tool(self, context, tool_call, tool_id):
        if self.started >= self.maximum:
            (self.directory / "TOOL_BUDGET_STOP.json").write_text(
                json.dumps(
                    {"started_calls": self.started, "undispatched_call_id": str(tool_call.id)}
                )
            )
            raise NativeToolBudget("No dispatch beyond the frozen native tool budget")
        self.started += 1
        return await super()._run_tool(context, tool_call, tool_id)


def has_trusted_fields(value):
    if isinstance(value, dict):
        if any(str(k).lower() in TRUSTED for k in value):
            return True
        if str(value.get("parameter_name", "")).lower() in TRUSTED:
            return True
        return any(has_trusted_fields(v) for v in value.values())
    return isinstance(value, list) and any(has_trusted_fields(v) for v in value)


class OncePerCandidateProvider(QwenPolicyProvider):
    defer_argument_rejection = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started_candidates = set()

    async def generate(self, prompt, hints={}):
        frame = FRAME.get()
        candidate = CANDIDATE.get()
        key = (frame.episode_id, frame.turn_id, frame.engine_iteration, candidate[0].to_string())
        if key in self.started_candidates:
            raise RuntimeError("Native candidate retry forbidden; no second generation")
        self.started_candidates.add(key)
        return await super().generate(prompt, hints)


class OnceSingleToolBatch(ObservedSingleToolBatch):
    async def _evaluate_consequential_tool_calls(self, inference_output, candidate_descriptor):
        pid = inference_output.policy_step_id
        if self.trajectory.steps[pid]["parse_status"] == "failed":
            from parlant.core.engines.alpha.tool_calling.single_tool_batch import SingleToolBatch

            result = await SingleToolBatch._evaluate_consequential_tool_calls(
                self, inference_output, candidate_descriptor
            )
            if result[0]:
                raise RuntimeError("Failed parse produced a native executable call")
            # TrajectoryLogger.gate correctly requires a successful parse.
            # Preserve that contract and record this separate failure boundary.
            self.trajectory.update(
                pid,
                actual_gate_result={
                    "status": "parse_failed_no_dispatch",
                    "accepted_calls": [],
                    "native_gate_consumed_empty_evaluations": True,
                },
            )
            return result
        # Preserve every original evaluation. In particular the native zero-
        # parameter converter ignores hallucinated arguments: it must not see
        # an invalid proposal and silently execute a stripped version of it.
        tool_id, tool, _ = candidate_descriptor
        visible = visible_tool_schema(tool)["function"]["parameters"]["properties"]
        rejected = []
        remaining = []
        for index, evaluation in enumerate(inference_output):
            arguments = {a.parameter_name: a.value_as_string
                         for a in evaluation.argument_evaluations or []}
            forbidden = sorted(set(arguments) - set(visible))
            if not forbidden:
                remaining.append(evaluation)
                continue
            try:
                validate_tool_arguments(tool, arguments)
            except ToolExecutionError as error:
                rejected.append({"proposal_index": index, "status": "CANNOT_RUN",
                                 "argument_names": forbidden,
                                 "validator": "parlant.core.tools.validate_tool_arguments",
                                 "reason": str(error), "arguments": arguments})
            else:
                # A hidden parameter recognized by native validation needs a
                # separately authorized contract; do not fabricate rejection.
                raise RuntimeError("Native validator did not reject a forbidden proposal")
        if rejected:
            from parlant.core.engines.alpha.tool_calling.single_tool_batch import SingleToolBatch

            # Legal proposals still pass the original converter/validator/gate.
            # The rejected ones retain their proposal indices and original args.
            result = await SingleToolBatch._evaluate_consequential_tool_calls(
                self, AttributedEvaluations(remaining, pid), candidate_descriptor
            )
            result[1].extend((tool_id, ToolCallEvaluation.CANNOT_RUN) for _ in rejected)
            self.trajectory.record_boundary_gate(pid, result, rejected)
            return result
        return await super()._evaluate_consequential_tool_calls(
            inference_output, candidate_descriptor
        )

    async def _run_consequential_tool_inference(self, prompt, tool_id, temperature):
        start = perf_counter()
        try:
            return await super()._run_consequential_tool_inference(prompt, tool_id, temperature)
        except ValueError:
            request = LAST_REQUEST.get()
            if request is None:
                raise
            pid = request["policy_step_id"]
            row = self.trajectory.steps[pid]
            if row["parse_status"] != "failed":
                raise
            if row.get("failure_type") != "PARSE_OR_BOUNDARY_ERROR":
                raise
            backend = self._consequential_schema_generator.backend
            usage = backend.usage_for(pid) if hasattr(backend, "usage_for") else UsageInfo(0, 0)
            # No schema is manufactured. The raw parser failure remains in the
            # journal; the native gate sees zero evaluable actions and dispatches none.
            return GenerationInfo(
                "SingleToolBatchSchema",
                self._consequential_schema_generator.id,
                perf_counter() - start,
                usage,
            ), AttributedEvaluations([], pid)


class OnceBatcher(ObservedBatcher):
    def _create_single_tool_batch(self, candidate_tool, context):
        if not candidate_tool[1].consequential:
            return super()._create_single_tool_batch(candidate_tool, context)
        return OnceSingleToolBatch(
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
