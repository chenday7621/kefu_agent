"""Formal experimental APP binding: no Oracle actions, replies or gold inputs."""

import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from unittest.mock import patch

from .backend import RemoteQwenBackend
from .provider import ReferenceNLP
from .boundary import OncePerCandidateProvider, OnceBatcher
from ..trajectory import FRAME, DecisionFrame

RUNTIME = None


async def configure_container(container):
    from parlant.core.engines.alpha.hooks import EngineHooks, EngineHookResult
    from ..integration import install_container, ObservedToolCaller
    from parlant.core.engines.alpha.tool_calling.tool_caller import ToolCaller, ToolCallBatcher
    from lagom import Singleton

    c = install_container(container, RUNTIME.policy, RUNTIME.logger).clone()
    original = c[ToolCallBatcher]
    batcher = OnceBatcher(original, RUNTIME.logger, RUNTIME.policy)
    c[ToolCallBatcher] = Singleton(lambda container: batcher)
    base = c[ToolCaller]
    c[ToolCaller] = Singleton(lambda container: ObservedToolCaller(base, batcher, RUNTIME.logger))
    hooks = c[EngineHooks]

    async def start(context, payload=None, exception=None):
        # Native processing is serial per session. Reserve whole six-candidate
        # rounds before the concurrent generators are scheduled.
        if RUNTIME.reserved + 6 > RUNTIME.config["max_policy_decisions"]:
            (RUNTIME.directory / "BUDGET_STOP.json").write_text(
                json.dumps({"decisions": len(RUNTIME.logger.steps), "reserved": RUNTIME.reserved})
            )
            return EngineHookResult.BAIL
        RUNTIME.iteration_start_count = len(RUNTIME.logger.steps)
        RUNTIME.reserved += 6
        RUNTIME.iteration += 1
        event = context.interaction.last_customer_message_event
        FRAME.set(
            DecisionFrame(
                RUNTIME.config["episode_id"],
                str(context.session.id),
                str(event.id),
                RUNTIME.iteration,
                tuple(str(e.id) for e in context.interaction.events),
                tuple(RUNTIME.logger.steps),
                tuple(r for s in RUNTIME.logger.steps.values() for r in s["tool_results"]),
            )
        )
        return EngineHookResult.CALL_NEXT

    async def errors(context, payload=None, exception=None):
        RUNTIME.errors.append({"type": type(exception).__name__})
        (RUNTIME.directory / "APP_ERRORS.json").write_text(json.dumps(RUNTIME.errors))
        return EngineHookResult.CALL_NEXT

    async def end(context, payload=None, exception=None):
        # Empty native preparations consume no candidate decisions.
        generated = len(RUNTIME.logger.steps) - RUNTIME.iteration_start_count
        if generated > 6:
            raise RuntimeError("Candidate batch exceeded its atomic reservation")
        RUNTIME.reserved -= 6 - generated
        return EngineHookResult.CALL_NEXT

    c[EngineHooks] = replace(
        hooks,
        on_preparation_iteration_start=[start],
        on_preparation_iteration_end=[end],
        on_error=[errors],
    )
    return c


async def initialize_container(container):
    from parlant.core.sessions import SessionStore, EventKind, EventSource
    from apps.retail_demo.db import connect
    from apps.retail_demo.business import plain
    from ..dev_protocol import confirmation_phrase

    store = container[SessionStore]
    original = store.create_event

    async def observed(*args, **kwargs):
        event = await original(*args, **kwargs)
        if event.kind != EventKind.MESSAGE:
            return event
        text = event.data.get("message", "")
        journal = RUNTIME.logger.journal
        if event.source == EventSource.CUSTOMER:
            journal.append("user_message", event_id=str(event.id), text=text)
            # This state has only user-visible fields, not final gold or DB facts.
            user = json.loads((RUNTIME.directory / "USER_STATE.json").read_text())
            if user.get("script_sets") and user["message"] == text:
                journal.append(
                    "user_update", user_event_id=str(event.id), fields=user["script_sets"]
                )
            if text.startswith("确认退货 "):
                oid = text.split()[1]
                with connect(RUNTIME.config["database_url"]) as conn:
                    row = conn.execute(
                        "SELECT confirmation_event_id FROM session_operations WHERE operation_id=%s",
                        (oid,),
                    ).fetchone()
                if not row or row["confirmation_event_id"] != str(event.id):
                    raise RuntimeError("Native confirmation did not bind the actual user event")
                journal.append(
                    "confirmation_recorded", operation_id=oid, user_event_id=str(event.id)
                )
        elif event.source == EventSource.AI_AGENT:
            # Privileged reads validate already-emitted facts, never select tools
            # or manufacture a phrase that was not in the actual visible answer.
            with connect(RUNTIME.config["database_url"]) as conn:
                ops = plain(
                    conn.execute(
                        "SELECT * FROM return_operations ORDER BY created_at,id"
                    ).fetchall()
                )
            for op in ops:
                phrase = confirmation_phrase(op)
                if phrase in text and op["id"] not in RUNTIME.displayed:
                    fields = (
                        "id",
                        "order_id",
                        "item_id",
                        "quantity",
                        "reason",
                        "quoted_unit_price_cents",
                    )
                    journal.append(
                        "snapshot_displayed",
                        operation_id=op["id"],
                        snapshot={k: op[k] for k in fields},
                        native_event_id=str(event.id),
                    )
                    # Separate controller-only visible phrase record; do not
                    # add unknown fields to the frozen scorer's canonical event.
                    RUNTIME.displayed.add(op["id"])
                    with (RUNTIME.directory / "VISIBLE_PHRASES.jsonl").open("a") as out:
                        out.write(
                            json.dumps(
                                {
                                    "operation_id": op["id"],
                                    "native_event_id": str(event.id),
                                    "phrase": phrase,
                                }
                            )
                            + "\n"
                        )
            user = json.loads((RUNTIME.directory / "USER_STATE.json").read_text())
            script = user.get("next_script")
            if script and script["trigger"] == "clarification_requested":
                cues = {
                    "order_id": ("订单", "哪笔"),
                    "item_id": ("明细", "规格", "哪件"),
                    "quantity": ("数量", "几件"),
                    "reason": ("原因", "为什么"),
                }
                if any(cue in text for cue in cues[script["field"]]):
                    journal.append(
                        "clarification_requested",
                        field=script["field"],
                        text=text,
                        native_event_id=str(event.id),
                    )
        return event

    store.create_event = observed
    RUNTIME.container = container


async def configure_api(api):
    from fastapi import HTTPException
    from parlant.core.application import Application
    from parlant.core.sessions import SessionStore, SessionId

    @api.post("/posttraining/sessions/{sid}/process")
    async def process(sid: str):
        if sid != RUNTIME.config["session_id"]:
            raise HTTPException(403)
        session = await RUNTIME.container[SessionStore].read_session(SessionId(sid))
        await RUNTIME.container[Application].sessions.dispatch_processing_task(session)
        return {"processing_dispatched": True}

    @api.post("/posttraining/sessions/{sid}/finish")
    async def finish(sid: str):
        if sid != RUNTIME.config["session_id"]:
            raise HTTPException(403)
        score = json.loads((RUNTIME.directory / "SCORE.json").read_text())
        if score["task_id"] != RUNTIME.config["episode_id"]:
            raise RuntimeError("Cross-episode terminal score")
        RUNTIME.logger.finish_episode(score["task_id"], "formal_episode_complete", score["reward"])
        return {"terminal_recorded": True}

    return api


async def run(config):
    from types import SimpleNamespace
    from dotenv import dotenv_values
    from transformers import AutoTokenizer
    from apps.retail_demo.settings import Settings
    from apps.retail_demo import nlp
    from apps.retail_demo.postgres_stores import open_stores
    from apps.retail_demo.parlant_app import serve
    from apps.retail_demo.nlp import LocalEmbedder
    from parlant.core.meter import Meter
    import parlant.sdk as p
    from .evidence import ResearchTrajectory

    global RUNTIME
    directory = Path(config["directory"])
    # TASK is read only by the evidence/scoring side, never by either generator.
    audit_task = json.loads((directory / "TASK.json").read_text())
    logger = ResearchTrajectory(directory, config, audit_task)
    tokenizer = AutoTokenizer.from_pretrained(config["model_path"], local_files_only=True)
    backend = RemoteQwenBackend(config["policy_url"], config["policy_capability"])
    policy = OncePerCandidateProvider(tokenizer, logger, backend)
    key = os.environ.get("DEEPSEEK_API_KEY") or dotenv_values(config["repo_env"]).get(
        "DEEPSEEK_API_KEY"
    )
    if not key:
        raise RuntimeError("PROVIDER_BINDING_BLOCKED: DEEPSEEK_API_KEY missing")
    os.environ.update(
        DEEPSEEK_API_KEY=key,
        DEMO_DISABLE_LLM="0",
        PARLANT_SDK_MODULE=__name__,
        DEMO_EMBEDDING_MODEL=config["embedding_path"],
        PARLANT_HOME=str(directory / "parlant"),
    )
    RUNTIME = SimpleNamespace(
        config=config,
        directory=directory,
        logger=logger,
        policy=policy,
        reserved=0,
        iteration=0,
        errors=[],
        displayed=set(),
    )

    def factory(container):
        from .embedding import create_reference_embedder

        container[LocalEmbedder] = create_reference_embedder(
            container[p.Logger],
            container[p.Tracer],
            container[Meter],
            config["embedding_path"],
            config["embedding_runtime_cache"],
        )
        return ReferenceNLP(
            container[p.Logger], container[p.Tracer], container[Meter], policy, config
        )

    settings = Settings(
        config["database_url"],
        config["customer_id"],
        config["mcp_port"],
        config["app_port"],
        config["tool_port"],
        directory / "parlant",
    )
    workers = []
    with patch.object(nlp, "nlp_factory", factory):
        async with open_stores(settings.database_url, exclusive=True) as stores:
            try:
                await serve(settings, False, stores, workers)
            finally:
                for worker in workers:
                    await worker.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    asyncio.run(run(json.loads(parser.parse_args().config.read_text())))
