from __future__ import annotations

import json
from contextlib import nullcontext

import pytest
from fastapi import HTTPException

from cairn.dispatcher.runtime.execution_trace import persist_execution_trace
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks.common import run_worker_process
from cairn.server.routers import logs
from conftest import make_config


def test_worker_invocation_retains_prompt_output_and_redacts_manifest(tmp_path):
    result = ProcessResult(1, "中文 output", "failure", timed_out=True)
    destination = tmp_path / "logs/workers/proj_001/run_001"

    class Process:
        log_dir = destination

        def start(self):
            pass

        def communicate(self, timeout):
            return result

    class Backend:
        def build_exec_process(self, *args, **kwargs):
            return Process()

    actual = run_worker_process(
        Backend(), "proj_001", make_config().workers[0],
        ["worker", "--api-key", "secret-value", "--token=other-secret"],
        phase="bootstrap", timeout_seconds=1, prompt="完整 prompt", stdin_text="input",
    )
    assert actual is result
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["argv"] == ["worker", "--api-key", "<redacted>", "--token=<redacted>"]
    assert manifest["timed_out"] is True
    assert manifest["returncode"] == 1
    assert manifest["stdout_bytes"] == len(result.stdout.encode("utf-8"))
    assert (destination / "prompt.txt").read_text(encoding="utf-8") == "完整 prompt"
    assert (destination / "stdin.txt").read_text(encoding="utf-8") == "input"
    assert (destination / "stdout.log").read_text(encoding="utf-8") == result.stdout
    assert (destination / "stderr.log").read_text(encoding="utf-8") == result.stderr


def test_container_trace_fallback_keeps_executions_separate(tmp_path):
    kwargs = dict(
        trace_dir=None, fallback_root=tmp_path, container_name="proj_001",
        worker_name="mock", phase="reason", argv=["worker"], prompt=None,
        stdin_text=None, result=ProcessResult(0, "", ""), started_at="2026-09-11T00:00:00Z",
    )
    first = persist_execution_trace(**kwargs)
    second = persist_execution_trace(**kwargs)
    assert first != second
    assert first.is_relative_to(tmp_path)
    manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["prompt_file"] is None
    assert manifest["stdin_file"] is None


def test_new_trace_files_remain_scoped_to_project(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "_launcher_root", lambda: tmp_path)
    monkeypatch.setattr(logs, "get_conn", nullcontext)
    monkeypatch.setattr(logs, "get_project_or_404", lambda conn, project: None)
    for project in ("proj_001", "proj_0012"):
        directory = tmp_path / "logs/workers" / project / "run_001"
        directory.mkdir(parents=True)
        for name in ("manifest.json", "prompt.txt", "stdin.txt", "private.txt"):
            (directory / name).write_text("record", encoding="utf-8")
    entries = logs._worker_entries("proj_001")
    assert len(entries) == 3
    assert all("proj_0012" not in entry["url"] for entry in entries)
    assert logs.view_worker_log("proj_001", "proj_001/run_001/prompt.txt").status_code == 200
    for path in ("proj_0012/run_001/prompt.txt", "proj_001/run_001/private.txt", "../run_001/prompt.txt"):
        with pytest.raises(HTTPException) as error:
            logs.view_worker_log("proj_001", path)
        assert error.value.status_code == 404
