from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from cairn.server import db
from cairn.server.db import get_conn
from cairn.server.services import get_project_or_404

router = APIRouter(tags=["logs"])

RUNTIME_LOGS = {
    "dispatcher-stderr": ("dispatcher.stderr.log", "Cairn dispatcher errors"),
    "dispatcher-stdout": ("dispatcher.stdout.log", "Cairn dispatcher output"),
    "server-stderr": ("server.stderr.log", "Cairn server errors"),
    "server-stdout": ("server.stdout.log", "Cairn server output"),
}
CHILD_LOGS = {
    "dispatcher-stderr": ("dispatcher.stderr.log", "Child dispatcher errors"),
    "dispatcher-stdout": ("dispatcher.stdout.log", "Child dispatcher output"),
}


def _launcher_root() -> Path:
    database = db.configured_path().resolve()
    if database.parent.name == "data" and database.parent.parent.name == ".cairn-launcher":
        return database.parent.parent
    return (database.parent / ".cairn-launcher").resolve()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _runtime_log_dir() -> Path | None:
    root = _launcher_root()
    state_path = root / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        configured = Path(state["log_dir"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not configured.is_absolute():
        configured = root / configured
    resolved = configured.resolve()
    allowed_root = (root / "logs").resolve()
    return resolved if _within(resolved, allowed_root) else None


def _child_log_dir(project_id: str) -> Path:
    return (
        _launcher_root() / "runs" / project_id / ".cairn-child" / "logs"
    ).resolve()


def _log_file(directory: Path | None, filename: str, allowed_root: Path) -> Path | None:
    if directory is None:
        return None
    path = (directory / filename).resolve()
    if not _within(path, allowed_root.resolve()) or not path.is_file():
        return None
    return path


def _file_entry(
    *,
    path: Path,
    label: str,
    url: str,
) -> dict[str, object]:
    stat = path.stat()
    return {
        "label": label,
        "url": url,
        "local_path": str(path),
        "size": stat.st_size,
        "modified_at": datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(),
    }


def _runtime_entries(project_id: str) -> list[dict[str, object]]:
    allowed_root = _launcher_root() / "logs"
    entries: list[dict[str, object]] = []
    if not allowed_root.is_dir():
        return entries
    for directory in sorted(allowed_root.iterdir(), reverse=True):
        if not directory.is_dir() or directory.name == "workers":
            continue
        for key, (filename, label) in RUNTIME_LOGS.items():
            path = _log_file(directory, filename, allowed_root)
            if path is not None:
                entries.append(_file_entry(
                    path=path, label=f"{directory.name} / {label}",
                    url=f"/projects/{project_id}/records/runtime/{key}?run_id={quote(directory.name, safe='')}",
                ))
    return entries


def _worker_entries(project_id: str) -> list[dict[str, object]]:
    root = _launcher_root() / "logs" / "workers"
    entries = []
    if not root.is_dir():
        return entries
    for workspace in sorted(root.iterdir()):
        if workspace.name != project_id and not workspace.name.startswith(project_id + "-"):
            continue
        for path in sorted(workspace.glob("*/*.log"), reverse=True):
            resolved = path.resolve()
            if not _within(resolved, root.resolve()) or not resolved.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            entries.append(_file_entry(path=resolved, label=f"Agent {path.parent.name} / {path.name}",
                           url=f"/projects/{project_id}/records/workers/{quote(relative)}"))
    return entries


def _child_entries(project_id: str) -> list[dict[str, object]]:
    directory = _child_log_dir(project_id)
    allowed_root = _launcher_root() / "runs" / project_id / ".cairn-child" / "logs"
    entries: list[dict[str, object]] = []
    for key, (filename, label) in CHILD_LOGS.items():
        path = _log_file(directory, filename, allowed_root)
        if path is not None:
            entries.append(
                _file_entry(
                    path=path,
                    label=label,
                    url=f"/projects/{project_id}/records/child/{key}",
                )
            )
    return entries + _worker_entries(project_id)


@router.get("/projects/{project_id}/logs")
def list_project_logs(project_id: str):
    with get_conn() as conn:
        project = get_project_or_404(conn, project_id)
        groups: list[dict[str, object]] = []
        if project["kind"] == "parent":
            runtime = _runtime_entries(project_id) + _worker_entries(project_id)
            if runtime:
                groups.append(
                    {
                        "project_id": project_id,
                        "title": "Cairn runtime (all projects)",
                        "files": runtime,
                    }
                )
            children = conn.execute(
                """
                SELECT id, title FROM projects
                WHERE parent_project_id = ?
                ORDER BY priority, created_at, id
                """,
                (project_id,),
            ).fetchall()
            for child in children:
                files = _child_entries(child["id"])
                if files:
                    groups.append(
                        {
                            "project_id": child["id"],
                            "title": f"{child['id']} · {child['title']}",
                            "files": files,
                        }
                    )
        else:
            files = _child_entries(project_id)
            if files:
                groups.append(
                    {
                        "project_id": project_id,
                        "title": f"{project_id} · {project['title']}",
                        "files": files,
                    }
                )
        return {"project_id": project_id, "groups": groups}


def _inline_log(path: Path | None) -> FileResponse:
    if path is None:
        raise HTTPException(status_code=404, detail="Log file not found")
    return FileResponse(
        path,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'inline; filename="{path.name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.get("/projects/{project_id}/logs/runtime/{log_key}")
@router.get("/projects/{project_id}/records/runtime/{log_key}")
def view_runtime_log(project_id: str, log_key: str, run_id: str | None = None):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
    definition = RUNTIME_LOGS.get(log_key)
    if definition is None:
        raise HTTPException(status_code=404, detail="Unknown log file")
    directory = _runtime_log_dir()
    if run_id is not None:
        if Path(run_id).name != run_id or run_id in {".", ".."}:
            raise HTTPException(404, "Unknown log run")
        directory = _launcher_root() / "logs" / run_id
    return _inline_log(
        _log_file(directory, definition[0], _launcher_root() / "logs")
    )


@router.get("/projects/{project_id}/logs/child/{log_key}")
@router.get("/projects/{project_id}/records/child/{log_key}")
def view_child_log(project_id: str, log_key: str):
    with get_conn() as conn:
        project = get_project_or_404(conn, project_id)
        if project["kind"] != "child":
            raise HTTPException(status_code=404, detail="Child log file not found")
    definition = CHILD_LOGS.get(log_key)
    if definition is None:
        raise HTTPException(status_code=404, detail="Unknown log file")
    allowed_root = _launcher_root() / "runs" / project_id / ".cairn-child" / "logs"
    return _inline_log(
        _log_file(_child_log_dir(project_id), definition[0], allowed_root)
    )


@router.get("/projects/{project_id}/records/workers/{relative_path:path}")
def view_worker_log(project_id: str, relative_path: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
    parts = Path(relative_path).parts
    if len(parts) != 3 or (parts[0] != project_id and not parts[0].startswith(project_id + "-")):
        raise HTTPException(404, "Unknown worker log")
    if parts[-1] not in {"stdout.log", "stderr.log"}:
        raise HTTPException(404, "Unknown worker log")
    root = _launcher_root() / "logs" / "workers"
    return _inline_log(_log_file(root / parts[0] / parts[1], parts[2], root))
