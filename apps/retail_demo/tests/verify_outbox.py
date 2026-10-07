"""Real PostgreSQL/HTTP MCP/native Stores, process crashes and bounded retry.

No model calls. Explicitly marked offer/task fixtures exercise persistence and
authorization; they are not synthetic model answers or a dialogue benchmark.
"""

import asyncio
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
from uuid import uuid4
import httpx
from psycopg import sql
from parlant.core.agents import AgentId
from parlant.core.customers import CustomerId
from parlant.core.sessions import EventKind, EventSource, SessionId
from parlant.core.services.tools.mcp_service import MCPToolClient
from parlant.core.tools import ToolContext
from parlant.core.loggers import StdoutLogger, LogLevel
from parlant.core.tracer import LocalTracer
from parlant.core.background_tasks import BackgroundTaskService
from ..business import RetailService, BusinessError, plain
from ..db import connect
from ..postgres_stores import open_stores
from ..outbox import Outbox
from ..session_tools import SessionBoundMCP
from ..coordination import SessionCoordinator
from ..snapshot_assertions import compare_snapshots
from ..settings import ROOT, load_settings
from .support import fixture_order, process


async def verify():
    s = load_settings()
    folder = (
        ROOT
        / "runtime-data/retail-demo/outbox"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    folder.mkdir(parents=True)
    url = f"http://127.0.0.1:{s.parlant_port}"
    tracer = LocalTracer()
    logger = StdoutLogger(tracer, LogLevel.ERROR)
    service = RetailService(s.database_url, s.customer_id, require_session_binding=True)
    usage = s.parlant_home / "api_calls.jsonl"
    usage_before = usage.read_bytes() if usage.exists() else b""
    report = {"passed": False, "model_calls": 0, "checks": [], "cases": {}}
    triggers = []

    def save():
        (folder / "outbox_report.json").write_text(
            json.dumps(plain(report), ensure_ascii=False, indent=2) + "\n"
        )

    def check(name):
        report["checks"].append(name)
        save()

    def control(enabled):
        with connect(s.database_url) as c:
            c.execute("UPDATE outbox_control SET enabled=%s", (enabled,))

    def row(op):
        with connect(s.database_url) as c:
            return plain(
                c.execute("SELECT * FROM return_outbox WHERE operation_id=%s", (op,)).fetchone()
            )

    def count(op, item):
        with connect(s.database_url) as c:
            return dict(
                c.execute(
                    "SELECT (SELECT count(*) FROM return_requests WHERE operation_id=%s) AS requests,(SELECT reserved_return_quantity FROM order_items WHERE id=%s) AS reserved,(SELECT count(*) FROM return_outbox WHERE operation_id=%s) AS notifications,(SELECT count(*) FROM parlant_events WHERE doc#>>'{metadata,operation_id}'=%s AND doc#>>'{metadata,outbox_id}' IS NOT NULL) AS receipts",
                    (op, item, op, op),
                ).fetchone()
            )

    async def until(fn, timeout=20):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            value = fn()
            if value:
                return value
            await asyncio.sleep(0.15)
        raise TimeoutError("Expected database state did not appear")

    def inject(table, condition):
        name = "verify_outbox_" + uuid4().hex[:12]
        with connect(s.database_url) as c:
            c.execute(
                sql.SQL(
                    "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {} THEN RAISE EXCEPTION 'injected Outbox test failure' USING ERRCODE='23514'; END IF; RETURN NEW; END $$"
                ).format(sql.Identifier(name), condition)
            )
            c.execute(
                sql.SQL(
                    "CREATE TRIGGER {} BEFORE INSERT ON {} FOR EACH ROW EXECUTE FUNCTION {}()"
                ).format(sql.Identifier(name), sql.Identifier(table), sql.Identifier(name))
            )
        triggers.append((table, name))
        return table, name

    def drop(trigger):
        table, name = trigger
        with connect(s.database_url) as c:
            c.execute(
                sql.SQL("DROP TRIGGER {} ON {}").format(sql.Identifier(name), sql.Identifier(table))
            )
            c.execute(sql.SQL("DROP FUNCTION {}()").format(sql.Identifier(name)))
        triggers.remove(trigger)

    def native():
        return MCPToolClient(s.mcp_url, None, logger, tracer)

    async def new_session(store, customer=None):
        return await store.create_session(
            CustomerId(customer or s.customer_id),
            AgentId("retail-persistent-demo"),
            title="Outbox离线验证",
        )

    async def prepared(store, mcp, session, confirm=True):
        oid, item = fixture_order(s, 2)
        args = {"order_id": oid, "item_id": item, "quantity": 1, "reason": "Outbox离线验证"}
        ctx = ToolContext("retail-persistent-demo", str(session.id), s.customer_id)
        result = (await mcp.call_tool("check_return_eligibility", ctx, args)).data
        assert result["ok"], result
        preview = result["data"]
        # Association already exists before the native tool-result event is saved.
        with connect(s.database_url) as c:
            bound = c.execute(
                "SELECT * FROM session_operations WHERE operation_id=%s", (preview["operation_id"],)
            ).fetchone()
            assert (
                bound["session_id"] == str(session.id)
                and bound["confirmation_event_id"] is None
                and bound["prepared_event_id"] is None
            )
        await store.create_event(
            session.id,
            EventSource.SYSTEM,
            EventKind.TOOL,
            "fixture-prepare",
            {
                "tool_calls": [
                    {
                        "tool_id": "retail-demo-business:check_return_eligibility",
                        "arguments": args,
                        "result": {"data": result, "metadata": {}, "control": {}},
                    }
                ]
            },
        )
        if confirm:
            await store.create_event(
                session.id,
                EventSource.AI_AGENT,
                EventKind.MESSAGE,
                "fixture-offer",
                {
                    "message": preview["confirmation_phrase"],
                    "participant": {"id": "retail-persistent-demo", "display_name": "离线夹具"},
                },
                metadata={"fixture_offer": True},
            )
            await store.create_event(
                session.id,
                EventSource.CUSTOMER,
                EventKind.MESSAGE,
                "fixture-confirm",
                {
                    "message": preview["confirmation_phrase"],
                    "participant": {"id": s.customer_id, "display_name": "验证用户"},
                },
            )
        return {
            "session_id": str(session.id),
            "operation_id": preview["operation_id"],
            "preview": preview,
            "args": args,
            "ctx": ctx,
        }

    async def create(mcp, case):
        result = (
            await mcp.call_tool(
                "create_return_request",
                case["ctx"],
                {"operation_id": case["operation_id"], **case["args"]},
            )
        ).data
        if result["ok"]:
            report["cases"][case["operation_id"]] = {
                "session_id": case["session_id"],
                "request_id": result["data"]["request"]["id"],
                "outbox_id": (row(case["operation_id"]) or {}).get("id"),
            }
        return result

    try:
        control(False)
        with process(
            "apps.retail_demo.mcp_server",
            s.mcp_port,
            folder,
            env={**os.environ, "DEMO_TEST_DROP_COMMITTED_MCP_RESPONSE": str(folder / "drop_mcp")},
        ) as proc:
            async with native() as wire, open_stores(s.database_url) as (store, _, __):
                mcp = SessionBoundMCP(wire, service)
                assert len(await mcp.list_tools()) == 6
                assert (
                    "session_token"
                    not in (await mcp.read_tool("check_return_eligibility")).parameters
                )
                session = await new_session(store)
                a = await prepared(store, mcp, session)
                made = await create(mcp, a)
                assert made["ok"]
                await store.create_event(
                    session.id,
                    EventSource.SYSTEM,
                    EventKind.TOOL,
                    "fixture-submit",
                    {
                        "tool_calls": [
                            {
                                "tool_id": "retail-demo-business:create_return_request",
                                "arguments": {"operation_id": a["operation_id"], **a["args"]},
                                "result": {"data": made, "metadata": {}, "control": {}},
                            }
                        ]
                    },
                )
                automatic = await store.create_event(
                    session.id,
                    EventSource.AI_AGENT,
                    EventKind.MESSAGE,
                    "fixture-submit",
                    {
                        "message": "此文本不应成为数据库提交回执",
                        "participant": {"id": "retail-persistent-demo", "display_name": "夹具"},
                    },
                )
                assert automatic.metadata["outbox_id"] == row(a["operation_id"])["id"]
                assert (
                    made["data"]["request"]["id"] in automatic.data["message"]
                    and "尚未退款" in automatic.data["message"]
                )
                b = await prepared(store, mcp, session, confirm=False)
                assert (
                    service.session_operation(str(session.id))["data"]["operation_id"]
                    == b["operation_id"]
                )
                await Outbox(store).deliver(row(a["operation_id"])["id"])
                assert (
                    service.session_operation(str(session.id))["data"]["operation_id"]
                    == b["operation_id"]
                )
                assert not (await create(mcp, b))["ok"]
                check(
                    "prepare_and_session_association_atomic_without_confirmation; normal_receipt_is_fixed_and_unique; A_does_not_displace_B"
                )

                ccase = await prepared(store, mcp, await new_session(store))
                trigger = inject(
                    "return_outbox",
                    sql.SQL("NEW.operation_id={}").format(sql.Literal(ccase["operation_id"])),
                )
                assert not (await create(mcp, ccase))["ok"]
                assert count(ccase["operation_id"], ccase["args"]["item_id"]) == {
                    "requests": 0,
                    "reserved": 0,
                    "notifications": 0,
                    "receipts": 0,
                }
                drop(trigger)
                assert (await create(mcp, ccase))["ok"]
                check(
                    "business_and_outbox_rollback_together; original_confirmed_operation_retry_succeeds"
                )

                expired = await prepared(store, mcp, await new_session(store), confirm=False)
                await store.create_event(
                    SessionId(expired["session_id"]),
                    EventSource.AI_AGENT,
                    EventKind.MESSAGE,
                    "fixture-expired-offer",
                    {"message": expired["preview"]["confirmation_phrase"]},
                    metadata={"fixture_offer": True},
                )
                with connect(s.database_url) as c:
                    c.execute(
                        "UPDATE return_operations SET expires_at=now()-interval '1 second' WHERE id=%s",
                        (expired["operation_id"],),
                    )
                try:
                    await store.create_event(
                        SessionId(expired["session_id"]),
                        EventSource.CUSTOMER,
                        EventKind.MESSAGE,
                        "expired",
                        {"message": expired["preview"]["confirmation_phrase"]},
                    )
                    raise AssertionError("Expired confirmation accepted")
                except BusinessError as exc:
                    assert "operation expired" in exc.message
                assert not (await create(mcp, expired))["ok"]
                foreign = await new_session(store, "demo-bob")
                try:
                    await mcp.call_tool(
                        "check_return_eligibility",
                        ToolContext("retail-persistent-demo", str(foreign.id), s.customer_id),
                        b["args"],
                    )
                    raise AssertionError("Foreign session accepted")
                except BusinessError:
                    pass
                try:
                    await mcp.call_tool(
                        "get_operation_result", ccase["ctx"], {"operation_id": a["operation_id"]}
                    )
                    raise AssertionError("Other session operation accepted")
                except BusinessError:
                    pass
                with connect(s.database_url) as c:
                    old = c.execute(
                        "SELECT o.* FROM return_operations o JOIN return_requests r ON r.operation_id=o.id JOIN session_operations b ON b.operation_id=o.id WHERE o.customer_id=%s AND NOT EXISTS(SELECT 1 FROM return_outbox WHERE operation_id=o.id) ORDER BY r.created_at LIMIT 1",
                        (s.customer_id,),
                    ).fetchone()
                assert old
                replay = service.create_return_request(
                    str(old["id"]), old["order_id"], old["item_id"], old["quantity"], old["reason"]
                )
                assert (
                    replay["ok"]
                    and replay["data"]["idempotent_replay"]
                    and row(str(old["id"])) is None
                )
                check(
                    "unconfirmed_expiry_and_session_ownership_enforced; historical_replay_does_not_enqueue"
                )

                # Native BackgroundTaskService still running after ready; snapshot
                # must await its late state update, not sample on ready alone.
                coord = SessionCoordinator()
                bg = BackgroundTaskService(logger)
                coord.tasks = bg
                late = await new_session(store)
                ready, release = asyncio.Event(), asyncio.Event()

                async def native_task_fixture():
                    async with coord.locks[str(late.id)]:
                        await store.create_event(
                            late.id,
                            EventSource.AI_AGENT,
                            EventKind.STATUS,
                            "late-state-fixture",
                            {"status": "ready", "data": {}},
                        )
                        ready.set()
                        await release.wait()
                        await store.update_session(
                            late.id, {"metadata": {"after_ready": "persisted"}}
                        )

                task = await bg.start(native_task_fixture(), tag=f"process-session({late.id})")
                await ready.wait()
                pending_snapshot = asyncio.create_task(coord.snapshot(str(late.id), store))
                await asyncio.sleep(0.1)
                assert not pending_snapshot.done()
                release.set()
                late_snapshot = await pending_snapshot
                await task
                assert late_snapshot["session"]["metadata"]["after_ready"] == "persisted"
                (folder / "late_state_snapshot.json").write_text(
                    json.dumps(late_snapshot, indent=2) + "\n"
                )
                check("snapshot_awaits_actual_native_task_and_late_session_state_after_ready")

                lost = await prepared(store, mcp, await new_session(store))
                (folder / "drop_mcp").write_text(lost["operation_id"])
                try:
                    await create(mcp, lost)
                    raise AssertionError("Dropped MCP response unexpectedly delivered")
                except AssertionError:
                    raise
                except Exception as exc:
                    report["lost_mcp_error"] = type(exc).__name__
                assert proc.wait(timeout=10) == 77
                assert row(lost["operation_id"])["status"] == "pending"
                assert count(lost["operation_id"], lost["args"]["item_id"]) == {
                    "requests": 1,
                    "reserved": 1,
                    "notifications": 1,
                    "receipts": 0,
                }
                report["lost_mcp"] = {
                    "operation_id": lost["operation_id"],
                    "session_id": lost["session_id"],
                    "outbox_id": row(lost["operation_id"])["id"],
                }

        control(True)
        with process("apps.retail_demo.mcp_server", s.mcp_port, folder):
            with process(
                "apps.retail_demo.parlant_app", s.parlant_port, folder, ("--disable-llm",)
            ):
                await until(lambda: row(lost["operation_id"])["status"] == "delivered")
                async with httpx.AsyncClient(base_url=url, timeout=70) as web:
                    snapshot = await web.post(f"/demo/sessions/{session.id}/snapshot")
                    snapshot.raise_for_status()
                    before_snapshot = snapshot.json()
                    (folder / "before_restart_snapshot.json").write_text(
                        json.dumps(before_snapshot, indent=2) + "\n"
                    )
                    latest = (await web.get(f"/demo/sessions/{session.id}/operation-result")).json()
                    explicit = (
                        await web.get(
                            f"/demo/sessions/{session.id}/operation-result",
                            params={"operation_id": a["operation_id"]},
                        )
                    ).json()
                    assert (
                        latest["data"]["operation_id"] == b["operation_id"]
                        and latest["data"]["request"] is None
                    )
                    assert explicit["data"]["request"]["id"] == made["data"]["request"]["id"]
                    report["A_B"] = {
                        "session_id": str(session.id),
                        "submitted_A": a["operation_id"],
                        "unconfirmed_current_B": b["operation_id"],
                    }
                check(
                    "actual_MCP_commit_then_exit_77; restart_worker_delivers_without_any_user_query"
                )
            with process(
                "apps.retail_demo.parlant_app", s.parlant_port, folder, ("--disable-llm",)
            ):
                async with httpx.AsyncClient(base_url=url, timeout=70) as web:
                    r = await web.post(f"/demo/sessions/{session.id}/snapshot")
                    r.raise_for_status()
                    after_snapshot = r.json()
                    (folder / "after_restart_snapshot.json").write_text(
                        json.dumps(after_snapshot, indent=2) + "\n"
                    )
                    report["snapshot_comparison"] = compare_snapshots(
                        before_snapshot, after_snapshot
                    )
                    response = await web.post(
                        f"/sessions/{session.id}/events",
                        json={
                            "kind": "message",
                            "source": "customer",
                            "message": "刚才退货成功了吗",
                        },
                    )
                    response.raise_for_status()
                    events = (
                        await web.get(f"/sessions/{session.id}/events?wait_for_data=0")
                    ).json()
                    answer = [
                        e for e in events if e["kind"] == "message" and e["source"] == "ai_agent"
                    ][-1]["data"]["message"]
                    assert (
                        b["operation_id"] in answer
                        and "尚未提交" in answer
                        and made["data"]["request"]["id"] not in answer
                    )
                check(
                    "A_B_survives_process_restart; recovery_answers_B_not_A; full_native_snapshot_and_state_equal"
                )

            control(False)
            async with native() as wire, open_stores(s.database_url) as (store, _, __):
                mcp = SessionBoundMCP(wire, service)
                failing = await prepared(store, mcp, await new_session(store))
                assert (await create(mcp, failing))["ok"]
                failing_id = row(failing["operation_id"])["id"]
                with connect(s.database_url) as c:
                    offset_before = c.execute(
                        "SELECT next_offset FROM parlant_sessions WHERE id=%s",
                        (failing["session_id"],),
                    ).fetchone()["next_offset"]
                fault = inject(
                    "parlant_events",
                    sql.SQL("NEW.doc#>>'{{metadata,outbox_id}}'={}").format(
                        sql.Literal(failing_id)
                    ),
                )
            control(True)
            with process(
                "apps.retail_demo.parlant_app", s.parlant_port, folder, ("--disable-llm",)
            ):
                for target in range(1, 6):
                    await until(lambda: row(failing["operation_id"])["attempts"] >= target)
                    if target < 5:
                        with connect(s.database_url) as c:
                            c.execute(
                                "UPDATE return_outbox SET next_attempt_at=now() WHERE id=%s",
                                (failing_id,),
                            )
                failed = row(failing["operation_id"])
                assert (
                    failed["status"] == "failed"
                    and failed["total_attempts"] == 5
                    and failed["last_error"]
                )
                await asyncio.sleep(1.2)
                assert row(failing["operation_id"])["attempts"] == 5
                with connect(s.database_url) as c:
                    assert (
                        c.execute(
                            "SELECT next_offset FROM parlant_sessions WHERE id=%s",
                            (failing["session_id"],),
                        ).fetchone()["next_offset"]
                        == offset_before
                    )
                    assert (
                        c.execute(
                            "SELECT count(*) AS n FROM return_outbox_errors WHERE outbox_id=%s",
                            (failing_id,),
                        ).fetchone()["n"]
                        == 5
                    )
                report["bounded_failure"] = failed
            drop(fault)
            subprocess.run(
                [sys.executable, "-m", "apps.retail_demo.outbox", "retry", "--id", failing_id],
                check=True,
                stdout=(folder / "manual_retry.json").open("w"),
            )
            with process(
                "apps.retail_demo.parlant_app", s.parlant_port, folder, ("--disable-llm",)
            ):
                await until(lambda: row(failing["operation_id"])["status"] == "delivered")
                assert row(failing["operation_id"])["total_attempts"] == 6
                assert count(failing["operation_id"], failing["args"]["item_id"]) == {
                    "requests": 1,
                    "reserved": 1,
                    "notifications": 1,
                    "receipts": 1,
                }
            check(
                "actual_receipt_INSERT_failure_rolls_back_offset; five_bounded_retries; persisted_errors; manual_retry_after_restart_succeeds"
            )

            control(False)
            async with native() as wire, open_stores(s.database_url) as (store, _, __):
                mcp = SessionBoundMCP(wire, service)
                crashed = await prepared(store, mcp, await new_session(store))
                assert (await create(mcp, crashed))["ok"]
                crash_id = row(crashed["operation_id"])["id"]
                (folder / "crash_after_commit").write_text(crash_id)
            with process(
                "apps.retail_demo.parlant_app",
                s.parlant_port,
                folder,
                ("--disable-llm",),
                env={
                    **os.environ,
                    "DEMO_TEST_OUTBOX_AFTER_COMMIT": str(folder / "crash_after_commit"),
                },
            ) as proc:
                control(True)
                await until(lambda: proc.poll() is not None)
                assert proc.returncode == 78
                assert row(crashed["operation_id"])["status"] == "delivered"
            with process(
                "apps.retail_demo.parlant_app", s.parlant_port, folder, ("--disable-llm",)
            ):
                await asyncio.sleep(1.2)
                assert count(crashed["operation_id"], crashed["args"]["item_id"]) == {
                    "requests": 1,
                    "reserved": 1,
                    "notifications": 1,
                    "receipts": 1,
                }
            report["post_delivery_commit_crash"] = row(crashed["operation_id"])
            check(
                "actual_delivery_transaction_commit_then_process_exit_78; restart_does_not_duplicate_receipt"
            )

            control(False)
            async with native() as wire, open_stores(s.database_url) as (store, _, __):
                mcp = SessionBoundMCP(wire, service)
                interleaved = await prepared(store, mcp, await new_session(store))
                assert (await create(mcp, interleaved))["ok"]
                delivery_id = row(interleaved["operation_id"])["id"]
            with process(
                "apps.retail_demo.parlant_app", s.parlant_port, folder, ("--disable-llm",)
            ):
                async with (
                    native() as wire,
                    open_stores(s.database_url) as (store, _, __),
                    httpx.AsyncClient(base_url=url, timeout=30) as web,
                ):
                    outbox = Outbox(store)
                    # Native long polling starts before the receipt is committed.
                    with connect(s.database_url) as c:
                        start_offset = c.execute(
                            "SELECT next_offset FROM parlant_sessions WHERE id=%s",
                            (interleaved["session_id"],),
                        ).fetchone()["next_offset"]
                    poll = asyncio.create_task(
                        web.get(
                            f"/sessions/{interleaved['session_id']}/events",
                            params={"min_offset": start_offset, "wait_for_data": 10},
                        )
                    )
                    await asyncio.sleep(0.1)
                    control(True)
                    results = await asyncio.gather(
                        outbox.deliver(delivery_id),
                        outbox.deliver(delivery_id),
                        web.post(
                            f"/sessions/{interleaved['session_id']}/events",
                            json={
                                "kind": "message",
                                "source": "customer",
                                "message": "刚才退货成功了吗",
                            },
                        ),
                        SessionBoundMCP(wire, service).call_tool(
                            "create_return_request",
                            interleaved["ctx"],
                            {"operation_id": interleaved["operation_id"], **interleaved["args"]},
                        ),
                    )
                    assert results[2].status_code == 201 and results[3].data["ok"]
                    notified = await poll
                    notified.raise_for_status()
                    assert any(
                        e["metadata"].get("outbox_id") == delivery_id for e in notified.json()
                    )
                    again = await web.post(
                        f"/sessions/{interleaved['session_id']}/events",
                        json={
                            "kind": "message",
                            "source": "customer",
                            "message": "刚才退货成功了吗",
                        },
                    )
                    assert again.status_code == 201
                    assert count(interleaved["operation_id"], interleaved["args"]["item_id"]) == {
                        "requests": 1,
                        "reserved": 1,
                        "notifications": 1,
                        "receipts": 1,
                    }
            report["interleaved"] = row(interleaved["operation_id"])
            check(
                "worker_manual_recovery_replay_and_duplicate_processing_interleaved; one_canonical_receipt; native_UI_long_poll_observes_commit"
            )

        assert (usage.read_bytes() if usage.exists() else b"") == usage_before
        report["passed"] = True
    except Exception as exc:
        report.update(error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        for trigger in list(triggers):
            drop(trigger)
        control(True)
        save()
        print(folder.relative_to(ROOT) / "outbox_report.json")


if __name__ == "__main__":
    asyncio.run(verify())
