"""One synthetic Qwen policy generation; native gate only, dispatch forbidden.

Run with the locked Conda prefix, offline HF variables, and one idle GPU exposed.
No APP server, service registry, database connection or MCP transport is created.
"""

import argparse
import asyncio
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
from types import SimpleNamespace
from unittest.mock import patch

import jsonschema
import torch
from transformers import AutoTokenizer

from apps.retail_demo.mcp_server import make_server
from apps.retail_demo.session_tools import SessionBoundMCP
from mcp.types import Tool as MCPTool
from parlant.core.engines.alpha.tool_calling.single_tool_batch import SingleToolBatch, SingleToolBatchSchema
from parlant.core.engines.alpha.tool_calling.tool_caller import ToolCaller
from parlant.core.services.tools.mcp_service import (
    MCPToolClient, mcp_tool_to_parlant_tool, prepare_tool_arguments,
)
from parlant.core.tools import ToolId, validate_tool_arguments

from .integration import ObservedSingleToolBatch
from .local_qwen import LocalQwenSmokeBackend
from .policy import MODEL_NAME, MODEL_REVISION, QwenPolicyProvider, visible_tool_schema
from .trajectory import CANDIDATE, FRAME, TRUSTED, DecisionFrame, TrajectoryLogger, digest

MODEL_PATH = Path("/mnt/nvme3/chenyi/posttraining/models/Qwen3-8B")
PHASE1A = Path("_reviews/20261009_232007_POSTTRAIN_PHASE1A_SETUP")


@contextmanager
def nullscope():
    yield


class QuietLogger:
    def __getattr__(self, name):
        if name == "scope":
            return lambda *a, **k: nullscope()
        return lambda *a, **k: None


def assert_model_lock(review):
    lock = json.loads((PHASE1A / "QWEN_MODEL_LOCK.json").read_text())
    checked = json.loads((review / "MODEL_RECHECK.json").read_text())
    assert lock["repo_id"] == MODEL_NAME and lock["revision"] == MODEL_REVISION
    assert checked["passed"] and checked["revision"] == MODEL_REVISION
    # The full hash precheck is required immediately before this run; catch any intervening size/mtime change.
    original = json.loads((PHASE1A / "QWEN_MODEL_HASHES.json").read_text())
    for name, expected in original.items():
        assert (MODEL_PATH / name).stat().st_size == expected["size_bytes"]
        assert checked["files"][name]["sha256"] == expected["sha256"]


async def candidate_fixture():
    # Define the real APP tool signatures locally; do not start or call the MCP server.
    definitions = await make_server(SimpleNamespace()).list_tools()
    definition = next(t for t in definitions if t.name == "submit_confirmed_return")
    native = mcp_tool_to_parlant_tool(MCPTool(
        name=definition.name, description=definition.description, inputSchema=definition.parameters,
    ))
    tool = SessionBoundMCP.visible(None, native)
    assert tool.consequential
    return ToolId("retail", tool.name), tool


def fixture_prompt(no_call=False):
    # Single frozen fixture, no answer example, no adaptive prompt changes.
    fixture = {
        "fixture_namespace": "APP/fixture/phase1b",
        "bound_candidate": "retail:submit_confirmed_return",
        "conversation": [
            {"id": "fixture_event_1", "role": "assistant", "content": "The immutable return snapshot for operation op_smoke_001 has been displayed. Do you confirm this operation?"},
            {"id": "fixture_event_2", "role": "user", "content": "Yes, I confirm the displayed return operation op_smoke_001. Please submit it."},
        ],
        "tool_observation": {
            "id": "fixture_observation_1", "tool_name": "check_return_eligibility",
            "result": {"ok": True, "operation_id": "op_smoke_001", "eligible": True, "snapshot_displayed": True},
            "origin": "synthetic_fixture_not_executed",
        },
        "staged_calls": [],
        "instructions": "Evaluate only the bound candidate. The operation reference is provided by the displayed synthetic snapshot and explicitly confirmed by the user. Return the SingleToolBatchSchema JSON specified by the system message. For each proposed argument include all required evaluation fields and valid_invalid_or_missing. same_call_is_already_staged is false when no matching staged call exists. If it should not run, mark is_applicable false or return an empty tool_calls_for_candidate_tool list. Never execute a tool.",
    }
    if no_call:
        fixture["conversation"][1]["content"] = "Do not submit this return. I have not confirmed operation op_smoke_001."
        fixture["instructions"] = "Evaluate only the bound candidate. The user explicitly refuses confirmation of the displayed synthetic snapshot. Return the SingleToolBatchSchema JSON specified by the system message. If submission is not applicable, mark is_applicable false or return an empty tool_calls_for_candidate_tool list. Never execute a tool."
    return json.dumps(fixture, ensure_ascii=False)


async def run(review, no_call=False, require_call=False):
    review = review.resolve()
    runtime = Path("/nas/chenyi/posttraining-qwen/runtime") / review.name
    if review.name == "NO_CALL_SMOKE":
        runtime = runtime.parent / review.parent.name / review.name
    runtime.mkdir(parents=True, exist_ok=True)

    def save(name, data):
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        # Small review artifacts are also kept beside the runtime evidence, not weights/env copies.
        (runtime / name).write_text(text)
        (review / name).write_text(text)

    assert_model_lock(review)
    config = {
        "model_name": MODEL_NAME, "revision": MODEL_REVISION, "model_path": str(MODEL_PATH),
        "dtype": "bfloat16", "device_map": {"": "cuda:0"}, "eval": True,
        "inference_mode": True, "enable_thinking": False, "do_sample": False,
        "num_beams": 1, "max_new_tokens": 1024, "seed": 20261009,
        "local_files_only": True, "trust_remote_code": False,
        "attn_implementation": "sdpa", "max_generations": 1,
        "dispatch_enabled": False, "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "fixture_prompt_hash": digest(fixture_prompt(no_call)), "runtime": str(runtime),
        "fixture_kind": "no_call" if no_call else "confirmed_proposal",
        "use_model_defaults": False,
        "frozen_rendered_prompt_hash": digest(json.loads((review / "FROZEN_PROMPT.json").read_text())["rendered_prompt"]),
    }
    save("QWEN_SMOKE_CONFIG.json", config)
    assert os.environ.get("HF_HUB_OFFLINE") == "1"
    assert os.environ.get("TRANSFORMERS_OFFLINE") == "1"
    assert os.environ.get("DEMO_DISABLE_LLM") == "1"
    assert config["cuda_visible_devices"].isdigit(), "Expose exactly one physical GPU"
    # An exclusive durable marker prevents an accidental second smoke invocation.
    marker = os.open(runtime / "SMOKE_STARTED", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(marker)
    result = {
        "status": "BLOCKED", "reason": None, "generation_origin": "local_qwen",
        "qwen_generations": 0, "deepseek_calls": 0, "benchmark": 0, "training": 0,
        "sft_rl": 0, "real_mcp_execution": 0, "postgresql_business_writes": 0,
        "actual_executed_calls": [], "task_reward": None,
    }
    logger = TrajectoryLogger(runtime / "trajectories")
    backend = None
    forbidden_attempts = []

    def denied(*a, **k):
        forbidden_attempts.append("network_or_tool_dispatch")
        raise RuntimeError("Phase 1-B prohibits network and business tool dispatch")

    try:
        with ExitStack() as guards:
            guards.enter_context(patch.object(socket.socket, "connect", denied))
            guards.enter_context(patch.object(socket.socket, "connect_ex", denied))
            guards.enter_context(patch.object(socket, "create_connection", denied))
            dispatch = guards.enter_context(patch.object(ToolCaller, "_run_tool", side_effect=denied))
            mcp_dispatch = guards.enter_context(patch.object(MCPToolClient, "call_tool", side_effect=denied))
            import psycopg
            pg = guards.enter_context(patch.object(psycopg, "connect", side_effect=denied))
            tool_id, tool = await candidate_fixture()
            visible = visible_tool_schema(tool)
            assert set(visible["function"]["parameters"]["properties"]) == {"operation_id"}
            tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, local_files_only=True, trust_remote_code=False)
            schema_text = json.dumps(SingleToolBatchSchema.model_json_schema())
            assert all(n not in schema_text for n in TRUSTED)
            assert all(n not in json.dumps(visible) for n in TRUSTED)
            frame = DecisionFrame(
                "APP/fixture/phase1b-no-call" if no_call else "APP/fixture/phase1b", "session_fixture_phase1b", "turn_fixture_phase1b_no_call" if no_call else "turn_fixture_phase1b", 1,
                ("fixture_event_1", "fixture_event_2"), (),
                ({"id": "fixture_observation_1", "operation_id": "op_smoke_001", "origin": "synthetic_fixture_not_executed"},),
            )
            backend = LocalQwenSmokeBackend(MODEL_PATH, tokenizer, config, save)
            provider = QwenPolicyProvider(tokenizer, logger, backend)
            assert not any(hasattr(provider, n) for n in ("call_tool", "mcp", "service_registry"))
            batch = ObservedSingleToolBatch(
                logger=QuietLogger(), meter=None, optimization_policy=None, service_registry=None,
                consequential_schema_generator=provider, non_consequential_schema_generator=None,
                candidate_tool=(tool_id, tool, []), context=SimpleNamespace(staged_events=[]), trajectory=logger,
            )
            native_gate = SingleToolBatch._evaluate_consequential_tool_calls
            gate_invocations = []

            async def counted_native_gate(instance, evaluations, candidate):
                gate_invocations.append(evaluations.policy_step_id)
                return await native_gate(instance, evaluations, candidate)

            ftoken, ctoken = FRAME.set(frame), CANDIDATE.set((tool_id, tool))
            try:
                # Call the generation boundary once, avoiding the native process() retry loop.
                info, evaluations = await batch._run_consequential_tool_inference(fixture_prompt(no_call), tool_id, 0)
                pid = evaluations.policy_step_id
                with patch.object(SingleToolBatch, "_evaluate_consequential_tool_calls", counted_native_gate):
                    calls, gates, missing, invalid = await batch._evaluate_consequential_tool_calls(evaluations, batch._candidate_tool)
                converted = []
                for call in calls:
                    assert logger.call_parents[str(call.id)] == pid
                    validate_tool_arguments(tool, call.arguments)
                    arguments = prepare_tool_arguments(call.arguments, tool.parameters)
                    validate_tool_arguments(tool, arguments)
                    converted.append({"tool_call_id": str(call.id), "arguments": arguments})
                assert gate_invocations == [pid]
                assert not forbidden_attempts and dispatch.call_count == mcp_dispatch.call_count == pg.call_count == 0
                step = logger.steps[pid]
                assert step["parse_status"] == "passed" and step["generation_origin"] == "local_qwen"
                assert step["actual_gate_result"]["status"] in ("accepted", "no_call")
                if no_call:
                    assert step["proposed_action"] == "NO_CALL" and not calls
                if require_call:
                    assert len(calls) == 1 and step["proposed_action"] == "CALL"
                    assert calls[0].arguments == {"operation_id": "op_smoke_001"}
                assert step["actual_executed_calls"] == [] and step["tool_results"] == []
                assert step["prompt_hash"] == digest(step["rendered_prompt"])
                assert all(n not in step["rendered_prompt"] for n in TRUSTED)
                assert all(n not in step["raw_model_output"] for n in TRUSTED)
                assert "<think>\n\n</think>" in step["rendered_prompt"]
                assert "<think>" not in step["raw_model_output"]
                logger.update(pid, terminal_status="SMOKE_WOULD_EXECUTE" if calls else "SMOKE_NO_CALL", final_reward=None)
                records = [json.loads(line) for line in logger.path.read_text().splitlines()]
                schema = json.loads((review / "LOGGER_SCHEMA.json").read_text())
                for i, record in enumerate(records, 1):
                    jsonschema.validate(record, schema)
                    assert record["policy_step_id"] == pid and record["record_sequence"] == i
                result.update(
                    status="READY_FOR_QWEN_DEV_PILOT", policy_step_id=pid,
                    prompt_hash=step["prompt_hash"], parse_status=step["parse_status"],
                    parsed_schema_output=step["parsed_schema_output"], proposed_calls=step["proposed_calls"],
                    gate=step["actual_gate_result"], converter_validator_outputs=converted,
                    native_gate_invocations=gate_invocations, native_gate_module=native_gate.__module__,
                    dispatch_attempts=0, logger_validated_records=len(records),
                    terminal_status=logger.steps[pid]["terminal_status"],
                    provider_usage={"input_tokens": info.usage.input_tokens, "output_tokens": info.usage.output_tokens},
                    thinking_disabled_verified=True, trusted_fields_absent=True,
                    native_dispatch_calls=dispatch.call_count, mcp_dispatch_calls=mcp_dispatch.call_count, pg_connect_calls=pg.call_count,
                )
            finally:
                CANDIDATE.reset(ctoken)
                FRAME.reset(ftoken)
    except Exception as exc:
        result["reason"] = type(exc).__name__ + ": " + str(exc)
        if logger.steps:
            pid, step = next(iter(logger.steps.items()))
            result.update(policy_step_id=pid, parse_status=step["parse_status"], parsed_schema_output=step["parsed_schema_output"], gate=step["actual_gate_result"])
        # No retries, no fallback model/provider, and no prompt changes after failure.
    finally:
        result["forbidden_attempts"] = forbidden_attempts
        if backend is not None:
            result["qwen_generations"] = backend.calls
            result["generation"] = backend.records
            if torch.cuda.is_initialized():
                backend.memory.update(peak_allocated_bytes=torch.cuda.max_memory_allocated(), peak_reserved_bytes=torch.cuda.max_memory_reserved())
                save("MEMORY_PROFILE.json", backend.memory)
        if logger.path.exists():
            shutil.copyfile(logger.path, runtime / "POLICY_STEP.jsonl")
            shutil.copyfile(logger.path, review / "POLICY_STEP.jsonl")
        else:
            save("POLICY_STEP.jsonl", "")
        for name, default in (("QWEN_RAW_OUTPUT.txt", ""), ("MEMORY_PROFILE.json", {"model_loaded": False})):
            if not (review / name).exists():
                save(name, default)
        save("QWEN_SMOKE_RESULT.json", result)
        print(json.dumps({"status": result["status"], "reason": result["reason"], "qwen_generations": result["qwen_generations"]}))
    return result["status"] == "READY_FOR_QWEN_DEV_PILOT"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--no-call", action="store_true")
    parser.add_argument("--require-call", action="store_true")
    args = parser.parse_args()
    if args.no_call and args.require_call:
        parser.error("Choose either NO_CALL or required CALL")
    if not asyncio.run(run(args.review, args.no_call, args.require_call)):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
