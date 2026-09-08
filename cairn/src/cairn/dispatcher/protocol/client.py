from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import logging
import threading

from pydantic import TypeAdapter
import requests
from requests.adapters import HTTPAdapter

from cairn.server.models import AgentRuntime, Intent, ProjectDetail, ProjectSummary, Settings

LOG = logging.getLogger(__name__)


class ProtocolError(RuntimeError):
    def __init__(self, message: str, status_code: int, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


@dataclass(slots=True)
class ApiResult:
    status_code: int
    data: Any | None = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class CairnClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._summary_adapter = TypeAdapter(list[ProjectSummary])
        self._agent_adapter = TypeAdapter(list[AgentRuntime])
        self._local = threading.local()
        self._sessions: dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def list_projects(
        self, *, kind: str | None = None, parent_id: str | None = None
    ) -> list[ProjectSummary]:
        params = {key: value for key, value in {"kind": kind, "parent_id": parent_id}.items() if value}
        response = self._session().get(self._url("/projects"), params=params, timeout=self._timeout)
        response.raise_for_status()
        return self._summary_adapter.validate_python(response.json())

    def get_project(self, project_id: str) -> ProjectDetail:
        response = self._session().get(self._url(f"/projects/{project_id}"), timeout=self._timeout)
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def download_attachment(self, project_id: str, attachment_id: str) -> bytes:
        response = self._session().get(
            self._url(f"/projects/{project_id}/attachments/{attachment_id}"), timeout=max(self._timeout, 60),
        )
        response.raise_for_status()
        return response.content

    def get_settings(self) -> Settings:
        response = self._session().get(self._url("/settings"), timeout=self._timeout)
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def list_runtime_agents(self) -> list[AgentRuntime]:
        response = self._session().get(self._url("/agents/runtime"), timeout=self._timeout)
        response.raise_for_status()
        return self._agent_adapter.validate_python(response.json())

    def export_project(self, project_id: str) -> str:
        response = self._session().get(
            self._url(f"/projects/{project_id}/export"),
            params={"format": "yaml"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.text

    def heartbeat(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/heartbeat",
            json={"worker": worker},
        )

    def claim_reason(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/claim",
            json={"worker": worker, "trigger": trigger},
        )

    def reason_heartbeat(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/heartbeat",
            json={"worker": worker},
        )

    def release_reason(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/release",
            json={"worker": worker},
        )

    def release(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/release",
            json={"worker": worker},
        )

    def conclude(self, project_id: str, intent_id: str, worker: str, description: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            json={"worker": worker, "description": description},
        )

    def complete(
        self,
        project_id: str,
        from_ids: list[str],
        description: str,
        worker: str,
        evidence: dict[str, Any] | None = None,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/complete",
            json={
                "from": from_ids,
                "description": description,
                "worker": worker,
                "evidence": evidence,
            },
        )

    def update_agent_health(self, agent_id: str, status: str, detail: str | None = None) -> ApiResult:
        return self._request_json(
            "PUT",
            f"/agents/{agent_id}/health",
            json={"status": status, "detail": detail},
        )

    def update_project_status(self, project_id: str, status: str) -> ApiResult:
        return self._request_json(
            "PUT",
            f"/projects/{project_id}/status",
            json={"status": status},
        )

    def create_intent(
        self,
        project_id: str,
        from_ids: list[str],
        description: str,
        creator: str,
        *,
        child_title: str | None = None,
        child_origin: str | None = None,
        child_goal: str | None = None,
        priority: int = 100,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents",
            json={
                "from": from_ids,
                "description": description,
                "creator": creator,
                "worker": None,
                "child_title": child_title,
                "child_origin": child_origin,
                "child_goal": child_goal,
                "priority": priority,
            },
        )

    def move_agent(
        self,
        parent_project_id: str,
        agent_id: str,
        target_child_id: str,
        reason: str,
        *,
        breakthrough: bool = False,
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{parent_project_id}/allocations/move",
            json={
                "agent_id": agent_id,
                "target_child_id": target_child_id,
                "reason": reason,
                "breakthrough": breakthrough,
            },
        )

    def create_hint(self, project_id: str, content: str, creator: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/hints",
            json={"content": content, "creator": creator},
        )

    def materialize_children(self, parent_project_id: str) -> ApiResult:
        return self._request_json(
            "POST", f"/projects/{parent_project_id}/children/materialize", json={}
        )

    def rebalance_allocations(self, parent_project_id: str) -> ApiResult:
        return self._request_json(
            "POST", f"/projects/{parent_project_id}/allocations/rebalance", json={}
        )

    def publish_child_fact(self, child_project_id: str, fact_id: str, description: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{child_project_id}/blackboard/facts",
            json={"fact_id": fact_id, "description": description},
        )

    def sync_child_completion(
        self, child_project_id: str, description: str, *, breakthrough: bool = False
    ) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{child_project_id}/sync-completion",
            json={"description": description, "breakthrough": breakthrough},
        )

    def update_child_process(
        self,
        child_project_id: str,
        status: str,
        *,
        pid: int | None = None,
        log_dir: str | None = None,
        error: str | None = None,
    ) -> ApiResult:
        return self._request_json(
            "PUT",
            f"/projects/{child_project_id}/process",
            json={"status": status, "pid": pid, "log_dir": log_dir, "error": error},
        )

    def _request_json(self, method: str, path: str, json: dict[str, Any]) -> ApiResult:
        try:
            response = self._session().request(
                method,
                self._url(path),
                json=json,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            LOG.warning("request failed method=%s path=%s error=%s", method, path, exc)
            return ApiResult(status_code=0, text=str(exc))
        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            data = response.json()
        if 200 <= response.status_code < 300:
            from cairn.extensions.trace import attach
            try:
                attach(self, path, data)
            except (requests.RequestException, ValueError, OSError):
                LOG.warning("Fact saved, but extension trace could not be attached", exc_info=True)
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session

        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._local.session = session
        with self._sessions_lock:
            self._sessions[threading.get_ident()] = session
        return session
