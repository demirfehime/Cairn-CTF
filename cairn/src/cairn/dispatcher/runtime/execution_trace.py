from __future__ import annotations

"""Durable, replay-friendly records for every worker process invocation."""

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cairn.dispatcher.runtime.process import ProcessResult

_SENSITIVE = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|bearer)")


def _redact_argv(argv: list[str]) -> list[str]:
    """Keep command provenance without persisting obvious inline credentials."""
    result: list[str] = []
    redact_next = False
    for item in argv:
        text = str(item)
        if redact_next:
            result.append("<redacted>")
            redact_next = False
            continue
        if text.startswith("-") and _SENSITIVE.search(text):
            if "=" in text:
                result.append(text.split("=", 1)[0] + "=<redacted>")
            else:
                result.append(text)
                redact_next = True
            continue
        if "=" in text and _SENSITIVE.search(text.split("=", 1)[0]):
            result.append(text.split("=", 1)[0] + "=<redacted>")
        else:
            result.append(text)
    return result


def persist_execution_trace(
    *,
    trace_dir: Path | None,
    fallback_root: Path,
    container_name: str,
    worker_name: str,
    phase: str,
    argv: list[str],
    prompt: str | None,
    stdin_text: str | None,
    result: ProcessResult,
    started_at: str,
) -> Path:
    """Persist one complete worker invocation as files plus a JSON manifest."""
    if trace_dir is None:
        safe_project = Path(container_name).name.replace("/", "-").replace("\\", "-")
        trace_dir = fallback_root / safe_project / phase / uuid.uuid4().hex
    trace_dir.mkdir(parents=True, exist_ok=True)
    if prompt is not None:
        (trace_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    if stdin_text is not None:
        (trace_dir / "stdin.txt").write_text(stdin_text, encoding="utf-8")
    (trace_dir / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (trace_dir / "stderr.log").write_text(result.stderr, encoding="utf-8")
    finished_at = datetime.now(timezone.utc).isoformat()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": finished_at,
        "container": container_name,
        "worker": worker_name,
        "phase": phase,
        "argv": _redact_argv(argv),
        "prompt_file": "prompt.txt" if prompt is not None else None,
        "stdin_file": "stdin.txt" if stdin_text is not None else None,
        "stdout_file": "stdout.log",
        "stderr_file": "stderr.log",
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "cancelled": result.cancelled,
        "cancel_reason": result.cancel_reason,
        "stdout_bytes": len(result.stdout.encode("utf-8")),
        "stderr_bytes": len(result.stderr.encode("utf-8")),
    }
    (trace_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return trace_dir
