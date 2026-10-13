"""Pure fixtures + local tokenizer only. No model, network, DB, or MCP process."""

import asyncio
import argparse
from contextlib import contextmanager, ExitStack
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lagom import Container
from transformers import AutoConfig, AutoTokenizer, AutoModelForCausalLM
import torch
from parlant.core.engines.alpha.canned_response_generator import CannedResponseDraftSchema
from parlant.core.engines.alpha.hooks import EngineHooks
from parlant.core.engines.alpha.tool_calling.single_tool_batch import (
    SingleToolBatchSchema,
    NonConsequentialToolBatchSchema,
)
from parlant.core.engines.alpha.tool_calling.tool_caller import (
    ToolCaller,
    ToolCallBatcher,
    ToolCallResult,
)
from parlant.core.nlp.generation import SchematicGenerator
from parlant.core.tools import (
    Tool,
    ToolId,
    ToolOverlap,
    ToolParameterOptions,
    ToolContext,
    ToolResult,
    validate_tool_arguments,
)
from parlant.core.services.tools.mcp_service import prepare_tool_arguments
from parlant.core.engines.alpha.optimization_policy import OptimizationPolicy
from parlant.core.loggers import Logger
from parlant.core.meter import Meter
from parlant.core.relationships import RelationshipStore
from parlant.core.services.tools.service_registry import ServiceRegistry
from parlant.core.engines.alpha.tool_calling.overlapping_tools_batch import (
    OverlappingToolsBatchSchema,
)

from .integration import (
    ObservedSingleToolBatch,
    ObservedToolCaller,
    IterationTracker,
    install_container,
)
from .policy import QwenPolicyProvider, SchemaRouter, visible_tool_schema
from .trajectory import FRAME, CANDIDATE, DecisionFrame, TrajectoryLogger, digest

ROOT = Path("/mnt/nvme3/chenyi/posttraining")
REVIEW = ROOT / "runtime/offline-checks"


class StubLogger:
    def __getattr__(self, name):
        if name == "scope":
            return lambda *a, **k: nullscope()
        return lambda *a, **k: None


@contextmanager
def nullscope():
    yield


class FakeBackend:
    origin = "fixture"

    def __init__(self, raw):
        self.raw, self.requests = raw, []

    async def complete(self, request):
        self.requests.append(request)
        await asyncio.sleep(0)  # Force interleaving of concurrent candidates.
        return self.raw


class FrozenProvider:
    supports_streaming = False

    def __init__(self):
        self.calls = []

    async def get_schematic_generator(self, t, hints={}):
        self.calls.append((t, hints))
        return self

    async def get_embedder(self, hints={}):
        return self

    async def get_moderation_service(self):
        return self

    async def get_streaming_text_generator(self, hints={}):
        return self


def tool(name="submit_confirmed_return", extra=False):
    parameters = {"operation_id": ({"type": "string"}, ToolParameterOptions())}
    if extra:
        parameters.update(
            session_token=({"type": "string"}, ToolParameterOptions()),
            customer_id=({"type": "string"}, ToolParameterOptions()),
            internal=({"type": "string"}, ToolParameterOptions(hidden=True)),
        )
    return Tool(
        name=name,
        creation_utc=datetime(2026, 10, 9, tzinfo=timezone.utc),
        metadata={},
        description="Fixture operation reference",
        parameters=parameters,
        required=list(parameters),
        consequential=True,
        overlap=ToolOverlap.NONE,
    )


def output(
    name="submit_confirmed_return",
    applicable=True,
    staged=False,
    status="valid",
    value="op_fixture",
    parameter="operation_id",
):
    arg = dict(
        parameter_name=parameter,
        acceptable_source_for_this_argument_according_to_its_tool_definition="history",
        evaluate_is_it_provided_by_an_acceptable_source="yes",
        evaluate_was_it_already_provided_and_should_it_be_provided_again="no",
        evaluate_is_it_potentially_problematic_to_guess_what_the_value_is_if_it_isnt_provided="yes",
        valid_invalid_or_missing=status,
        value_as_string=value,
    )
    return json.dumps(
        dict(
            name=name,
            subtleties_to_be_aware_of="fixture",
            tool_calls_for_candidate_tool=[
                dict(
                    applicability_rationale="fixture",
                    is_applicable=applicable,
                    argument_evaluations=[arg],
                    same_call_is_already_staged=staged,
                    relevant_subtleties="fixture",
                )
            ],
        )
    )


class OfflineChecks(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokenizer = AutoTokenizer.from_pretrained(
            ROOT / "models/Qwen3-8B", local_files_only=True
        )
        cls.config = AutoConfig.from_pretrained(ROOT / "models/Qwen3-8B", local_files_only=True)

    def setUp(self):
        self.log = TrajectoryLogger(ROOT / "runtime/trajectories/offline-fixtures")
        self.frame = DecisionFrame(
            "APP/fixture/episode", "session_fixture", "turn_fixture", 1, ("history_1",)
        )
        self.frame_token = FRAME.set(self.frame)
        self.tool = tool()
        self.tool_id = ToolId("retail", self.tool.name)
        self.candidate_token = CANDIDATE.set((self.tool_id, self.tool))

    def tearDown(self):
        CANDIDATE.reset(self.candidate_token)
        FRAME.reset(self.frame_token)
        # Keep fixture journals on /mnt for audit, explicitly marked by episode namespace.

    def provider(self, raw):
        return QwenPolicyProvider(self.tokenizer, self.log, FakeBackend(raw))

    def batch(self, provider, candidate=None):
        candidate = candidate or (self.tool_id, self.tool, [])
        return ObservedSingleToolBatch(
            logger=StubLogger(),
            meter=None,
            optimization_policy=None,
            service_registry=None,
            consequential_schema_generator=provider,
            non_consequential_schema_generator=None,
            candidate_tool=candidate,
            context=SimpleNamespace(staged_events=[]),
            trajectory=self.log,
        )

    async def generate_gate(self, raw, candidate=None):
        p = self.provider(raw)
        b = self.batch(p, candidate)
        info, evaluations = await b._run_consequential_tool_inference(
            "Fixture history", self.tool_id, 0
        )
        calls, gates, missing, invalid = await b._evaluate_consequential_tool_calls(
            evaluations, b._candidate_tool
        )
        return evaluations.policy_step_id, calls, p

    async def test_router_exact_type_and_delegation(self):
        frozen, qwen = FrozenProvider(), self.provider(output())
        router = SchemaRouter(frozen, qwen)
        self.assertIs(await router.get_schematic_generator(SingleToolBatchSchema), qwen)
        for schema in (
            CannedResponseDraftSchema,
            NonConsequentialToolBatchSchema,
            OverlappingToolsBatchSchema,
            type("SingleToolBatchSchema", (), {}),
        ):
            self.assertIs(await router.get_schematic_generator(schema), frozen)
        self.assertEqual(len(frozen.calls), 4)
        self.assertIs(await router.get_embedder(), frozen)
        self.assertIs(await router.get_moderation_service(), frozen)
        self.assertIs(await router.get_streaming_text_generator(), frozen)

    async def test_native_parser_gate_and_converter(self):
        pid, calls, p = await self.generate_gate(output())
        self.assertEqual(len(calls), 1)
        validate_tool_arguments(self.tool, calls[0].arguments)
        self.assertEqual(
            prepare_tool_arguments(calls[0].arguments, self.tool.parameters),
            {"operation_id": "op_fixture"},
        )
        self.assertEqual(self.log.steps[pid]["actual_gate_result"]["status"], "accepted")
        self.assertEqual(self.log.steps[pid]["actual_executed_calls"], [])
        self.assertIsInstance(
            SingleToolBatchSchema.model_validate(self.log.steps[pid]["parsed_schema_output"]),
            SingleToolBatchSchema,
        )

    async def test_invalid_json_and_schema(self):
        for raw in ("bad json", '{"name":"only"}'):
            p = self.provider(raw)
            with self.assertRaises(Exception):
                await p.generate("fixture")
            step = list(self.log.steps.values())[-1]
            self.assertEqual(step["parse_status"], "failed")
            self.assertIsNone(step["actual_gate_result"])

    async def test_no_call_and_empty_output(self):
        for raw in (
            output(applicable=False),
            json.dumps(
                dict(
                    name=self.tool.name,
                    subtleties_to_be_aware_of="",
                    tool_calls_for_candidate_tool=[],
                )
            ),
        ):
            pid, calls, _ = await self.generate_gate(raw)
            self.assertEqual(calls, [])
            self.assertEqual(self.log.steps[pid]["proposed_action"], "NO_CALL")
            self.assertEqual(self.log.steps[pid]["actual_gate_result"]["status"], "no_call")

    async def test_duplicate_gate_separate_from_parse(self):
        pid, calls, _ = await self.generate_gate(output(staged=True))
        self.assertEqual(calls, [])
        self.assertEqual(self.log.steps[pid]["parse_status"], "passed")
        self.assertEqual(self.log.steps[pid]["proposed_action"], "CALL")
        self.assertEqual(self.log.steps[pid]["actual_gate_result"]["status"], "rejected")

    async def test_missing_and_invalid_arguments_rejected(self):
        pid, calls, _ = await self.generate_gate(output(status="missing", value=None))
        self.assertFalse(calls)
        self.assertEqual(self.log.steps[pid]["actual_gate_result"]["missing"], ["operation_id"])
        enum_tool = replace(
            self.tool,
            parameters={
                "operation_id": ({"type": "string", "enum": ["allowed"]}, ToolParameterOptions())
            },
        )
        pid, calls, _ = await self.generate_gate(
            output(value="forbidden"), (self.tool_id, enum_tool, [])
        )
        self.assertFalse(calls)
        self.assertEqual(self.log.steps[pid]["actual_gate_result"]["invalid"], ["operation_id"])

    async def test_trusted_fields_hidden_and_rejected(self):
        visible = visible_tool_schema(tool(extra=True))
        for field in ("session_token", "customer_id", "internal"):
            self.assertNotIn(field, visible["function"]["parameters"]["properties"])
            self.assertNotIn(field, visible["function"]["parameters"]["required"])
        for field in ("session_token", "customer_id", "unknown"):
            with self.assertRaises(ValueError):
                await self.provider(output(parameter=field, value="fixture_sensitive")).generate(
                    "fixture"
                )
        for field in ("session_token", "customer_id"):
            self.assertNotIn(
                "fixture_sensitive",
                "\n".join(line for line in self.log.path.read_text().splitlines() if field in line),
            )
        raw = json.loads(output())
        raw["customer_id"] = "fixture_trusted"
        with self.assertRaises(ValueError):
            await self.provider(json.dumps(raw)).generate("fixture")
        self.assertNotIn("fixture_trusted", self.log.path.read_text())

    async def test_real_app_tool_schemas_offline(self):
        from apps.retail_demo.mcp_server import make_server
        from apps.retail_demo.session_tools import SessionBoundMCP
        from parlant.core.services.tools.mcp_service import mcp_tool_to_parlant_tool
        from mcp.types import Tool as McpTool

        # Construct tool definitions only; no transport, DB, settings load or function call.
        mcp = make_server(SimpleNamespace())
        native_tools = await mcp.list_tools()
        names = set()
        for definition in native_tools:
            names.add(definition.name)
            wire = McpTool(
                name=definition.name,
                description=definition.description,
                inputSchema=definition.parameters,
            )
            native = mcp_tool_to_parlant_tool(wire)
            candidate = SessionBoundMCP.visible(None, native)
            schema = visible_tool_schema(candidate)
            self.assertNotIn("session_token", schema["function"]["parameters"]["properties"])
            self.assertNotIn("customer_id", schema["function"]["parameters"]["properties"])
            if native.name == "submit_confirmed_return":
                self.assertEqual(
                    set(schema["function"]["parameters"]["properties"]), {"operation_id"}
                )
        self.assertEqual(
            names,
            {
                "list_my_orders",
                "get_order_details",
                "check_return_eligibility",
                "submit_confirmed_return",
                "get_return_request",
                "get_operation_result",
            },
        )

    async def test_prompt_official_template_and_hash(self):
        p = self.provider(output())
        result = await p.generate(
            'Observed {"session_token":"fixture_secret", "customer_id":"fixture_customer"}'
        )
        request = p.backend.requests[0]
        step = self.log.steps[result.policy_step_id]
        self.assertIn("<|im_start|>", request["rendered_prompt"])
        self.assertIn("# Tools", request["rendered_prompt"])
        self.assertIn("submit_confirmed_return", request["rendered_prompt"])
        self.assertNotIn("fixture_secret", request["rendered_prompt"])
        self.assertNotIn("fixture_customer", request["rendered_prompt"])
        self.assertEqual(step["prompt_hash"], digest(request["rendered_prompt"]))
        self.assertEqual(self.config.model_type, "qwen3")

    async def test_disabled_backend_and_missing_context(self):
        qwen = QwenPolicyProvider(self.tokenizer, self.log)
        with self.assertRaises(RuntimeError):
            await qwen.generate("fixture")
        self.assertEqual(list(self.log.steps.values())[-1]["failure_type"], "GENERATION_DISABLED")
        token = FRAME.set(None)
        try:
            with self.assertRaises(RuntimeError):
                await self.provider(output()).generate("fixture")
        finally:
            FRAME.reset(token)

    async def test_exact_chain_and_terminal(self):
        pid, calls, _ = await self.generate_gate(output())
        self.log.executing(calls[0])
        result = ToolCallResult(
            "result_fixture", calls[0], {"data": {"ok": True, "session_token": "secret_fixture"}}
        )
        self.log.result(result)
        token = FRAME.set(
            replace(
                self.frame,
                engine_iteration=2,
                parent_ids=(pid,),
                observations=tuple(self.log.steps[pid]["tool_results"]),
            )
        )
        try:
            next_pid, _, _ = await self.generate_gate(output(applicable=False))
        finally:
            FRAME.reset(token)
        self.assertEqual(self.log.call_parents[str(calls[0].id)], pid)
        self.assertEqual(self.log.result_parents["result_fixture"], pid)
        self.assertEqual(self.log.steps[pid]["next_policy_step_id"], next_pid)
        self.assertEqual(self.log.steps[next_pid]["parent_policy_step_id"], pid)
        self.assertEqual(
            self.log.steps[next_pid]["relevant_tool_observations"][0]["tool_result_id"],
            "result_fixture",
        )
        self.log.finish_episode(self.frame.episode_id, "fixture_terminal", 1.0)
        self.assertEqual(self.log.steps[pid]["final_reward"], 1.0)
        self.assertNotIn("secret_fixture", self.log.path.read_text())

    async def test_parallel_candidates_never_cross(self):
        async def candidate(name):
            t = tool(name)
            tid = ToolId("retail", name)
            token = CANDIDATE.set((tid, t))
            try:
                return await self.generate_gate(output(name=name), (tid, t, []))
            finally:
                CANDIDATE.reset(token)

        decisions = await asyncio.gather(candidate("candidate_a"), candidate("candidate_b"))
        self.assertNotEqual(decisions[0][0], decisions[1][0])
        for pid, calls, _ in decisions:
            self.log.executing(calls[0])
            self.log.result(ToolCallResult("r_" + pid, calls[0], {"data": pid}))
            self.assertEqual(self.log.steps[pid]["actual_tool_name"], calls[0].tool_id.to_string())
            self.assertEqual(self.log.result_parents["r_" + pid], pid)
        wrong = replace(decisions[0][1][0], tool_id=decisions[1][1][0].tool_id)
        p = await self.provider(output()).generate("fixture")
        with self.assertRaises(ValueError):
            self.log.gate(p.policy_step_id, [wrong], [], [], [])

    async def test_multiple_calls_have_unique_result_edges(self):
        raw = json.loads(output())
        raw["tool_calls_for_candidate_tool"].append(
            json.loads(output(value="op_second"))["tool_calls_for_candidate_tool"][0]
        )
        pid, calls, _ = await self.generate_gate(json.dumps(raw))
        self.assertEqual(len(calls), 2)
        for index, call in enumerate(calls):
            self.log.executing(call)
            self.log.result(ToolCallResult(f"multi_{index}", call, {"data": index}))
        self.assertEqual(len(self.log.steps[pid]["tool_results"]), 2)
        self.assertIsNone(self.log.steps[pid]["actual_executed_tool_call_id"])
        self.assertIsNone(self.log.steps[pid]["tool_result_id"])
        with self.assertRaises(ValueError):
            self.log.result(ToolCallResult("duplicate", calls[0], {}))

    async def test_tool_execution_uses_original_caller_only(self):
        pid, calls, p = await self.generate_gate(output())
        invoked = []

        class FixtureService:
            async def call_tool(service_self, name, context, arguments):
                invoked.append((name, arguments))
                validate_tool_arguments(self.tool, arguments)
                converted = prepare_tool_arguments(arguments, self.tool.parameters)
                return ToolResult(data={"fixture": converted})

        class FixtureRegistry:
            async def read_tool_service(registry_self, name):
                return FixtureService()

        native = ToolCaller(StubLogger(), None, FixtureRegistry(), None)
        observed = ObservedToolCaller(native, None, self.log)
        results = await observed.execute_tool_calls(
            ToolContext("agent_fixture", "session_fixture", "customer_fixture"), calls
        )
        self.assertEqual(len(invoked), 1)
        self.assertEqual(self.log.steps[pid]["tool_result_id"], str(results[0].id))
        self.assertFalse(hasattr(p, "call_tool"))
        self.assertFalse(hasattr(p, "_service_registry"))

    async def test_iteration_hook_provides_explicit_parents(self):
        tracker = IterationTracker(self.log)
        tracker.run_id = "fixture/hooks"
        event = SimpleNamespace(id="turn_hook")
        ctx = SimpleNamespace(
            agent=SimpleNamespace(id="retail-persistent-demo"),
            session=SimpleNamespace(id="session_hook"),
            interaction=SimpleNamespace(last_customer_message_event=event, events=[event]),
            state=SimpleNamespace(iterations=[]),
        )
        await tracker.start(ctx)
        pid, _, _ = await self.generate_gate(output(applicable=False))
        await tracker.end(ctx)
        ctx.state.iterations.append("fixture_iteration")
        await tracker.start(ctx)
        next_pid, _, _ = await self.generate_gate(output(applicable=False))
        await tracker.end(ctx)
        self.assertEqual(self.log.steps[next_pid]["parent_policy_step_id"], pid)
        self.assertEqual(self.log.steps[next_pid]["engine_iteration"], 2)

    async def test_container_extensions_construct_offline(self):
        c = Container()
        for dependency in (Logger, Meter, OptimizationPolicy, ServiceRegistry, RelationshipStore):
            c[dependency] = SimpleNamespace()
        for schema in (
            SingleToolBatchSchema,
            NonConsequentialToolBatchSchema,
            OverlappingToolsBatchSchema,
        ):
            c[SchematicGenerator[schema]] = SimpleNamespace()
        c[EngineHooks] = EngineHooks()
        qwen = self.provider(output())
        original = c
        c = install_container(c, qwen, self.log)
        self.assertIs(c[SchematicGenerator[SingleToolBatchSchema]], qwen)
        self.assertIsNot(original[SchematicGenerator[SingleToolBatchSchema]], qwen)
        self.assertIs(c[ToolCallBatcher]._single_tool_schematic_generator, qwen)
        self.assertIsInstance(c[ToolCaller], ObservedToolCaller)
        self.assertIs(c[ToolCaller], c[ToolCaller])
        self.assertEqual(len(c[EngineHooks].on_preparation_iteration_start), 1)


def main():
    REVIEW.mkdir(parents=True, exist_ok=True)
    # Prohibit network even accidentally; local HF config/tokenizer are the only assets opened.
    with ExitStack() as guards:
        guards.enter_context(
            patch.object(
                socket.socket,
                "connect",
                side_effect=AssertionError("Offline checks prohibit networking"),
            )
        )
        for method in ("from_pretrained", "from_config"):
            guards.enter_context(
                patch.object(
                    AutoModelForCausalLM,
                    method,
                    side_effect=AssertionError("Offline checks prohibit model construction"),
                )
            )
        guards.enter_context(
            patch.object(
                torch.cuda,
                "_lazy_init",
                side_effect=AssertionError("Offline checks prohibit GPU initialization"),
            )
        )
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(OfflineChecks)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    assert not torch.cuda.is_initialized()
    output_json = {
        "tests_run": result.testsRun,
        "passed": result.wasSuccessful(),
        "failures": [str(test) for test, _ in result.failures],
        "errors": [str(test) for test, _ in result.errors],
        "qwen_inference": 0,
        "deepseek_api_calls": 0,
        "benchmark": 0,
        "training": 0,
        "sft_rl": 0,
        "network_blocked": True,
        "fixture_only": True,
        "gpu_model_loads": 0,
        "model_construction_blocked": True,
        "cuda_initialization_blocked": True,
    }
    (REVIEW / "OFFLINE_TEST_RESULTS.json").write_text(json.dumps(output_json, indent=2))
    if not result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--review", type=Path, default=REVIEW)
    REVIEW = parser.parse_args().review
    main()
