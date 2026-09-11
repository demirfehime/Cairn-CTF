# Cairn Codex Development Guide

## Project map

Read `CAIRN_AGENT_ENTRY.md` before changing CTF runtime code.

## Default architecture

- Prefer `dispatch.codex.yaml` and local execution for development on this machine.
- Docker is an optional compatibility backend, not a prerequisite.
- The Codex integration entry point is `python -m cairn.codex_mcp`.
- Persistent data lives in `data/`; per-project agent workspaces live in `runs/`.

## Commands

- Install: `.\.venv\Scripts\python.exe -m pip install -e .\cairn`
- Test: `.\.venv\Scripts\python.exe -m pytest -q .\cairn\tests`
- MCP/runtime: `.\.venv\Scripts\python.exe -m cairn.codex_mcp --config .\dispatch.codex.yaml --db-path .\data\cairn.db`
- UI while the MCP runtime is active: `http://127.0.0.1:8792`

## Change rules

- Keep the fact/intent/hint protocol backward compatible unless a migration and tests are added.
- Keep Codex host execution sandboxed by default. Do not restore unconditional approval/sandbox bypass flags.
- Add tests for Windows and POSIX behavior when changing local process management.
- Do not place credentials in dispatcher YAML, source files, or committed Codex configuration.
- Launch/restart the long-running Cairn server and dispatcher from a network-enabled terminal. If the launcher's network preflight reports permission denied, use an explicitly approved execution context; do not remove the check or relax agent sandbox settings. Run preflight before stopping an existing service, preserve its config/database paths, and verify `/agents/discover-models` through the running server after restart.
