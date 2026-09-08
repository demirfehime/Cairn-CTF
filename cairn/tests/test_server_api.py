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


def test_completion_guard_blocks_intermediate_secret_while_work_remains(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "find a key", "creator": "reasoner", "worker": None},
    )
    client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )
    concluded = client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={
            "worker": "explorer",
            "description": "Read flag{intermediate} from jwt_secret.key; admin_console=/admin",
        },
    )
    assert concluded.status_code == 200
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["f001"], "description": "use the key", "creator": "reasoner", "worker": None},
    )

    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["f001"], "description": "found a flag-shaped key", "worker": "reasoner"},
    )

    assert response.status_code == 409
    assert "intermediate" in response.json()["detail"]
    detail = client.get(f"/projects/{project_id}").json()
    assert detail["project"]["status"] == "active"
    assert any(hint["creator"] == "dispatcher.completion_guard" for hint in detail["hints"])


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
    assert settings["bootstrap_task_timeout"] == 300
    assert settings["blackboard_refresh_interval"] == 2

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


def test_agent_profiles_mask_keys_preserve_saved_key_and_drive_project_scope(client: TestClient) -> None:
    response = client.put(
        "/agents",
        json={
            "agents": [
                {
                    "name": "Recon",
                    "api_base_url": "http://127.0.0.1:9001/v1/",
                    "api_key": "secret-one",
                    "model": "model-a",
                    "use_penetration_prompt": True,
                    "enabled": True,
                    "max_running": 2,
                    "priority": 0,
                },
                {
                    "name": "Logic",
                    "api_base_url": "http://127.0.0.1:9002/v1",
                    "api_key": "secret-two",
                    "model": "model-b",
                    "use_penetration_prompt": False,
                    "enabled": True,
                    "max_running": 1,
                    "priority": 1,
                },
            ]
        },
    )

    assert response.status_code == 200
    public_agents = response.json()
    assert [agent["id"] for agent in public_agents] == ["agent_001", "agent_002"]
    assert all(agent["has_api_key"] for agent in public_agents)
    assert all("api_key" not in agent for agent in public_agents)
    assert all(agent["health_status"] == "unknown" for agent in public_agents)
    assert public_agents[0]["api_base_url"] == "http://127.0.0.1:9001/v1"

    public_agents[0].update(model="model-a2", api_key="")
    public_agents[1].update(api_key="********")
    updated = client.put("/agents", json={"agents": public_agents})
    assert updated.status_code == 200
    runtime = client.get("/agents/runtime").json()
    assert runtime[0]["api_key"] == "secret-one"
    assert runtime[0]["model"] == "model-a2"
    assert runtime[1]["api_key"] == "secret-two"

    health = client.post(
        f"/agents/{runtime[0]['id']}/health",
        json={"status": "unhealthy", "detail": "HTTP 403 model unavailable"},
    )
    assert health.status_code == 200
    assert health.json()["health_status"] == "unhealthy"
    assert health.json()["health_detail"] == "HTTP 403 model unavailable"
    assert health.json()["last_healthcheck_at"] is not None

    project = client.post(
        "/projects",
        json={
            "title": "Juice Shop",
            "target_url": "http://127.0.0.1:3000/",
            "agent_ids": ["agent_001", "agent_002"],
            "origin": "authorized local target",
            "goal": "find reproducible vulnerabilities",
        },
    )
    assert project.status_code == 201
    meta = project.json()["project"]
    assert meta["target_url"] == "http://127.0.0.1:3000"
    assert meta["agent_ids"] == ["agent_001", "agent_002"]
    exported = client.get(f"/projects/{meta['id']}/export?format=yaml").text
    assert "target_url: http://127.0.0.1:3000" in exported
    assert "- agent_001" in exported
    assert "- agent_002" in exported


def test_project_rejects_unknown_or_disabled_selected_agents(client: TestClient) -> None:
    saved = client.put(
        "/agents",
        json={
            "agents": [
                {
                    "name": "Disabled",
                    "api_base_url": "http://127.0.0.1:9001/v1",
                    "api_key": "secret",
                    "model": "model-a",
                    "enabled": False,
                }
            ]
        },
    ).json()

    response = client.post(
        "/projects",
        json={
            "title": "invalid agents",
            "target_url": "http://127.0.0.1:3000",
            "agent_ids": [saved[0]["id"], "agent_missing"],
            "origin": "start",
            "goal": "finish",
        },
    )

    assert response.status_code == 400
    assert "Unknown or disabled agents" in response.json()["detail"]


def test_model_discovery_can_reuse_saved_agent_key(client: TestClient, monkeypatch) -> None:
    saved = client.put(
        "/agents",
        json={
            "agents": [
                {
                    "name": "Discovery",
                    "api_base_url": "http://127.0.0.1:9001/v1",
                    "api_key": "saved-key",
                    "model": "existing",
                }
            ]
        },
    ).json()[0]
    captured: dict = {}

    class Response:
        ok = True
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"data": [{"id": "z-model"}, {"id": "a-model"}, "a-model"]}

    def fake_get(url, *, headers, timeout):
        captured.update(url=url, headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setattr(agents_router.requests, "get", fake_get)
    response = client.post(
        "/agents/discover-models",
        json={
            "api_base_url": "http://127.0.0.1:9001/v1",
            "agent_id": saved["id"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {"models": ["a-model", "z-model"]}
    assert captured == {
        "url": "http://127.0.0.1:9001/v1/models",
        "headers": {"Authorization": "Bearer saved-key"},
        "timeout": 15,
    }


def test_selected_agents_are_enforced_server_side_and_legacy_bootstrap_is_removed(
    client: TestClient,
) -> None:
    agents = client.put(
        "/agents",
        json={
            "agents": [
                {
                    "name": "First",
                    "api_base_url": "http://127.0.0.1:9001/v1",
                    "api_key": "key-one",
                    "model": "model-one",
                },
                {
                    "name": "Second",
                    "api_base_url": "http://127.0.0.1:9002/v1",
                    "api_key": "key-two",
                    "model": "model-two",
                },
            ]
        },
    ).json()
    agent_ids = [agent["id"] for agent in agents]
    project_id = client.post(
        "/projects",
        json={
            "title": "scoped",
            "target_url": "http://127.0.0.1:3000",
            "agent_ids": agent_ids,
            "origin": "start",
            "goal": "finish",
        },
    ).json()["project"]["id"]

    legacy_bootstrap = client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["origin"],
            "description": "bootstrap",
            "creator": "dispatcher.bootstrap",
            "worker": None,
        },
    )
    assert legacy_bootstrap.status_code == 403

    intent = client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["origin"],
            "description": "human path",
            "creator": "Human",
            "worker": None,
        },
    ).json()
    assert client.post(
        f"/projects/{project_id}/intents/{intent['id']}/heartbeat",
        json={"worker": "local-codex"},
    ).status_code == 403
    assert client.post(
        f"/projects/{project_id}/intents/{intent['id']}/heartbeat",
        json={"worker": agent_ids[0]},
    ).status_code == 200
    assert client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "local-codex", "trigger": "initial"},
    ).status_code == 403
    assert client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": agent_ids[1], "trigger": "initial"},
    ).status_code == 200

    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO intents (
                id, project_id, to_fact_id, description, creator, worker,
                last_heartbeat_at, created_at, concluded_at
            ) VALUES ('legacy-bootstrap', ?, NULL, 'bootstrap', 'dispatcher.bootstrap',
                      'local-codex', '2026-01-01T00:00:00Z',
                      '2026-01-01T00:00:00Z', NULL)
            """,
            (project_id,),
        )
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id) "
            "VALUES ('legacy-bootstrap', ?, 'origin')",
            (project_id,),
        )

    detail = client.get(f"/projects/{project_id}").json()
    assert all(intent["id"] != "legacy-bootstrap" for intent in detail["intents"])
