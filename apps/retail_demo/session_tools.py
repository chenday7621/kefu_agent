"""App-scoped adapter: trusted ToolContext -> one-use DB capability -> real MCP.

Native MCP owns HTTP discovery/calls/lifecycle. The adapter hides its internal
capability from the model's schema and never trusts model-supplied identity.
"""

import asyncio
from dataclasses import replace
from parlant.core.services.tools.mcp_service import MCPToolClient
from .business import BusinessError


class SessionBoundMCP(MCPToolClient):
    def __init__(self, native, business):
        self.native, self.business = native, business
        # Retain native MCP type/URL introspection used by the services API.
        # Connection ownership stays with the registry's original client.
        self.url, self.port = native.url, native.port
        self.endpoint_url = native.endpoint_url

    def visible(self, tool):
        return replace(
            tool, parameters={k: v for k, v in tool.parameters.items() if k != "session_token"}
        )

    async def list_tools(self):
        return [self.visible(t) for t in await self.native.list_tools()]

    async def read_tool(self, name):
        return self.visible(await self.native.read_tool(name))

    async def resolve_tool(self, name, context):
        return await self.read_tool(name)

    async def call_tool(self, name, context, arguments):
        if (
            context.customer_id != self.business.customer_id
            or context.agent_id != "retail-persistent-demo"
        ):
            raise BusinessError("SESSION_FORBIDDEN", "不可信的业务工具上下文。")
        args = {k: v for k, v in arguments.items() if k != "session_token"}
        if name in ("check_return_eligibility", "submit_confirmed_return"):
            args["session_token"] = await asyncio.to_thread(
                self.business.issue_tool_context, context.session_id
            )
        if name in ("submit_confirmed_return", "get_operation_result"):
            owned = await asyncio.to_thread(
                self.business.session_operation, context.session_id, args.get("operation_id")
            )
            if not owned["ok"]:
                raise BusinessError("SESSION_FORBIDDEN", "操作没有关联到当前会话。")
        # Original MCPToolClient validates against the full wire schema and sends
        # actual Streamable HTTP; no Python business function replaces the call.
        return await self.native.call_tool(name, context, args)


def install_session_adapter(registry, name, native, business):
    adapter = SessionBoundMCP(native, business)
    read = registry.read_tool_service

    async def read_service(service_name):
        return adapter if service_name == name else await read(service_name)

    registry.read_tool_service = read_service
    return adapter
