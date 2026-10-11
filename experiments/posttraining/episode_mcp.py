"""Real FastMCP server bound to one owned DEV database and customer."""

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    task = json.loads((Path(config["directory"]) / "TASK.json").read_text())
    os.environ["DEMO_DATABASE_URL"] = config["database_url"]
    os.environ["DEMO_SESSION_STORAGE"] = "postgres"
    os.environ["DEMO_DISABLE_LLM"] = "1"
    from apps.retail_demo.business import RetailService
    from apps.retail_demo.mcp_server import make_server
    from fastmcp.server.middleware import Middleware
    from .trajectory import redact
    from uuid import uuid4

    class InvocationCounter(Middleware):
        async def on_call_tool(self, context, call_next):
            entry = {"invocation_id": uuid4().hex, "tool_name": context.message.name,
                     "arguments": redact(context.message.arguments),
                     "purpose": "wire-count-only; causal attribution uses native ToolCall/ToolResult IDs"}
            with (Path(config["directory"]) / "MCP_INVOCATIONS.jsonl").open("a") as out:
                out.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return await call_next(context)
    service = RetailService(config["database_url"], task["initial_db_fixture"]["trusted_context"]["customer_id"],
                            require_session_binding=True)
    server = make_server(service)
    server.add_middleware(InvocationCounter())
    server.run(transport="streamable-http", host="127.0.0.1", port=config["mcp_port"], path="/mcp")


if __name__ == "__main__":
    main()
