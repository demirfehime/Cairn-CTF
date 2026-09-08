from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import timedelta
import hashlib
import os
import json
from pathlib import Path
from mcp.types import Tool, CallToolResult, TextContent
from cairn.extensions.trace import record

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from cairn.extensions.config import MCPServer


class MCPConnections:
    """Keep upstream sessions alive for the entire Agent process."""

    def __init__(self, servers: dict):
        self.servers = servers
        self.stack = AsyncExitStack()
        self.sessions = {}
        self.tool_map = {}
        self.skills = json.loads(os.environ.get('CAIRN_SKILL_MANIFEST', '[]'))

    async def __aenter__(self):
        await self.stack.__aenter__()
        try:
            for name, raw in self.servers.items():
                config = MCPServer.model_validate(raw)
                if not config.enabled:
                    continue
                if config.transport == "stdio":
                    env = {key: os.environ[key] for key in config.env_vars if key in os.environ}
                    env.update(config.env)
                    streams = await self.stack.enter_async_context(stdio_client(StdioServerParameters(
                        command=config.command, args=config.args, env=env, cwd=os.getcwd(),
                    )))
                elif config.transport == "sse":
                    streams = await self.stack.enter_async_context(sse_client(config.url, headers=config.headers, timeout=15))
                else:
                    streams = await self.stack.enter_async_context(streamablehttp_client(config.url, headers=config.headers, timeout=15))
                session = await self.stack.enter_async_context(ClientSession(streams[0], streams[1], read_timeout_seconds=timedelta(seconds=60)))
                await session.initialize()
                self.sessions[name] = session
            return self
        except BaseException:
            await self.stack.__aexit__(*__import__("sys").exc_info())
            raise

    async def __aexit__(self, *args):
        return await self.stack.__aexit__(*args)

    async def list_tools(self):
        tools = []
        if self.skills:
            tools.append(Tool(name='cairn_read_skill', description='Read a configured Skill instruction file before using it. Available: ' + ', '.join(item['name'] for item in self.skills), inputSchema={'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name']}))
        self.tool_map.clear()
        for server, session in self.sessions.items():
            cursor = None
            while True:
                result = await session.list_tools(cursor=cursor)
                for tool in result.tools:
                    name = "mcp_" + hashlib.sha256(f"{server}\0{tool.name}".encode()).hexdigest()[:24]
                    self.tool_map[name] = (server, tool.name)
                    label = self.servers[server].get('display_name') or server
                    tools.append(tool.model_copy(update={"name": name, "description": f"[{label}/{tool.name}] {tool.description or ''}"}))
                cursor = result.nextCursor
                if not cursor:
                    break
        return tools

    async def call_tool(self, name: str, arguments: dict):
        if name == 'cairn_read_skill':
            skill = next((item for item in self.skills if item['name'] == arguments.get('name')), None)
            if skill is None:
                raise ValueError('Unknown configured Skill')
            text = Path(skill['path']).read_text(encoding='utf-8-sig')
            record('skill', skill['name'], 'read', 'read')
            return CallToolResult(content=[TextContent(type='text', text=f"Skill file: {skill['path']}\nResolve references relative to this file.\n\n{text}")])
        if name not in self.tool_map:
            await self.list_tools()
        server, tool = self.tool_map[name]
        label = self.servers[server].get('display_name') or server
        try:
            result = await self.sessions[server].call_tool(tool, arguments)
        except Exception:
            record('mcp', label, tool, 'error')
            raise
        record('mcp', label, tool, 'error' if result.isError else 'success')
        return result
