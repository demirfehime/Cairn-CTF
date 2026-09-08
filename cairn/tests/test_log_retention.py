from __future__ import annotations

import os
import sys
import time

from cairn.dispatcher.config import LocalConfig
from cairn.dispatcher.runtime.local_backend import LocalBackend


def test_worker_output_is_saved_live_and_survives_workspace_cleanup(tmp_path):
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "runs"), completed_action="remove"))
    workspace = backend.ensure_running("proj_logs")
    runs = []
    for _ in range(2):
        process = backend.build_exec_process(workspace, {}, [sys.executable, "-u", "-c",
            "import sys,time; print('saved stdout', flush=True); print('saved stderr',file=sys.stderr,flush=True); time.sleep(1)"], timeout_seconds=10)
        process.start()
        runs.append(process.log_dir)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            path = process.log_dir / "stdout.log"
            if path.exists() and "saved stdout" in path.read_text(encoding="utf-8"):
                break
            time.sleep(0.02)
        else:
            raise AssertionError("Worker output was not persisted")
        result = process.communicate(timeout=10)
        assert result.stdout.strip() == "saved stdout"
        assert result.stderr.strip() == "saved stderr"
    assert runs[0] != runs[1]
    backend.cleanup_completed("proj_logs")
    for run in runs:
        assert (run / "stdout.log").read_text(encoding="utf-8").strip() == "saved stdout"
        assert (run / "stderr.log").read_text(encoding="utf-8").strip() == "saved stderr"


def test_history_listing_survives_restart_without_state(tmp_path, monkeypatch):
    from cairn.server.routers import logs
    monkeypatch.setattr(logs, "_launcher_root", lambda: tmp_path)
    for run in ["old-run", "new-run"]:
        directory = tmp_path / "logs" / run
        directory.mkdir(parents=True)
        (directory / "dispatcher.stderr.log").write_text(run, encoding="utf-8")
    entries = logs._runtime_entries("proj_logs")
    assert len(entries) == 2
    assert {entry["url"].split("run_id=")[1] for entry in entries} == {"old-run", "new-run"}
    assert all("dispatcher.stderr.log" in entry["local_path"] for entry in entries)


def test_worker_listing_keeps_project_boundaries(tmp_path, monkeypatch):
    from cairn.server.routers import logs
    monkeypatch.setattr(logs, "_launcher_root", lambda: tmp_path)
    for project in ["proj_001", "proj_001-identity", "proj_0012", "proj_002"]:
        directory = tmp_path / "logs/workers" / project / "execution"
        directory.mkdir(parents=True)
        (directory / "stdout.log").write_text(project, encoding="utf-8")
    entries = logs._worker_entries("proj_001")
    assert len(entries) == 2
    assert all("proj_0012" not in entry["url"] and "proj_002" not in entry["url"] for entry in entries)


def test_launcher_restart_appends_existing_logs(tmp_path):
    import json
    import subprocess
    from pathlib import Path
    runner = Path(__file__).resolve().parents[2] / "cairn-launcher-runner.py"
    output = tmp_path / "stdout.log"
    error = tmp_path / "stderr.log"
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"stdout": str(output), "stderr": str(error),
        "pid_file": str(tmp_path / "runner.pid"), "cwd": str(tmp_path),
        "executable": sys.executable,
        "arguments": ["-c", "import sys;print('restart output');print('restart error',file=sys.stderr)"]}), encoding="utf-8")
    for _ in range(2):
        subprocess.run([sys.executable, str(runner), str(spec)], check=True, timeout=10)
    assert output.read_text(encoding="utf-8").splitlines() == ["restart output"] * 2
    assert error.read_text(encoding="utf-8").splitlines() == ["restart error"] * 2
