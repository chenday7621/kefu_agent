"""Targeted real PG/MCP/native ToolCaller and receipt tests; zero model requests."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from apps.retail_demo.business import RetailService
from apps.retail_demo.db import connect
from apps.retail_demo.outbox import Outbox
from apps.retail_demo.postgres_stores import open_stores
from apps.retail_demo.session_tools import SessionBoundMCP
from parlant.core.engines.alpha.tool_calling.tool_caller import ToolCaller, ToolCall, ToolCallId
from parlant.core.loggers import StdoutLogger, LogLevel
from parlant.core.services.tools.mcp_service import MCPToolClient
from parlant.core.sessions import SessionId, EventKind, EventSource
from parlant.core.tools import ToolContext, ToolId
from parlant.core.tracer import LocalTracer

from ..dev_controller import Services, free_port, save
from ..episode_state import EpisodeDatabase
from .resources import ResearchResources


def logical_fixture():
    # Independent contract fixture. No DEV task, oracle answer or model is used.
    cid, oid, item = "CONTRACT-OWNER", "CONTRACT-ORDER", "CONTRACT-LINE"
    return {
        "task_id": "CONTRACT-REGRESSION",
        "initial_db_fixture": {
            "trusted_context": {"customer_id": cid, "session_id": "CONTRACT-SESSION"},
            "trusted_history": [],
            "tables": {
                "customers": [{"id": cid, "name": "错误契约验证用户"}],
                "orders": [{"id": oid, "customer_id": cid, "status": "delivered", "currency": "CNY",
                            "created_at": {"seconds_from_episode_start": -172800},
                            "delivered_at": {"seconds_from_episode_start": -86400}}],
                "order_items": [{"id": item, "order_id": oid, "product_name": "契约测试物品",
                                 "variant": "单一规格", "quantity": 2, "reserved_return_quantity": 0,
                                 "unit_price_cents": 1200, "returnable": True}],
                "return_operations": [], "return_requests": [], "session_operations": [],
                "session_current_operations": [], "operation_logs": [], "return_outbox": [],
            },
        },
    }


async def verify(db):
    directory, task = db.directory, db.task
    sid = SessionId(task["initial_db_fixture"]["trusted_context"]["session_id"])
    cid = task["initial_db_fixture"]["trusted_context"]["customer_id"]
    config = {"directory": str(directory), "database_url": db.url, "mcp_port": free_port()}
    save(directory / "TASK.json", task)
    save(directory / "APP_CONFIG.json", config)
    (directory / "APP_CONFIG.json").chmod(0o600)
    services = Services(config)
    checks, evidence = [], []
    tracer = LocalTracer()
    logger = StdoutLogger(tracer, LogLevel.CRITICAL)
    try:
        services.start("experiments.posttraining.episode_mcp", config["mcp_port"])
        async with MCPToolClient(f"http://127.0.0.1:{config['mcp_port']}", None, logger, tracer) as wire:
            adapter = SessionBoundMCP(wire, RetailService(db.url, cid, require_session_binding=True))

            class Registry:
                async def read_tool_service(self, name):
                    assert name == "retail-demo-business"
                    return adapter

            caller = ToolCaller(logger, None, Registry(), None)
            ctx = ToolContext("retail-persistent-demo", str(sid), cid)
            async with open_stores(db.url) as (store, _, __):
                async def message(text, source, trace):
                    return await store.create_event(sid, source, EventKind.MESSAGE, trace,
                        {"message": text, "participant": {"display_name": "独立契约夹具"}})

                async def invoke(name, args, trace):
                    call = ToolCall(ToolCallId("contract-call-" + uuid4().hex),
                                    ToolId("retail-demo-business", name), args)
                    result = (await caller.execute_tool_calls(ctx, [call]))[0]
                    pid = "contract-policy-" + uuid4().hex
                    entry = {"tool_id": call.tool_id.to_string(), "arguments": dict(call.arguments),
                             "result": result.result, "tool_call_id": str(call.id),
                             "tool_result_id": str(result.id), "policy_step_id": pid}
                    event = await store.create_event(sid, EventSource.SYSTEM, EventKind.TOOL, trace,
                        {"tool_calls": [entry]}, metadata={"contract_fixture": True})
                    persisted = await store.read_event(sid, event.id)
                    assert persisted.data["tool_calls"][0] == entry
                    evidence.append({"policy_step_id": pid, "tool_call_id": str(call.id),
                                     "tool_result_id": str(result.id), "tool_event_id": str(event.id),
                                     "tool_name": name, "arguments": args, "result": result.result})
                    return result

                await message("仅执行独立错误契约检查", EventSource.CUSTOMER, "reject")
                rejected = await invoke("submit_confirmed_return", {"operation_id": "CONTRACT-ORDER"}, "reject")
                assert rejected.result["data"] == "Tool call error"
                assert "SESSION_FORBIDDEN" in rejected.result["metadata"]["error_details"]
                reply = await message("该错误操作未提交。", EventSource.AI_AGENT, "reject")
                assert reply.data["message"] == "该错误操作未提交。" and not reply.metadata.get("outbox_id")
                with connect(db.url) as c:
                    assert c.execute("SELECT count(*) AS n FROM return_requests").fetchone()["n"] == 0
                    assert c.execute("SELECT count(*) AS n FROM return_outbox").fetchone()["n"] == 0
                    assert c.execute("SELECT sum(reserved_return_quantity) AS n FROM order_items").fetchone()["n"] == 0
                checks.append("real_native_rejected_operation_string_preserved_reply_persists_zero_writes")

                params = {"order_id": "CONTRACT-ORDER", "item_id": "CONTRACT-LINE", "quantity": 1,
                          "reason": "仅验证唯一回执契约"}
                await message("开始独立合法回执检查", EventSource.CUSTOMER, "prepare")
                prepared = await invoke("check_return_eligibility", params, "prepare")
                assert prepared.result["data"]["ok"] is True
                preview = prepared.result["data"]["data"]
                await message(preview["confirmation_phrase"], EventSource.AI_AGENT, "offer")
                confirmed = await message(preview["confirmation_phrase"], EventSource.CUSTOMER, "confirm")
                result = await invoke("submit_confirmed_return", {"operation_id": preview["operation_id"]}, "success")
                assert result.result["data"]["ok"] is True
                first = await message("必须被原生唯一回执替换的测试文本", EventSource.AI_AGENT, "success")
                second = await message("再次测试同一回执", EventSource.AI_AGENT, "success")
                assert first.id == second.id and first.metadata.get("outbox_id")
                request = result.result["data"]["data"]["request"]
                assert request["id"] in first.data["message"]
                with connect(db.url) as c:
                    row = c.execute("SELECT count(*) AS n FROM parlant_events WHERE doc#>>'{metadata,outbox_id}'=%s",
                                    (str(first.metadata["outbox_id"]),)).fetchone()
                    assert row["n"] == 1
                    assert c.execute("SELECT count(*) AS n FROM return_requests").fetchone()["n"] == 1
                evidence.append({"confirmation_event_id": str(confirmed.id), "receipt_event_id": str(first.id),
                                 "outbox_id": str(first.metadata["outbox_id"]), "request_id": request["id"]})
                checks.append("real_native_confirmed_success_unique_receipt_repeated_persistence_same_id")

                await message("后续只做普通问答", EventSource.CUSTOMER, "later")
                normal = await message("独立后续普通回复", EventSource.AI_AGENT, "success")
                assert normal.data["message"] == "独立后续普通回复" and not normal.metadata.get("outbox_id")
                checks.append("later_user_boundary_no_old_success_receipt_override")

                payloads = ["Tool call error", None, [], ["error"], {}, {"ok": False, "error": {"code": "REJECTED"}},
                            {"ok": True}, {"ok": True, "data": {}}, {"ok": True, "data": {"request": {}}}]
                for i, payload in enumerate(payloads):
                    trace = "payload-" + str(i)
                    await message("无成功授权的类型测试", EventSource.CUSTOMER, trace)
                    entry = {"tool_id": "retail-demo-business:submit_confirmed_return",
                             "arguments": {"operation_id": "CONTRACT-ORDER"},
                             "result": {"data": payload, "metadata": {}, "control": {}}}
                    event = await store.create_event(sid, EventSource.SYSTEM, EventKind.TOOL, trace,
                                                     {"tool_calls": [entry]})
                    persisted = await store.read_event(sid, event.id)
                    assert persisted.data["tool_calls"][0]["result"]["data"] == payload
                    response = await message("非成功结果普通回复", EventSource.AI_AGENT, trace)
                    assert response.data["message"] == "非成功结果普通回复" and not response.metadata.get("outbox_id")
                checks.append("structured_string_null_list_missing_field_payloads_never_receipt_and_raw_preserved")

                for payload in ({"ok": True, "data": ["invalid"]},
                                {"ok": True, "data": {"request": ["invalid"]}}):
                    trace = "unsupported-" + uuid4().hex
                    await message("显式不支持的成功结构检查", EventSource.CUSTOMER, trace)
                    await store.create_event(sid, EventSource.SYSTEM, EventKind.TOOL, trace,
                        {"tool_calls": [{"tool_id": "retail-demo-business:submit_confirmed_return",
                                         "arguments": {"operation_id": preview["operation_id"]}, "result": {"data": payload}}]})
                    try:
                        await Outbox(store).for_submission_trace(str(sid), trace)
                    except AttributeError as exc:
                        evidence.append({"contract_error": type(exc).__name__, "reason": str(exc), "raw_payload": payload})
                    else:
                        raise AssertionError("Unsupported success structure silently swallowed")
                checks.append("unsupported_nested_success_formats_explicit_contract_error_not_swallowed")
    finally:
        services.stop()
    return {"passed": True, "model_loads": 0, "qwen_generations": 0, "paid_calls": 0,
            "oracle_episodes": 0, "checks": checks, "explicit_causal_evidence": evidence,
            "native_tool_caller": True, "real_pg_mcp": True, "business_fixture_not_baseline": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--attempt", type=int, default=1)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    config = json.loads(Path(plan["experiment_config"]).read_text())
    # Stay within PostgreSQL's 107-byte Unix socket path bound on the NAS.
    root = Path(plan["runtime"]) / ("c" + str(args.attempt))
    (root / "logs").mkdir(parents=True, exist_ok=False)
    resources = ResearchResources(root, config)
    db = None
    try:
        dsn = resources.start_pg()
        db = EpisodeDatabase(dsn, "ptdev_contract_" + uuid4().hex[:12], logical_fixture(), root / "episode")
        db.materialize()
        report = asyncio.run(verify(db))
        save(root / "CONTRACT_REGRESSION.json", report)
        save(Path(plan["review"]) / "CONTRACT_REGRESSION.json", report)
    finally:
        if db is not None:
            db.cleanup()
        resources.close()


if __name__ == "__main__":
    main()
