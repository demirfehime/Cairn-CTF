from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests


def record_project(
    server: str,
    project_id: str,
    output_dir: Path,
    *,
    interval: float = 5.0,
    follow: bool = True,
    timeout: float = 15.0,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    server = server.rstrip("/")
    history_path = output_dir / "monitor.jsonl"
    started_at = _timestamp()
    last_signature: str | None = None
    sample_count = 0

    while True:
        response = requests.get(f"{server}/projects/{project_id}", timeout=timeout)
        response.raise_for_status()
        project = response.json()
        meta = project["project"]
        intents = project.get("intents", [])
        open_intents = [intent for intent in intents if intent.get("to") is None]
        active_intents = [
            {
                "id": intent.get("id"),
                "worker": intent.get("worker"),
                "heartbeat": intent.get("last_heartbeat_at"),
                "description": intent.get("description"),
            }
            for intent in open_intents
        ]
        reason = meta.get("reason") or {}
        event = {
            "at": _timestamp(),
            "project_id": meta.get("id", project_id),
            "target_url": meta.get("target_url"),
            "agent_ids": meta.get("agent_ids", []),
            "status": meta["status"],
            "counts": {
                "facts": len(project.get("facts", [])),
                "intents": len(intents),
                "open_intents": len(open_intents),
                "hints": len(project.get("hints", [])),
            },
            "reason": {
                "worker": reason.get("worker"),
                "trigger": reason.get("trigger"),
                "heartbeat": reason.get("last_heartbeat_at"),
            },
            "active_intents": active_intents,
        }
        signature = json.dumps(
            {key: value for key, value in event.items() if key != "at"},
            ensure_ascii=False,
            sort_keys=True,
        )
        if signature != last_signature:
            sample_count += 1
            with history_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            _write_json(output_dir / "project-latest.json", project)
            _write_numbered_snapshot(output_dir, project)
            _write_export(
                server,
                project_id,
                output_dir / "graph-latest.yaml",
                "yaml",
                timeout=timeout,
            )
            last_signature = signature

        if meta["status"] != "active" or not follow:
            _write_final_exports(server, project_id, output_dir, project, timeout=timeout)
            _write_json(
                output_dir / "manifest.json",
                {
                    "project_id": project_id,
                    "target_url": meta.get("target_url"),
                    "agent_ids": meta.get("agent_ids", []),
                    "server": server,
                    "started_at": started_at,
                    "finished_at": _timestamp(),
                    "status": meta["status"],
                    "samples": sample_count,
                    "files": {
                        "events": "monitor.jsonl",
                        "latest_project": "project-latest.json",
                        "latest_graph": "graph-latest.yaml",
                        "final_project": "project-final.json",
                        "final_graph": "graph-final.yaml",
                        "timeline": "timeline-final.txt",
                    },
                },
            )
            return project
        time.sleep(max(0.5, interval))


def _write_final_exports(
    server: str,
    project_id: str,
    output_dir: Path,
    project: dict[str, Any],
    *,
    timeout: float,
) -> None:
    _write_json(output_dir / "project-final.json", project)
    for export_format, filename in (("yaml", "graph-final.yaml"), ("timeline", "timeline-final.txt")):
        _write_export(
            server,
            project_id,
            output_dir / filename,
            export_format,
            timeout=timeout,
        )


def _write_export(
    server: str,
    project_id: str,
    path: Path,
    export_format: str,
    *,
    timeout: float,
) -> None:
    response = requests.get(
        f"{server}/projects/{project_id}/export",
        params={"format": export_format},
        timeout=timeout,
    )
    response.raise_for_status()
    path.write_text(response.text, encoding="utf-8")


def _write_numbered_snapshot(output_dir: Path, project: dict[str, Any]) -> None:
    index = 1
    while True:
        path = output_dir / f"project-{index:04d}.json"
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(project, ensure_ascii=False, indent=2) + "\n")
            return
        except FileExistsError:
            index += 1


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
