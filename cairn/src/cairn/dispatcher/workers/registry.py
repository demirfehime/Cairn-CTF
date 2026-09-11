from __future__ import annotations

from cairn.dispatcher.workers.adapters import ClaudeCodeDriver, CodexDriver, MockDriver, PiDriver
from cairn.dispatcher.workers.base import WorkerDriver


_CLAUDE = ClaudeCodeDriver()
_MOCK = MockDriver()

DRIVERS: dict[str, WorkerDriver] = {
    "claudecode": _CLAUDE,
    "codex": CodexDriver(),
    "pi": PiDriver(),
    "mock": _MOCK,
}

# Local variants invoke the host CLIs in their native configuration (no cairn provider
# injection unless provider settings are supplied). Mock uses the host interpreter.
LOCAL_DRIVERS: dict[str, WorkerDriver] = {
    "claudecode": _CLAUDE,
    "codex": CodexDriver(local=True),
    "pi": PiDriver(local=True),
    "mock": MockDriver(local=True),
}

API_DRIVERS: dict[str, WorkerDriver] = {
    **DRIVERS,
    "codex": CodexDriver(stdin_prompt=True),
    "pi": PiDriver(api=True),
}


def get_driver(name: str, execution: str = "container") -> WorkerDriver:
    if execution == "local":
        drivers = LOCAL_DRIVERS
    elif execution == "api":
        drivers = API_DRIVERS
    else:
        drivers = DRIVERS
    return drivers[name]
