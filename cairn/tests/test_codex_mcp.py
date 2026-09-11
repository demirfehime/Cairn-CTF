from __future__ import annotations

from typing import Any

from cairn import codex_mcp


class _Response:
    def __init__(self, payload: Any):
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def test_mcp_registers_codex_facing_tools() -> None:
    assert set(codex_mcp.MCP._tool_manager._tools) == {
        "cairn_status",
        "cairn_list_projects",
        "cairn_create_project",
        "cairn_get_project",
        "cairn_export_project",
        "cairn_add_hint",
        "cairn_set_project_status",
        "cairn_reopen_project",
    }


def test_create_project_translates_plain_hints(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_request(method: str, path: str, **kwargs) -> _Response:
        captured.update(method=method, path=path, **kwargs)
        return _Response({"project": {"id": "proj_001"}})

    monkeypatch.setattr(codex_mcp, "_request", fake_request)

    result = codex_mcp.cairn_create_project(
        "title",
        "origin",
        "goal",
        ["first", "second"],
        False,
    )

    assert result["project"]["id"] == "proj_001"
    assert captured["method"] == "POST"
    assert captured["path"] == "/projects"
    assert captured["json"] == {
        "title": "title",
        "origin": "origin",
        "goal": "goal",
        "bootstrap_enabled": False,
        "attachment_ids": [],
        "hints": [
            {"content": "first", "creator": "codex"},
            {"content": "second", "creator": "codex"},
        ],
    }

