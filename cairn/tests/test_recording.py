from __future__ import annotations

import json

from cairn import recording


class _Response:
    def __init__(self, *, payload=None, text: str = ""):
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        return None


def test_repeated_recording_does_not_replace_numbered_snapshots(tmp_path):
    recording._write_numbered_snapshot(tmp_path, {"run": "first"})
    recording._write_numbered_snapshot(tmp_path, {"run": "second"})
    assert json.loads((tmp_path / "project-0001.json").read_text()) == {"run": "first"}
    assert json.loads((tmp_path / "project-0002.json").read_text()) == {"run": "second"}


def test_record_project_writes_debug_snapshots_exports_and_manifest(tmp_path, monkeypatch) -> None:
    project = {
        "project": {
            "id": "proj_001",
            "title": "Juice Shop",
            "status": "active",
            "target_url": "http://127.0.0.1:3000",
            "agent_ids": ["agent_001", "agent_002"],
            "reason": {
                "worker": "agent_001",
                "trigger": "initial",
                "last_heartbeat_at": "2026-01-01T00:00:02Z",
            },
        },
        "facts": [{"id": "origin", "description": "start"}],
        "intents": [
            {
                "id": "i001",
                "from": ["origin"],
                "to": None,
                "description": "inspect authentication",
                "worker": "agent_002",
                "last_heartbeat_at": "2026-01-01T00:00:03Z",
            }
        ],
        "hints": [],
    }

    def fake_get(url, *, timeout, params=None):
        if url.endswith("/projects/proj_001"):
            assert params is None
            return _Response(payload=project)
        if params == {"format": "yaml"}:
            return _Response(text="project:\n  title: Juice Shop\n")
        if params == {"format": "timeline"}:
            return _Response(text="PROJECT CREATED\n")
        raise AssertionError((url, params, timeout))

    monkeypatch.setattr(recording.requests, "get", fake_get)
    result = recording.record_project(
        "http://127.0.0.1:8791/",
        "proj_001",
        tmp_path,
        follow=False,
    )

    assert result == project
    event = json.loads((tmp_path / "monitor.jsonl").read_text(encoding="utf-8"))
    assert event["target_url"] == "http://127.0.0.1:3000"
    assert event["agent_ids"] == ["agent_001", "agent_002"]
    assert event["counts"] == {"facts": 1, "intents": 1, "open_intents": 1, "hints": 0}
    assert event["active_intents"][0]["worker"] == "agent_002"
    assert json.loads((tmp_path / "project-0001.json").read_text(encoding="utf-8")) == project
    assert (tmp_path / "graph-latest.yaml").read_text(encoding="utf-8").startswith("project:")
    assert (tmp_path / "graph-final.yaml").exists()
    assert (tmp_path / "timeline-final.txt").read_text(encoding="utf-8") == "PROJECT CREATED\n"
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["project_id"] == "proj_001"
    assert manifest["samples"] == 1
    assert manifest["files"]["events"] == "monitor.jsonl"
