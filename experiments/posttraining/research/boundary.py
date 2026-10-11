"""Consume a failed parse once, without native re-generation or fake success."""

from contextvars import ContextVar
from time import perf_counter

from parlant.core.nlp.generation_info import GenerationInfo, UsageInfo
from ..integration import ObservedSingleToolBatch, ObservedBatcher, AttributedEvaluations
from ..policy import QwenPolicyProvider
from ..trajectory import FRAME, CANDIDATE

LAST_REQUEST = ContextVar("research_exact_request", default=None)


class OncePerCandidateProvider(QwenPolicyProvider):
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
