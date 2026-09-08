from __future__ import annotations

import json
import asyncio

from cairn.extensions.library import resolve
from cairn.extensions.config import public_mcp, merge_mcp, validate_skill_paths

from fastapi import APIRouter, HTTPException, Request
import requests

from cairn.server.db import get_conn
from cairn.server.models import (
    AgentHealthUpdate,
    AgentRuntime,
    AgentSummary,
    AgentUpsertRequest,
    DiscoverAgentModelsRequest,
    DiscoveredModels,
)
from cairn.server.services import next_agent_id, utcnow

router = APIRouter(prefix="/agents", tags=["agents"])

_MODEL_NAME_FIELDS = (
    "id",
    "name",
    "model",
    "model_id",
    "modelId",
    "model_name",
    "modelName",
    "display_name",
    "displayName",
    "slug",
    "model_slug",
    "modelSlug",
    "canonical_slug",
    "canonicalSlug",
)
_MODEL_COLLECTION_FIELDS = (
    "data",
    "models",
    "items",
    "result",
    "results",
    "model_list",
    "modelList",
    "model_ids",
    "model_names",
    "available_models",
    "availableModels",
    "channels",
    "endpoints",
)
_MODEL_MAPPING_METADATA_KEYS = {
    "code",
    "detail",
    "error",
    "errors",
    "message",
    "object",
    "status",
    "type",
}


def _model_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _extract_model_names(payload: object) -> list[str]:
    """Extract model identifiers from common OpenAI-compatible response shapes."""
    names: list[str] = []

    def add(value: object) -> None:
        name = _model_name(value)
        if name and name not in names:
            names.append(name)

    def visit(value: object, *, mapping_keys: bool = False) -> None:
        if isinstance(value, str):
            add(value)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return

        collection_fields = [field for field in _MODEL_COLLECTION_FIELDS if field in value]
        if collection_fields:
            for field in collection_fields:
                visit(value[field], mapping_keys=field in {"data", "models"})
            return

        for field in _MODEL_NAME_FIELDS:
            name = _model_name(value.get(field))
            if name:
                add(name)
                return

        if mapping_keys:
            for key, item in value.items():
                key_name = _model_name(key)
                if not key_name or key_name.casefold() in _MODEL_MAPPING_METADATA_KEYS:
                    continue
                if isinstance(item, (dict, list)):
                    before = len(names)
                    visit(item)
                    if len(names) == before:
                        add(key_name)
                else:
                    add(key_name)

    visit(payload)
    return sorted(names, key=str.casefold)


def _model_endpoint_urls(base_url: str) -> list[str]:
    """Return compatible model-list URLs for a provider base or endpoint URL."""
    base = base_url.strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/messages"):
        if base.casefold().endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
            break

    candidates: list[str] = []

    def add(url: str) -> None:
        if url and url not in candidates:
            candidates.append(url)

    if base.casefold().endswith("/models"):
        add(base)
    else:
        add(f"{base}/models")

    if base.casefold().endswith("/v1"):
        add(f"{base[:-3].rstrip('/')}/models")
    elif not base.casefold().endswith("/models"):
        add(f"{base}/v1/models")
    return candidates


def _model_endpoint_url(base_url: str) -> str:
    """Keep the original single-URL helper for callers and tests."""
    return _model_endpoint_urls(base_url)[0]


def _model_request_headers(api_key: str) -> list[dict[str, str]]:
    """Return common OpenAI, Anthropic, and gateway authentication variants."""
    return [
        {"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
        },
        {"api-key": api_key, "Accept": "application/json"},
    ]


def _is_anthropic_endpoint(base_url: str) -> bool:
    """Whether a provider may legitimately need the Anthropic auth header."""
    value = base_url.strip().rstrip("/").casefold()
    return "anthropic" in value or value.endswith("/messages")


def _payload_shape(payload: object) -> str:
    if isinstance(payload, dict):
        keys = [str(key) for key in payload.keys()]
        return "object keys=" + ",".join(keys[:12])
    if isinstance(payload, list):
        return f"array length={len(payload)}"
    return type(payload).__name__


def _api_key_hint(api_key: str) -> str:
    if len(api_key) <= 4:
        return "****"
    return f"****{api_key[-4:]}"


def _agent_summary(row) -> AgentSummary:
    return AgentSummary(
        id=row["id"],
        name=row["name"],
        base_url=row["base_url"],
        model=row["model"],
        api_format=row["api_format"],
        skill_paths=json.loads(row["skill_paths"]),
        mcp_servers=public_mcp(json.loads(row["mcp_servers"])),
        enabled=bool(row["enabled"]),
        task_types=json.loads(row["task_types"]),
        max_running=row["max_running"],
        priority=row["priority"],
        health_status=row["health_status"],
        health_detail=row["health_detail"],
        health_checked_at=row["health_checked_at"],
        health_failures=row["health_failures"],
        api_key_configured=bool(row["api_key"]),
        api_key_hint=_api_key_hint(row["api_key"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get("", response_model=list[AgentSummary])
def list_agents():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM agents ORDER BY priority, name").fetchall()
        return [_agent_summary(row) for row in rows]


@router.get("/runtime", response_model=list[AgentRuntime])
def list_runtime_agents(request: Request):
    """Dispatcher-only view; API secrets never leave the local host."""
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(403, "Runtime Agent credentials are only available from localhost")
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM agents WHERE enabled = 1 ORDER BY priority, name").fetchall()
        return [AgentRuntime(**{**_agent_summary(row).model_dump(), **resolve(conn, row)}, api_key=row["api_key"]) for row in rows]


@router.put("/{agent_id}/health", response_model=AgentSummary)
def update_agent_health(agent_id: str, body: AgentHealthUpdate, request: Request):
    """Accept health reports only from a local Dispatcher process."""
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(403, "Agent health can only be updated from localhost")
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Agent not found")
        failures = 0 if body.status == "healthy" else row["health_failures"] + 1
        conn.execute(
            """
            UPDATE agents
            SET health_status = ?, health_detail = ?, health_checked_at = ?,
                health_failures = ?, updated_at = ?
            WHERE id = ?
            """,
            (body.status, body.detail, utcnow(), failures, utcnow(), agent_id),
        )
        updated = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return _agent_summary(updated)


@router.post("/discover-models", response_model=DiscoveredModels)
def discover_models(body: DiscoverAgentModelsRequest):
    api_key = body.api_key
    api_format = body.api_format
    if body.agent_id and (not api_key or "api_format" not in body.model_fields_set):
        with get_conn() as conn:
            row = conn.execute("SELECT api_key, api_format FROM agents WHERE id = ?", (body.agent_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Agent not found")
            api_key = api_key or row["api_key"]
            if "api_format" not in body.model_fields_set:
                api_format = row["api_format"]
    if not api_key:
        raise HTTPException(400, "API key is required to discover models")

    last_non_success = None
    last_error: requests.RequestException | None = None
    empty_shapes: list[str] = []
    anthropic_endpoint = api_format == "anthropic" or (api_format == "auto" and _is_anthropic_endpoint(body.base_url))
    header_options = _model_request_headers(api_key)
    if anthropic_endpoint:
        header_options = [header_options[1], header_options[0]]
    elif api_format != "auto":
        header_options = header_options[:1]
    for url in _model_endpoint_urls(body.base_url):
        for headers in header_options:
            try:
                # Keep the UI responsive when a gateway drops or blackholes a
                # request. A model listing should never block for minutes.
                response = requests.get(url, headers=headers, timeout=(5, 10))
            except requests.RequestException as exc:
                last_error = exc
                break

            if not 200 <= response.status_code < 300:
                # A 401/403 is a credential failure for an OpenAI-compatible
                # endpoint. Retrying another path or header cannot make the
                # same token valid and previously made the button appear stuck.
                last_non_success = response
                if response.status_code in {401, 403} and anthropic_endpoint:
                    # Anthropic's Messages API commonly rejects Bearer before
                    # accepting x-api-key, so retain the header fallbacks there.
                    continue
                break
            try:
                payload = response.json()
            except ValueError:
                empty_shapes.append("non-JSON response")
                continue

            models = _extract_model_names(payload)
            if models:
                return DiscoveredModels(models=models)
            empty_shapes.append(_payload_shape(payload))

        # A connection failure is endpoint-wide; trying the alternate path
        # would only repeat the same timeout.
        if last_error is not None or (
            last_non_success is not None
            and last_non_success.status_code in {401, 403}
            and not anthropic_endpoint
        ):
            break

    if last_non_success is not None:
        detail = " ".join(last_non_success.text.split())[:300]
        raise HTTPException(
            last_non_success.status_code,
            f"Model endpoint returned {last_non_success.status_code}: {detail}",
        )
    if empty_shapes:
        shapes = "; ".join(dict.fromkeys(empty_shapes))
        raise HTTPException(502, f"Model endpoint returned no model names ({shapes})")
    if last_error is not None:
        raise HTTPException(502, f"Model discovery failed: {last_error}") from last_error
    raise HTTPException(502, "Model discovery failed: no model endpoint response")


@router.post("", response_model=AgentSummary, status_code=201)
def create_agent(body: AgentUpsertRequest):
    if not body.api_key:
        raise HTTPException(400, "API key is required")
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM agents WHERE name = ?", (body.name,)).fetchone():
            raise HTTPException(409, "Agent name already exists")
        agent_id = next_agent_id(conn)
        now = utcnow()
        conn.execute(
            """
            INSERT INTO agents
                (id, name, base_url, api_key, model, api_format, skill_paths, mcp_servers, enabled, task_types, max_running, priority, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_id,
                body.name,
                body.base_url.rstrip("/"),
                body.api_key,
                body.model,
                body.api_format,
                json.dumps(body.skill_paths),
                _saved_mcp(body, {}),
                int(body.enabled),
                json.dumps(body.task_types, separators=(",", ":")),
                body.max_running,
                body.priority,
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return _agent_summary(row)


@router.put("/{agent_id}", response_model=AgentSummary)
def update_agent(agent_id: str, body: AgentUpsertRequest):
    with get_conn() as conn:
        current = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if current is None:
            raise HTTPException(404, "Agent not found")
        duplicate = conn.execute(
            "SELECT 1 FROM agents WHERE name = ? AND id != ?", (body.name, agent_id)
        ).fetchone()
        if duplicate:
            raise HTTPException(409, "Agent name already exists")
        api_key = body.api_key or current["api_key"]
        conn.execute(
            """
            UPDATE agents
            SET name = ?, base_url = ?, api_key = ?, model = ?, api_format = ?, skill_paths = ?, mcp_servers = ?, enabled = ?,
                task_types = ?, max_running = ?, priority = ?, health_status = 'unknown',
                health_detail = NULL, health_checked_at = NULL, health_failures = 0,
                updated_at = ?
            WHERE id = ?
            """,
            (
                body.name,
                body.base_url.rstrip("/"),
                api_key,
                body.model,
                body.api_format if "api_format" in body.model_fields_set else current["api_format"],
                json.dumps(body.skill_paths) if "skill_paths" in body.model_fields_set else current["skill_paths"],
                _saved_mcp(body, json.loads(current["mcp_servers"])) if "mcp_servers" in body.model_fields_set else current["mcp_servers"],
                int(body.enabled),
                json.dumps(body.task_types, separators=(",", ":")),
                body.max_running,
                body.priority,
                utcnow(),
                agent_id,
            ),
        )
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return _agent_summary(row)


@router.delete("/{agent_id}", status_code=204)
def delete_agent(agent_id: str):
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,)).fetchone() is None:
            raise HTTPException(404, "Agent not found")
        linked = conn.execute(
            "SELECT COUNT(*) AS count FROM project_agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()["count"]
        linked += conn.execute(
            "SELECT COUNT(*) AS count FROM projects WHERE parent_agent_id = ?", (agent_id,)
        ).fetchone()["count"]
        if linked:
            raise HTTPException(409, "Agent is used by existing projects; disable it instead")
        conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))


def _saved_mcp(body: AgentUpsertRequest, previous: dict) -> str:
    try:
        raw = {name: server.model_dump() for name, server in body.mcp_servers.items()}
        return json.dumps(merge_mcp(raw, previous))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{agent_id}/extensions/test")
async def test_agent_extensions(agent_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT skill_paths, mcp_servers FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Agent not found")
        skills = json.loads(row["skill_paths"])
        servers = json.loads(row["mcp_servers"])
    try:
        validate_skill_paths(skills)
        from cairn.extensions.mcp_client import MCPConnections
        async with asyncio.timeout(45):
            async with MCPConnections(servers) as clients:
                tools = await clients.list_tools()
        return {"ok": True, "skills": skills, "tools": [{"name": tool.name, "description": tool.description} for tool in tools]}
    except Exception as exc:
        raise HTTPException(502, f"Extension check failed ({type(exc).__name__}); check Skill paths, MCP command/URL and credentials") from exc
