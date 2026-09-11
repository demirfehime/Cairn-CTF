from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from cairn.server import db
from cairn.server.app import app
from cairn.server.routers import agents as agents_router


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as test_client:
        yield test_client


def _create_project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={
            "title": "test",
            "origin": "starting point",
            "goal": "finish",
            "hints": [{"content": "initial clue", "creator": "human"}],
        },
    )
    assert response.status_code == 201
    assert response.json()["project"]["bootstrap_enabled"] is True
    return response.json()["project"]["id"]


def test_fact_scope_parses_full_origin_hostnames(client: TestClient) -> None:
    legacy = client.post(
        "/projects",
        json={
            "title": "legacy",
            "origin": "http://ins-b2423ba0c316fb54-p80.wobushou.com/",
            "goal": "finish",
        },
    )
    assert legacy.status_code == 201

    current = client.post(
        "/projects",
        json={
            "title": "current",
            "origin": "http://127.0.0.1:3000/",
            "goal": "finish",
        },
    )
    assert current.status_code == 201
    project_id = current.json()["project"]["id"]

    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "inspect login", "creator": "reasoner", "worker": None},
    ).status_code == 201
    assert client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    ).status_code == 200

    response = client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={
            "worker": "explorer",
            "description": "The login page is reachable at http://127.0.0.1:3000/.",
        },
    )
    assert response.status_code == 200


def test_project_workflow_create_conclude_complete_and_reopen(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    assert response.status_code == 201
    assert response.json()["id"] == "i001"

    response = client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "explorer"

    response = client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "explorer", "description": "new fact"},
    )
    assert response.status_code == 200
    assert response.json()["fact"] == {"id": "f001", "description": "new fact"}

    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["f001"], "description": "solved", "worker": "reasoner"},
    )
    assert response.status_code == 200
    assert response.json()["to"] == "goal"

    response = client.post(
        f"/projects/{project_id}/reopen",
        json={"description": "human correction", "creator": "human"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["status"] == "active"
    assert payload["fact"] == {"id": "f002", "description": "human correction"}
    assert payload["intent"]["from"] == ["f001"]
    assert payload["intent"]["to"] == "f002"


def test_parent_completion_is_blocked_while_work_remains(client: TestClient) -> None:
    project_id = _create_project(client)
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET kind='parent' WHERE id=?", (project_id,))
        conn.execute("INSERT INTO facts(id,project_id,description) VALUES ('evidence',?,'intermediate result')", (project_id,))
        conn.execute("INSERT INTO intents(id,project_id,description,creator,created_at) VALUES ('open',?,'unfinished work','test','2026-01-01')", (project_id,))
    response = client.post(f"/projects/{project_id}/complete", json={
        "from": ["evidence"], "description": "intermediate result", "worker": "reasoner"})
    assert response.status_code == 409
    assert "unfinished" in response.json()["detail"]
    assert client.get(f"/projects/{project_id}").json()["project"]["status"] == "active"


def test_stopping_project_releases_claims_and_reason_but_keeps_hints_writable(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    response = client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    assert response.status_code == 200
    assert response.json()["reason"] is None

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["intents"][0]["worker"] is None
    assert client.post(
        f"/projects/{project_id}/hints",
        json={"content": "manual note", "creator": "human"},
    ).status_code == 201
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "blocked", "creator": "reasoner", "worker": None},
    ).status_code == 403


def test_intent_creation_rejects_goal_source_and_mismatched_initial_worker(client: TestClient) -> None:
    project_id = _create_project(client)

    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["goal"], "description": "invalid", "creator": "reasoner", "worker": None},
    ).status_code == 400
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "invalid", "creator": "reasoner", "worker": "explorer"},
    ).status_code == 400


def test_settings_and_export_are_backed_by_the_same_database(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.put("/settings", json={"intent_timeout": 30, "reason_timeout": 45})
    assert response.status_code == 200
    settings = client.get("/settings").json()
    assert settings["intent_timeout"] == 30
    assert settings["reason_timeout"] == 45
    assert settings["agent_reassignment_cooldown"] == 300
    assert settings["parent_review_debounce"] == 15

    exported = client.get(f"/projects/{project_id}/export?format=yaml")
    assert exported.status_code == 200
    assert "origin: starting point" in exported.text
    assert "goal: finish" in exported.text
    assert client.get(f"/projects/{project_id}/export?format=invalid").status_code == 400


def test_expired_intent_and_reason_leases_can_be_reclaimed(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "bootstrap"},
    )
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE intents SET last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE project_id = ?",
            (project_id,),
        )
        conn.execute(
            "UPDATE projects SET reason_last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (project_id,),
        )

    response = client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "worker-b"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "worker-b"

    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )
    assert response.status_code == 200
    assert response.json()["reason"]["worker"] == "worker-b"


def test_live_reason_lease_rejects_competing_worker(client: TestClient) -> None:
    project_id = _create_project(client)
    assert client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "bootstrap"},
    ).status_code == 200

    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    assert response.status_code == 409
    assert "worker-a" in response.json()["detail"]


def test_project_creation_persists_disabled_bootstrap_and_exports_it(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "no bootstrap",
            "origin": "start",
            "goal": "finish",
            "bootstrap_enabled": False,
        },
    )

    assert response.status_code == 201
    project_id = response.json()["project"]["id"]
    assert client.get(f"/projects/{project_id}").json()["project"]["bootstrap_enabled"] is False
    assert "bootstrap_enabled: false" in client.get(f"/projects/{project_id}/export?format=yaml").text


def test_project_creation_rejects_invalid_bootstrap_enabled(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "invalid bootstrap",
            "origin": "start",
            "goal": "finish",
            "bootstrap_enabled": "sometimes",
        },
    )

    assert response.status_code == 422


def _agent(client, name, **overrides):
    body = dict(name=name, base_url="http://127.0.0.1:9001/v1", api_key="saved-key", model="model-a")
    body.update(overrides)
    response = client.post("/agents", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_agent_profiles_mask_keys_preserve_saved_key_and_drive_project_scope(client):
    parent = _agent(client, "Parent")
    agent = _agent(client, "Recon")
    assert agent["api_key_configured"]
    assert "api_key" not in agent
    update = client.put(f"/agents/{agent['id']}", json={
        "name": "Recon", "base_url": agent["base_url"], "model": "model-b"})
    assert update.status_code == 200
    runtime = {row["id"]: row for row in client.get("/agents/runtime").json()}
    assert runtime[agent["id"]]["api_key"] == "saved-key"
    assert runtime[agent["id"]]["model"] == "model-b"
    health = client.put(f"/agents/{agent['id']}/health", json={"status": "unhealthy", "detail": "model unavailable"})
    assert health.status_code == 200
    assert health.json()["health_checked_at"] is not None
    assert health.json()["health_status"] == "unhealthy"
    project = client.post("/projects", json={"title": "scoped", "origin": "authorized local target", "goal": "finish",
        "parent_agent_id": parent["id"], "agents": [{"agent_id": agent["id"]}]})
    assert project.status_code == 201, project.text
    meta = project.json()["project"]
    assert meta["parent_agent"]["agent_id"] == parent["id"]
    assert [row["agent_id"] for row in meta["agents"]] == [agent["id"]]
    exported = client.get(f"/projects/{meta['id']}/export?format=yaml")
    assert exported.status_code == 200
    assert agent["id"] in exported.text
    assert "saved-key" not in exported.text


@pytest.mark.parametrize("disabled", [True, False])
def test_project_rejects_unknown_or_disabled_selected_agents(client, disabled):
    parent = _agent(client, "Parent")
    agent = _agent(client, "Disabled", enabled=False)
    selected = agent["id"] if disabled else "agent_missing"
    response = client.post("/projects", json={"title": "invalid", "origin": "start", "goal": "finish",
        "parent_agent_id": parent["id"], "agents": [{"agent_id": selected}]})
    assert response.status_code == 400
    assert ("Disabled" if disabled else "Unknown") in response.json()["detail"]


def test_model_discovery_can_reuse_saved_agent_key(client, monkeypatch):
    agent = _agent(client, "Discovery")
    captured = {}
    class Response:
        status_code = 200
        text = ""
        def json(self):
            return {"data": [{"id": "z-model"}, {"id": "a-model"}, "a-model"]}
    def fake_get(url, *, headers, timeout):
        captured.update(url=url, headers=headers, timeout=timeout)
        return Response()
    monkeypatch.setattr(agents_router.requests, "get", fake_get)
    response = client.post("/agents/discover-models", json={"base_url": agent["base_url"], "agent_id": agent["id"]})
    assert response.status_code == 200, response.text
    assert response.json() == {"models": ["a-model", "z-model"]}
    assert captured == {"url": agent["base_url"] + "/models", "headers": {"Authorization": "Bearer saved-key", "Accept": "application/json"}, "timeout": (5, 10)}


def test_parent_and_participant_agents_must_use_distinct_slots(client):
    parent = _agent(client, "Parent")
    body = {"title": "invalid", "origin": "start", "goal": "finish", "agents": [{"agent_id": parent["id"]}]}
    assert client.post("/projects", json=body).status_code == 400
    body["parent_agent_id"] = parent["id"]
    assert client.post("/projects", json=body).status_code == 400
    child = _agent(client, "Participant")
    body["agents"] = [{"agent_id": child["id"]}]
    result = client.post("/projects", json=body)
    assert result.status_code == 201, result.text
    assert result.json()["project"]["kind"] == "parent"
