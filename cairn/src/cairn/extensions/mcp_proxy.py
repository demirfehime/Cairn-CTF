"""Expose configured upstream MCP tools to native CLI clients or the Pi bridge."""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from cairn.extensions.mcp_client import MCPConnections


async def main():
    config = json.loads(os.environ.get("CAIRN_EXTENSIONS_JSON", "{}"))
    async with MCPConnections(config.get("mcp_servers", {})) as clients:
        if "--json-lines" in sys.argv:
            while line := await asyncio.to_thread(sys.stdin.readline):
                request = json.loads(line)
                try:
                    if request["op"] == "list":
                        result = [tool.model_dump(by_alias=True, exclude_none=True) for tool in await clients.list_tools()]
                    elif request["op"] == "call":
                        result = (await clients.call_tool(request["name"], request.get("arguments", {}))).model_dump(by_alias=True, exclude_none=True)
                    else:
                        raise ValueError("Unknown operation")
                    print(json.dumps({"id": request["id"], "result": result}), flush=True)
                except Exception as exc:
                    # Transport messages may contain credentials: return only the type.
                    print(json.dumps({"id": request["id"], "error": type(exc).__name__}), flush=True)
            return
        server = Server("cairn-mcp")

        @server.list_tools()
        async def list_tools():
            return await clients.list_tools()

        @server.call_tool()
        async def call_tool(name, arguments):
            return await clients.call_tool(name, arguments)

        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
