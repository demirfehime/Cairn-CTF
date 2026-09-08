from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4
import os
import shutil
import hashlib
from pathlib import Path

from cairn.dispatcher.config import LocalConfig
from cairn.dispatcher.runtime.local_process import LocalProcess

LOG = logging.getLogger(__name__)

PROJECT_INSTRUCTIONS = """# Cairn Project Isolation

This directory is the complete workspace for one Cairn project.

- Use only the Origin, Goal, Hints, graph snapshots, and evidence stored in this directory.
- Never inspect parent or sibling directories, Cairn databases, launcher records/runs, browser history, or another project's API/export.
- Never query the Cairn control-plane HTTP API to discover other projects.
- Treat prior Agent sessions outside this directory as unrelated and untrusted.
- If required target context is missing, report that gap in the current result instead of searching other projects.
"""


class LocalBackend:
    """Runs workers directly on the dispatcher host instead of in per-project containers.

    Each project gets an isolated working directory under ``workspace_root`` (defaulting
    to the directory the dispatcher was started in). Worker processes inherit the host
    environment so the pre-configured ``claude`` / ``codex`` / ``pi`` CLIs and their
    credentials are used as-is; no API keys are injected. There are no containers to
    build or tear down, so the container-lifecycle methods are inert.
    """

    def __init__(self, config: LocalConfig):
        self._config = config
        root = config.workspace_root
        # Resolve relative roots once so attachment delivery can distinguish a
        # host workspace from a Docker container name and keep all writes scoped.
        self._root = (Path(root).expanduser() if root else Path.cwd()).resolve()
        self._project_identities: dict[str, str] = {}

    def close(self) -> None:
        return None

    def container_name(self, project_id: str) -> str:
        return str(self._project_dir(project_id))

    def set_project_identity(self, project_id: str, identity: str) -> None:
        self._project_identities[project_id] = identity

    def ensure_running(self, project_id: str) -> str:
        project_dir = self._project_dir(project_id)
        project_dir.mkdir(parents=True, exist_ok=True)
        instructions = project_dir / "AGENTS.md"
        if not instructions.exists() or instructions.read_text(encoding="utf-8") != PROJECT_INSTRUCTIONS:
            instructions.write_text(PROJECT_INSTRUCTIONS, encoding="utf-8")
        LOG.debug("local project workdir ready project=%s dir=%s", project_id, project_dir)
        return str(project_dir)

    def build_exec_process(
        self,
        container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
        stdin_text: str | None = None,
    ) -> LocalProcess:
        merged_env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "LANG": "C.UTF-8",
            **(env or {}),
        }
        merged_env["CAIRN_PROJECT_WORKSPACE"] = str(Path(container_name).resolve())
        merged_env["CAIRN_PROJECT_ISOLATION"] = "strict"
        if command and Path(command[0]).stem.casefold() == "codex" and "CODEX_BASE_URL" in merged_env:
            # API mode must not reuse the operator's logged-in Codex home. Keeping
            # sessions inside the project workspace still allows ``exec resume``.
            agent_id = merged_env.get("CAIRN_AGENT_ID", "agent")
            assignment_epoch = merged_env.get("CAIRN_ASSIGNMENT_EPOCH", "0")
            codex_home = (
                Path(container_name)
                / ".cairn-codex"
                / agent_id.replace("/", "-")
                / f"assignment-{assignment_epoch}"
            )
            codex_home.mkdir(parents=True, exist_ok=True)
            merged_env["CODEX_HOME"] = str(codex_home)
        from cairn.extensions.runtime import prepare_extensions
        command, stdin_text = prepare_extensions(container_name, merged_env, command, stdin_text)
        # Archive outside project workspaces so completed_action=remove cannot
        # delete worker logs. Every execution has its own collision-free folder.
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid4().hex[:12]
        log_dir = self._root.resolve().parent / "logs" / "workers" / Path(container_name).name / run_id
        return LocalProcess(
            command,
            cwd=container_name,
            env=merged_env,
            timeout_seconds=timeout_seconds,
            term_grace_seconds=kill_after_seconds,
            stdin_text=stdin_text,
            log_dir=log_dir,
        )

    def write_bytes_file(self, container_name: str, path: str, content: bytes) -> None:
        target = Path(path).resolve()
        root = Path(container_name).resolve()
        if not target.is_relative_to(root):
            raise ValueError("Attachment path must remain inside project workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replacement prevents concurrent Agents from seeing partial files.
        temporary = target.with_name(target.name + "." + uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(content)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        target = Path(path)
        if not target.is_absolute():
            raise ValueError(f"local file path must be absolute: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def needs_completed_cleanup(self, project_id: str) -> bool:
        return self._config.completed_action == "remove" and self._project_dir(project_id).exists()

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        return False

    def cleanup_completed(self, project_id: str) -> bool:
        if self._config.completed_action == "remove":
            project_dir = self._project_dir(project_id)
            LOG.info("removing completed project workdir project=%s dir=%s", project_id, project_dir)
            shutil.rmtree(project_dir, ignore_errors=True)
        return True

    def cleanup_stopped(self, project_id: str) -> bool:
        return True

    def managed_container_names(self) -> list[str]:
        return []

    def _project_dir(self, project_id: str) -> Path:
        sanitized = project_id.replace("/", "-")
        identity = self._project_identities.get(project_id)
        if not identity:
            return self._root / sanitized
        digest = hashlib.sha256(f"{project_id}:{identity}".encode("utf-8")).hexdigest()[:12]
        return self._root / f"{sanitized}-{digest}"
