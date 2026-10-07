"""Real PostgreSQL + HTTP MCP + Parlant MCPToolClient integration; zero LLM calls."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import subprocess
from uuid import uuid4
from parlant.core.services.tools.mcp_service import MCPToolClient
from parlant.core.tools import ToolContext, ToolError
from parlant.core.tracer import LocalTracer
from parlant.core.loggers import StdoutLogger, LogLevel
from ..business import RetailService
from ..db import connect, initialize, seed
from ..settings import load_settings, ROOT, APP
from .support import fixture_order, process, confirm_prepared


async def verify(restart_db):
    s = load_settings()
    initialize(s.database_url)
    seed(s.database_url)
    run = (
        ROOT
        / "runtime-data/retail-demo/verification"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    run.mkdir(parents=True, exist_ok=True)
    records = []
    service = RetailService(s.database_url, s.customer_id)
    ctx = ToolContext(
        "verification", "verification", "demo-bob"
    )  # deliberately wrong; MCP does not receive it
    tracer = LocalTracer()
    logger = StdoutLogger(tracer, LogLevel.WARNING)

    def client():
        return MCPToolClient(s.mcp_url, None, logger, tracer)

    async def call(c, name, args):
        try:
            r = (await c.call_tool(name, ctx, args)).data
        except ToolError as e:
            r = {
                "ok": False,
                "error": {"code": "MCP_ARGUMENT_ERROR", "message": str(e)},
                "data": None,
            }
        records.append({"tool": name, "arguments": args, "result": r})
        assert isinstance(r, dict) and isinstance(r.get("ok"), bool), r
        return r

    def expect(r, code):
        assert not r["ok"] and r["error"]["code"] == code, r

    def counts():
        with connect(s.database_url) as conn:
            return tuple(
                conn.execute(
                    "SELECT (SELECT count(*) FROM return_requests) AS requests,(SELECT count(*) FROM operation_logs) AS logs,(SELECT sum(reserved_return_quantity) FROM order_items) AS reserved"
                )
                .fetchone()
                .values()
            )

    oid, item = fixture_order(s)
    reason = "验证尺寸不合适"
    args = {"order_id": oid, "item_id": item, "quantity": 1, "reason": reason}
    checks = []
    with process("apps.retail_demo.mcp_server", s.mcp_port, run):
        async with client() as c:
            tools = await c.list_tools()
            names = {t.name for t in tools}
            assert names == {
                "list_my_orders",
                "get_order_details",
                "check_return_eligibility",
                "create_return_request",
                "get_return_request",
                "get_operation_result",
            }
            creation = next(t for t in tools if t.name == "create_return_request")
            assert (
                "customer_id" not in creation.parameters
                and creation.parameters["quantity"][0]["type"] == "integer"
            )
            checks.append("real_mcp_discovery_schema")
            orders = await call(c, "list_my_orders", {})
            assert orders["ok"] and orders["data"]["bound_customer_id"] == s.customer_id
            details = await call(c, "get_order_details", {"order_id": oid})
            assert details["ok"] and any(i["id"] == item for i in details["data"]["items"])
            checks.append("server_identity_ignores_forged_tool_context")
            before = counts()
            expect(
                await call(c, "get_order_details", {"order_id": "DEMO-2001"}),
                "ORDER_NOT_FOUND_OR_FORBIDDEN",
            )
            expect(
                await call(
                    c,
                    "check_return_eligibility",
                    {**args, "order_id": "DEMO-2001", "item_id": "DEMO-2001-MUG"},
                ),
                "ORDER_NOT_FOUND_OR_FORBIDDEN",
            )
            expect(
                await call(c, "check_return_eligibility", {**args, "item_id": "DEMO-2001-MUG"}),
                "ITEM_NOT_IN_ORDER",
            )
            for qty in [0, -1, 1001]:
                expect(
                    await call(c, "check_return_eligibility", {**args, "quantity": qty}),
                    "INVALID_QUANTITY",
                )
            for qty in [1.5, True, "1.5", "1e0", "+1", "01", " 1"]:
                expect(
                    await call(c, "check_return_eligibility", {**args, "quantity": qty}),
                    "MCP_ARGUMENT_ERROR",
                )
            expect(
                await call(c, "check_return_eligibility", {**args, "quantity": 3}),
                "QUANTITY_EXCEEDS_AVAILABLE",
            )
            expect(
                await call(
                    c,
                    "check_return_eligibility",
                    {
                        "order_id": "DEMO-1002",
                        "item_id": "DEMO-1002-KEYBOARD",
                        "quantity": 1,
                        "reason": reason,
                    },
                ),
                "ORDER_NOT_DELIVERED",
            )
            expect(
                await call(
                    c,
                    "check_return_eligibility",
                    {
                        "order_id": "DEMO-1003",
                        "item_id": "DEMO-1003-MOUSE",
                        "quantity": 1,
                        "reason": reason,
                    },
                ),
                "RETURN_WINDOW_EXPIRED",
            )
            expect(
                await call(
                    c,
                    "check_return_eligibility",
                    {
                        "order_id": "DEMO-1001",
                        "item_id": "DEMO-1001-DIGITAL",
                        "quantity": 1,
                        "reason": reason,
                    },
                ),
                "ITEM_NOT_RETURNABLE",
            )
            expect(
                await call(c, "list_my_orders", {"customer_id": "demo-bob"}), "MCP_ARGUMENT_ERROR"
            )
            assert counts() == before
            checks.append("ownership_item_quantity_eligibility_denials_no_writes")
            preview = await call(c, "check_return_eligibility", {**args, "quantity": "1"})
            assert preview["ok"] and preview["data"]["quantity"] == 1
            payload = {**args, "operation_id": preview["data"]["operation_id"]}
            before = counts()
            expect(await call(c, "create_return_request", payload), "CONFIRMATION_EVENT_REQUIRED")
            assert counts() == before
            expect(
                await call(c, "create_return_request", {**payload, "operation_id": str(uuid4())}),
                "OPERATION_NOT_FOUND",
            )
            confirm = await confirm_prepared(s, preview["data"])
            assert confirm["ok"]
            submitted = await call(c, "create_return_request", payload)
            assert submitted["ok"]
            rid = submitted["data"]["request"]["id"]
            assert submitted["data"]["request"]["amount_cents"] == 12900
            replay = await call(c, "create_return_request", payload)
            assert (
                replay["ok"]
                and replay["data"]["request"]["id"] == rid
                and replay["data"]["idempotent_replay"]
            )
            expect(
                await call(c, "create_return_request", {**payload, "quantity": 2}),
                "IDEMPOTENCY_CONFLICT",
            )
            expect(
                await call(c, "create_return_request", {**payload, "reason": "另一原因"}),
                "IDEMPOTENCY_CONFLICT",
            )
            queried = await call(c, "get_return_request", {"request_id": rid})
            assert queried["ok"] and len(queried["data"]["operation_logs"]) == 3
            checks.append(
                "confirmation_required_idempotency_parameter_conflicts_real_cents_and_logs"
            )
            # Same key under contention: all answers must refer to exactly one request.
            replays = await asyncio.gather(
                *(call(c, "create_return_request", payload) for _ in range(8))
            )
            assert all(r["ok"] and r["data"]["request"]["id"] == rid for r in replays)
            checks.append("eight_concurrent_same_key_replays")
            # Different issued keys for the same line race: database allows only one pending request.
            race_order, race_item = fixture_order(s, 2)
            race_args = {
                "order_id": race_order,
                "item_id": race_item,
                "quantity": 2,
                "reason": "并发验证",
            }
            drafts = [
                (await call(c, "check_return_eligibility", race_args))["data"] for _ in range(8)
            ]
            for draft in drafts:
                assert (await confirm_prepared(s, draft))["ok"]
            raced = await asyncio.gather(
                *(
                    call(
                        c, "create_return_request", {**race_args, "operation_id": d["operation_id"]}
                    )
                    for d in drafts
                )
            )
            assert sum(r["ok"] for r in raced) == 1, raced
            with connect(s.database_url) as conn:
                assert conn.execute(
                    "SELECT reserved_return_quantity,quantity FROM order_items WHERE id=%s",
                    (race_item,),
                ).fetchone() == {"reserved_return_quantity": 2, "quantity": 2}
                assert (
                    conn.execute(
                        "SELECT count(*) AS n FROM return_requests WHERE item_id=%s", (race_item,)
                    ).fetchone()["n"]
                    == 1
                )
            checks.append("eight_distinct_keys_race_no_duplicate_or_over_return")
            # Cross-customer request lookup, created using a separately trusted operator binding.
            bob_order, bob_item = fixture_order(s, 1, "demo-bob")
            bob = RetailService(s.database_url, "demo-bob")
            bp = bob.check_return_eligibility(bob_order, bob_item, 1, "验证")["data"]
            assert bob.confirm_operation(
                bp["operation_id"], bp["confirmation_phrase"], "verification-bob-human"
            )["ok"]
            br = bob.create_return_request(bp["operation_id"], bob_order, bob_item, 1, "验证")
            assert br["ok"]
            before = counts()
            expect(
                await call(c, "get_return_request", {"request_id": br["data"]["request"]["id"]}),
                "REQUEST_NOT_FOUND_OR_FORBIDDEN",
            )
            assert counts() == before
            checks.append("other_customer_application_denied")
    before = counts()
    initialize(s.database_url)
    seed(s.database_url)
    initialize(s.database_url)
    seed(s.database_url)
    assert counts() == before
    checks.append("repeat_migration_seed_preserve_applications_and_logs")
    if restart_db:
        command = [
            "docker",
            "compose",
            "--env-file",
            str(APP / ".env"),
            "-f",
            str(APP / "compose.yaml"),
            "restart",
            "db",
        ]
        subprocess.run(command, check=True, timeout=60)
        for _ in range(100):
            if service.list_my_orders()["ok"]:
                break
            await asyncio.sleep(0.3)
        else:
            raise AssertionError("PostgreSQL did not recover after restart")
        checks.append("real_postgresql_container_restart")
    with process("apps.retail_demo.mcp_server", s.mcp_port, run):
        async with client() as c:
            restored = await call(c, "get_return_request", {"request_id": rid})
            assert restored["ok"] and restored["data"] == queried["data"]
            assert counts() == before
            checks.append("mcp_service_restart_same_request_and_three_operation_logs")
    report = {
        "checks": checks,
        "all_passed": True,
        "request_id": rid,
        "order_id": oid,
        "operation_id": payload["operation_id"],
        "logs": restored["data"]["operation_logs"],
        "database_counts_before_and_after_restart": before,
        "database_container_restarted": restart_db,
        "llm_calls": 0,
        "synthetic_test_orders_preserved": True,
        "transport": "real Streamable HTTP /mcp",
        "client": "parlant.core.services.tools.mcp_service.MCPToolClient",
    }
    (run / "mcp_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (run / "mcp_calls.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("Evidence directory:", run.relative_to(ROOT))


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--restart-db",
        action="store_true",
        help="Restart only this demo Compose db and verify persistence.",
    )
    asyncio.run(verify(p.parse_args().restart_db))


if __name__ == "__main__":
    main()
