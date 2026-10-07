"""Real PostgreSQL, native Stores, HTTP MCP, and two actual lost-reply faults.

No model API is used. Synthetic fixtures use native Store APIs, and receipts are
returned through the actual application HTTP path. This is not an LLM benchmark.
"""

import asyncio
from datetime import datetime, timezone
import json
import os
from uuid import uuid4
import httpx
from psycopg import sql, Error as PgError
from parlant.core.sessions import SessionId, EventKind, EventSource
from parlant.core.customers import CustomerId
from parlant.core.agents import AgentId
from parlant.core.tags import TagId
from parlant.core.services.tools.mcp_service import MCPToolClient
from parlant.core.tools import ToolContext
from parlant.core.loggers import StdoutLogger, LogLevel
from parlant.core.tracer import LocalTracer
from ..business import RetailService
from ..db import connect
from ..postgres_stores import open_stores
from ..postgres_documents import PostgresDocumentDatabase
from ..settings import ROOT, load_settings
from .support import fixture_order, process


async def verify():
    s = load_settings()
    folder = (
        ROOT
        / "runtime-data/retail-demo/reliability"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    folder.mkdir(parents=True)
    checks = []
    details = {}
    url = f"http://127.0.0.1:{s.parlant_port}"
    service = RetailService(s.database_url, s.customer_id, require_session_binding=True)
    ctx = ToolContext("retail-persistent-demo", "offline-reliability", s.customer_id)
    tracer = LocalTracer()
    logger = StdoutLogger(tracer, LogLevel.ERROR)

    def client():
        return MCPToolClient(s.mcp_url, None, logger, tracer)

    api_file = s.parlant_home / "api_calls.jsonl"
    usage_before = api_file.read_bytes() if api_file.exists() else b""
    originals = {
        name: (s.parlant_home / name).read_bytes()
        for name in ("sessions.json", "customers.json", "context_variables.json")
    }

    async def tool(c, name, args):
        return (await c.call_tool(name, ctx, args)).data

    def counts(opid, item):
        with connect(s.database_url) as c:
            return c.execute(
                "SELECT (SELECT count(*) FROM return_requests WHERE operation_id=%s) AS requests,(SELECT reserved_return_quantity FROM order_items WHERE id=%s) AS quantity",
                (opid, item),
            ).fetchone()

    async def new_offer(c, label):
        oid, item = fixture_order(s, 2)
        args = {"order_id": oid, "item_id": item, "quantity": 1, "reason": label}
        prepared = await tool(c, "check_return_eligibility", args)
        assert prepared["ok"], prepared
        data = prepared["data"]
        op = data["operation_id"]
        async with open_stores(s.database_url) as (sessions, _, __):
            session = await sessions.create_session(
                CustomerId(s.customer_id),
                AgentId("retail-persistent-demo"),
                title="可靠性验证 " + label,
                metadata={"fixture": True},
                labels={"reliability"},
            )
            sid = str(session.id)
            await sessions.create_event(
                session.id,
                EventSource.CUSTOMER,
                EventKind.MESSAGE,
                "fixture",
                {
                    "message": "请准备演示退货",
                    "participant": {"id": s.customer_id, "display_name": "验证用户"},
                },
                metadata={"test": "history"},
            )
            await sessions.create_event(
                session.id,
                EventSource.AI_AGENT,
                EventKind.TOOL,
                "fixture",
                {
                    "tool_calls": [
                        {
                            "tool_id": "retail-demo-business:check_return_eligibility",
                            "arguments": args,
                            "result": {"data": prepared, "metadata": {}, "control": {}},
                        }
                    ]
                },
            )
            await sessions.create_event(
                session.id,
                EventSource.AI_AGENT,
                EventKind.MESSAGE,
                "fixture",
                {
                    "message": f"申请内容：{oid} / {item} / 数量1 / 原因{label} / 129.00 CNY\n请复制确认：\n{data['confirmation_phrase']}",
                    "participant": {"id": "retail-persistent-demo", "display_name": "验证夹具"},
                },
                metadata={"fixture_offer": True},
            )
        return sid, op, args, data

    async def confirm(c, sid, phrase):
        r = await c.post(f"/demo/sessions/{sid}/confirm", json={"message": phrase})
        r.raise_for_status()
        return r.json()

    with process("apps.retail_demo.mcp_server", s.mcp_port, folder):
        async with client() as mcp:
            names = {t.name for t in await mcp.list_tools()}
            assert "get_operation_result" in names and len(names) == 6
            checks.append("real_mcp_operation_result_discovery")
            pending_sid, pending_op, pending_args, pending_data = await new_offer(mcp, "待确认重启")
            lost_sid, lost_op, lost_args, lost_data = await new_offer(mcp, "MCP响应丢失")
            final_sid, final_op, final_args, final_data = await new_offer(mcp, "最终回复未保存")
            retry_sid, retry_op, retry_args, retry_data = await new_offer(mcp, "确认后原键重试")
            expired_sid, expired_op, expired_args, expired_data = await new_offer(mcp, "过期未提交")
        with process("apps.retail_demo.parlant_app", s.parlant_port, folder, ("--disable-llm",)):
            async with httpx.AsyncClient(base_url=url, timeout=20) as web:
                assert (await web.get("/demo/info")).json()["session_storage"] == "postgres"
                r = await web.get(f"/demo/sessions/{pending_sid}/operation-result")
                assert r.json()["data"]["state"] == "awaiting_confirmation"
                bad = await web.post(
                    f"/demo/sessions/{pending_sid}/confirm",
                    json={"message": pending_data["confirmation_phrase"].replace("数量1", "数量2")},
                )
                assert bad.status_code == 409
                # Model-looking metadata cannot authorize a pending operation.
                async with open_stores(s.database_url) as (store, _, __):
                    await store.create_event(
                        SessionId(pending_sid),
                        EventSource.AI_AGENT,
                        EventKind.CUSTOM,
                        "fixture",
                        {"confirmed": True, "operation_id": pending_op},
                        metadata={"confirmed": True},
                    )
                with connect(s.database_url) as db:
                    assert (
                        db.execute(
                            "SELECT confirmed_at FROM return_operations WHERE id=%s", (pending_op,)
                        ).fetchone()["confirmed_at"]
                        is None
                    )
                for sid, data in [
                    (lost_sid, lost_data),
                    (final_sid, final_data),
                    (retry_sid, retry_data),
                ]:
                    confirmed = await confirm(web, sid, data["confirmation_phrase"])
                    assert confirmed["customer_event_id"]
                    with connect(s.database_url) as db:
                        binding = db.execute(
                            "SELECT * FROM session_operations WHERE session_id=%s", (sid,)
                        ).fetchone()
                        actual = db.execute(
                            "SELECT doc FROM parlant_events WHERE id=%s",
                            (binding["confirmation_event_id"],),
                        ).fetchone()["doc"]
                        assert (
                            binding["confirmation_event"] == actual
                            and actual["source"] == "customer"
                        )
                        assert actual["data"]["message"] == data["confirmation_phrase"]
                checks.append("actual_customer_event_and_confirmation_binding_atomic")
                checks.append("model_metadata_cannot_authorize_and_wrong_phrase_rejected")
                # No prior offer in another owned session; no event/counter or
                # confirmation stamp can partially commit on rejection.
                async with open_stores(s.database_url) as (store, _, __):
                    stranger = await store.create_session(
                        CustomerId(s.customer_id), AgentId("retail-persistent-demo")
                    )
                nooffer = await web.post(
                    f"/demo/sessions/{stranger.id}/confirm",
                    json={"message": pending_data["confirmation_phrase"]},
                )
                assert nooffer.status_code == 409
                with connect(s.database_url) as db:
                    assert (
                        db.execute(
                            "SELECT next_offset FROM parlant_sessions WHERE id=%s",
                            (str(stranger.id),),
                        ).fetchone()["next_offset"]
                        == 0
                    )
                checks.append("rejected_confirmation_rolls_back_event_and_offset")
                # Native application server owns the single-instance lock.
                try:
                    async with open_stores(s.database_url, exclusive=True):
                        pass
                except RuntimeError:
                    checks.append("second_parlant_instance_rejected")
                else:
                    raise AssertionError("Second writer lock accepted")
        # Restart Parlant before confirming the previously unconfirmed operation.
        with process("apps.retail_demo.parlant_app", s.parlant_port, folder, ("--disable-llm",)):
            async with httpx.AsyncClient(base_url=url, timeout=20) as web:
                state = (await web.get(f"/demo/sessions/{pending_sid}/operation-result")).json()
                assert state["data"]["state"] == "awaiting_confirmation"
                assert counts(pending_op, pending_args["item_id"]) == {"requests": 0, "quantity": 0}
                denied = service.create_return_request(pending_op, **pending_args)
                assert not denied["ok"]
                await confirm(web, pending_sid, pending_data["confirmation_phrase"])
                checks.append("pending_operation_restart_still_requires_real_confirmation")
                # Explicit retry passes exact database parameters through native
                # MCPToolClient, never asks a model to synthesize another key.
                for _ in range(2):
                    r = await web.post(f"/demo/sessions/{retry_sid}/operations/{retry_op}/retry")
                    r.raise_for_status()
                retried = (await web.get(f"/demo/sessions/{retry_sid}/operation-result")).json()
                assert retried["data"]["state"] == "submitted" and counts(
                    retry_op, retry_args["item_id"]
                ) == {"requests": 1, "quantity": 1}
                with connect(s.database_url) as db:
                    args = db.execute(
                        "SELECT doc#>'{data,tool_calls,0,arguments}' AS args FROM parlant_events WHERE session_id=%s AND event_kind='tool' ORDER BY event_offset DESC LIMIT 1",
                        (retry_sid,),
                    ).fetchone()["args"]
                    assert args == {"operation_id": retry_op, **retry_args}
                    db.execute(
                        "UPDATE return_operations SET expires_at=now()-interval '1 second' WHERE id=%s",
                        (expired_op,),
                    )
                expired = (await web.get(f"/demo/sessions/{expired_sid}/operation-result")).json()
                assert expired["data"]["state"] == "expired_not_submitted"
                r = await web.post(
                    f"/demo/sessions/{expired_sid}/confirm",
                    json={"message": expired_data["confirmation_phrase"]},
                )
                assert r.status_code == 409
                checks.append("explicit_retry_original_key_parameters_no_duplicate")
                checks.append("expired_unsubmitted_operation_not_reauthorized")
        # Create normally then genuinely reject the FINAL native AI event insert
        # at the database boundary (not merely restart a successfully saved reply).
        async with client() as mcp:
            final = await tool(
                mcp, "create_return_request", {"operation_id": final_op, **final_args}
            )
            assert final["ok"]
        trigger = "verify_reject_" + uuid4().hex[:12]
        with connect(s.database_url) as db:
            db.execute(
                sql.SQL(
                    "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.doc->>'session_id'={} AND NEW.doc->>'kind'='message' AND NEW.doc->>'source'='ai_agent' THEN RAISE EXCEPTION 'injected final reply storage fault' USING ERRCODE='23514'; END IF; RETURN NEW; END $$"
                ).format(sql.Identifier(trigger), sql.Literal(final_sid))
            )
            db.execute(
                sql.SQL(
                    "CREATE TRIGGER {} BEFORE INSERT ON parlant_events FOR EACH ROW EXECUTE FUNCTION {}()"
                ).format(sql.Identifier(trigger), sql.Identifier(trigger))
            )
        try:
            async with open_stores(s.database_url) as (store, _, __):
                try:
                    await store.create_event(
                        SessionId(final_sid),
                        EventSource.AI_AGENT,
                        EventKind.MESSAGE,
                        "reply-fault",
                        {"message": "退货申请已提交 " + final["data"]["request"]["id"]},
                    )
                except PgError:
                    pass
                else:
                    raise AssertionError("Final reply fault did not actually reject persistence")
            with connect(s.database_url) as db:
                assert not db.execute(
                    "SELECT 1 FROM parlant_events WHERE session_id=%s AND trace_id='reply-fault'",
                    (final_sid,),
                ).fetchone()
        finally:
            with connect(s.database_url) as db:
                db.execute(
                    sql.SQL("DROP TRIGGER {} ON parlant_events").format(sql.Identifier(trigger))
                )
                db.execute(sql.SQL("DROP FUNCTION {}()").format(sql.Identifier(trigger)))
        details["final_reply_fault"] = {
            "session_id": final_sid,
            "operation_id": final_op,
            "request_id": final["data"]["request"]["id"],
            "native_ai_insert_rejected": True,
        }
    # First server is now stopped. A separate actual MCP process exits AFTER
    # business commit and BEFORE response delivery for this exact operation only.
    marker = folder / "drop_once.txt"
    marker.write_text(lost_op)
    env = {**os.environ, "DEMO_TEST_DROP_COMMITTED_MCP_RESPONSE": str(marker)}
    with process("apps.retail_demo.mcp_server", s.mcp_port, folder, env=env) as faulty:
        async with client() as mcp:
            try:
                await tool(mcp, "create_return_request", {"operation_id": lost_op, **lost_args})
            except Exception as exc:
                details["lost_mcp_exception_type"] = type(exc).__name__
            else:
                raise AssertionError("Committed MCP response was not dropped")
        faulty.wait(timeout=10)
        assert faulty.returncode == 77 and not marker.exists()
    assert counts(lost_op, lost_args["item_id"]) == {"requests": 1, "quantity": 1}
    original_lost = service.get_operation_result(lost_op)
    assert original_lost["ok"] and original_lost["data"]["state"] == "submitted"
    details["mcp_response_fault"] = {
        "session_id": lost_sid,
        "operation_id": lost_op,
        "request_id": original_lost["data"]["request"]["id"],
        "actual_server_exit_code": 77,
    }
    # Both original faults happened before these process restarts and recovery.
    with process("apps.retail_demo.mcp_server", s.mcp_port, folder):
        with process("apps.retail_demo.parlant_app", s.parlant_port, folder, ("--disable-llm",)):
            async with httpx.AsyncClient(base_url=url, timeout=20) as web:
                for sid, op, args in [
                    (lost_sid, lost_op, lost_args),
                    (final_sid, final_op, final_args),
                ]:
                    request = service.get_operation_result(op)["data"]["request"]
                    before = counts(op, args["item_id"])
                    # Expire a submitted operation: result reads must still work.
                    with connect(s.database_url) as db:
                        db.execute(
                            "UPDATE return_operations SET expires_at=now()-interval '1 second' WHERE id=%s",
                            (op,),
                        )
                    for _ in range(2):
                        r = await web.post(
                            f"/sessions/{sid}/events",
                            json={
                                "kind": "message",
                                "source": "customer",
                                "message": "刚才退货成功了吗",
                            },
                        )
                        r.raise_for_status()
                        result = (await web.get(f"/demo/sessions/{sid}/operation-result")).json()
                        assert (
                            result["data"]["request"]["id"] == str(request["id"])
                            and result["data"]["state"] == "submitted"
                        )
                        assert "尚未退款" in result["receipt"] and "129.00" in result["receipt"]
                    r = await web.post(f"/demo/sessions/{sid}/operations/{op}/retry")
                    r.raise_for_status()
                    assert counts(op, args["item_id"]) == before == {"requests": 1, "quantity": 1}
                    events = (await web.get(f"/sessions/{sid}/events?wait_for_data=0")).json()
                    receipts = [
                        e for e in events if e["metadata"].get("deterministic_operation_receipt")
                    ]
                    assert receipts and all(
                        str(request["id"]) in e["data"]["message"] for e in receipts
                    )
                checks.extend(
                    [
                        "committed_mcp_response_lost_restart_recovers_same_application",
                        "final_ai_reply_not_saved_restart_recovers_same_application",
                        "repeated_recovery_and_expired_submitted_key_no_extra_quantity",
                    ]
                )
                # Own session cannot query a different same-customer operation.
                denied = (
                    await web.get(
                        f"/demo/sessions/{pending_sid}/operation-result?operation_id={lost_op}"
                    )
                ).json()
                assert not denied["ok"]
                async with open_stores(s.database_url) as (store, _, __):
                    foreign = await store.create_session(
                        CustomerId("demo-bob"), AgentId("retail-persistent-demo")
                    )
                assert (
                    await web.get(f"/demo/sessions/{foreign.id}/operation-result")
                ).status_code == 403
                assert (
                    await web.get(f"/sessions/{foreign.id}/events?wait_for_data=0")
                ).status_code == 409
                assert not RetailService(s.database_url, "demo-bob").get_operation_result(lost_op)[
                    "ok"
                ]
                checks.append("operation_session_customer_ownership_enforced")
        async with client() as mcp:
            queried = await tool(mcp, "get_operation_result", {"operation_id": lost_op})
            assert (
                queried["ok"]
                and queried["data"]["request"]["id"] == original_lost["data"]["request"]["id"]
            )
            checks.append("read_only_recovery_via_actual_http_mcp")
    # Native complete data, concurrent sequence and Store lifecycle tests.
    async with open_stores(s.database_url) as (store, customers, variables):
        seq = await store.create_session(
            CustomerId(s.customer_id),
            AgentId("retail-persistent-demo"),
            metadata={"nested": {"a": [1, True, None]}},
            labels={"reliability"},
        )
        await asyncio.gather(
            *(
                store.create_event(
                    seq.id,
                    EventSource.SYSTEM,
                    EventKind.CUSTOM,
                    "parallel",
                    {"number": i},
                    metadata={"index": i},
                )
                for i in range(24)
            )
        )
        event_list = await store.list_events(seq.id)
        assert [e.offset for e in event_list] == list(range(24))
        event = event_list[0]
        await store.update_event(
            seq.id, event.id, {"data": {"updated": True}, "metadata": {"complete": [1, None]}}
        )
        await store.delete_event(event_list[1].id)
        assert (
            len(await store.list_events(seq.id)) == 23
            and len(await store.list_events(seq.id, exclude_deleted=False)) == 24
        )
        filtered = await store.list_events(
            seq.id, source=EventSource.SYSTEM, kinds=[EventKind.CUSTOM], min_offset=20
        )
        assert [e.offset for e in filtered] == list(range(20, 24))
        await store.set_metadata(seq.id, "operation_hint", "retained")
        await store.upsert_labels(seq.id, {"new"})
        listing = await store.list_sessions(customer_id=CustomerId(s.customer_id), limit=2)
        assert len(listing) == 2 and listing.has_more and listing.next_cursor
        next_page = await store.list_sessions(
            customer_id=CustomerId(s.customer_id), limit=2, cursor=listing.next_cursor
        )
        assert not {s.id for s in listing} & {s.id for s in next_page}
        checks.append("concurrent_24_events_unique_offsets_complete_metadata_filters_pagination")
        cid = CustomerId("verify-" + uuid4().hex[:12])
        await customers.create_customer("持久化验证客户", id=cid, extra={"fixture": "yes"})
        await customers.upsert_tag(cid, TagId("verify-tag"))
        await customers.upsert_extra(cid, {"retained": "true"})
        variable = await variables.create_variable(
            "verify-" + uuid4().hex[:12], description="Native variable persistence check"
        )
        value = await variables.update_value(
            variable.id, s.customer_id, {"nested": [1, True, None]}
        )
        sequence_id = seq.id
    async with open_stores(s.database_url) as (store, customers, variables):
        assert (await store.read_session(sequence_id)).metadata["operation_hint"] == "retained"
        assert len(await store.list_events(sequence_id, exclude_deleted=False)) == 24
        assert (await customers.read_customer(cid)).extra["retained"] == "true"
        assert (await variables.read_value(variable.id, s.customer_id)).id == value.id
        await variables.delete_variable(variable.id)  # test variable never enters customer prompt
        checks.append("native_customer_tags_extra_and_context_values_survive_store_restart")
    closed = PostgresDocumentDatabase(s.database_url, "sessions")
    try:
        async with closed.connection():
            pass
    except RuntimeError:
        checks.append("closed_async_storage_rejects_io")
    else:
        raise AssertionError("Closed DB lifecycle accepted I/O")
    # Connection failure must throw rather than returning local data.

    unavailable = PostgresDocumentDatabase(
        "postgresql://retail_demo@127.0.0.1:1/retail_demo", "sessions"
    )
    try:
        async with unavailable:
            pass
    except PgError:
        checks.append("database_error_no_local_fallback")
    else:
        raise AssertionError("Unavailable PG unexpectedly opened")
    assert api_file.read_bytes() == usage_before if api_file.exists() else usage_before == b""
    assert originals == {name: (s.parlant_home / name).read_bytes() for name in originals}, (
        "Original local files were still being synchronized"
    )
    report = {
        "passed": True,
        "checks": checks,
        "faults": details,
        "model_calls": 0,
        "old_local_files_unchanged": True,
        "sequence_session_id": str(sequence_id),
        "synthetic_fixtures_only": True,
        "not_an_llm_dialogue_test": True,
    }
    (folder / "reliability_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("Evidence directory:", folder.relative_to(ROOT))


if __name__ == "__main__":
    asyncio.run(verify())
