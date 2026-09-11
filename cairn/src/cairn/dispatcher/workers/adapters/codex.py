from __future__ import annotations

import json
import re

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, RegexSessionDriver
from cairn.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


class CodexDriver(RegexSessionDriver):
    type_name = "codex"

    def __init__(self, local: bool = False, stdin_prompt: bool = False):
        self.local = local
        self.stdin_prompt = stdin_prompt

    def local_binary(self) -> str | None:
        return "codex"

    def _local_argv(self, worker: WorkerConfig, session: str | None = None) -> list[str]:
        env = worker.env
        sandbox = env.get("CAIRN_CODEX_SANDBOX", "workspace-write")
        if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("invalid CAIRN_CODEX_SANDBOX")
        argv = ["codex", "exec", "--sandbox", sandbox, "--json", "--skip-git-repo-check", "-c", 'approval_policy="never"']
        domains = list(dict.fromkeys(host.strip() for host in env.get("CAIRN_CODEX_NETWORK_ALLOW", "").split(",") if host.strip()))
        if domains:
            if sandbox != "workspace-write":
                raise ValueError("network allow requires CAIRN_CODEX_SANDBOX=workspace-write")
            rules = ", ".join(f'{json.dumps(host)} = "allow"' for host in domains)
            argv += ["-c", "sandbox_workspace_write.network_access=true",
                     "-c", "features.network_proxy.enabled=true",
                     "-c", "features.network_proxy.domains={ " + rules + " }"]
        if all(env.get(key) for key in ("CODEX_MODEL", "CODEX_BASE_URL", "OPENAI_API_KEY")):
            provider = "cairn_agent_" + re.sub(r"[^a-zA-Z0-9_]", "_", worker.agent_id or worker.name)
            argv += ["--model", env["CODEX_MODEL"], "-c", f'model_provider={json.dumps(provider)}']
            for key, value in {
                "name": env.get("CAIRN_AGENT_DISPLAY_NAME", worker.name),
                "base_url": env["CODEX_BASE_URL"], "env_key": "OPENAI_API_KEY", "wire_api": "responses",
            }.items():
                argv += ["-c", f"model_providers.{provider}.{key}={json.dumps(value)}"]
        if session:
            argv += ["resume", session]
        return argv

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                return event["thread_id"]
        return super().extract_session(session, stdout, stderr)

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        messages = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if isinstance(event, dict) and event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                messages.append(item["text"])
        return messages[-1] if messages else stdout

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        env = worker.env
        return http_ping(
            f"{env['CODEX_BASE_URL']}/responses",
            headers={
                "Authorization": f"Bearer {env['OPENAI_API_KEY']}",
                "content-type": "application/json",
            },
            json_body={
                "model": env["CODEX_MODEL"],
                "input": [{"role": "user", "content": "ping"}],
                "stream": False,
            },
            timeout=timeout,
            proxies=proxies_from_env(env),
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        return f"POST {worker.env['CODEX_BASE_URL']}/responses (model={worker.env['CODEX_MODEL']})"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        if self.local:
            argv = self._local_argv(worker)
            return DriverResult(argv=argv + (["-"] if self.stdin_prompt else ["--", prompt]),
                                stdin_text=prompt if self.stdin_prompt else None)
        env = worker.env
        argv = [
            "codex",
            "exec",
            "--ignore-user-config",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            env["CODEX_MODEL"],
            "-c",
            'model_provider="cairn"',
            "-c",
            'model_providers.cairn.name="cairn"',
            "-c",
            'model_providers.cairn.wire_api="responses"',
            "-c",
            'model_reasoning_effort="high"',
            "-c",
            f'model_providers.cairn.base_url="{env["CODEX_BASE_URL"]}"',
            "-c",
            'model_providers.cairn.env_key="OPENAI_API_KEY"',
        ]
        if self.stdin_prompt:
            argv.append("-")
            return DriverResult(argv=argv, stdin_text=prompt)
        argv.extend(["--", prompt])
        return DriverResult(argv=argv)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> DriverResult:
        if self.local:
            argv = self._local_argv(worker, session)
            return DriverResult(argv=argv + (["-"] if self.stdin_prompt else ["--", prompt]),
                                stdin_text=prompt if self.stdin_prompt else None)
        env = worker.env
        argv = [
            "codex",
            "exec",
            "resume",
            session,
            "--ignore-user-config",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            env["CODEX_MODEL"],
            "-c",
            'model_provider="cairn"',
            "-c",
            'model_providers.cairn.name="cairn"',
            "-c",
            'model_providers.cairn.wire_api="responses"',
            "-c",
            'model_reasoning_effort="high"',
            "-c",
            f'model_providers.cairn.base_url="{env["CODEX_BASE_URL"]}"',
            "-c",
            'model_providers.cairn.env_key="OPENAI_API_KEY"',
        ]
        if self.stdin_prompt:
            argv.append("-")
            return DriverResult(argv=argv, stdin_text=prompt)
        argv.extend(["--", prompt])
        return DriverResult(argv=argv)
