"""Run the Pi API adapter on the host without relying on /bin/sh."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def bundled_cli() -> Path:
    repo = Path(__file__).resolve().parents[6]
    return repo / ".cairn-launcher/pi-runtime/node_modules/@mariozechner/pi-coding-agent/dist/cli.js"


def main() -> int:
    payload = json.load(sys.stdin)
    agent_dir = Path.cwd() / ".cairn-pi"
    for segment in payload["agent_dir"]:
        if segment in {"", ".", ".."} or Path(segment).name != segment:
            raise ValueError("Invalid Pi session directory")
        agent_dir /= segment
    sessions = agent_dir / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    provider = {
        "baseUrl": os.environ["PI_BASE_URL"],
        "api": os.environ["PI_PROVIDER_API"],
        "apiKey": "PI_API_KEY",
        "models": [{"id": os.environ["PI_MODEL"]}],
    }
    (agent_dir / "models.json").write_text(
        json.dumps({"providers": {"cairn": provider}}), encoding="utf-8"
    )
    # Prefer the project-local pinned install; accept a host install otherwise.
    cli = bundled_cli()
    if cli.is_file():
        command = [shutil.which("node") or "node", str(cli)]
    else:
        binary = shutil.which("pi")
        if not binary:
            raise RuntimeError("Pi is required for OpenAI Chat. Run npm install --prefix .cairn-launcher/pi-runtime @mariozechner/pi-coding-agent@0.73.0 from Cairn.")
        command = [binary]
    command += ["--provider", "cairn", "--model", os.environ["PI_MODEL"],
                "--mode", "json", "--session-dir", str(sessions),
                "--no-extensions", "--no-skills", "--no-prompt-templates",
                "--no-themes", "--no-context-files", "-p"]
    if os.environ.get("CAIRN_PI_MCP_EXTENSION"):
        command += ["--extension", os.environ["CAIRN_PI_MCP_EXTENSION"]]
    if payload.get("session"):
        command += ["--session", payload["session"]]
    result = subprocess.run(
        command, input=payload["prompt"], text=True, encoding="utf-8",
        env={**os.environ, "PI_CODING_AGENT_DIR": str(agent_dir)},
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
