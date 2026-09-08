from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4

from cairn.extensions.config import read_skill


def prepare_extensions(workspace: str, env: dict[str, str], command: list[str], stdin_text: str | None):
    config = json.loads(env.get("CAIRN_EXTENSIONS_JSON", "{}"))
    skills = config.get("skill_paths", [])
    servers = {name: value for name, value in config.get("mcp_servers", {}).items() if value.get("enabled", True)}
    if not skills and not servers:
        return command, stdin_text
    root = Path(workspace) / ".cairn-extensions" / uuid4().hex
    root.mkdir(parents=True)
    env["CAIRN_EXTENSION_TRACE"] = str(root / "usage.jsonl")
    manifest = []
    index = []
    for number, value in enumerate(skills):
        source, metadata = read_skill(value)
        destination = root / "skills" / f"skill-{number}"
        # A skill bundle is a directory, including its scripts and references.
        for path in source.parent.rglob("*"):
            if path.is_symlink():
                raise ValueError("Skill bundles must not contain symbolic links")
        shutil.copytree(source.parent, destination, ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", "__pycache__"))
        manifest.append({"name": metadata["name"], "path": str(destination / "SKILL.md")})
        index.append(f"- {metadata['name']}: {metadata['description']}\n  Instructions: {destination / 'SKILL.md'}")
    env["CAIRN_SKILL_MANIFEST"] = json.dumps(manifest)
    context = ""
    if index:
        context = ("\n\n## User-configured Skills\nThese skills are available for this task. "
                   "When a skill is relevant, call cairn_read_skill with its name to read SKILL.md, then follow its instructions. "
                   "Resolve scripts and references relative to that file.\n" + "\n".join(index))
    command = list(command)
    pi_runner = "cairn.dispatcher.workers.adapters.pi_runner" in command
    if pi_runner:
        payload = json.loads(stdin_text or "{}")
        payload["prompt"] = payload.get("prompt", "") + context
        stdin_text = json.dumps(payload)
        if servers or skills:
            env["CAIRN_PI_MCP_EXTENSION"] = str(Path(__file__).with_name("pi_mcp.ts"))
            env["CAIRN_PYTHON"] = sys.executable
        return command, stdin_text
    binary = Path(command[0]).stem.casefold()
    if binary not in {"codex", "claude"}:
        raise ValueError("Skills/MCP require the Codex, Claude Code, or Pi host adapter")
    if context:
        if stdin_text is not None:
            stdin_text += context
        elif "--" in command:
            command[-1] += context
        else:
            raise ValueError("Cannot locate the worker prompt for Skill injection")
    if servers or skills:
        if binary == "codex":
            settings = {
                "command": sys.executable,
                "args": ["-m", "cairn.extensions.mcp_proxy"],
                "env_vars": ["CAIRN_EXTENSIONS_JSON", "PYTHONPATH", "CAIRN_EXTENSION_TRACE", "CAIRN_SKILL_MANIFEST"],
                "startup_timeout_sec": 60,
                "tool_timeout_sec": 90,
            }
            flags = []
            for key, value in settings.items():
                flags.extend(["-c", f"mcp_servers.cairn_extensions.{key}={json.dumps(value)}"])
        else:
            path = root / "mcp.json"
            path.write_text(json.dumps({"mcpServers": {"cairn_extensions": {
                "command": sys.executable, "args": ["-m", "cairn.extensions.mcp_proxy"],
            }}}), encoding="utf-8")
            flags = ["--strict-mcp-config", "--mcp-config", str(path)]
        position = command.index("--") if "--" in command else len(command) - 1
        command[position:position] = flags
    return command, stdin_text
