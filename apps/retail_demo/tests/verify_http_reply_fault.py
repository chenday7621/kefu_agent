"""Reject an actual HTTP recovery receipt at INSERT, then restore after restart."""

import asyncio
from datetime import datetime, timezone
import json
from uuid import uuid4
import httpx
from psycopg import sql
from parlant.core.services.tools.mcp_service import MCPToolClient
from parlant.core.tools import ToolContext
from parlant.core.loggers import StdoutLogger, LogLevel
from parlant.core.tracer import LocalTracer
from ..db import connect
from ..settings import ROOT, load_settings
from .support import process, fixture_order, confirm_prepared


async def verify():
    s = load_settings()
    folder = (
        ROOT
        / "runtime-data/retail-demo/reliability"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    folder.mkdir(parents=True)
    url = f"http://127.0.0.1:{s.parlant_port}"
    tracer = LocalTracer()
    logger = StdoutLogger(tracer, LogLevel.ERROR)
    ctx = ToolContext("retail-persistent-demo", "http-fault", s.customer_id)
    oid, item = fixture_order(s, 2)
    args = {"order_id": oid, "item_id": item, "quantity": 1, "reason": "HTTP最终回执存储故障"}
    usage = s.parlant_home / "api_calls.jsonl"
    before = usage.read_bytes()
    with process("apps.retail_demo.mcp_server", s.mcp_port, folder):
        async with MCPToolClient(s.mcp_url, None, logger, tracer) as mcp:
            preview = (await mcp.call_tool("check_return_eligibility", ctx, args)).data["data"]
            confirmation = await confirm_prepared(s, preview)
            sid = confirmation["session_id"]
            op = preview["operation_id"]
            created = (
                await mcp.call_tool("create_return_request", ctx, {"operation_id": op, **args})
            ).data
            assert created["ok"]
            request_id = created["data"]["request"]["id"]
        trigger = "verify_http_" + uuid4().hex[:12]
        with connect(s.database_url) as db:
            db.execute(
                sql.SQL(
                    "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.doc->>'session_id'={} AND NEW.doc#>>'{{metadata,deterministic_operation_receipt}}'='true' THEN RAISE EXCEPTION 'injected actual HTTP receipt storage failure' USING ERRCODE='23514'; END IF; RETURN NEW; END $$"
                ).format(sql.Identifier(trigger), sql.Literal(sid))
            )
            db.execute(
                sql.SQL(
                    "CREATE TRIGGER {} BEFORE INSERT ON parlant_events FOR EACH ROW EXECUTE FUNCTION {}()"
                ).format(sql.Identifier(trigger), sql.Identifier(trigger))
            )
        try:
            with process(
                "apps.retail_demo.parlant_app", s.parlant_port, folder, ("--disable-llm",)
            ):
                async with httpx.AsyncClient(base_url=url, timeout=20) as web:
                    failed = await web.post(
                        f"/sessions/{sid}/events",
                        json={
                            "kind": "message",
                            "source": "customer",
                            "message": "刚才退货成功了吗",
                        },
                    )
                    assert failed.status_code == 503, failed.text
                    with connect(s.database_url) as db:
                        assert not db.execute(
                            "SELECT 1 FROM parlant_events WHERE session_id=%s AND doc#>>'{metadata,deterministic_operation_receipt}'='true'",
                            (sid,),
                        ).fetchone()
                        assert (
                            db.execute(
                                "SELECT count(*) AS n FROM return_requests WHERE operation_id=%s",
                                (op,),
                            ).fetchone()["n"]
                            == 1
                        )
        finally:
            with connect(s.database_url) as db:
                db.execute(
                    sql.SQL("DROP TRIGGER {} ON parlant_events").format(sql.Identifier(trigger))
                )
                db.execute(sql.SQL("DROP FUNCTION {}()").format(sql.Identifier(trigger)))
        with process("apps.retail_demo.parlant_app", s.parlant_port, folder, ("--disable-llm",)):
            async with httpx.AsyncClient(base_url=url, timeout=20) as web:
                for _ in range(2):
                    restored = await web.post(
                        f"/sessions/{sid}/events",
                        json={
                            "kind": "message",
                            "source": "customer",
                            "message": "刚才退货成功了吗",
                        },
                    )
                    restored.raise_for_status()
                events = (await web.get(f"/sessions/{sid}/events?wait_for_data=0")).json()
                replies = [
                    e for e in events if e["metadata"].get("deterministic_operation_receipt")
                ]
                assert len(replies) == 2 and all(
                    request_id in e["data"]["message"] and "尚未退款" in e["data"]["message"]
                    for e in replies
                )
        with connect(s.database_url) as db:
            assert (
                db.execute(
                    "SELECT count(*) AS n FROM return_requests WHERE operation_id=%s", (op,)
                ).fetchone()["n"]
                == 1
            )
            assert (
                db.execute(
                    "SELECT reserved_return_quantity FROM order_items WHERE id=%s", (item,)
                ).fetchone()["reserved_return_quantity"]
                == 1
            )
    assert usage.read_bytes() == before
    report = {
        "passed": True,
        "session_id": sid,
        "operation_id": op,
        "request_id": request_id,
        "actual_http_receipt_insert_rejected": True,
        "initial_http_status": 503,
        "post_restart_receipts": 2,
        "applications": 1,
        "reserved_quantity": 1,
        "model_calls": 0,
    }
    (folder / "http_reply_fault_report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print("Evidence directory:", folder.relative_to(ROOT))


if __name__ == "__main__":
    asyncio.run(verify())
