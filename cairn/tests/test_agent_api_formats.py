from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from cairn.server import db
from cairn.server.app import app
from cairn.server.models import AgentRuntime
from cairn.server.routers import agents
from cairn.dispatcher.scheduler.loop import _server_agent_worker
from cairn.dispatcher.workers.registry import get_driver


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("api_format,driver,base_key,base", [
    ("anthropic", "claudecode", "ANTHROPIC_BASE_URL", "https://gateway.test"),
    ("openai-chat", "pi", "PI_BASE_URL", "https://gateway.test/v1"),
    ("openai-responses", "codex", "CODEX_BASE_URL", "https://gateway.test/v1"),
])
def test_format_persists_and_routes_independently_of_model_name(client, api_format, driver, base_key, base):
    body = dict(name="model", model="claude-compatible", base_url="https://gateway.test",
                api_key="test-key", api_format=api_format)
    result = client.post("/agents", json=body)
    assert result.status_code == 201, result.text
    saved = result.json()
    assert saved["api_format"] == api_format
    assert "api_key" not in saved
    runtime = client.get("/agents/runtime").json()[0]
    worker = _server_agent_worker(AgentRuntime(**runtime))
    assert worker.type == driver
    assert worker.env[base_key] == base
    if api_format == "anthropic":
        assert worker.env["ANTHROPIC_API_KEY"] == "test-key"
    if api_format == "openai-chat":
        assert worker.env["PI_PROVIDER_API"] == "openai-completions"
        command = get_driver(worker.type, "api").build_execute(worker, "long prompt", None)
        assert "/bin/sh" not in command.argv
        assert "test-key" not in " ".join(command.argv)
        assert json.loads(command.stdin_text)["prompt"] == "long prompt"
    # An older client updating an unrelated field must keep the saved format/key.
    body.pop("api_key")
    body.pop("api_format")
    result = client.put(f"/agents/{saved['id']}", json=body)
    assert result.status_code == 200
    assert result.json()["api_format"] == api_format
    body["api_format"] = "openai-responses"
    assert client.put(f"/agents/{saved['id']}", json=body).json()["api_format"] == "openai-responses"
    assert client.get("/agents/runtime").json()[0]["api_key"] == "test-key"


@pytest.mark.parametrize("api_format", ["anthropic", "openai-chat", "openai-responses"])
def test_discovery_uses_selected_auth_on_custom_gateway(client, monkeypatch, api_format):
    class Response:
        status_code = 200
        def json(self):
            return {"data": [{"id": "custom-model"}]}
    calls = []
    def get(url, headers, timeout):
        calls.append((url, headers))
        return Response()
    monkeypatch.setattr(agents.requests, "get", get)
    suffix = {"anthropic": "messages", "openai-chat": "chat/completions", "openai-responses": "responses"}[api_format]
    response = client.post("/agents/discover-models", json={
        "base_url": f"https://gateway.test/v1/{suffix}", "api_key": "test-key", "api_format": api_format,
    })
    assert response.json() == {"models": ["custom-model"]}
    assert calls[0][0] == "https://gateway.test/v1/models"
    assert len(calls) == 1
    if api_format == "anthropic":
        assert calls[0][1]["x-api-key"] == "test-key"
        assert calls[0][1]["anthropic-version"] == "2023-06-01"
    else:
        assert calls[0][1]["Authorization"] == "Bearer test-key"


def test_unknown_format_rejected(client):
    response = client.post("/agents", json=dict(name="bad", model="m", base_url="https://gateway.test",
                                              api_key="test-key", api_format="unsupported"))
    assert response.status_code == 422


def test_legacy_agent_migration_keeps_key_and_codex_routing(client):
    result = client.post("/agents", json=dict(name="legacy", model="claude-compatible",
                         base_url="https://gateway.test/v1", api_key="legacy-test-key"))
    assert result.status_code == 201
    with db.get_conn() as conn:
        conn.execute("ALTER TABLE agents DROP COLUMN api_format")
        db._ensure_orchestration_columns(conn)
        db._ensure_orchestration_columns(conn)
    runtime = AgentRuntime(**client.get("/agents/runtime").json()[0])
    assert runtime.api_format == "auto"
    assert runtime.api_key == "legacy-test-key"
    assert _server_agent_worker(runtime).type == "codex"


@pytest.mark.parametrize("api_format,suffix", [
    ("anthropic", "/v1/messages"),
    ("openai-chat", "/v1/chat/completions"),
    ("openai-responses", "/v1/responses"),
])
def test_full_endpoint_health_request(client, monkeypatch, api_format, suffix):
    calls = []
    class Response:
        status_code = 200
        text = "ok"
    def post(url, headers, json, timeout, proxies):
        calls.append((url, headers, json))
        return Response()
    monkeypatch.setattr(agents.requests, "post", post)
    result = client.post("/agents", json=dict(name="health", model="arbitrary-model",
                         base_url="https://gateway.test" + suffix, api_key="test-key", api_format=api_format))
    assert result.status_code == 201
    worker = _server_agent_worker(AgentRuntime(**client.get("/agents/runtime").json()[0]))
    assert get_driver(worker.type, "api").check_health(worker, timeout=5).ok
    assert calls[0][0] == "https://gateway.test" + suffix
    assert calls[0][2]["model"] == "arbitrary-model"
    assert ("input" if api_format == "openai-responses" else "messages") in calls[0][2]
    assert calls[0][1].get("x-api-key" if api_format == "anthropic" else "Authorization") == (
        "test-key" if api_format == "anthropic" else "Bearer test-key")


def test_host_pi_sends_chat_request_and_resumes_session(tmp_path, monkeypatch):
    """Exercise the real CLI against a local stub; no provider key or spend."""
    import os
    from pathlib import Path
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    repo = Path(__file__).resolve().parents[2]
    if not (repo / ".cairn-launcher/pi-runtime/node_modules/@mariozechner/pi-coding-agent/dist/cli.js").exists():
        pytest.skip("project-local Pi CLI is not installed")
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append((self.path, self.headers.get("Authorization"), body))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in [({"role": "assistant", "content": "format-ok"}, None), ({}, "stop")]:
                chunk = {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1,
                         "model": "test-model", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {**os.environ, "PI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
           "PI_API_KEY": "local-test-key", "PI_MODEL": "test-model", "PI_PROVIDER_API": "openai-completions",
           "NO_PROXY": "127.0.0.1,localhost"}
    driver = get_driver("pi", "api")
    try:
        session = None
        for prompt in ["Reply format-ok", "Repeat the previous reply"]:
            result = subprocess.run(
                [sys.executable, "-m", "cairn.dispatcher.workers.adapters.pi_runner"],
                input=json.dumps({"prompt": prompt, "session": session, "agent_dir": ["test-agent", "assignment-0"]}),
                cwd=tmp_path, env=env, text=True, encoding="utf-8", capture_output=True, timeout=40,
            )
            assert result.returncode == 0, result.stderr
            assert driver.extract_response_text(result.stdout, result.stderr) == "format-ok", result.stdout
            session = driver.extract_session(session, result.stdout, result.stderr)
            assert session
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert len(captured) == 2
    assert all(path == "/v1/chat/completions" and auth == "Bearer local-test-key" for path, auth, _ in captured)
    assert all(body["model"] == "test-model" for _, _, body in captured)
    assert any(message["role"] == "assistant" for message in captured[1][2]["messages"])
