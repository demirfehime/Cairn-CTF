from __future__ import annotations

import logging
import json
import shutil
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests
import yaml

from cairn.dispatcher.config import DispatchConfig, LocalConfig, WorkerConfig
from cairn.dispatcher.models import ReasonCheckpoint, RunningTask
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.startup_healthcheck import format_failure_summary, run_startup_healthchecks
from cairn.dispatcher.scheduler.worker_select import choose_worker
from cairn.dispatcher.workers.registry import get_driver
from cairn.dispatcher.tasks.bootstrap import run_bootstrap_task
from cairn.dispatcher.tasks.explore import run_explore_task
from cairn.dispatcher.tasks.reason import run_reason_task
from cairn.server.models import AgentRuntime, Intent, ProjectDetail, ProjectSummary

LOG = logging.getLogger(__name__)
UNHEALTHY_RETRY_AFTER_SECONDS = 30
REJECTED_RETRY_AFTER_SECONDS = 5
BOOTSTRAP_INTENT_DESCRIPTION = "bootstrap"
BOOTSTRAP_INTENT_CREATOR = "dispatcher.bootstrap"


@dataclass(slots=True)
class WorkerSelection:
    worker: WorkerConfig | None
    blocked_busy: list[str]
    blocked_unhealthy: list[str]
    blocked_rejected: list[str]
    blocked_task_type: list[str]


def _server_agent_worker(
    agent: AgentRuntime,
    common_env: dict[str, str] | None = None,
) -> WorkerConfig:
    driver = "codex"
    if agent.api_format != "auto":
        driver = {"anthropic": "claudecode", "openai-chat": "pi", "openai-responses": "codex"}[agent.api_format]
    base_url = agent.base_url.rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/messages", "/models"):
        if base_url.casefold().endswith(suffix):
            base_url = base_url[:-len(suffix)].rstrip("/")
            break
    if agent.api_format != "auto":
        from urllib.parse import urlsplit
        if not urlsplit(base_url).path:
            base_url += "/v1"
    if driver == "claudecode":
        # Claude Code appends /v1/messages itself; accept UI values with or without /v1.
        anthropic_base = base_url[:-3] if base_url.casefold().endswith("/v1") else base_url
        env = {
            **(common_env or {}),
            "ANTHROPIC_MODEL": agent.model,
            "ANTHROPIC_BASE_URL": anthropic_base.rstrip("/"),
            "ANTHROPIC_AUTH_TOKEN": agent.api_key,
            **({"ANTHROPIC_API_KEY": agent.api_key} if agent.api_format == "anthropic" else {}),
            "CAIRN_AGENT_ID": agent.id,
        }
    elif driver == "pi":
        env = {
            **(common_env or {}),
            "PI_MODEL": agent.model,
            "PI_BASE_URL": base_url,
            "PI_API_KEY": agent.api_key,
            "PI_PROVIDER_API": "openai-completions",
            "CAIRN_AGENT_ID": agent.id,
        }
    else:
        env = {
            **(common_env or {}),
            "CODEX_MODEL": agent.model,
            "CODEX_BASE_URL": base_url,
            "OPENAI_API_KEY": agent.api_key,
            "CAIRN_AGENT_ID": agent.id,
        }
    env["CAIRN_EXTENSIONS_JSON"] = json.dumps({"skill_paths": agent.skill_paths, "mcp_servers": {name: server.model_dump() for name, server in agent.mcp_servers.items()}})
    return WorkerConfig(
        name=agent.name,
        agent_id=agent.id,
        type=driver,
        task_types=agent.task_types,
        max_running=agent.max_running,
        priority=agent.priority,
        env=env,
    )


class DispatcherLoop:
    def __init__(
        self,
        config_path: Path,
        *,
        scope_kind: str | None = None,
        scope_project_id: str | None = None,
    ):
        self.config_path = config_path
        self.config = DispatchConfig.load(config_path)
        self.client = CairnClient(self.config.server)
        if self.config.runtime.execution in ("local", "api"):
            self.container_manager = LocalBackend(self.config.local or LocalConfig())
        else:
            assert self.config.container is not None
            self.container_manager = ContainerManager(self.config.container)
        self.executor = ThreadPoolExecutor(max_workers=self.config.runtime.max_workers)
        self.cleanup_executor = ThreadPoolExecutor(max_workers=max(1, min(8, self.config.runtime.max_workers)))
        self.futures: dict[Future[str], RunningTask] = {}
        self.cleanup_futures: dict[Future[bool], tuple[str, str | None, str | None]] = {}
        self.reason_checkpoints: dict[str, ReasonCheckpoint] = {}
        self.runtime_project_ids: set[str] = set()
        self.worker_unhealthy_until: dict[str, float] = {}
        self.worker_rejected_until: dict[tuple[str, str, str], float] = {}
        self._log_state: dict[str, tuple[int, str, tuple[object, ...]]] = {}
        self._cleanup_pending: set[str] = set()
        self._inactive_cleanup_done: dict[str, str] = {}
        self.project_cursor = 0
        self.project_agent_ids: dict[str, set[str]] = {}
        self.project_agent_hint_usage: dict[str, dict[str, bool]] = {}
        self._settings_checked = False
        self._startup_healthchecks_checked = False
        self.scope_kind = scope_kind
        self.scope_project_id = scope_project_id
        self.project_assignment_epochs: dict[str, dict[str, int]] = {}
        self.parent_reason_last_dispatch: dict[str, float] = {}
        self.server_settings = None

    def close(self) -> None:
        if self.futures:
            LOG.info(
                "dispatcher shutting down waiting_for_tasks=%s running_projects=%s",
                len(self.futures),
                sorted({task.project_id for task in self.futures.values()}),
            )
        self.executor.shutdown(wait=True)
        self.cleanup_executor.shutdown(wait=True)
        self.container_manager.close()
        self.client.close()

    def run(self, once: bool = False) -> None:
        try:
            self.run_startup_healthchecks()
            while True:
                try:
                    self.run_iteration()
                except requests.RequestException as exc:
                    if once:
                        raise
                    LOG.warning(
                        "dispatcher server request failed error=%s retry_in=%ss",
                        exc,
                        self.config.runtime.interval,
                    )
                    time.sleep(self.config.runtime.interval)
                    continue
                if once:
                    break
                time.sleep(self.config.runtime.interval)
        finally:
            self.close()

    def run_iteration(self) -> None:
        if self.config.runtime.server_managed_agents:
            self._refresh_server_agents()
        if not self._settings_checked:
            self._validate_server_settings()
            self._settings_checked = True
        self._reap_futures()
        self._reap_cleanup_futures()
        scope_kind = getattr(self, "scope_kind", None)
        summaries = (
            self.client.list_projects(kind=scope_kind)
            if scope_kind is not None
            else self.client.list_projects()
        )
        scope_project_id = getattr(self, "scope_project_id", None)
        if scope_project_id is not None:
            summaries = [summary for summary in summaries if summary.id == scope_project_id]
        self._initialize_reason_checkpoints(summaries)
        self._refresh_runtime_projects(summaries)
        self._cancel_inactive_tasks(summaries)
        self._queue_container_cleanups(summaries)
        self._dispatch_available(summaries)

    def run_startup_healthchecks_only(self) -> None:
        try:
            self.run_startup_healthchecks(show_commands=True, force=True)
        finally:
            self.close()

    def run_startup_healthchecks(self, *, show_commands: bool = False, force: bool = False) -> None:
        if self._startup_healthchecks_checked:
            return
        if self.config.runtime.execution == "local":
            self._run_local_binary_check()
            self._startup_healthchecks_checked = True
            return
        if self.config.runtime.execution == "api":
            if self.config.runtime.server_managed_agents:
                self._refresh_server_agents()
            self._run_local_binary_check()
            if self.config.workers and self.config.runtime.worker_healthcheck != "disabled":
                self._run_startup_healthchecks(show_commands=show_commands)
            self._startup_healthchecks_checked = True
            return
        if not force and self.config.runtime.worker_healthcheck == "disabled":
            LOG.info("skip startup worker healthchecks because runtime.worker_healthcheck=disabled")
            self._startup_healthchecks_checked = True
            return
        self._run_startup_healthchecks(show_commands=show_commands)
        self._startup_healthchecks_checked = True

    def _run_local_binary_check(self) -> None:
        binaries: dict[str, list[str]] = {}
        for worker in self.config.workers:
            binary = get_driver(worker.type, self.config.runtime.execution).local_binary()
            if binary is None:
                continue
            binaries.setdefault(binary, []).append(worker.name)
        if not binaries:
            return

        LOG.info("[*] Local execution: checking %d worker CLI(s) on this host", len(binaries))
        available: list[str] = []
        missing: list[str] = []
        for binary in sorted(binaries):
            workers = ", ".join(sorted(binaries[binary]))
            path, runnable = self._probe_local_cli(binary)
            if path is None:
                missing.append(binary)
                LOG.error("[-] %-8s not found on PATH (workers: %s)", binary, workers)
            elif runnable:
                available.append(binary)
                LOG.info("[+] %-8s %s (workers: %s)", binary, path, workers)
            else:
                available.append(binary)
                LOG.warning("[!] %-8s %s found but `%s --help` failed (workers: %s)", binary, path, binary, workers)

        if not available:
            raise RuntimeError(
                "local execution: none of the configured worker CLIs are installed on PATH ("
                + ", ".join(sorted(binaries))
                + "). Install them and make sure each runs directly from your shell, then retry."
            )
        if missing:
            LOG.warning(
                "[!] Missing CLIs, their workers cannot run: %s. Install them or drop those workers.",
                ", ".join(sorted(missing)),
            )
        if self.config.runtime.execution == "api":
            LOG.info(
                "[+] API mode injects Server-managed endpoint, model, and API key into %s; host login is not used.",
                ", ".join(sorted(available)),
            )
        else:
            LOG.warning(
                "[!] Local mode uses each CLI's own host config: make sure %s is logged in and usable directly.",
                ", ".join(sorted(available)),
            )

    def _refresh_server_agents(self) -> None:
        agents = self.client.list_runtime_agents()
        allowed_ids: set[str] | None = None
        if self.scope_project_id is not None:
            detail = self.client.get_project(self.scope_project_id)
            if detail.project.kind == "parent" and detail.project.parent_agent is not None:
                allowed_ids = {detail.project.parent_agent.agent_id}
            else:
                allowed_ids = {agent.agent_id for agent in detail.project.agents}
        elif self.scope_kind == "parent":
            allowed_ids = {
                summary.parent_agent.agent_id
                for summary in self.client.list_projects(kind="parent")
                if summary.parent_agent is not None and summary.status == "active"
            }
        if allowed_ids is not None:
            agents = [agent for agent in agents if agent.id in allowed_ids]
        self.config.workers = [_server_agent_worker(agent) for agent in agents]

    @staticmethod
    def _probe_local_cli(binary: str) -> tuple[str | None, bool]:
        path = shutil.which(binary)
        if path is None:
            return None, False
        try:
            result = subprocess.run(
                [path, "--help"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return path, False
        return path, result.returncode == 0

    def _dispatch_available(self, summaries: list[ProjectSummary]) -> None:
        if len(self.futures) >= self.config.runtime.max_workers:
            self._log_changed(
                "dispatch/global",
                logging.INFO,
                "skip dispatch because max_workers reached running_tasks=%s",
                len(self.futures),
            )
            return
        active = [summary for summary in summaries if summary.status == "active"]
        if not active:
            self._log_changed("dispatch/global", logging.INFO, "skip dispatch because no active projects")
            return

        running_projects = self._ordered_projects(
            [summary for summary in active if summary.id in self.runtime_project_ids]
        )
        idle_projects = self._ordered_projects(
            [summary for summary in active if summary.id not in self.runtime_project_ids]
        )

        dispatched = True
        while dispatched and len(self.futures) < self.config.runtime.max_workers:
            dispatched = False
            for summary in running_projects:
                if self._try_dispatch_project(summary):
                    dispatched = True
                    if len(self.futures) >= self.config.runtime.max_workers:
                        return
            if dispatched:
                continue
            if self._running_project_count(active) >= self.config.runtime.max_running_projects:
                self._log_changed(
                    "dispatch/idle-limit",
                    logging.INFO,
                    "skip idle project dispatch because max_running_projects reached running_projects=%s",
                    self._running_project_count(active),
                )
                return
            for summary in idle_projects:
                if self._running_project_count(active) >= self.config.runtime.max_running_projects:
                    self._log_changed(
                        "dispatch/idle-limit",
                        logging.INFO,
                        "stop idle project dispatch because max_running_projects reached running_projects=%s",
                        self._running_project_count(active),
                    )
                    return
                if self._try_dispatch_project(summary):
                    dispatched = True
                    break

    def _ordered_projects(self, summaries: list[ProjectSummary]) -> list[ProjectSummary]:
        if not summaries:
            return []
        ids = [summary.id for summary in summaries]
        ids.sort()
        offset = self.project_cursor % len(ids)
        ordered_ids = ids[offset:] + ids[:offset]
        by_id = {summary.id: summary for summary in summaries}
        self.project_cursor += 1
        return [by_id[project_id] for project_id in ordered_ids]

    def _try_dispatch_project(self, summary: ProjectSummary) -> bool:
        skip_scope = f"project:{summary.id}:skip"
        container_name = self.container_manager.container_name(summary.id)
        if container_name in self._cleanup_pending:
            self._log_changed(
                f"{skip_scope}:cleanup_pending",
                logging.DEBUG,
                "skip project=%s because container cleanup is still pending container=%s",
                summary.id,
                container_name,
            )
            return False
        if self._project_running_task_count(summary.id) >= self.config.runtime.max_project_workers:
            self._log_changed(
                f"{skip_scope}:max_project_workers",
                logging.INFO,
                "skip project=%s because max_project_workers reached running_tasks=%s",
                summary.id,
                self._project_running_task_summary(summary.id),
            )
            return False

        project = self.client.get_project(summary.id)
        set_identity = getattr(self.container_manager, "set_project_identity", None)
        if set_identity is not None:
            set_identity(summary.id, project.project.created_at)
        if not hasattr(self, "project_agent_ids"):
            self.project_agent_ids = {}
        if not hasattr(self, "project_agent_hint_usage"):
            self.project_agent_hint_usage = {}
        if not hasattr(self, "project_assignment_epochs"):
            self.project_assignment_epochs = {}
        self.project_agent_ids[summary.id] = {agent.agent_id for agent in project.project.agents}
        if project.project.kind == "parent" and project.project.parent_agent is not None:
            self.project_agent_ids[summary.id] = {project.project.parent_agent.agent_id}
        self.project_agent_hint_usage[summary.id] = {
            agent.agent_id: agent.use_hints for agent in project.project.agents
        }
        self.project_assignment_epochs[summary.id] = {
            agent.agent_id: agent.assignment_epoch for agent in project.project.agents
        }
        allowed_worker_names = {agent.name for agent in project.project.agents}
        if project.project.kind == "parent" and project.project.parent_agent is not None:
            allowed_worker_names = {project.project.parent_agent.name}
        for running in self.futures.values():
            if (
                running.project_id == summary.id
                and running.worker_name not in allowed_worker_names
                and running.cancellation.cancel("agent reassigned")
            ):
                LOG.info(
                    "cancelling task after Agent reassignment project=%s worker=%s task=%s",
                    summary.id,
                    running.worker_name,
                    running.task_type,
                )
        if project.project.status != "active":
            self._log_changed(
                f"{skip_scope}:status",
                logging.INFO,
                "skip project=%s because status=%s",
                summary.id,
                project.project.status,
            )
            return False
        if self._is_initial_project(project):
            if project.project.reason is not None:
                return False
            if self._project_requires_bootstrap(project):
                return self._dispatch_initial_project(project)
            export_yaml = self.client.export_project(summary.id)
            return self._dispatch_reason(project, export_yaml, "initial")
        if project.project.reason is None:
            reason_trigger = self._reason_trigger(project)
            if reason_trigger is not None:
                export_yaml = self.client.export_project(summary.id)
                return self._dispatch_reason(project, export_yaml, reason_trigger)
        if project.project.kind == "parent":
            # Parent intents become Child processes. The dedicated Parent Agent never
            # consumes an Explore slot or performs concrete exploitation.
            return False
        running_intent_ids = self._project_running_explore_intents(summary.id)
        unclaimed_intents = [
            intent
            for intent in project.intents
            if intent.to is None
            and intent.worker is None
            and intent.id not in running_intent_ids
            and not self._is_bootstrap_intent(intent)
        ]
        if running_intent_ids and not unclaimed_intents:
            self._log_changed(
                f"{skip_scope}:explore_running",
                logging.DEBUG,
                "skip explore project=%s because all unclaimed intents are already running locally intents=%s",
                summary.id,
                sorted(running_intent_ids),
            )
        if unclaimed_intents:
            newest = max(unclaimed_intents, key=lambda i: i.created_at)
            export_yaml = self.client.export_project(summary.id)
            return self._dispatch_explore(project, export_yaml, newest)
        if project.project.reason is not None:
            self._log_changed(
                f"{skip_scope}:reason_claimed",
                logging.DEBUG,
                "skip reason project=%s because reason is already claimed by %s",
                summary.id,
                project.project.reason.worker,
            )
            return False
        self._log_changed(
            f"{skip_scope}:graph_unchanged",
            logging.DEBUG,
            "skip reason project=%s because reason state unchanged facts=%s hints=%s open_intents=%s intents=%s",
            summary.id,
            len(project.facts),
            len(project.hints),
            self._project_open_intent_count(project),
            len(project.intents),
        )
        return False

    def _dispatch_initial_project(self, project: ProjectDetail) -> bool:
        intent = self._get_bootstrap_intent(project)
        if intent is None:
            intent = self._create_bootstrap_intent(project.project.id)
            if intent is None:
                return False
        if self._project_has_running_bootstrap(project.project.id):
            self._log_changed(
                f"project:{project.project.id}:skip:bootstrap_running",
                logging.DEBUG,
                "skip bootstrap project=%s because bootstrap task is already running locally",
                project.project.id,
            )
            return False
        if intent.worker is not None:
            self._log_changed(
                f"project:{project.project.id}:skip:bootstrap_claimed",
                logging.DEBUG,
                "skip bootstrap project=%s because bootstrap intent=%s is already claimed by %s",
                project.project.id,
                intent.id,
                intent.worker,
            )
            return False
        return self._dispatch_bootstrap(project, intent)

    def _dispatch_reason(self, project: ProjectDetail, export_yaml: str, trigger: str) -> bool:
        if project.project.kind == "parent" and trigger != "initial":
            debounce = (
                self.server_settings.parent_review_debounce
                if getattr(self, "server_settings", None) is not None
                else 15
            )
            last_dispatch = getattr(self, "parent_reason_last_dispatch", {}).get(
                project.project.id, 0
            )
            if time.time() - last_dispatch < debounce:
                return False
        selection = self._select_worker(project.project.id, "reason")
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:reason",
                logging.INFO,
                "no worker available for reason project=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:reason")
        actual_hint_count = len(project.hints)
        project, export_yaml = self._worker_context(project, export_yaml, worker)
        claim = self.client.claim_reason(project.project.id, worker.name, trigger)
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "reason claim failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "reason claim failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                claim.status_code,
            )
            return False
        try:
            future = self.executor.submit(
                run_reason_task,
                self.config,
                self.client,
                self.container_manager,
                project,
                export_yaml,
                worker,
                cancellation := TaskCancellation(),
            )
        except Exception:
            LOG.exception("failed to submit reason task project=%s worker=%s", project.project.id, worker.name)
            self._best_effort_release_reason(project.project.id, worker.name)
            return False
        self.futures[future] = RunningTask(
            project.project.id,
            "reason",
            worker.name,
            cancellation,
            intent_id=None,
            fact_count=len(project.facts),
            hint_count=actual_hint_count,
            open_intent_count=self._project_open_intent_count(project),
        )
        self.runtime_project_ids.add(project.project.id)
        if project.project.kind == "parent":
            if not hasattr(self, "parent_reason_last_dispatch"):
                self.parent_reason_last_dispatch = {}
            self.parent_reason_last_dispatch[project.project.id] = time.time()
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched reason project=%s worker=%s trigger=%s", project.project.id, worker.name, trigger)
        return True

    def _dispatch_bootstrap(self, project: ProjectDetail, intent: Intent) -> bool:
        selection = self._select_worker(project.project.id, "bootstrap")
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:bootstrap",
                logging.INFO,
                "no worker available for bootstrap project=%s intent=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                intent.id,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:bootstrap")
        project, _ = self._worker_context(project, None, worker)
        claim = self.client.heartbeat(project.project.id, intent.id, worker.name)
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "bootstrap claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "bootstrap claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        try:
            future = self.executor.submit(
                run_bootstrap_task,
                self.config,
                self.client,
                self.container_manager,
                project,
                intent,
                worker,
                cancellation := TaskCancellation(),
            )
        except Exception:
            LOG.exception("failed to submit bootstrap task project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
            self._best_effort_release(project.project.id, intent.id, worker.name)
            return False
        self.futures[future] = RunningTask(project.project.id, "bootstrap", worker.name, cancellation, intent_id=intent.id)
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched bootstrap project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        return True

    def _dispatch_explore(self, project: ProjectDetail, export_yaml: str, intent: Intent) -> bool:
        selection = self._select_worker(project.project.id, "explore")
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:explore",
                logging.INFO,
                "no worker available for explore project=%s intent=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                intent.id,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:explore")
        project, export_yaml = self._worker_context(project, export_yaml, worker)
        claim = self.client.heartbeat(project.project.id, intent.id, worker.name)
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "explore claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "explore claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        try:
            future = self.executor.submit(
                run_explore_task,
                self.config,
                self.client,
                self.container_manager,
                project,
                export_yaml,
                intent,
                worker,
                cancellation := TaskCancellation(),
            )
        except Exception:
            LOG.exception("failed to submit explore task project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
            self._best_effort_release(project.project.id, intent.id, worker.name)
            return False
        self.futures[future] = RunningTask(project.project.id, "explore", worker.name, cancellation, intent_id=intent.id)
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched explore project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        return True

    def _select_worker(self, project_id: str, task_type: str) -> WorkerSelection:
        now = time.time()
        candidates: list[WorkerConfig] = []
        blocked_busy: list[str] = []
        blocked_unhealthy: list[str] = []
        blocked_rejected: list[str] = []
        blocked_task_type: list[str] = []
        running_counts = self._worker_counts()
        for worker in self.config.workers:
            if self.config.runtime.server_managed_agents:
                allowed = self.project_agent_ids.get(project_id, set())
                if worker.agent_id not in allowed:
                    blocked_task_type.append(f"{worker.name}(not selected)")
                    continue
            if task_type not in worker.task_types:
                blocked_task_type.append(worker.name)
                continue
            running = running_counts.get(worker.name, 0)
            project_running = sum(
                1
                for task in self.futures.values()
                if task.project_id == project_id and task.worker_name == worker.name
            )
            if self.config.runtime.server_managed_agents and project_running >= 1:
                blocked_busy.append(
                    f"{worker.name}(project {project_running}/1,global {running}/{worker.max_running})"
                )
                continue
            if running >= worker.max_running:
                blocked_busy.append(f"{worker.name}({running}/{worker.max_running})")
                continue
            unhealthy_until = self.worker_unhealthy_until.get(worker.name, 0)
            if unhealthy_until > now:
                blocked_unhealthy.append(f"{worker.name}({unhealthy_until - now:.1f}s)")
                continue
            rejected_until = self.worker_rejected_until.get((project_id, task_type, worker.name), 0)
            if rejected_until > now:
                blocked_rejected.append(f"{worker.name}({rejected_until - now:.1f}s)")
                continue
            candidates.append(worker)
        if not candidates:
            LOG.debug(
                "worker selection project=%s task=%s no candidates blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s blocked_task_type=%s",
                project_id,
                task_type,
                blocked_busy,
                blocked_unhealthy,
                blocked_rejected,
                blocked_task_type,
            )
            return WorkerSelection(
                worker=None,
                blocked_busy=blocked_busy,
                blocked_unhealthy=blocked_unhealthy,
                blocked_rejected=blocked_rejected,
                blocked_task_type=blocked_task_type,
            )
        ordered = choose_worker(candidates, running_counts)
        selected = ordered[0] if ordered else None
        if (
            selected is not None
            and selected.agent_id is not None
            and ("CAIRN_AGENT_ID" in selected.env or selected.agent_id in getattr(self, "project_assignment_epochs", {}).get(project_id, {}))
        ):
            epoch = getattr(self, "project_assignment_epochs", {}).get(project_id, {}).get(
                selected.agent_id, 0
            )
            selected = selected.model_copy(
                update={"env": {**selected.env, "CAIRN_ASSIGNMENT_EPOCH": str(epoch)}}
            )
        LOG.debug(
            "worker selection project=%s task=%s candidates=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s blocked_task_type=%s chosen=%s",
            project_id,
            task_type,
            [f"{worker.name}({running_counts.get(worker.name, 0)}/{worker.max_running},p{worker.priority})" for worker in candidates],
            blocked_busy,
            blocked_unhealthy,
            blocked_rejected,
            blocked_task_type,
            selected.name if selected else None,
        )
        return WorkerSelection(
            worker=selected,
            blocked_busy=blocked_busy,
            blocked_unhealthy=blocked_unhealthy,
            blocked_rejected=blocked_rejected,
            blocked_task_type=blocked_task_type,
        )

    def _worker_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.futures.values():
            counts[task.worker_name] = counts.get(task.worker_name, 0) + 1
        return counts

    def _project_running_task_count(self, project_id: str) -> int:
        return sum(1 for task in self.futures.values() if task.project_id == project_id)

    def _project_running_task_summary(self, project_id: str) -> list[str]:
        summary: list[str] = []
        for task in self.futures.values():
            if task.project_id != project_id:
                continue
            if task.intent_id is None:
                summary.append(f"{task.task_type}:{task.worker_name}")
            else:
                summary.append(f"{task.task_type}:{task.worker_name}:{task.intent_id}")
        summary.sort()
        return summary

    def _project_has_running_bootstrap(self, project_id: str) -> bool:
        return any(task.project_id == project_id and task.task_type == "bootstrap" for task in self.futures.values())

    def _project_running_explore_intents(self, project_id: str) -> set[str]:
        return {
            task.intent_id
            for task in self.futures.values()
            if task.project_id == project_id and task.task_type == "explore" and task.intent_id is not None
        }

    def _running_project_count(self, summaries: list[ProjectSummary]) -> int:
        active_ids = {summary.id for summary in summaries if summary.status == "active"}
        return len(self.runtime_project_ids & active_ids)

    def _project_open_intent_count(self, project: ProjectDetail) -> int:
        return sum(1 for intent in project.intents if intent.to is None)

    def _is_bootstrap_intent(self, intent: Intent) -> bool:
        return (
            intent.description == BOOTSTRAP_INTENT_DESCRIPTION
            and intent.creator == BOOTSTRAP_INTENT_CREATOR
            and intent.from_ == ["origin"]
            and intent.to is None
        )

    def _get_bootstrap_intent(self, project: ProjectDetail) -> Intent | None:
        intents = [intent for intent in project.intents if self._is_bootstrap_intent(intent)]
        if not intents:
            return None
        if len(intents) > 1:
            LOG.warning("project has multiple bootstrap intents project=%s intents=%s", project.project.id, [intent.id for intent in intents])
        intents.sort(key=lambda intent: (intent.worker is not None, intent.created_at, intent.id))
        return intents[0]

    def _is_initial_project(self, project: ProjectDetail) -> bool:
        fact_ids = {fact.id for fact in project.facts}
        if fact_ids != {"origin", "goal"} or len(project.facts) != 2:
            return False
        if not project.intents:
            return True
        return all(self._is_bootstrap_intent(intent) for intent in project.intents)

    def _project_requires_bootstrap(self, project: ProjectDetail) -> bool:
        if project.project.kind == "parent" and project.project.parent_agent is not None:
            return False
        if not project.project.bootstrap_enabled:
            return False
        if self._get_bootstrap_intent(project) is not None:
            return True
        if project.project.kind == "parent" and project.project.parent_agent is not None:
            allowed = {project.project.parent_agent.agent_id}
        else:
            allowed = {agent.agent_id for agent in project.project.agents}
        return any(
            "bootstrap" in worker.task_types
            and (not self.config.runtime.server_managed_agents or worker.agent_id in allowed)
            for worker in self.config.workers
        )

    def _worker_context(
        self,
        project: ProjectDetail,
        export_yaml: str | None,
        worker: WorkerConfig,
    ) -> tuple[ProjectDetail, str | None]:
        if not self.config.runtime.server_managed_agents or worker.agent_id is None:
            return project, export_yaml
        use_hints = self.project_agent_hint_usage.get(project.project.id, {}).get(worker.agent_id, True)
        if use_hints:
            return project, export_yaml
        filtered_project = project.model_copy(update={"hints": []})
        if export_yaml is None:
            return filtered_project, None
        payload = yaml.safe_load(export_yaml) or {}
        if isinstance(payload, dict):
            payload.pop("hints", None)
        filtered_yaml = yaml.dump(payload, allow_unicode=True, default_flow_style=False, sort_keys=False)
        return filtered_project, filtered_yaml

    def _create_bootstrap_intent(self, project_id: str) -> Intent | None:
        response = self.client.create_intent(
            project_id,
            ["origin"],
            BOOTSTRAP_INTENT_DESCRIPTION,
            BOOTSTRAP_INTENT_CREATOR,
        )
        if response.status_code == 403:
            LOG.info("project became inactive before bootstrap intent create project=%s", project_id)
            return None
        if not response.ok:
            LOG.warning(
                "bootstrap intent write failed project=%s status=%s body=%s",
                project_id,
                response.status_code,
                response.text,
            )
            return None
        if not isinstance(response.data, dict):
            LOG.warning("bootstrap intent create returned empty body project=%s", project_id)
            return None
        intent = Intent.model_validate(response.data)
        LOG.info("created bootstrap intent project=%s intent=%s", project_id, intent.id)
        return intent

    def _reason_trigger(self, project: ProjectDetail) -> str | None:
        open_intent_count = self._project_open_intent_count(project)
        checkpoint = self.reason_checkpoints.get(project.project.id)
        if checkpoint is None:
            return "initial"
        changes: list[str] = []
        if len(project.facts) > checkpoint.fact_count:
            changes.append(f"facts:{checkpoint.fact_count}->{len(project.facts)}")
        if len(project.hints) > checkpoint.hint_count:
            changes.append(f"hints:{checkpoint.hint_count}->{len(project.hints)}")
        if checkpoint.open_intent_count > 0 and open_intent_count == 0:
            changes.append(f"open_intents:{checkpoint.open_intent_count}->0")
        if not changes:
            return None
        return ",".join(changes)

    def _reap_futures(self) -> None:
        done = [future for future in self.futures if future.done()]
        for future in done:
            task = self.futures.pop(future)
            try:
                outcome = future.result()
                if outcome == "cancelled":
                    LOG.info(
                        "task cancelled project=%s task=%s worker=%s",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                    )
                elif outcome != "success":
                    LOG.warning(
                        "task finished project=%s task=%s worker=%s outcome=%s",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        outcome,
                    )
                self._clear_project_log_state(task.project_id)
                if outcome == "unhealthy":
                    retry_after_seconds = UNHEALTHY_RETRY_AFTER_SECONDS
                    self.worker_unhealthy_until[task.worker_name] = time.time() + retry_after_seconds
                    LOG.info(
                        "worker marked unhealthy worker=%s retry_after=%.0fs",
                        task.worker_name,
                        retry_after_seconds,
                    )
                else:
                    self.worker_unhealthy_until.pop(task.worker_name, None)
                rejection_key = (task.project_id, task.task_type, task.worker_name)
                if outcome == "rejected":
                    retry_after_seconds = REJECTED_RETRY_AFTER_SECONDS
                    self.worker_rejected_until[rejection_key] = time.time() + retry_after_seconds
                    LOG.info(
                        "worker marked rejected project=%s task=%s worker=%s retry_after=%.0fs",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        retry_after_seconds,
                    )
                else:
                    self.worker_rejected_until.pop(rejection_key, None)
                if outcome == "success" and task.task_type == "reason":
                    assert task.fact_count is not None
                    assert task.hint_count is not None
                    assert task.open_intent_count is not None
                    self.reason_checkpoints[task.project_id] = ReasonCheckpoint(
                        fact_count=task.fact_count,
                        hint_count=task.hint_count,
                        open_intent_count=task.open_intent_count,
                    )
                    LOG.debug(
                        "reason checkpoint updated project=%s facts=%s hints=%s open_intents=%s",
                        task.project_id,
                        task.fact_count,
                        task.hint_count,
                        task.open_intent_count,
                    )
            except Exception:
                LOG.exception("task crashed project=%s task=%s worker=%s", task.project_id, task.task_type, task.worker_name)

    def _cleanup_completed_containers(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "completed":
                continue
            if self._inactive_cleanup_done.get(summary.id) == summary.status:
                continue
            container_name = self.container_manager.container_name(summary.id)
            if container_name in self._cleanup_pending:
                continue
            if not self.container_manager.needs_completed_cleanup(summary.id):
                self._inactive_cleanup_done[summary.id] = summary.status
                continue
            future = self.cleanup_executor.submit(self.container_manager.cleanup_completed, summary.id)
            self.cleanup_futures[future] = (container_name, summary.id, summary.status)
            self._cleanup_pending.add(container_name)

    def _cleanup_stopped_containers(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "stopped":
                continue
            if self._inactive_cleanup_done.get(summary.id) == summary.status:
                continue
            container_name = self.container_manager.container_name(summary.id)
            if container_name in self._cleanup_pending:
                continue
            if not self.container_manager.needs_stopped_cleanup(summary.id):
                self._inactive_cleanup_done[summary.id] = summary.status
                continue
            future = self.cleanup_executor.submit(self.container_manager.cleanup_stopped, summary.id)
            self.cleanup_futures[future] = (container_name, summary.id, summary.status)
            self._cleanup_pending.add(container_name)

    def _queue_container_cleanups(self, summaries: list[ProjectSummary]) -> None:
        self._cleanup_completed_containers(summaries)
        self._cleanup_stopped_containers(summaries)

    def _reap_cleanup_futures(self) -> None:
        done = [future for future in self.cleanup_futures if future.done()]
        for future in done:
            name, project_id, target_status = self.cleanup_futures.pop(future)
            self._cleanup_pending.discard(name)
            try:
                success = future.result()
                if success and project_id is not None and target_status in ("completed", "stopped"):
                    self._inactive_cleanup_done[project_id] = target_status
                elif project_id is not None:
                    self._inactive_cleanup_done.pop(project_id, None)
            except Exception:
                if project_id is not None:
                    self._inactive_cleanup_done.pop(project_id, None)
                LOG.exception("container cleanup failed container=%s", name)

    def _refresh_runtime_projects(self, summaries: list[ProjectSummary]) -> None:
        active_ids = {summary.id for summary in summaries if summary.status == "active"}
        self.runtime_project_ids.intersection_update(active_ids)
        inactive_status_by_id = {summary.id: summary.status for summary in summaries if summary.status != "active"}
        for project_id, status in list(self._inactive_cleanup_done.items()):
            current_status = inactive_status_by_id.get(project_id)
            if current_status != status:
                self._inactive_cleanup_done.pop(project_id, None)

    def _cancel_inactive_tasks(self, summaries: list[ProjectSummary]) -> None:
        status_by_project = {summary.id: summary.status for summary in summaries}
        for task in self.futures.values():
            status = status_by_project.get(task.project_id, "deleted")
            if status != "active" and task.cancellation.cancel(status):
                LOG.info(
                    "cancelling running task for inactive project project=%s task=%s worker=%s status=%s",
                    task.project_id,
                    task.task_type,
                    task.worker_name,
                    status,
                )

    def _initialize_reason_checkpoints(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "active":
                continue
            if summary.id in self.reason_checkpoints:
                continue
            open_intent_count = summary.working_intent_count + summary.unclaimed_intent_count
            if open_intent_count == 0:
                continue
            self.reason_checkpoints[summary.id] = ReasonCheckpoint(
                fact_count=summary.fact_count,
                hint_count=summary.hint_count,
                open_intent_count=open_intent_count,
            )
            LOG.debug(
                "reason checkpoint initialized project=%s facts=%s hints=%s open_intents=%s",
                summary.id,
                summary.fact_count,
                summary.hint_count,
                open_intent_count,
            )

    def _best_effort_release(self, project_id: str, intent_id: str, worker_name: str) -> None:
        response = self.client.release(project_id, intent_id, worker_name)
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning("release failed project=%s intent=%s worker=%s status=%s", project_id, intent_id, worker_name, response.status_code)

    def _best_effort_release_reason(self, project_id: str, worker_name: str) -> None:
        response = self.client.release_reason(project_id, worker_name)
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning("reason release failed project=%s worker=%s status=%s", project_id, worker_name, response.status_code)

    def _log_changed(self, scope: str, level: int, message: str, *args: object) -> None:
        state = (level, message, args)
        if self._log_state.get(scope) == state:
            return
        self._log_state[scope] = state
        LOG.log(level, message, *args)

    def _clear_log_state(self, scope: str) -> None:
        self._log_state.pop(scope, None)

    def _clear_project_log_state(self, project_id: str) -> None:
        prefix = f"project:{project_id}:"
        for scope in list(self._log_state):
            if scope.startswith(prefix):
                self._log_state.pop(scope, None)

    def _validate_server_settings(self) -> None:
        settings = self.client.get_settings()
        self.server_settings = settings
        interval = self.config.runtime.interval
        for name, value in (("intent_timeout", settings.intent_timeout), ("reason_timeout", settings.reason_timeout)):
            if value <= interval:
                raise RuntimeError(
                    f"server {name}={value}s must be greater than dispatcher interval={interval}s"
                )
            if value < interval * 2:
                LOG.warning(
                    "server %s is tight %s=%ss interval=%ss; heartbeat slack is only %ss",
                    name,
                    name,
                    value,
                    interval,
                    value - interval,
                )
                continue
            LOG.info(
                "server setting validated %s=%ss interval=%ss",
                name,
                value,
                interval,
            )

    def _run_startup_healthchecks(self, *, show_commands: bool) -> None:
        results = run_startup_healthchecks(self.config, show_commands=show_commands)
        if self.config.runtime.server_managed_agents:
            workers_by_name = {worker.name: worker for worker in self.config.workers}
            for result in results:
                worker = workers_by_name.get(result.worker_name)
                if worker is None or worker.agent_id is None:
                    continue
                response = self.client.update_agent_health(
                    worker.agent_id,
                    "healthy" if result.ok else "unhealthy",
                    result.detail or None,
                )
                if not response.ok:
                    LOG.warning(
                        "failed to report Agent health agent=%s worker=%s status=%s body=%s",
                        worker.agent_id,
                        worker.name,
                        response.status_code,
                        response.text,
                    )
        if any(result.ok for result in results):
            return
        raise RuntimeError(format_failure_summary(results))
