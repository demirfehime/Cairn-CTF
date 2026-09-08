from __future__ import annotations

import copy
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

KEEP_SECRET = "__CAIRN_KEEP_SECRET__"


class MCPServer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    display_name: str | None = None
    transport: Literal["stdio", "http", "sse"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    env_vars: list[str] = Field(default_factory=list)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_transport(self):
        if self.transport == "stdio":
            if not self.command or not self.command.strip():
                raise ValueError("stdio MCP requires command")
            if self.url:
                raise ValueError("stdio MCP does not use url")
        else:
            from urllib.parse import urlsplit
            url = urlsplit(self.url or "")
            if url.scheme not in {"http", "https"} or not url.hostname:
                raise ValueError("HTTP/SSE MCP requires an http(s) URL")
            if self.command:
                raise ValueError("HTTP/SSE MCP does not use command")
        return self


def read_skill(value: str) -> tuple[Path, dict]:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("Skill paths must be absolute")
    if path.is_dir():
        path /= "SKILL.md"
    if path.name != "SKILL.md" or not path.is_file():
        raise ValueError(f"SKILL.md not found: {path}")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("SKILL.md must be smaller than 1 MiB")
    try:
        text = path.read_text(encoding="utf-8-sig")
        parts = text.split("---", 2)
        metadata = yaml.safe_load(parts[1]) if len(parts) == 3 and not parts[0].strip() else None
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("Cannot read valid UTF-8 Skill YAML metadata") from exc
    if not isinstance(metadata, dict) or not all(isinstance(metadata.get(k), str) and metadata[k].strip() for k in ("name", "description")):
        raise ValueError("SKILL.md requires YAML name and description")
    return path.resolve(), metadata


def validate_skill_paths(paths: list[str]) -> list[str]:
    if len(paths) > 32:
        raise ValueError("At most 32 Skills per Agent")
    return list(dict.fromkeys(str(read_skill(path)[0]) for path in paths))


def public_mcp(servers: dict) -> dict:
    result = copy.deepcopy(servers)
    for server in result.values():
        for field in ("env", "headers"):
            server[field] = {key: KEEP_SECRET for key in server.get(field, {})}
    return result


def merge_mcp(servers: dict, previous: dict) -> dict:
    result = copy.deepcopy(servers)
    for name, server in result.items():
        for field in ("env", "headers"):
            for key, value in server.get(field, {}).items():
                if value == KEEP_SECRET:
                    old = previous.get(name, {}).get(field, {})
                    if key not in old:
                        raise ValueError(f"No saved MCP secret for {name}.{field}.{key}")
                    server[field][key] = old[key]
    return result
