from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import requests

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server.models import ProjectDetail, ProjectSummary

LOG = logging.getLogger(__name__)
CHILD_RESTART_DELAYS_SECONDS = (5, 15, 30, 60, 300)
CHILD_MAX_CONSECUTIVE_FAILURES = 5
CHILD_STABLE_RUN_SECONDS = 60


@dataclass(slots=True)
class ChildProcess:
    project_id: str
    process: subprocess.Popen[str]
    stdout_handle: object
    stderr_handle: object
    log_dir: Path
    started_at: float


class ChildProcessManager:
    def __init__(self, config_path: Path, config: DispatchConfig, client: CairnClient):
        self.config_path = config_path
        self.config = config
        self.client = client
        root = config.local.workspace_root if config.local else None
        self.workspace_root = Path(root).expanduser() if root else Path.cwd()
        self.processes: dict[str, ChildProcess] = {}
        self.restart_after: dict[str, float] = {}
        self.failure_counts: dict[str, int] = {}
        self.suspended_until_inactive: set[str] = set()

    def sync(self, children: list[ProjectSummary]) -> None:
        for child in children:
            if child.status != "active":
                self.suspended_until_inactive.discard(child.id)
                self.failure_counts.pop(child.id, None)
                self.restart_after.pop(child.id, None)
        desired = {
            child.id
            for child in children
            if child.status == "active" and child.agents and child.kind == "child"
        }
        for project_id in list(self.processes):
            managed = self.processes[project_id]
            returncode = managed.process.poll()
            if project_id not in desired:
                self._stop(project_id, "Child inactive")
                continue
            if returncode is not None:
                runtime = max(0.0, time.time() - managed.started_at)
                self._close_handles(managed)
                self.processes.pop(project_id, None)
                previous = 0 if runtime >= CHILD_STABLE_RUN_SECONDS else self.failure_counts.get(project_id, 0)
                failures = previous + 1
                self.failure_counts[project_id] = failures
                delay = CHILD_RESTART_DELAYS_SECONDS[min(failures - 1, len(CHILD_RESTART_DELAYS_SECONDS) - 1)]
                error = (
                    f"Child dispatcher exited with code {returncode}; "
                    f"consecutive failures={failures}"
                )
                self.client.update_child_process(
                    project_id,
                    "failed",
                    log_dir=str(managed.log_dir),
                    error=error,
                )
                if failures >= CHILD_MAX_CONSECUTIVE_FAILURES:
                    self.suspended_until_inactive.add(project_id)
                    self.restart_after.pop(project_id, None)
                    self.client.update_project_status(project_id, "paused")
                    LOG.error(
                        "Child dispatcher paused after repeated failures child=%s failures=%s code=%s",
                        project_id,
                        failures,
                        returncode,
                    )
                else:
                    self.restart_after[project_id] = time.time() + delay
                    LOG.warning(
                        "Child dispatcher exited child=%s code=%s failures=%s retry_in=%ss",
                        project_id,
                        returncode,
                        failures,
                        delay,
                    )

            elif time.time() - managed.started_at >= CHILD_STABLE_RUN_SECONDS:
                self.failure_counts.pop(project_id, None)

        for project_id in sorted(desired):
            if project_id in self.processes:
                continue
            if project_id in self.suspended_until_inactive:
                continue
            if self.restart_after.get(project_id, 0) > time.time():
                continue
            self._start(project_id)

    def close(self) -> None:
        for project_id in list(self.processes):
            self._stop(project_id, "Parent dispatcher stopping")

    def _start(self, project_id: str) -> None:
        log_dir = self.workspace_root / project_id / ".cairn-child" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / "dispatcher.stdout.log"
        stderr_path = log_dir / "dispatcher.stderr.log"
        stdout_handle = stdout_path.open("a", encoding="utf-8")
        stderr_handle = stderr_path.open("a", encoding="utf-8")
        command = [
            sys.executable,
            "-m",
            "cairn.cli",
            "child-dispatch",
            "--config",
            str(self.config_path),
            "--project-id",
            project_id,
        ]
        options: dict[str, object] = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        child_env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "LANG": "C.UTF-8",
        }
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.config_path.parent),
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                env=child_env,
                **options,
            )
        except Exception:
            stdout_handle.close()
            stderr_handle.close()
            raise
        managed = ChildProcess(
            project_id,
            process,
            stdout_handle,
            stderr_handle,
            log_dir,
            time.time(),
        )
        self.processes[project_id] = managed
        self.restart_after.pop(project_id, None)
        self.client.update_child_process(
            project_id, "running", pid=process.pid, log_dir=str(log_dir)
        )
        LOG.info("started Child dispatcher child=%s pid=%s logs=%s", project_id, process.pid, log_dir)

    def _stop(self, project_id: str, reason: str) -> None:
        managed = self.processes.pop(project_id, None)
        if managed is None:
            return
        process = managed.process
        if process.poll() is None:
            if os.name == "nt":
                with suppress(Exception):
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=15,
                        check=False,
                    )
            else:
                with suppress(ProcessLookupError):
                    process.terminate()
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
                if process.poll() is None:
                    process.kill()
        self._close_handles(managed)
        self.client.update_child_process(
            project_id, "stopped", log_dir=str(managed.log_dir), error=reason
        )
        LOG.info("stopped Child dispatcher child=%s reason=%s", project_id, reason)

    @staticmethod
    def _close_handles(managed: ChildProcess) -> None:
        with suppress(Exception):
            managed.stdout_handle.close()
        with suppress(Exception):
            managed.stderr_handle.close()


class ParentOrchestrator:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.parent_loop = DispatcherLoop(config_path, scope_kind="parent")
        self.config = self.parent_loop.config
        self.client = self.parent_loop.client
        self.children = ChildProcessManager(config_path, self.config, self.client)
        self._last_parent_review: dict[str, float] = {}

    def run(self, once: bool = False) -> None:
        try:
            self.parent_loop.run_startup_healthchecks()
            while True:
                try:
                    self.parent_loop.run_iteration()
                    self._sync_hierarchy()
                except requests.RequestException as exc:
                    if once:
                        raise
                    LOG.warning(
                        "Parent orchestrator request failed error=%s retry_in=%ss",
                        exc,
                        self.config.runtime.interval,
                    )
                if once:
                    break
                time.sleep(self.config.runtime.interval)
        finally:
            self.children.close()
            self.parent_loop.close()

    def _sync_hierarchy(self) -> None:
        parents = self.client.list_projects(kind="parent")
        all_children: list[ProjectSummary] = []
        for parent in parents:
            if parent.status == "active":
                self.client.materialize_children(parent.id)
                self.client.rebalance_allocations(parent.id)
            children = self.client.list_projects(kind="child", parent_id=parent.id)
            all_children.extend(children)
            for child in children:
                self._publish_new_facts(child)
                if child.status == "completed":
                    self._sync_completion(child)
        self.children.sync(all_children)

    def _publish_new_facts(self, child: ProjectSummary) -> None:
        detail = self.client.get_project(child.id)
        for fact in detail.facts:
            if fact.id in {"origin", "goal"}:
                continue
            response = self.client.publish_child_fact(child.id, fact.id, fact.description)
            if not response.ok:
                LOG.warning(
                    "failed to publish Child Fact child=%s fact=%s status=%s body=%s",
                    child.id,
                    fact.id,
                    response.status_code,
                    response.text,
                )

    def _sync_completion(self, child: ProjectSummary) -> None:
        detail: ProjectDetail = self.client.get_project(child.id)
        description = child.summary
        if not description:
            completion = next((intent for intent in detail.intents if intent.to == "goal"), None)
            description = completion.description if completion else f"Child {child.id} completed"
        response = self.client.sync_child_completion(
            child.id, description, breakthrough=child.breakthrough
        )
        if not response.ok:
            LOG.warning(
                "failed to sync Child completion child=%s status=%s body=%s",
                child.id,
                response.status_code,
                response.text,
            )
