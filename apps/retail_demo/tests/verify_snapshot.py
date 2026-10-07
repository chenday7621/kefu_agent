"""Focused native-task/worker coordination and identified snapshot-tail checks."""

import asyncio
import copy
from datetime import datetime, timezone
import json
from parlant.core.agents import AgentId
from parlant.core.customers import CustomerId
from parlant.core.sessions import EventKind, EventSource
from parlant.core.tools import ToolContext
from parlant.core.background_tasks import BackgroundTaskService
from parlant.core.services.tools.mcp_service import MCPToolClient
from parlant.core.loggers import StdoutLogger, LogLevel
from parlant.core.tracer import LocalTracer
from ..business import RetailService
from ..coordination import SessionCoordinator
from ..outbox import OutboxWorker
from ..postgres_stores import open_stores
from ..session_tools import SessionBoundMCP
from ..snapshot_assertions import compare_snapshots
from ..db import connect
from ..settings import ROOT, load_settings
from .support import fixture_order, process


async def verify():
    s = load_settings()
    folder = (
        ROOT
        / "runtime-data/retail-demo/outbox"
        / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_snapshot")
    )
    folder.mkdir(parents=True)
    tracer = LocalTracer()
    logger = StdoutLogger(tracer, LogLevel.ERROR)
    api = s.parlant_home / "api_calls.jsonl"
    usage = api.read_bytes() if api.exists() else b""
    report = {"passed": False, "model_calls": 0}
    worker = None
    release = asyncio.Event()
    try:
        with connect(s.database_url) as c:
            assert c.execute("SELECT enabled FROM outbox_control").fetchone()["enabled"]
        with process("apps.retail_demo.mcp_server", s.mcp_port, folder):
            async with (
                open_stores(s.database_url, exclusive=True) as (store, _, __),
                MCPToolClient(s.mcp_url, None, logger, tracer) as native,
            ):
                mcp = SessionBoundMCP(
                    native,
                    RetailService(s.database_url, s.customer_id, require_session_binding=True),
                )
                session = await store.create_session(
                    CustomerId(s.customer_id),
                    AgentId("retail-persistent-demo"),
                    title="快照边界离线夹具",
                )
                sid = str(session.id)
                ctx = ToolContext("retail-persistent-demo", sid, s.customer_id)
                oid, item = fixture_order(s, 2)
                args = {"order_id": oid, "item_id": item, "quantity": 1, "reason": "快照验证"}
                preview = (await mcp.call_tool("check_return_eligibility", ctx, args)).data["data"]
                await store.create_event(
                    session.id,
                    EventSource.AI_AGENT,
                    EventKind.MESSAGE,
                    "fixture",
                    {"message": preview["confirmation_phrase"]},
                    metadata={"fixture_offer": True},
                )
                await store.create_event(
                    session.id,
                    EventSource.CUSTOMER,
                    EventKind.MESSAGE,
                    "fixture",
                    {"message": preview["confirmation_phrase"]},
                )
                made = (
                    await mcp.call_tool(
                        "create_return_request",
                        ctx,
                        {"operation_id": preview["operation_id"], **args},
                    )
                ).data
                assert made["ok"]
                coord = SessionCoordinator()
                bg = BackgroundTaskService(logger)
                coord.tasks = bg
                ready = asyncio.Event()

                async def actual_native_task_fixture():
                    async with coord.locks[sid]:
                        await store.create_event(
                            session.id,
                            EventSource.AI_AGENT,
                            EventKind.STATUS,
                            "fixture-late-state",
                            {"status": "ready", "data": {}},
                        )
                        ready.set()
                        await release.wait()
                        await store.update_session(
                            session.id, {"metadata": {"late_state": "saved_after_ready"}}
                        )

                task = await bg.start(actual_native_task_fixture(), tag=f"process-session({sid})")
                await ready.wait()
                worker = OutboxWorker(store, coord)
                worker.start()
                pending = asyncio.create_task(coord.snapshot(sid, store))
                await asyncio.sleep(1.2)
                with connect(s.database_url) as c:
                    outbox = c.execute(
                        "SELECT * FROM return_outbox WHERE operation_id=%s",
                        (preview["operation_id"],),
                    ).fetchone()
                    assert outbox["attempts"] == 0 and outbox["status"] == "pending"
                assert not pending.done()
                await worker.stop()
                release.set()
                before = await pending
                await task
                assert before["session"]["metadata"]["late_state"] == "saved_after_ready"
                (folder / "before_delivery_snapshot.json").write_text(
                    json.dumps(before, indent=2) + "\n"
                )
                worker.start()
                for _ in range(50):
                    with connect(s.database_url) as c:
                        delivered = (
                            c.execute(
                                "SELECT status FROM return_outbox WHERE id=%s", (outbox["id"],)
                            ).fetchone()["status"]
                            == "delivered"
                        )
                    if delivered:
                        break
                    await asyncio.sleep(0.1)
                assert delivered
                await worker.stop()
                after = await coord.snapshot(sid, store)
                (folder / "after_delivery_snapshot.json").write_text(
                    json.dumps(after, indent=2) + "\n"
                )
                compared = compare_snapshots(before, after)
                assert compared["identified_outbox_tail_ids"] == [str(outbox["id"])]
                rejections = []
                for label in (
                    "arbitrary_ready_tail",
                    "changed_session_state",
                    "changed_original_event",
                ):
                    wrong = copy.deepcopy(after)
                    if label == "arbitrary_ready_tail":
                        wrong["events"][-1]["kind"] = "status"
                        wrong["events"][-1]["data"] = {"status": "ready", "data": {}}
                    elif label == "changed_session_state":
                        wrong["session"]["metadata"]["unexpected"] = True
                    else:
                        wrong["events"][0]["metadata"]["unexpected"] = True
                    try:
                        compare_snapshots(before, wrong)
                    except AssertionError:
                        rejections.append(label)
                assert len(rejections) == 3
            async with open_stores(s.database_url, exclusive=True) as (store, _, __):
                restored = await SessionCoordinator().snapshot(sid, store)
                (folder / "after_store_restart_snapshot.json").write_text(
                    json.dumps(restored, indent=2) + "\n"
                )
                assert compare_snapshots(after, restored)["identified_outbox_tail_ids"] == []
        assert (api.read_bytes() if api.exists() else b"") == usage
        report.update(
            passed=True,
            session_id=sid,
            operation_id=preview["operation_id"],
            outbox_id=str(outbox["id"]),
            worker_deferred_until_native_task_finished=True,
            busy_attempts=0,
            ready_did_not_complete_snapshot=True,
            comparison=compared,
            rejected_differences=rejections,
            full_snapshot_equal_after_store_restart=True,
        )
    finally:
        release.set()
        if worker:
            await worker.stop()
        (folder / "snapshot_report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(folder.relative_to(ROOT) / "snapshot_report.json")


if __name__ == "__main__":
    asyncio.run(verify())
