# Cairn CTF

![Cairn banner](README/banner.png)

Cairn CTF is an independent edition of Cairn for CTF and Flag-oriented projects.
A dedicated Parent Agent splits a goal into Child projects, reviews their facts,
and submits verified Flag candidates for human review. Each Child has its own
workspace, agent sessions and fact/intent graph. Cross-Child information is shared
through the Parent Blackboard and concise hints.

## Quick start on Windows

Requirements: Python 3.12 or newer and the CLI required by your selected Agent.
Docker is optional for local execution.

```powershell
.\start-cairn-ctf.cmd
```

The launcher prepares the virtual environment and opens **http://127.0.0.1:8001**.
Configure Agents and their provider settings in the UI. Stop this instance with
`stop-cairn-ctf.cmd`. `start-cairn.cmd` and `stop-cairn.cmd` are compatible aliases.
Launcher state, generated configuration, databases and logs live under this
checkout's `.cairn-launcher/` directory. Never share these with Cairn Pentest.

## Optional standalone local configuration

```powershell
.\scripts\install.ps1
.\scripts\start-cairn.ps1
```

This separate entry point uses `dispatch.codex.yaml`, `data/cairn.db`, `runs/` and
**http://127.0.0.1:8792**. It is an alternative to the launcher above. Agent workers
are configured through the UI. For optional MCP registration, the existing
`scripts/register-codex.ps1` resolves paths from this checkout. The committed
`.codex/config.toml` is disabled by default; its relative paths require launching
from the repository root with this checkout's virtual environment activated.

## Docker deployment

The CTF worker image is separate from the Pentest worker image. Build it locally
before first use, or pull it after this repository's publishing workflow succeeds:

```sh
docker build -t ghcr.io/demirfehime/cairn-ctf-worker-container:latest container
cp dispatch.example.yaml dispatch.yaml
# Edit dispatch.yaml for your provider and environment.
docker compose up -d --build
```

The UI is available at **http://127.0.0.1:8001** (container port `8000`). Persistent
server data is stored in `datas/cairn/`. `dispatch.yaml` is local configuration and
is excluded from version control. The GitHub workflow publishes the CTF image and
its own build cache; it does not overwrite the Pentest image.

## Development and verification

```powershell
.\scripts\install.ps1
.\.venv\Scripts\python.exe -m pytest -q .\cairn\tests
```

Or, with uv:

```sh
uv run --project cairn --group dev pytest cairn/tests
```

`dispatch_mock.yaml` selects the mock worker and mock prompts, including the
Parent reasoning prompt. Configuration validation checks these resources before
dispatch begins. Mock execution does not require a model API, but its configured
execution backend may require Docker.

## Execution records

Worker executions retain `manifest.json`, `prompt.txt`, optional `stdin.txt`,
`stdout.log` and `stderr.log`. Local worker records are under
`.cairn-launcher/logs/workers/<project>/<run-id>/`. Container executions without a
local log directory fall back to `.cairn-launcher/logs/traces/` relative to the
dispatcher's working directory. Worker records shown in the UI stay scoped to the
selected project. The manifest redacts common credential arguments; prompt and
output files contain the actual execution text and are local runtime data.

## Documentation

- [Agent project map](CAIRN_AGENT_ENTRY.md)
- [Local deployment](docs/CODEX_DEPLOYMENT.zh-CN.md)
- [Model API formats](docs/model-api-formats.md)
- [Skills and MCP](docs/skills-and-mcp.md)
- [Log retention](docs/log-retention.md)
- [Server protocol](docs/specs/server-protocol.md)
- [Dispatcher design](docs/specs/dispatcher-design.md)
- [Historical reconstruction notes](docs/history/RECOVERY.md) ? provenance of an
  earlier source snapshot, not instructions for the current release.

The original Cairn project and authors are credited in the source metadata and
[LICENSE](LICENSE). The two editions intentionally retain different features;
Pentest-only Free Agent files are not missing CTF dependencies.
