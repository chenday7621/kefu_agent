"""Real FastMCP Streamable HTTP server. Identity is bound in the server process."""

import asyncio
import re
import os
from pathlib import Path
from typing import Annotated
from pydantic import Field
from pydantic import BeforeValidator
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
from fastmcp.exceptions import ToolError
from .business import RetailService
from .settings import load_settings


def native_integer(value):
    # Parlant 3.3.2 ToolCallArgumentEvaluation.value is a string, even for an
    # MCP integer schema. Accept only canonical decimal strings at the boundary;
    # never truncate floats, accept booleans, or coerce arbitrary numeric text.
    if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,3}", value):
        return int(value)
    return value


Quantity = Annotated[
    int,
    BeforeValidator(native_integer),
    Field(
        strict=True,
        description="Integer quantity; Parlant canonical decimal strings normalized, never booleans or fractions",
    ),
]


def make_server(service: RetailService):
    mcp = FastMCP("Persistent retail demo")

    class SnapshotArguments(Middleware):
        async def on_call_tool(self, context, call_next):
            # Reject extra fields on the wire as well as in the native client.
            # Keep existing integer normalization on preparation unchanged.
            if context.message.name == "submit_confirmed_return":
                extra = set(context.message.arguments or {}) - {"operation_id", "session_token"}
                if extra:
                    raise ToolError("Snapshot submission accepts only operation_id and server context")
            return await call_next(context)

    mcp.add_middleware(SnapshotArguments())

    @mcp.tool
    async def list_my_orders() -> dict:
        """列出服务端绑定演示客户的订单。无需也不接受customer_id。"""
        return await asyncio.to_thread(service.list_my_orders)

    @mcp.tool
    async def get_order_details(order_id: str) -> dict:
        """查 PostgreSQL 订单与明细事实；拒绝其他客户订单。"""
        return await asyncio.to_thread(service.get_order_details, order_id)

    @mcp.tool
    async def check_return_eligibility(
        order_id: str,
        item_id: str,
        quantity: Quantity,
        reason: str,
        session_token: str | None = None,
        replaces_operation_id: str | None = None,
    ) -> dict:
        """检查退货资格并由服务器生成持久化操作键/确认口令；只准备，不提交申请。"""
        if load_settings().session_storage == "local_rollback":
            return {
                "ok": False,
                "data": None,
                "error": {
                    "code": "ROLLBACK_READ_ONLY",
                    "message": "Explicit rollback is read-only",
                    "details": {},
                },
            }
        return await asyncio.to_thread(
            service.check_return_eligibility, order_id, item_id, quantity, reason, session_token, replaces_operation_id
        )

    @mcp.tool
    async def submit_confirmed_return(operation_id: str, session_token: str | None = None) -> dict:
        """仅引用已展示并由真实用户确认的原operation_id；后端读取不可变快照提交，不接受订单、商品、数量、原因、金额或confirmed。成功不表示退款到账。"""
        if load_settings().session_storage == "local_rollback":
            return {
                "ok": False,
                "data": None,
                "error": {
                    "code": "ROLLBACK_READ_ONLY",
                    "message": "Explicit rollback is read-only",
                    "details": {},
                },
            }
        result = await asyncio.to_thread(
            service.submit_confirmed_return, operation_id, session_token
        )
        # Opt-in, one-shot integration fault: real DB commit, then process dies
        # before MCP can deliver a response. Never enabled by tool arguments.
        marker = os.environ.get("DEMO_TEST_DROP_COMMITTED_MCP_RESPONSE")
        if (
            marker
            and result["ok"]
            and Path(marker).exists()
            and Path(marker).read_text().strip() == operation_id
        ):
            Path(marker).unlink()
            os._exit(77)
        return result

    @mcp.tool
    async def get_return_request(request_id: str) -> dict:
        """查询绑定演示客户的申请编号、状态及 PostgreSQL 操作日志。"""
        return await asyncio.to_thread(service.get_return_request, request_id)

    @mcp.tool
    async def get_operation_result(operation_id: str) -> dict:
        """只读查询原退货操作及真实申请；已提交结果不受确认口令过期影响。不能创建或确认新操作。"""
        return await asyncio.to_thread(service.get_operation_result, operation_id)

    return mcp


def main():
    settings = load_settings()
    service = RetailService(
        settings.database_url, settings.customer_id, require_session_binding=True
    )
    # Fail early with a structured health result, never pretend to have a database.
    if not service.list_my_orders()["ok"]:
        raise SystemExit("PostgreSQL unavailable; initialize database first.")
    mcp = make_server(service)
    mcp.run(transport="streamable-http", host="127.0.0.1", port=settings.mcp_port, path="/mcp")


if __name__ == "__main__":
    main()
