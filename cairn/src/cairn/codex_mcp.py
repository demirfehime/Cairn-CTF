from __future__ import annotations

import base64
import logging
import threading
import time
from pathlib import Path
from typing import Any, Literal

import requests
import uvicorn
from mcp.server.fastmcp import FastMCP

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.logging import configure_logging
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server import db

LOG = logging.getLogger(__name__)
MCP = FastMCP("Cairn")

_base_url = "http://127.0.0.1:8791"


def _request(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> requests.Response:
    try:
        response = requests.request(
            method,
            f"{_base_url}{path}",
            json=json,
            params=params,
            data=data,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Cairn server request failed: {exc}") from exc
    if not response.ok:
        detail = response.text.strip() or response.reason
        raise RuntimeError(f"Cairn API {method} {path} failed ({response.status_code}): {detail}")
    return response


@MCP.tool()
def cairn_status() -> dict[str, Any]:
    """Return Cairn runtime settings and a compact list of all projects."""
    return {
        "server": _base_url,
        "settings": _request("GET", "/settings").json(),
        "projects": _request("GET", "/projects").json(),
    }


@MCP.tool()
def cairn_list_projects() -> list[dict[str, Any]]:
    """List Cairn projects and their scheduling/progress counters."""
    return _request("GET", "/projects").json()


@MCP.tool()
def cairn_create_project(
    title: str,
    origin: str,
    goal: str,
    hints: list[str] | None = None,
    bootstrap_enabled: bool = True,
    attachments: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Create a project and optionally upload task files.

    Each attachment is ``{"name": "file.bin", "content_base64": "..."}``.
    Files are uploaded as opaque bytes, linked to the new project atomically, and
    then placed in every assigned Agent workspace before its first task.
    """
    payload: dict[str, Any] = {
        "title": title,
        "origin": origin,
        "goal": goal,
        "bootstrap_enabled": bootstrap_enabled,
    }
    if hints:
        payload["hints"] = [{"content": hint, "creator": "codex"} for hint in hints]
    uploaded: list[str] = []
    total = 0
    try:
        for attachment in attachments or []:
            name = str(attachment.get("name", ""))
            encoded = attachment.get("content_base64")
            if not name or encoded is None:
                raise ValueError("Each attachment needs name and content_base64")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Invalid base64 attachment: {name}") from exc
            total += len(content)
            if len(content) > 50 * 1024 * 1024 or total > 200 * 1024 * 1024:
                raise ValueError("Attachments exceed Cairn's 50 MiB per-file or 200 MiB project limit")
            result = _request(
                "POST",
                "/attachments/uploads",
                params={"name": name},
                data=content,
                headers={"Content-Type": "application/octet-stream"},
            ).json()
            uploaded.append(result["id"])
        payload["attachment_ids"] = uploaded
        return _request("POST", "/projects", json=payload).json()
    except Exception:
        for attachment_id in uploaded:
            try:
                _request("DELETE", f"/attachments/uploads/{attachment_id}")
            except Exception:
                LOG.warning("failed to discard pending attachment=%s", attachment_id, exc_info=True)
        raise


@MCP.tool()
def cairn_get_project(project_id: str) -> dict[str, Any]:
    """Read a complete Cairn fact/intent/hint graph for one project."""
    return _request("GET", f"/projects/{project_id}").json()


@MCP.tool()
def cairn_export_project(
    project_id: str,
    format: Literal["yaml", "timeline"] = "yaml",
) -> str:
    """Export a project as graph YAML or a chronological text timeline."""
    return _request(
        "GET",
        f"/projects/{project_id}/export",
        params={"format": format},
    ).text


@MCP.tool()
def cairn_add_hint(project_id: str, content: str, creator: str = "codex") -> dict[str, Any]:
    """Inject human/Codex guidance into a project without rewriting existing facts."""
    return _request(
        "POST",
        f"/projects/{project_id}/hints",
        json={"content": content, "creator": creator},
    ).json()


@MCP.tool()
def cairn_set_project_status(
    project_id: str,
    status: Literal["active", "stopped"],
) -> dict[str, Any]:
    """Start/resume or hard-stop scheduling for a non-completed project."""
    return _request(
        "PUT",
        f"/projects/{project_id}/status",
        json={"status": status},
    ).json()


@MCP.tool()
def cairn_reopen_project(
    project_id: str,
    description: str,
    creator: str = "codex",
) -> dict[str, Any]:
    """Reopen an incorrectly completed project and add the correction as a new fact."""
    return _request(
        "POST",
        f"/projects/{project_id}/reopen",
        json={"description": description, "creator": creator},
    ).json()


class EmbeddedCairnRuntime:
    """Runs the REST/UI server and local dispatcher beside the stdio MCP server."""

    def __init__(self, config_path: Path, db_path: Path, host: str, port: int):
        self.config_path = config_path.resolve()
        self.db_path = db_path.resolve()
        self.host = host
        self.port = port
        self.stop_event = threading.Event()
        self._server: uvicorn.Server | None = None
        self._server_thread: threading.Thread | None = None
        self._dispatcher_thread: threading.Thread | None = None
        self._dispatcher_error: BaseException | None = None

    def start(self) -> None:
        global _base_url
        _base_url = f"http://{self.host}:{self.port}"
        configured_server = DispatchConfig.load(self.config_path).server.rstrip("/")
        if configured_server != _base_url:
            raise RuntimeError(
                f"dispatcher server {configured_server!r} does not match embedded MCP server {_base_url!r}"
            )
        db.configure(self.db_path)

        from cairn.server.app import app

        self._server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=self.host,
                port=self.port,
                log_level="warning",
                access_log=False,
            )
        )
        self._server_thread = threading.Thread(
            target=self._server.run,
            name="cairn-api",
            daemon=True,
        )
        self._server_thread.start()
        self._wait_for_server()

        try:
            dispatcher = DispatcherLoop(self.config_path)
        except Exception:
            self.stop()
            raise

        def run_dispatcher() -> None:
            try:
                dispatcher.run(stop_event=self.stop_event)
            except BaseException as exc:  # surfaced through startup and MCP status failures
                self._dispatcher_error = exc
                LOG.exception("embedded dispatcher stopped")

        self._dispatcher_thread = threading.Thread(
            target=run_dispatcher,
            name="cairn-dispatcher",
            daemon=True,
        )
        self._dispatcher_thread.start()
        time.sleep(0.25)
        if self._dispatcher_error is not None:
            raise RuntimeError(f"Cairn dispatcher failed to start: {self._dispatcher_error}")
        LOG.info("Cairn Codex runtime ready server=%s db=%s", _base_url, self.db_path)

    def stop(self) -> None:
        self.stop_event.set()
        if self._server is not None:
            self._server.should_exit = True
        if self._dispatcher_thread is not None:
            self._dispatcher_thread.join(timeout=10)
        if self._server_thread is not None:
            self._server_thread.join(timeout=10)

    def _wait_for_server(self) -> None:
        deadline = time.monotonic() + 30
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                response = requests.get(f"{_base_url}/settings", timeout=1)
                if response.ok:
                    return
            except requests.RequestException as exc:
                last_error = exc
            time.sleep(0.1)
        raise RuntimeError(f"Cairn API did not start at {_base_url}: {last_error}")


def run_mcp(config_path: Path, db_path: Path, host: str = "127.0.0.1", port: int = 8791) -> None:
    configure_logging("INFO")
    runtime = EmbeddedCairnRuntime(config_path, db_path, host, port)
    runtime.start()
    try:
        MCP.run(transport="stdio")
    finally:
        runtime.stop()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run Cairn as a Codex-callable MCP server")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()
    run_mcp(args.config, args.db_path, args.host, args.port)


if __name__ == "__main__":
    main()
