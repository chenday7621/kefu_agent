"""Opt-in DI extensions. Native gates and ToolCaller execution remain authoritative."""

from dataclasses import replace
from uuid import uuid4
from lagom import Singleton

from parlant.core.engines.alpha.hooks import EngineHookResult, EngineHooks
from parlant.core.engines.alpha.tool_calling.default_tool_call_batcher import DefaultToolCallBatcher
from parlant.core.engines.alpha.tool_calling.single_tool_batch import SingleToolBatch
from parlant.core.engines.alpha.tool_calling.tool_caller import ToolCallBatcher, ToolCaller
from parlant.core.engines.alpha.tool_calling.single_tool_batch import (
    SingleToolBatchSchema,
    NonConsequentialToolBatchSchema,
)
from parlant.core.engines.alpha.tool_calling.overlapping_tools_batch import (
    OverlappingToolsBatchSchema,
)
from parlant.core.engines.alpha.optimization_policy import OptimizationPolicy
from parlant.core.loggers import Logger
from parlant.core.meter import Meter
from parlant.core.nlp.generation import SchematicGenerator
from parlant.core.relationships import RelationshipStore
from parlant.core.services.tools.service_registry import ServiceRegistry

from .trajectory import CANDIDATE, FRAME, DecisionFrame


class AttributedEvaluations(list):
    def __init__(self, values, policy_step_id):
        super().__init__(values)
        self.policy_step_id = policy_step_id


class ObservedSingleToolBatch(SingleToolBatch):
    def __init__(self, *args, trajectory, **kwargs):
        super().__init__(*args, **kwargs)
        self.trajectory = trajectory

    async def process(self):
        candidate_token = CANDIDATE.set(self._candidate_tool[:2])
        try:
            return await super().process()
        finally:
            CANDIDATE.reset(candidate_token)

    async def _run_consequential_tool_inference(self, prompt, tool_id, temperature):
        # This is the native generation boundary with an explicit result ID attached.
        result = await self._consequential_schema_generator.generate(
            prompt, {"temperature": temperature}
        )
        return result.info, AttributedEvaluations(
            result.content.tool_calls_for_candidate_tool, result.policy_step_id
        )

    async def _evaluate_consequential_tool_calls(self, inference_output, candidate_descriptor):
        if not isinstance(inference_output, AttributedEvaluations):
            raise RuntimeError("Missing explicit generation attribution")
        pid = inference_output.policy_step_id
        try:
            # Native code executes exactly once; no custom duplicate/argument/confirmation logic.
            result = await super()._evaluate_consequential_tool_calls(
                inference_output, candidate_descriptor
            )
        except Exception:
            self.trajectory.update(
                pid, actual_gate_result={"status": "error"}, failure_type="NATIVE_GATE_ERROR"
            )
            raise
        self.trajectory.gate(pid, *result)
        return result


class ObservedBatcher(DefaultToolCallBatcher):
    def __init__(self, original, trajectory, qwen):
        super().__init__(
            logger=original._logger,
            meter=original._meter,
            optimization_policy=original._optimization_policy,
            service_registry=original._service_registry,
            single_tool_schematic_generator=qwen,
            simple_tool_schematic_generator=original._simple_tool_schematic_generator,
            overlapping_tools_schematic_generator=original._overlapping_tools_schematic_generator,
            relationship_store=original._relationship_store,
        )
        self.trajectory = trajectory

    def _create_single_tool_batch(self, candidate_tool, context):
        if not candidate_tool[1].consequential:
            return super()._create_single_tool_batch(candidate_tool, context)
        return ObservedSingleToolBatch(
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


class ObservedToolCaller(ToolCaller):
    def __init__(self, original, batcher, trajectory):
        super().__init__(original._logger, original._meter, original._service_registry, batcher)
        self.trajectory = trajectory

    async def _run_tool(self, context, tool_call, tool_id):
        tracked = str(tool_call.id) in self.trajectory.call_parents
        if tracked:
            self.trajectory.executing(tool_call)
        result = await super()._run_tool(context, tool_call, tool_id)
        if tracked:
            self.trajectory.result(result)
        return result


class IterationTracker:
    def __init__(self, trajectory):
        self.trajectory = trajectory
        self.run_id = uuid4().hex
        self.previous = {}
        self.frame_tokens = {}

    async def start(self, context, payload=None, exception=None):
        if str(context.agent.id) != "retail-persistent-demo":
            raise RuntimeError("This extension is restricted to the APP agent")
        event = context.interaction.last_customer_message_event
        if event is None:
            raise RuntimeError("APP policy requires a real customer turn event")
        session, turn = str(context.session.id), str(event.id)
        episode = f"APP/{self.run_id}/{session}"
        key = (episode, session, turn)
        parents = self.previous.get(key, ())
        observations = tuple(
            r
            for step in self.trajectory.steps.values()
            if step["episode_id"] == episode
            for r in step["tool_results"]
        )
        frame = DecisionFrame(
            episode,
            session,
            turn,
            len(context.state.iterations) + 1,
            tuple(str(e.id) for e in context.interaction.events),
            parents,
            observations,
        )
        self.frame_tokens[key] = FRAME.set(frame)
        return EngineHookResult.CALL_NEXT

    async def end(self, context, payload=None, exception=None):
        frame = FRAME.get()
        if frame is None:
            return EngineHookResult.CALL_NEXT
        key = (frame.episode_id, frame.session_id, frame.turn_id)
        self.previous[key] = tuple(
            pid
            for pid, s in self.trajectory.steps.items()
            if (s["episode_id"], s["session_id"], s["turn_id"], s["engine_iteration"])
            == (*key, frame.engine_iteration)
        )
        token = self.frame_tokens.pop(key, None)
        if token is not None:
            FRAME.reset(token)
        return EngineHookResult.CALL_NEXT

    async def error(self, context, payload=None, exception=None):
        await self.end(context)
        return EngineHookResult.CALL_NEXT


def install_container(container, qwen, trajectory):
    """Call only from the independent APP Server configure_container callback.

    Requires the normal provider generators to be initialized. Factory injection
    avoids resolving them early during the SDK's pre-NLP container configuration.
    """
    # Lagom rejects redefining a type in the same container. Shadow definitions
    # in a child, preserving original stores/services/provider instances.
    container = container.clone()

    def batcher_factory(c):
        base = DefaultToolCallBatcher(
            c[Logger],
            c[Meter],
            c[OptimizationPolicy],
            c[ServiceRegistry],
            c[SchematicGenerator[SingleToolBatchSchema]],
            c[SchematicGenerator[NonConsequentialToolBatchSchema]],
            c[SchematicGenerator[OverlappingToolsBatchSchema]],
            c[RelationshipStore],
        )
        return ObservedBatcher(base, trajectory, qwen)

    def caller_factory(c):
        base = ToolCaller(c[Logger], c[Meter], c[ServiceRegistry], c[ToolCallBatcher])
        return ObservedToolCaller(base, c[ToolCallBatcher], trajectory)

    # initialize_container.try_define preserves this explicit schema binding.
    container[SchematicGenerator[SingleToolBatchSchema]] = qwen
    container[ToolCallBatcher] = Singleton(batcher_factory)
    container[ToolCaller] = Singleton(caller_factory)
    tracker = IterationTracker(trajectory)
    hooks = container[EngineHooks]
    container[EngineHooks] = replace(
        hooks,
        on_preparation_iteration_start=[*hooks.on_preparation_iteration_start, tracker.start],
        on_preparation_iteration_end=[*hooks.on_preparation_iteration_end, tracker.end],
        on_error=[*hooks.on_error, tracker.error],
    )
    return container
