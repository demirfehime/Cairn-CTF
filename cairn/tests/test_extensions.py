from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

from cairn.extensions.config import KEEP_SECRET
from cairn.extensions.mcp_client import MCPConnections
from cairn.extensions.runtime import prepare_extensions
from cairn.server import db
from cairn.server.app import app
from cairn.server.models import AgentRuntime
from cairn.dispatcher.scheduler.loop import _server_agent_worker


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as client:
        yield client


@pytest.fixture
def skill(tmp_path):
    folder = tmp_path / "sample-skill"
    folder.mkdir()
    (folder / "SKILL.md").write_text("---\nname: sample\ndescription: Sample workflow\n---\nUse the MCP echo tool.\n", encoding="utf-8")
    (folder / "reference.txt").write_text("reference", encoding="utf-8")
    return folder


@pytest.fixture
def mcp_server(tmp_path):
    path = tmp_path / "echo_mcp.py"
    path.write_text('''from mcp.server.fastmcp import FastMCP
import sys
server = FastMCP("echo", host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8000)
count = 0
@server.tool()
def echo(text: str) -> str:
    global count
    count += 1
    return f"{count}:{text}"
server.run(transport=sys.argv[2] if len(sys.argv) > 2 else "stdio")
''', encoding="utf-8")
    return {"echo": {"command": sys.executable, "args": [str(path)], "env": {"TEST_TOKEN": "private-test-token"}}}


def test_extensions_roundtrip_mask_secrets_and_reach_worker(client, skill, mcp_server):
    body = {"name": "configured", "base_url": "https://model.test/v1", "api_key": "model-test-key", "model": "test",
            "skill_paths": [str(skill)], "mcp_servers": mcp_server}
    response = client.post("/agents", json=body)
    assert response.status_code == 201, response.text
    saved = response.json()
    assert "private-test-token" not in response.text
    assert saved["mcp_servers"]["echo"]["env"]["TEST_TOKEN"] == KEEP_SECRET
    body["mcp_servers"] = saved["mcp_servers"]
    body.pop("api_key")
    assert client.put(f"/agents/{saved['id']}", json=body).status_code == 200
    body.pop("mcp_servers")
    body.pop("skill_paths")
    assert client.put(f"/agents/{saved['id']}", json=body).status_code == 200
    runtime = AgentRuntime(**client.get("/agents/runtime").json()[0])
    assert runtime.mcp_servers["echo"].env["TEST_TOKEN"] == "private-test-token"
    config = json.loads(_server_agent_worker(runtime).env["CAIRN_EXTENSIONS_JSON"])
    assert config["skill_paths"] == [str(skill / "SKILL.md")]
    tested = client.post(f"/agents/{saved['id']}/extensions/test")
    assert tested.status_code == 200, tested.text
    assert len(tested.json()["tools"]) == 1
    assert "private-test-token" not in tested.text
    body.update(skill_paths=[], mcp_servers={})
    assert client.put(f"/agents/{saved['id']}", json=body).status_code == 200
    assert client.get("/agents/runtime").json()[0]["mcp_servers"] == {}


def test_invalid_skill_or_mcp_is_rejected(client):
    body = {"name": "bad", "base_url": "https://model.test", "api_key": "key", "model": "m"}
    assert client.post("/agents", json={**body, "skill_paths": ["relative"]}).status_code == 422
    assert client.post("/agents", json={**body, "mcp_servers": {"bad": {"transport": "http", "url": "file:///tmp"}}}).status_code == 422


@pytest.mark.parametrize("transport,path", [("http", "/mcp"), ("sse", "/sse")])
def test_remote_mcp_transports(mcp_server, transport, path):
    import socket
    import subprocess
    import time
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command = [sys.executable, *mcp_server["echo"]["args"], str(port), "streamable-http" if transport == "http" else "sse"]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.2):
                    break
            except OSError:
                assert process.poll() is None
                assert time.monotonic() < deadline, "MCP server startup timed out"
                time.sleep(.05)
        async def run():
            async with MCPConnections({"remote": {"transport": transport, "url": f"http://127.0.0.1:{port}{path}"}}) as clients:
                tools = await clients.list_tools()
                result = await clients.call_tool(tools[0].name, {"text": transport})
                assert result.content[0].text == f"1:{transport}"
        asyncio.run(run())
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize("binary", ["codex", "claude"])
def test_host_configuration_injects_skills_and_native_mcp_without_secret_args(tmp_path, skill, mcp_server, binary):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = {"CAIRN_EXTENSIONS_JSON": json.dumps({"skill_paths": [str(skill)], "mcp_servers": mcp_server})}
    command = [binary, "exec", "--", "original prompt"] if binary == "codex" else [binary, "-p", "--", "original prompt"]
    updated, stdin = prepare_extensions(str(workspace), env, command, None)
    assert command[-1] == "original prompt"
    assert "User-configured Skills" in updated[-1]
    assert "sample: Sample workflow" in updated[-1]
    assert len(list(workspace.glob(".cairn-extensions/*/skills/*/reference.txt"))) == 1
    assert "private-test-token" not in " ".join(updated)
    assert "mcp_servers.cairn_extensions.command" in " ".join(updated) if binary == "codex" else "--mcp-config" in updated
    if binary == "codex":
        import tomllib
        for i, arg in enumerate(updated):
            if arg == "-c":
                tomllib.loads(updated[i + 1])


def test_real_mcp_session_survives_multiple_calls(mcp_server):
    async def run():
        async with MCPConnections(mcp_server) as clients:
            tools = await clients.list_tools()
            assert len(tools) == 1
            one = await clients.call_tool(tools[0].name, {"text": "first"})
            two = await clients.call_tool(tools[0].name, {"text": "second"})
            assert one.content[0].text == "1:first"
            assert two.content[0].text == "2:second"
    asyncio.run(run())


def test_native_stdio_proxy_forwards_tool_calls(mcp_server):
    async def run():
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        env = {"CAIRN_EXTENSIONS_JSON": json.dumps({"mcp_servers": mcp_server}),
               "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        async with stdio_client(StdioServerParameters(command=sys.executable, args=["-m", "cairn.extensions.mcp_proxy"], env=env)) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool(tools.tools[0].name, {"text": "native"})
                assert not result.isError
                assert result.content[0].text == "1:native"
    asyncio.run(run())


def test_pi_agent_calls_mcp_tool_and_receives_skill_index(tmp_path, skill, mcp_server):
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from cairn.dispatcher.config import LocalConfig, WorkerConfig
    from cairn.dispatcher.runtime.local_backend import LocalBackend
    from cairn.dispatcher.workers.registry import get_driver

    repo = Path(__file__).resolve().parents[2]
    if not (repo / ".cairn-launcher/pi-runtime/node_modules/@mariozechner/pi-coding-agent/dist/cli.js").is_file():
        pytest.skip("project-local Pi CLI not installed")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if not any(item["role"] == "tool" for item in body["messages"]):
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_skill", "type": "function", "function": {"name": "cairn_read_skill", "arguments": '{"name":"sample"}'}}]}
                finish = "tool_calls"
            elif not any(item["role"] == "tool" and "1:from-pi" in str(item.get("content")) for item in body["messages"]):
                tool = next(tool["function"] for tool in body["tools"] if "[echo/echo]" in tool["function"].get("description", ""))
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_echo", "type": "function", "function": {"name": tool["name"], "arguments": '{"text":"from-pi"}'}}]}
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": "mcp-and-skills-ok"}
                finish = "stop"
            for change, reason in [(delta, None), ({}, finish)]:
                chunk = {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
                         "choices": [{"index": 0, "delta": change, "finish_reason": reason}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {"PI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1", "PI_API_KEY": "local-test-key",
           "PI_MODEL": "test-model", "PI_PROVIDER_API": "openai-completions", "NO_PROXY": "127.0.0.1,localhost",
           "CAIRN_EXTENSIONS_JSON": json.dumps({"skill_paths": [str(skill)], "mcp_servers": mcp_server})}
    worker = WorkerConfig(name="pi", type="pi", task_types=["reason"], max_running=1, priority=0, env=env)
    driver = get_driver("pi", "api")
    command = driver.build_execute(worker, "Use the sample skill and call echo", None)
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "runs")))
    workspace = backend.ensure_running("proj_test")
    process = backend.build_exec_process(workspace, env, command.argv, stdin_text=command.stdin_text)
    try:
        # Run the same prepared host command with a test timeout and no paid API.
        result = subprocess.run(process.command, cwd=workspace, env=process.env, input=process._stdin_text,
                                text=True, encoding="utf-8", capture_output=True, timeout=35)
        assert result.returncode == 0, result.stderr
        assert driver.extract_response_text(result.stdout, result.stderr) == "mcp-and-skills-ok", result.stdout
        assert len(requests) == 3
        assert "User-configured Skills" in json.dumps(requests[0]["messages"])
        assert "Use the MCP echo tool" in json.dumps(requests[1]["messages"])
        assert "1:from-pi" in json.dumps(requests[2]["messages"])
        events = [json.loads(line) for line in Path(process.env['CAIRN_EXTENSION_TRACE']).read_text().splitlines()]
        assert [event['kind'] for event in events] == ['skill', 'mcp']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
