"""Experimental process entrance into the unchanged retail APP.

Oracle mode substitutes only experiment NLP services and forbids model/API
loading. The ordinary APP serve(), rules, stores, confirmation and MCP stay
native. Oracle output must never be labelled Qwen baseline or training data.
"""

import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
from unittest.mock import patch

from .dev_protocol import confirmation_phrase

RUNTIME = None


class FixtureEmbedder:
    id = "dry-run-deterministic-embedding"
    dimensions = 384
    max_tokens = 512

    @property
    def tokenizer(self):
        from parlant.core.nlp.tokenization import ZeroEstimatingTokenizer
        return ZeroEstimatingTokenizer()

    async def embed(self, texts, hints=None):
        from parlant.core.nlp.embedding import EmbeddingResult
        return EmbeddingResult(vectors=[[1.0] + [0.0] * 383 for _ in texts])


class OracleGenerator:
    def __init__(self, schema):
        self.schema = schema
        self.id = "dry-run-oracle-other-schema"
        self.max_tokens = 32768

    @property
    def tokenizer(self):
        from parlant.core.nlp.tokenization import ZeroEstimatingTokenizer
        return ZeroEstimatingTokenizer()

    async def generate(self, prompt, hints=None):
        from parlant.core.nlp.generation import SchematicGenerationResult
        from parlant.core.nlp.generation_info import GenerationInfo, UsageInfo
        text = RUNTIME.response()
        name = self.schema.__name__
        values = {
            "CannedResponseDraftSchema": {"last_message_of_user": RUNTIME.state()["message"],
                                         "guidelines": [], "response_body": text},
            "CannedResponseRevisionSchema": {"revised_canned_response": text},
            "CannedResponseSelectionSchema": {"chosen_template_id": None, "match_quality": "none"},
            "FollowUpCannedResponseSelectionSchema": {"additional_response_required": False},
            "CannedResponsePreambleSchema": {"preamble": "正在查询。"},
        }
        if name not in values:
            raise RuntimeError("Unsupported dry-run schema: " + name)
        return SchematicGenerationResult(content=self.schema.model_validate(values[name]),
                                         info=GenerationInfo(name, self.id, 0.0, UsageInfo(0, 0)))


class OracleNLP:
    supports_streaming = False

    async def get_schematic_generator(self, t, hints=None):
        # Lagom registers implementation classes as well as generic interfaces;
        # each schema needs a distinct implementation type.
        implementation = type("Oracle_" + t.__name__, (OracleGenerator,), {})
        return implementation(t)

    async def get_embedder(self, hints=None):
        return FixtureEmbedder()

    async def get_moderation_service(self):
        from parlant.core.nlp.moderation import NoModeration
        return NoModeration()

    async def get_streaming_text_generator(self, hints=None):
        raise RuntimeError("Dry-run streaming disabled")


class OracleBackend:
    origin = "fixture"

    async def complete(self, request):
        from .trajectory import CANDIDATE, FRAME
        tool = CANDIDATE.get()[1]
        frame = FRAME.get()
        state = RUNTIME.state()
        current = [s for s in RUNTIME.logger.steps.values() if s["turn_id"] == frame.turn_id]
        names = {s["candidate_tool_name"] for s in current if s["tool_results"]}
        read_orders = {s["actual_gate_result"]["accepted_calls"][0]["arguments"]["order_id"]
                       for s in RUNTIME.logger.steps.values()
                       if s["candidate_tool_name"] == "get_order_details" and s["tool_results"]
                       and s["tool_results"][0]["result"]["data"].get("ok")}
        goal = state["visible"]
        planned, arguments = None, {}
        ops = RUNTIME.operations()
        matching = [o for o in ops if all(o.get(k) == goal.get(k) for k in ("order_id", "item_id", "quantity", "reason"))
                    and not o["superseded_by"]]
        if RUNTIME.task["family_id"] == "F3":
            if "get_order_details" not in names:
                planned, arguments = "get_order_details", {"order_id": goal["order_id"]}
            elif "get_operation_result" not in names and ops:
                reference = next(o for o in ops if o["id"] in RUNTIME.task["conversation_opening"])
                planned, arguments = "get_operation_result", {"operation_id": reference["id"]}
            else:
                from .budget_protocol import NEGATIVE_PROBES
                probe = NEGATIVE_PROBES.get(RUNTIME.task["task_id"])
                if probe and probe not in names:
                    planned = probe
                    if probe == "submit_confirmed_return":
                        reference = next(o for o in ops if o["id"] in RUNTIME.task["conversation_opening"])
                        arguments = {"operation_id": reference["id"]}
                    else:
                        arguments = {k: goal[k] for k in ("order_id", "item_id", "quantity", "reason")}
        elif goal.get("submission_allowed") is not False and all(k in goal for k in ("order_id", "item_id", "quantity", "reason")):
            if state.get("confirmed_operation_id"):
                if "submit_confirmed_return" not in names:
                    planned, arguments = "submit_confirmed_return", {"operation_id": state["confirmed_operation_id"]}
            elif goal["order_id"] not in read_orders:
                planned, arguments = "get_order_details", {"order_id": goal["order_id"]}
            elif not matching and "check_return_eligibility" not in names:
                planned = "check_return_eligibility"
                arguments = {k: goal[k] for k in ("order_id", "item_id", "quantity", "reason")}
                old = state.get("displayed_operation_id")
                if old:
                    arguments["replaces_operation_id"] = old
        # read_orders is explicit native result evidence, never a
        # guessed last call. Real Qwen receives these via the native prompt.
        if planned == "check_return_eligibility" and not read_orders:
            raise RuntimeError("Oracle preparation without a native read result")
        evaluations = []
        if planned == tool.name:
            evaluations = [{"applicability_rationale": "fixed infrastructure oracle",
                            "is_applicable": True, "same_call_is_already_staged": False,
                            "relevant_subtleties": "synthetic validation only",
                            "argument_evaluations": [
                                {"parameter_name": key,
                                 "acceptable_source_for_this_argument_according_to_its_tool_definition": "history",
                                 "evaluate_is_it_provided_by_an_acceptable_source": "yes",
                                 "evaluate_was_it_already_provided_and_should_it_be_provided_again": "no",
                                 "evaluate_is_it_potentially_problematic_to_guess_what_the_value_is_if_it_isnt_provided": "yes",
                                 "valid_invalid_or_missing": "valid", "value_as_string": str(value)}
                                for key, value in arguments.items()]}]
        return json.dumps({"name": tool.name, "subtleties_to_be_aware_of": "oracle not model",
                           "tool_calls_for_candidate_tool": evaluations}, ensure_ascii=False)


class AppRuntime:
    def __init__(self, config):
        self.config = config
        self.directory = Path(config["directory"])
        self.task = json.loads((self.directory / "TASK.json").read_text())
        from .episode_evidence import CanonicalTrajectory
        self.logger = CanonicalTrajectory(self.directory, config, self.task)
        self.errors = []

    def state(self):
        return json.loads((self.directory / "USER_STATE.json").read_text())

    def operations(self):
        from apps.retail_demo.db import connect
        from apps.retail_demo.business import plain
        with connect(self.config["database_url"]) as c:
            return plain(c.execute("SELECT * FROM return_operations ORDER BY created_at,id").fetchall())

    def response(self):
        state = self.state()
        goal = state["visible"]
        if goal.get("submission_allowed") is False:
            return "已暂停，等待你决定是否继续。"
        missing = next((k for k in ("order_id", "item_id", "quantity", "reason") if k not in goal), None)
        if missing:
            return "请提供" + {"order_id": "订单", "item_id": "明细规格", "quantity": "数量", "reason": "原因"}[missing] + "？"
        if self.task["family_id"] != "F3":
            matching = [o for o in self.operations() if all(o[k] == goal[k] for k in ("order_id", "item_id", "quantity", "reason"))
                        and not o["superseded_by"]]
            if matching:
                op = matching[-1]
                return f"订单{op['order_id']}，明细{op['item_id']}，数量{op['quantity']}，原因{op['reason']}。请复制确认口令：\n```\n{confirmation_phrase(op)}\n```"
        return "已根据真实工具结果完成查询；没有提交新的退货。"


async def configure_container(container):
    from transformers import AutoTokenizer
    from parlant.core.engines.alpha.hooks import EngineHooks, EngineHookResult
    from .integration import install_container
    from .policy import QwenPolicyProvider
    from .trajectory import FRAME, DecisionFrame

    tokenizer = AutoTokenizer.from_pretrained("/mnt/nvme3/chenyi/posttraining/models/Qwen3-8B", local_files_only=True)
    provider = QwenPolicyProvider(tokenizer, RUNTIME.logger, OracleBackend())
    c = install_container(container, provider, RUNTIME.logger)
    c = c.clone()
    hooks = c[EngineHooks]

    async def start(context, payload=None, exception=None):
        # One iteration has six independent, concurrently evaluated APP tools.
        # Admit the whole native batch or none: the controller never changes
        # candidate membership or counts only the applicable outputs.
        budget = RUNTIME.task["episode_budget"]["max_policy_decisions"]
        if len(RUNTIME.logger.steps) + 6 > budget:
            (RUNTIME.directory / "BUDGET_STOP.json").write_text(json.dumps({"reason": "policy_decision_budget", "decisions": len(RUNTIME.logger.steps)}))
            return EngineHookResult.BAIL
        event = context.interaction.last_customer_message_event
        parents = tuple(RUNTIME.logger.steps)
        observations = tuple(r for s in RUNTIME.logger.steps.values() for r in s["tool_results"])
        RUNTIME.iteration_counter = getattr(RUNTIME, "iteration_counter", 0) + 1
        FRAME.set(DecisionFrame(RUNTIME.task["task_id"], str(context.session.id), str(event.id),
                                RUNTIME.iteration_counter,
                                tuple(str(e.id) for e in context.interaction.events), parents, observations))
        return EngineHookResult.CALL_NEXT

    async def errors(context, payload=None, exception=None):
        RUNTIME.errors.append({"type": type(exception).__name__})
        (RUNTIME.directory / "APP_ERRORS.json").write_text(json.dumps(RUNTIME.errors))
        return EngineHookResult.CALL_NEXT

    async def iteration_end(context, payload=None, exception=None):
        with (RUNTIME.directory / "NATIVE_ITERATIONS.jsonl").open("a") as out:
            out.write(json.dumps({"iteration": len(context.state.iterations),
                                  "max_engine_iterations": context.agent.max_engine_iterations,
                                  "prepared_to_respond": context.state.prepared_to_respond,
                                  "executed_tools": [t.to_string() for t in context.state.iterations[-1].executed_tools],
                                  "decisions": len(RUNTIME.logger.steps)}) + "\n")
        return EngineHookResult.CALL_NEXT

    c[EngineHooks] = replace(hooks, on_preparation_iteration_start=[start],
                            on_preparation_iteration_end=[iteration_end], on_error=[errors])
    # The independent process owns the ContextVar lifetime; no mutable APP/core
    # patch or time-near attribution is involved. Parents persist across turns.
    return c


async def initialize_container(container):
    from parlant.core.sessions import SessionStore, EventKind, EventSource
    from apps.retail_demo.db import connect
    store = container[SessionStore]
    original = store.create_event
    journal = RUNTIME.logger.journal
    displayed = set()

    async def observed(*args, **kwargs):
        event = await original(*args, **kwargs)
        if event.kind != EventKind.MESSAGE:
            return event
        text = event.data.get("message", "")
        if event.source == EventSource.CUSTOMER:
            journal.append("user_message", event_id=str(event.id), text=text,
                           native_event_hash=__import__("hashlib").sha256(str(event).encode()).hexdigest())
            state = RUNTIME.state()
            if state.get("script_sets") and text == state["message"]:
                journal.append("user_update", user_event_id=str(event.id), fields=state["script_sets"])
            if text.startswith("确认退货 "):
                oid = text.split()[1]
                with connect(RUNTIME.config["database_url"]) as c:
                    binding = c.execute("SELECT confirmation_event_id FROM session_operations WHERE operation_id=%s", (oid,)).fetchone()
                if not binding or binding["confirmation_event_id"] != str(event.id):
                    raise RuntimeError("Actual confirmation binding missing")
                journal.append("confirmation_recorded", operation_id=oid, user_event_id=str(event.id))
        elif event.source == EventSource.AI_AGENT:
            for op in RUNTIME.operations():
                if op["id"] not in displayed and confirmation_phrase(op) in text:
                    fields = ("id", "order_id", "item_id", "quantity", "reason", "quoted_unit_price_cents")
                    journal.append("snapshot_displayed", operation_id=op["id"],
                                   snapshot={k: op[k] for k in fields}, native_event_id=str(event.id))
                    displayed.add(op["id"])
            state = RUNTIME.state()
            script = state.get("next_script")
            if script and script["trigger"] == "clarification_requested":
                field = script["field"]
                cues = {"order_id": ("订单", "哪笔"), "item_id": ("明细", "规格", "哪件"),
                        "quantity": ("数量", "几件"), "reason": ("原因", "为什么")}
                if any(cue in text for cue in cues[field]):
                    journal.append("clarification_requested", field=field, text=text, native_event_id=str(event.id))
        return event

    store.create_event = observed
    RUNTIME.container = container


async def configure_api(api):
    from fastapi import Request
    from parlant.core.application import Application
    from parlant.core.sessions import SessionId, SessionStore

    @api.post("/posttraining/sessions/{sid}/process")
    async def process(sid: str, request: Request):
        expected = RUNTIME.task["initial_db_fixture"]["trusted_context"]["session_id"]
        if sid != expected:
            from fastapi import HTTPException
            raise HTTPException(status_code=403)
        session = await RUNTIME.container[SessionStore].read_session(SessionId(sid))
        await RUNTIME.container[Application].sessions.dispatch_processing_task(session)
        return {"processing_dispatched": True}

    @api.post("/posttraining/sessions/{sid}/finish")
    async def finish(sid: str):
        expected = RUNTIME.task["initial_db_fixture"]["trusted_context"]["session_id"]
        if sid != expected:
            from fastapi import HTTPException
            raise HTTPException(status_code=403)
        # Append terminal snapshots only from the real controller's persisted
        # frozen-scorer outcome, never invent a task reward in the provider.
        score = json.loads((RUNTIME.directory / "SCORE.json").read_text())
        if score["task_id"] != RUNTIME.task["task_id"]:
            raise RuntimeError("Cross-episode terminal outcome")
        RUNTIME.logger.finish_episode(score["task_id"], "oracle_infrastructure_complete", score["reward"])
        return {"terminal_recorded": True}

    return api


async def run(config):
    global RUNTIME
    RUNTIME = AppRuntime(config)
    os.environ["PARLANT_SDK_MODULE"] = __name__
    os.environ["PARLANT_HOME"] = str(RUNTIME.directory / "parlant")
    os.environ["DEMO_DATABASE_URL"] = config["database_url"]
    os.environ["DEMO_CUSTOMER_ID"] = "demo-alice"  # CLI loader unused for customer binding.
    os.environ["DEMO_DISABLE_LLM"] = "1"
    from contextlib import ExitStack
    from transformers import AutoModelForCausalLM
    import torch
    from apps.retail_demo import nlp
    from apps.retail_demo.settings import Settings
    from apps.retail_demo.postgres_stores import open_stores
    from apps.retail_demo.parlant_app import serve

    def deny(*args, **kwargs):
        raise RuntimeError("Dry-run model/GPU access prohibited")

    original_connect = socket.socket.connect

    def local_only(instance, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError("Dry-run external network prohibited")
        return original_connect(instance, address)

    def factory(container):
        embedder = FixtureEmbedder()
        container[FixtureEmbedder] = embedder
        return OracleNLP()

    customer = RUNTIME.task["initial_db_fixture"]["trusted_context"]["customer_id"]
    settings = Settings(config["database_url"], customer,
                        config["mcp_port"], config["app_port"], config["tool_port"],
                        RUNTIME.directory / "parlant")
    workers = []
    with ExitStack() as stack:
        stack.enter_context(patch.object(AutoModelForCausalLM, "from_pretrained", deny))
        stack.enter_context(patch.object(torch.cuda, "_lazy_init", deny))
        stack.enter_context(patch.object(socket.socket, "connect", local_only))
        stack.enter_context(patch.object(nlp, "nlp_factory", factory))
        async with open_stores(settings.database_url, exclusive=True) as stores:
            try:
                # Explicit /demo/.../confirm remains available with --disable-llm.
                # Normal customer messages are created through the native SDK
                # application module by the experiment's local API bridge.
                await serve(settings, False, stores, workers)
            finally:
                for worker in workers:
                    await worker.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(json.loads(args.config.read_text())))
