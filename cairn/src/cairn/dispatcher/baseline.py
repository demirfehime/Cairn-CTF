from __future__ import annotations

from dataclasses import dataclass

from cairn.server.models import ProjectDetail


BASELINE_CREATOR_PREFIX = "dispatcher.baseline."


@dataclass(frozen=True, slots=True)
class BaselineTask:
    key: str
    description: str
    coverage_markers: tuple[str, ...]

    @property
    def creator(self) -> str:
        return f"{BASELINE_CREATOR_PREFIX}{self.key}"


BASELINE_TASKS: tuple[BaselineTask, ...] = (
    BaselineTask(
        key="surface_mapping",
        description=(
            "Map the authorized target's reachable attack surface: fingerprint technologies, enumerate "
            "forms, parameters, linked routes, HTTP methods, redirects, cookies, and directly exposed APIs."
        ),
        coverage_markers=("fingerprint", "attack surface", "基础枚举", "指纹"),
    ),
    BaselineTask(
        key="weak_credentials",
        description=(
            "Perform a rate-limited default and weak credential check against in-scope login interfaces. "
            "Use only a small curated list (maximum 20 attempts per account or interface), stop immediately "
            "on lockout or rate-limit signals, and do not target real or unrelated accounts."
        ),
        coverage_markers=(
            "weak credential",
            "default credential",
            "weak password",
            "default password",
            "弱口令",
            "默认口令",
        ),
    ),
    BaselineTask(
        key="injection",
        description=(
            "Test in-scope inputs for SQL/NoSQL injection, command injection, server-side template injection, "
            "and other interpreter boundary failures using non-destructive proof payloads."
        ),
        coverage_markers=("sql injection", "nosql", "command injection", "template injection", "注入"),
    ),
    BaselineTask(
        key="access_control",
        description=(
            "Check authentication, session handling, authorization boundaries, IDOR, forced browsing, and "
            "privilege transitions without accessing unrelated users' data."
        ),
        coverage_markers=("access control", "authorization", "session", "idor", "权限", "会话"),
    ),
    BaselineTask(
        key="ssrf",
        description=(
            "Identify URL fetch, proxy, webhook, import, preview, or callback features and safely test for "
            "server-side request forgery within the authorized target scope."
        ),
        coverage_markers=("ssrf", "server-side request forgery"),
    ),
    BaselineTask(
        key="file_handling",
        description=(
            "Test path and file handling for traversal, local file inclusion, unsafe download, upload validation, "
            "archive extraction, and unintended file disclosure using non-destructive evidence."
        ),
        coverage_markers=("path traversal", "lfi", "file inclusion", "file upload", "目录穿越", "文件读取", "上传"),
    ),
    BaselineTask(
        key="client_side",
        description=(
            "Test reflected, stored, and DOM-based XSS together with CSRF-sensitive state changes using safe "
            "proofs that do not affect other users."
        ),
        coverage_markers=("xss", "csrf", "cross-site scripting", "cross-site request forgery", "跨站"),
    ),
    BaselineTask(
        key="sensitive_exposure",
        description=(
            "Check for debug endpoints, backups, source maps, exposed source or configuration, logs, default "
            "administration routes, verbose errors, and other sensitive information disclosure."
        ),
        coverage_markers=("backup", "source disclosure", "source map", "debug", "sensitive information", "备份", "源码", "调试"),
    ),
    BaselineTask(
        key="api_abuse",
        description=(
            "Assess APIs for undocumented methods, parameter pollution, mass assignment, unsafe content types, "
            "inconsistent validation, and authorization differences between equivalent endpoints."
        ),
        coverage_markers=("mass assignment", "parameter pollution", "undocumented method", "参数污染", "批量赋值"),
    ),
    BaselineTask(
        key="business_logic",
        description=(
            "Examine workflows and state transitions for business-logic bypass, replay, race conditions, invalid "
            "ordering, quantity or value manipulation, and missing server-side invariants."
        ),
        coverage_markers=("business logic", "race condition", "workflow bypass", "业务逻辑", "竞态"),
    ),
)


def missing_baseline_tasks(project: ProjectDetail) -> list[BaselineTask]:
    descriptions = [intent.description.casefold() for intent in project.intents]
    creators = {intent.creator for intent in project.intents}
    missing: list[BaselineTask] = []
    for task in BASELINE_TASKS:
        if task.creator in creators:
            continue
        if any(
            marker.casefold() in description
            for marker in task.coverage_markers
            for description in descriptions
        ):
            continue
        missing.append(task)
    return missing
