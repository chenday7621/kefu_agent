"""Native Parlant SDK entrance with PostgreSQL Store extensions and recovery."""

import argparse
import asyncio
import re
from .settings import load_settings, prepare_model_environment

AGENT_ID = "retail-persistent-demo"
SERVICE_NAME = "retail-demo-business"
TOOLS = {
    "list_my_orders",
    "get_order_details",
    "check_return_eligibility",
    "submit_confirmed_return",
    "get_return_request",
    "get_operation_result",
}


async def serve(settings, disable_llm, stores, workers):
    import os

    os.environ["DEMO_EMBEDDING_MODEL"] = settings.embedding_model
    if disable_llm:
        os.environ["DEMO_DISABLE_LLM"] = "1"
    import parlant.sdk as p
    from fastapi.responses import JSONResponse
    from fastapi import Request
    from psycopg import Error as DatabaseError
    from parlant.core.application import Application
    from parlant.core.sessions import SessionId, EventKind, EventSource
    from parlant.core.common import ItemNotFoundError
    from parlant.api.sessions import event_to_dto
    from parlant.core.app_modules.sessions import Moderation
    from parlant.core.tools import ToolContext
    from .recovery import RECOVERY_QUESTIONS, RETRY_QUESTIONS, latest_result, receipt
    from parlant.core.services.tools.service_registry import ServiceRegistry
    from .nlp import nlp_factory
    from .business import RetailService, BusinessError
    from .guidelines import RULES
    from .coordination import SessionCoordinator
    from .outbox import Outbox, OutboxWorker
    from .session_tools import install_session_adapter
    from parlant.core.background_tasks import BackgroundTaskService

    coordinator = SessionCoordinator()

    business = RetailService(
        settings.database_url, settings.customer_id, require_session_binding=True
    )
    if not business.list_my_orders()["ok"]:
        raise RuntimeError("Initialize PostgreSQL before starting Parlant.")
    server = None

    async def configure_api(api):
        async def owned(sid):
            session = await server.container[p.SessionStore].read_session(SessionId(sid))
            if str(session.customer_id) != settings.customer_id:
                raise BusinessError("SESSION_FORBIDDEN", "会话不属于当前演示客户。")
            return session

        async def customer_message(sid, text):
            return await server.container[Application].sessions.create_customer_message(
                session_id=SessionId(sid),
                moderation=Moderation.NONE,
                message=text,
                source=EventSource.CUSTOMER,
                trigger_processing=False,
                metadata=None,
            )

        async def recovery_reply(sid, text, operation_id=None, retry=False):
            await owned(sid)
            result = await asyncio.to_thread(latest_result, business, sid, operation_id)
            event = await customer_message(sid, text)
            if retry and result["ok"] and result["data"]["retry_allowed"]:
                arguments = {"operation_id": result["data"]["operation_id"]}
                service = await server.container[ServiceRegistry].read_tool_service(SERVICE_NAME)
                try:
                    tool = await service.call_tool(
                        "submit_confirmed_return",
                        ToolContext(AGENT_ID, sid, settings.customer_id),
                        arguments,
                    )
                    await server.container[p.SessionStore].create_event(
                        SessionId(sid),
                        EventSource.AI_AGENT,
                        EventKind.TOOL,
                        event.trace_id,
                        {
                            "tool_calls": [
                                {
                                    "tool_id": SERVICE_NAME + ":submit_confirmed_return",
                                    "arguments": arguments,
                                    "result": {
                                        "data": tool.data,
                                        "metadata": tool.metadata,
                                        "control": {},
                                    },
                                }
                            ]
                        },
                    )
                except Exception as exc:
                    # A dropped MCP reply does not imply a failed transaction.
                    # Read the original key, and never generate a replacement key.
                    await server.container[p.SessionStore].create_event(
                        SessionId(sid),
                        EventSource.AI_AGENT,
                        EventKind.TOOL,
                        event.trace_id,
                        {
                            "tool_calls": [
                                {
                                    "tool_id": SERVICE_NAME + ":submit_confirmed_return",
                                    "arguments": arguments,
                                    "result": {
                                        "data": {
                                            "ok": False,
                                            "error": {"code": "MCP_RESPONSE_UNAVAILABLE"},
                                        },
                                        "metadata": {"error_type": type(exc).__name__},
                                        "control": {},
                                    },
                                }
                            ]
                        },
                    )
                result = await asyncio.to_thread(
                    business.get_operation_result, arguments["operation_id"]
                )
            facts = result.get("data") or {}
            store = server.container[p.SessionStore]
            delivered_for_query = False
            if facts.get("request"):
                outbox = Outbox(store)
                oid = await outbox.for_operation(facts["operation_id"])
                if oid:
                    async with store._database.connection() as c:
                        pending = await (
                            await c.execute(
                                "SELECT status='pending' AND next_attempt_at<=now() AS due FROM return_outbox WHERE id=%s",
                                (oid,),
                            )
                        ).fetchone()
                    if pending["due"]:
                        await outbox.deliver(oid, trace_id=event.trace_id)
                        delivered_for_query = True
            if not delivered_for_query:
                await store.create_event(
                    SessionId(sid),
                    EventSource.AI_AGENT,
                    EventKind.MESSAGE,
                    event.trace_id,
                    {
                        "message": receipt(result),
                        "participant": {"id": AGENT_ID, "display_name": "持久化订单与退货演示客服"},
                    },
                    metadata={
                        "deterministic_operation_receipt": True,
                        "operation_id": str(facts.get("operation_id", "")),
                        "request_id": str((facts.get("request") or {}).get("id", "")),
                        "query_response": True,
                    },
                )
            await server.container[p.SessionStore].create_event(
                SessionId(sid),
                EventSource.AI_AGENT,
                EventKind.STATUS,
                event.trace_id,
                {"status": "ready", "data": {}},
            )
            return JSONResponse(event_to_dto(event).model_dump(mode="json"), status_code=201)

        @api.middleware("http")
        async def session_write_gate(request, call_next):
            match = re.match(r"/(?:demo/)?sessions/([^/]+)(?:/|$)", request.url.path)
            if (
                match
                and request.method in ("POST", "PATCH", "PUT", "DELETE")
                and not request.url.path.endswith("/snapshot")
            ):
                async with coordinator.locks[match.group(1)]:
                    return await demo_identity_and_confirmation(request, call_next)
            return await demo_identity_and_confirmation(request, call_next)

        async def demo_identity_and_confirmation(request, call_next):
            path = request.url.path
            if settings.session_storage == "local_rollback" and request.method in (
                "POST",
                "PATCH",
                "PUT",
                "DELETE",
            ):
                return JSONResponse(
                    {
                        "error": "Explicit local rollback is read-only; resume PostgreSQL before writing."
                    },
                    status_code=503,
                )
            try:
                match = re.fullmatch(r"/sessions/([^/]+)(?:/events(?:/[^/]+)?)?", path)
                if match:
                    await owned(match.group(1))
                if request.method == "POST" and path == "/sessions":
                    body = await request.json()
                    if (
                        body.get("customer_id") != settings.customer_id
                        or body.get("agent_id") != AGENT_ID
                    ):
                        return JSONResponse(
                            {"error": "This demo is bound to its displayed customer and agent."},
                            status_code=403,
                        )
                match = re.fullmatch(r"/sessions/([^/]+)/events", path)
                if request.method == "POST" and match:
                    body = await request.json()
                    if body.get("kind") == "message" and body.get("source") == "customer":
                        if not isinstance(body.get("message"), str):
                            return JSONResponse({"error": "message must be text"}, status_code=422)
                        text = body["message"].strip()
                        sid = match.group(1)
                        # These paths work with LLM disabled: real native messages
                        # and DB-grounded receipts, not a regenerated model answer.
                        if RECOVERY_QUESTIONS.search(text):
                            return await recovery_reply(sid, text)
                        if RETRY_QUESTIONS.fullmatch(text):
                            return await recovery_reply(sid, text, retry=True)
                        if disable_llm:
                            return JSONResponse(
                                {
                                    "error": "LLM disabled; use the explicit confirm/recovery endpoints for offline validation."
                                },
                                status_code=503,
                            )
                return await call_next(request)
            except BusinessError as exc:
                return JSONResponse(
                    {"ok": False, "error": {"code": exc.code, "message": exc.message}},
                    status_code=409,
                )
            except ItemNotFoundError:
                return JSONResponse({"error": "Session not found"}, status_code=404)
            except DatabaseError:
                return JSONResponse(
                    {
                        "error": "Database unavailable; original operation retained. No local fallback."
                    },
                    status_code=503,
                )

        @api.post("/demo/sessions/{sid}/snapshot")
        async def snapshot(sid: str):
            await owned(sid)
            if not stores:
                return JSONResponse({"error": "Snapshot requires PostgreSQL"}, status_code=503)
            try:
                return await coordinator.snapshot(sid, stores[0])
            except TimeoutError:
                return JSONResponse(
                    {"error": "Native processing has not completed; no snapshot saved"},
                    status_code=409,
                )

        @api.get("/demo/sessions/{sid}/operation-result")
        async def operation_result(sid: str, operation_id: str | None = None):
            try:
                await owned(sid)
                result = await asyncio.to_thread(latest_result, business, sid, operation_id)
                return {**result, "receipt": receipt(result)}
            except (BusinessError, ItemNotFoundError):
                return JSONResponse({"error": "Session not owned or not found"}, status_code=403)

        @api.post("/demo/sessions/{sid}/confirm")
        async def confirm(sid: str, request: Request):
            try:
                await owned(sid)
                body = await request.json()
                text = body.get("message")
                if not isinstance(text, str) or not text.strip().startswith("确认退货 "):
                    return JSONResponse(
                        {"error": "Copy the complete backend confirmation phrase."}, status_code=422
                    )
                event = await customer_message(sid, text.strip())
                return {
                    "customer_event_id": str(event.id),
                    "operation_result": await asyncio.to_thread(latest_result, business, sid),
                }
            except BusinessError as exc:
                return JSONResponse(
                    {"error": {"code": exc.code, "message": exc.message}}, status_code=409
                )
            except ItemNotFoundError:
                return JSONResponse({"error": "Session not found"}, status_code=404)

        @api.post("/demo/sessions/{sid}/operations/{operation_id}/retry")
        async def retry(sid: str, operation_id: str):
            try:
                await owned(sid)
                bound = await asyncio.to_thread(business.session_operation, sid, operation_id)
                if not bound["ok"]:
                    return JSONResponse(bound, status_code=409)
                return await recovery_reply(
                    sid, "按原操作重试 " + operation_id, operation_id, retry=True
                )
            except (BusinessError, ItemNotFoundError):
                return JSONResponse({"error": "Session not owned or not found"}, status_code=403)

        @api.get("/demo/info")
        async def demo_info():
            return {
                "agent_id": AGENT_ID,
                "customer_id": settings.customer_id,
                "mcp_url": settings.mcp_url,
                "local_identity_only": True,
                "llm_disabled": disable_llm,
                "session_storage": settings.session_storage,
                "return_policy": "30 days; one submitted application per line; no refund execution",
            }

    class Quiet(p.BasicPerceivedPerformancePolicy):
        async def is_preamble_required(self, context=None):
            return False

        async def is_message_splitting_required(self, context, message):
            return False

    async def always_apply(ctx, guideline):
        # General business policies apply to every turn; actual tool choice stays in Parlant.
        # This native SDK matcher avoids LLM startup policy-classification calls.
        return p.GuidelineMatch(
            id=guideline.id,
            matched=True,
            rationale="Persistent retail demo business policy applies to every customer turn",
        )

    server = p.Server(
        host="127.0.0.1",
        port=settings.parlant_port,
        tool_service_port=settings.tool_port,
        nlp_service=nlp_factory,
        session_store=stores[0] if stores else "local",
        customer_store=stores[1] if stores else "local",
        variable_store=stores[2] if stores else "local",
        configure_api=configure_api,
    )
    async with server:
        coordinator.install(
            server.container[Application].sessions, server.container[BackgroundTaskService]
        )
        service = await server.container[ServiceRegistry].update_tool_service(
            name=SERVICE_NAME, kind="mcp", url=settings.mcp_url, transient=True
        )
        actual = {t.name for t in await service.list_tools()}
        if actual != TOOLS:
            raise RuntimeError(f"MCP tool discovery mismatch: {actual}")
        if stores:
            install_session_adapter(
                server.container[ServiceRegistry], SERVICE_NAME, service, business
            )
        agent = await server.create_agent(
            id=AGENT_ID,
            name="持久化订单与退货演示客服",
            description="本机单客户演示。自行生成订单，真实PostgreSQL持久化；订单查询和退货申请，不执行真实支付或物流。",
            max_engine_iterations=5,
            perceived_performance_policy=Quiet(),
        )
        if not await server.find_customer(id=settings.customer_id):
            await server.create_customer(
                id=settings.customer_id,
                name="演示客户 " + settings.customer_id.removeprefix("demo-").title(),
                metadata={"demo_identity": "server-bound; not a login system"},
            )
        for key, action, names in RULES:
            await agent.create_guideline(
                id=p.GuidelineId("retail-demo-" + key),
                action=action,
                matcher=always_apply,
                tools=[p.ToolId(service_name=SERVICE_NAME, tool_name=name) for name in names],
            )
        print(
            f"Ready to start: http://127.0.0.1:{settings.parlant_port}/chat ; select customer {settings.customer_id}",
            flush=True,
        )
        if stores:
            worker = OutboxWorker(stores[0], coordinator)
            workers.append(worker)
            worker.start()


async def run(disable_llm=False):
    settings = load_settings()
    prepare_model_environment(settings)  # SDK captures PARLANT_HOME on import
    if settings.session_storage == "postgres":
        from .postgres_stores import open_stores

        async with open_stores(settings.database_url, exclusive=True) as stores:
            workers = []
            try:
                await serve(settings, disable_llm, stores, workers)
            finally:
                for worker in workers:
                    await worker.stop()
    else:
        from .postgres_stores import single_instance

        async with single_instance(settings.database_url):
            await serve(settings, disable_llm, None, [])  # explicit, read-only rollback only


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--disable-llm",
        action="store_true",
        help="Native SDK/PostgreSQL with model calls blocked; DB receipts remain usable.",
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args.disable_llm))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
