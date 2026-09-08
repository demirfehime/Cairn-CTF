from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    cancel_reason: str | None = None


@runtime_checkable
class ExecProcess(Protocol):
    """A worker process running either locally or in an optional container backend."""

    def start(self) -> None: ...

    def communicate(self, timeout: float | None) -> ProcessResult: ...

    def kill(self) -> None: ...

    def cancel(self, reason: str) -> None: ...
