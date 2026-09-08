from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cairn.server import db
from cairn.server.app import app
from cairn.server.routers.projects import report_fact_flags


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with db.get_conn() as conn:
        conn.execute("INSERT INTO projects(id,title,kind,created_at) VALUES ('parent','CTF','parent','2026-09-06')")
        conn.execute("INSERT INTO projects(id,title,kind,parent_project_id,created_at) VALUES ('child','Evidence','child','parent','2026-09-06')")
        for project in ["parent", "child"]:
            for fid in ["origin", "goal"]:
                conn.execute("INSERT INTO facts(id,project_id,description) VALUES (?,?,?)", (fid, project, fid))
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("value", ["flag{Real123}", "moectf{Real123}", "DASCTF{Real123}", "CTF{Real123}"])
def test_conclude_immediately_reports_child_candidate(client, value):
    created = client.post("/projects/child/intents", json={"from": ["origin"], "description": "inspect", "creator": "test"})
    assert created.status_code == 201, created.text
    iid = created.json()["id"]
    assert client.post(f"/projects/child/intents/{iid}/heartbeat", json={"worker": "agent"}).status_code == 200
    response = client.post(f"/projects/child/intents/{iid}/conclude", json={"worker": "agent", "description": f"Reading /flag returned {value}"})
    assert response.status_code == 200, response.text
    reports = client.get("/flag-candidates/pending").json()
    assert len(reports) == 1
    assert reports[0]["value"] == value
    assert reports[0]["project_id"] == "parent"
    assert reports[0]["reported_source"].startswith("child/")
    assert client.get("/projects/parent").json()["project"]["status"] == "active"
    decision = client.post(f"/projects/parent/flag-candidates/{reports[0]['id']}/decision", json={"decision": "confirm", "actor": "human"})
    assert decision.status_code == 200, decision.text
    assert client.get("/projects/parent").json()["project"]["status"] == "completed"
    assert client.get("/flag-candidates/pending").json() == []


def test_candidate_values_are_case_sensitive_and_rejection_is_not_resubmitted(client):
    with db.get_conn() as conn:
        text = "moectf{ABC123} moectf{abc123}"
        conn.execute("INSERT INTO facts VALUES ('f001','child',?)", (text,))
        report_fact_flags(conn, "child", "f001", text, "agent")
        report_fact_flags(conn, "child", "f001", text, "agent")
    pending = client.get("/flag-candidates/pending").json()
    assert len(pending) == 2
    cid = pending[0]["id"]
    assert client.post(f"/projects/parent/flag-candidates/{cid}/decision", json={"decision": "reject", "actor": "human"}).status_code == 200
    with db.get_conn() as conn:
        report_fact_flags(conn, "child", "f001", text, "agent")
    assert len(client.get("/flag-candidates/pending").json()) == 1


def test_explicit_unverified_candidate_requires_stored_evidence(client):
    with db.get_conn() as conn:
        conn.execute("INSERT INTO facts VALUES ('f001','parent','Reading /flag returned moectf{real123}')")
    body = {"from": ["f001"], "description": "moectf{invented123}", "worker": "agent",
            "evidence": {"kind": "flag", "value": "moectf{invented123}", "source": "f001", "verified": False}}
    assert client.post("/projects/parent/complete", json=body).status_code == 409
    body["evidence"]["value"] = "moectf{real123}"
    body["description"] = "Possible flag"
    assert client.post("/projects/parent/complete", json=body).status_code == 202


def test_placeholders_are_not_reported(client):
    with db.get_conn() as conn:
        assert report_fact_flags(conn, "child", "f001", "No exact moectf{...} or flag{exact_value} found", "agent") == []
    assert client.get("/flag-candidates/pending").json() == []


def test_child_completion_also_reports_and_parent_cannot_bypass_review(client):
    response = client.post("/projects/child/complete", json={"from": ["origin"],
                           "description": "Read moectf{completion123} from /flag", "worker": "agent"})
    assert response.status_code == 200, response.text
    pending = client.get("/flag-candidates/pending").json()
    assert len(pending) == 1
    response = client.post("/projects/parent/complete", json={"from": [pending[0]["source_fact_id"]],
                           "description": "Work done", "worker": "agent"})
    assert response.status_code == 409


def test_publication_rejects_missing_source_fact(client):
    response = client.post("/projects/child/blackboard/facts", json={"fact_id": "f999", "description": "moectf{invented123}"})
    assert response.status_code == 404
    assert client.get("/flag-candidates/pending").json() == []


def test_ui_polls_candidates_from_list_and_child_views():
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for UI behavior test")
    html = Path(__file__).resolve().parents[1] / "src/cairn/server/static/index.html"
    script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[1], 'utf8');
const code = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(x=>x[1]).find(x=>x.includes('function cairnApp()'));
let poll;
const context = {console, setInterval: fn => {poll=fn; return 1;}};
vm.createContext(context); vm.runInContext(code, context);
(async () => {
  const app = context.cairnApp();
  const candidate = {project_id:'parent', id:'c001', value:'moectf{reported}'};
  app.api = async (method, url) => {assert.equal(url, '/flag-candidates/pending'); return [candidate];};
  app.loadProjects = async () => {};
  app.loadProject = async () => {};
  app.updateGraph = () => {};
  app.startPolling();
  await poll(); assert.equal(app.globalFlagReports.length, 1);
  app.view = 'graph'; app.selectedProjectId = 'child'; app.globalFlagReports = [];
  await poll(); assert.equal(app.globalFlagReports[0].project_id, 'parent');
  assert(html.includes('data-testid="global-flag-reports"'));
})().catch(error => {console.error(error); process.exit(1);});
"""
    result = subprocess.run([node, "-e", script, str(html)], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
