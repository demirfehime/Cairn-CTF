from __future__ import annotations

from typing import Literal

from cairn.extensions.config import MCPServer, validate_skill_paths

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_serializer


class Settings(BaseModel):
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)
    agent_reassignment_cooldown: int = Field(default=300, ge=30)
    parent_review_debounce: int = Field(default=15, ge=5)


class AgentSummary(BaseModel):
    skill_paths: list[str] = Field(default_factory=list)
    mcp_servers: dict[str, MCPServer] = Field(default_factory=dict)
    api_format: Literal["auto", "anthropic", "openai-chat", "openai-responses"] = "auto"

    id: str
    name: str
    base_url: str
    model: str
    enabled: bool
    task_types: list[Literal["bootstrap", "reason", "explore"]]
    max_running: int
    priority: int
    health_status: Literal["unknown", "healthy", "unhealthy"] = "unknown"
    health_detail: str | None = None
    health_checked_at: str | None = None
    health_failures: int = 0
    api_key_configured: bool = True
    api_key_hint: str = ""
    created_at: str
    updated_at: str


class AgentRuntime(AgentSummary):
    api_key: str


class AgentHealthUpdate(BaseModel):
    status: Literal["healthy", "unhealthy"]
    detail: str | None = None

    @field_validator("detail")
    @classmethod
    def normalize_health_detail(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text[:1000] or None


class AgentUpsertRequest(BaseModel):
    skill_paths: list[str] = Field(default_factory=list)
    mcp_servers: dict[str, MCPServer] = Field(default_factory=dict)
    api_format: Literal["auto", "anthropic", "openai-chat", "openai-responses"] = "auto"

    name: str
    base_url: str
    api_key: str | None = None
    model: str
    enabled: bool = True
    task_types: list[Literal["bootstrap", "reason", "explore"]] = Field(
        default_factory=lambda: ["bootstrap", "reason", "explore"]
    )
    max_running: int = Field(default=1, gt=0)
    priority: int = Field(default=0, ge=0)

    @field_validator("name", "base_url", "model", "api_key")
    @classmethod
    def validate_agent_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("task_types")
    @classmethod
    def validate_agent_task_types(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("task_types must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("task_types must be unique")
        return value

    @field_validator("skill_paths")
    @classmethod
    def validate_skills(cls, value):
        return validate_skill_paths(value)

    @field_validator("mcp_servers")
    @classmethod
    def validate_servers(cls, value):
        import re
        if len(value) > 16 or any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) for name in value):
            raise ValueError("Use at most 16 MCP servers with simple alphanumeric names")
        return value


class DiscoverAgentModelsRequest(BaseModel):
    api_format: Literal["auto", "anthropic", "openai-chat", "openai-responses"] = "auto"

    # ``api_base_url`` was used by the earlier CTF client. Accept it while
    # exposing the current canonical ``base_url`` to the router and UI.
    base_url: str = Field(validation_alias=AliasChoices("base_url", "api_base_url"))
    api_key: str | None = None
    agent_id: str | None = None

    @field_validator("base_url", "api_key", "agent_id")
    @classmethod
    def validate_discovery_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class DiscoveredModels(BaseModel):
    models: list[str]


class ProjectAgentSelection(BaseModel):
    agent_id: str
    name: str
    model: str
    use_hints: bool = True
    role: Literal["participant", "assigned"] = "participant"
    assignment_epoch: int = 0
    health_status: Literal["unknown", "healthy", "unhealthy"] = "unknown"
    health_detail: str | None = None
    health_checked_at: str | None = None
    connection_status: Literal["connected", "degraded", "offline", "unknown"] = "unknown"
    task_status: Literal["idle", "working", "paused", "completed"] = "idle"
    current_task: str | None = None
    current_task_type: Literal["bootstrap", "reason", "explore"] | None = None
    current_intent_id: str | None = None
    task_started_at: str | None = None
    last_heartbeat_at: str | None = None
    assigned_child_id: str | None = None
    assigned_child_title: str | None = None


class CreateProjectAgentSelection(BaseModel):
    agent_id: str
    use_hints: bool = True

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class Fact(BaseModel):
    id: str
    description: str
    extension_trace: dict | None = None

    @model_serializer(mode='wrap')
    def serialize_fact(self, handler):
        result = handler(self)
        if self.extension_trace is None:
            result.pop('extension_trace', None)
        return result


class Intent(BaseModel):
    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None
    child_title: str | None = None
    child_origin: str | None = None
    child_goal: str | None = None
    priority: int = 100

    model_config = {"populate_by_name": True}


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    created_at: str


class ProjectReason(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


class ProjectMeta(BaseModel):
    id: str
    title: str
    status: Literal["active", "paused", "stopped", "completed"]
    bootstrap_enabled: bool
    created_at: str
    reason: ProjectReason | None = None
    agents: list[ProjectAgentSelection] = Field(default_factory=list)
    kind: Literal["parent", "child"] = "parent"
    parent_project_id: str | None = None
    parent_intent_id: str | None = None
    parent_agent: ProjectAgentSelection | None = None
    priority: int = 0
    summary: str | None = None
    breakthrough: bool = False
    process_status: Literal["stopped", "starting", "running", "failed"] = "stopped"
    process_pid: int | None = None


class FlagCandidate(BaseModel):
    id: str
    project_id: str
    value: str
    source_fact_id: str
    reported_source: str
    description: str
    worker: str
    status: Literal["pending", "confirmed", "rejected"] = "pending"
    created_at: str
    decided_at: str | None = None
    decided_by: str | None = None
    decision_note: str | None = None


class ProjectSummary(ProjectMeta):
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int


class ProjectAttachment(BaseModel):
    id: str
    name: str
    size: int
    sha256: str
    project_id: str | None = None


class ProjectDetail(BaseModel):
    attachments: list[ProjectAttachment] = Field(default_factory=list)
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]
    children: list[ProjectSummary] = Field(default_factory=list)
    blackboard: list["BlackboardEntry"] = Field(default_factory=list)
    flag_candidates: list[FlagCandidate] = Field(default_factory=list)


class CreateHintInline(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)
    title: str
    origin: str
    goal: str
    bootstrap_enabled: bool = True
    hints: list[CreateHintInline] | None = None
    agents: list[CreateProjectAgentSelection] = Field(default_factory=list)
    parent_agent_id: str | None = None

    @field_validator("title", "origin", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateHintRequest(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateIntentRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str
    worker: str | None = None
    child_title: str | None = None
    child_origin: str | None = None
    child_goal: str | None = None
    priority: int = Field(default=100, ge=0)

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "worker", "child_title", "child_origin", "child_goal")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class HeartbeatRequest(BaseModel):
    worker: str

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReasonClaimRequest(BaseModel):
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeRequest(BaseModel):
    worker: str
    description: str

    @field_validator("worker", "description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CompletionEvidence(BaseModel):
    kind: Literal["flag", "proof"]
    value: str
    source: str
    verified: bool = False

    @field_validator("value", "source")
    @classmethod
    def validate_evidence_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str
    evidence: CompletionEvidence | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class FlagCandidateDecisionRequest(BaseModel):
    decision: Literal["confirm", "reject"]
    actor: str
    note: str | None = None

    @field_validator("actor", "note")
    @classmethod
    def validate_decision_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeResponse(BaseModel):
    fact: Fact
    intent: Intent


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "paused", "stopped"]


class BlackboardEntry(BaseModel):
    id: str
    parent_project_id: str
    child_project_id: str | None = None
    kind: Literal[
        "origin",
        "goal",
        "fact",
        "intent_created",
        "intent_claimed",
        "intent_released",
        "intent_concluded",
        "hint",
        "agent_assigned",
        "agent_released",
        "completion",
        "child_created",
        "child_fact",
        "child_summary",
        "breakthrough",
        "allocation",
        "parent_hint",
        "process",
        "flag_candidate",
        "flag_confirmed",
        "flag_rejected",
    ]
    content: str
    metadata: dict = Field(default_factory=dict)
    created_at: str


class CreateChildRequest(BaseModel):
    parent_intent_id: str
    title: str
    origin: str
    goal: str
    priority: int = Field(default=100, ge=0)

    @field_validator("parent_intent_id", "title", "origin", "goal")
    @classmethod
    def validate_child_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class MoveAgentRequest(BaseModel):
    agent_id: str
    target_child_id: str
    reason: str
    breakthrough: bool = False

    @field_validator("agent_id", "target_child_id", "reason")
    @classmethod
    def validate_move_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ChildProcessUpdateRequest(BaseModel):
    status: Literal["stopped", "starting", "running", "failed"]
    pid: int | None = None
    log_dir: str | None = None
    error: str | None = None


class ChildFactPublishRequest(BaseModel):
    fact_id: str
    description: str


class ChildCompletionSyncRequest(BaseModel):
    description: str
    breakthrough: bool = False


class ParentSyncResponse(BaseModel):
    created_children: list[ProjectMeta] = Field(default_factory=list)
    active_children: list[str] = Field(default_factory=list)
    paused_children: list[str] = Field(default_factory=list)


class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenRequest(BaseModel):
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    intent: Intent
