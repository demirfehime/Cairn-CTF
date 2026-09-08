import hashlib
import io
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace
import zipfile

import pytest
from fastapi.testclient import TestClient

from cairn.server import db
from cairn.server.app import app
from cairn.server.models import ProjectDetail
from cairn.server.routers import attachments
from cairn.dispatcher.attachments import prepare_attachments
from cairn.dispatcher.config import LocalConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.local_backend import LocalBackend


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "test.db")
    with TestClient(app) as instance:
        yield instance


def upload(client, name="challenge.zip", content=b"\x00\xffbinary\x00"):
    response = client.post("/attachments/uploads", params={"name": name}, content=content)
    assert response.status_code == 201, response.text
    return response.json()


def create(client, ids=()):
    return client.post("/projects", json={
        "title": "Attachment task", "origin": "Analyze the supplied file", "goal": "Explain its contents",
        "attachment_ids": list(ids),
    })


def test_binary_roundtrip_duplicate_names_and_project_scope(client):
    first = upload(client, name="题目.zip")
    second = upload(client, name="题目.zip", content=b"another file")
    result = create(client, [first["id"], second["id"]])
    assert result.status_code == 201, result.text
    project = result.json()
    pid = project["project"]["id"]
    assert [item["name"] for item in project["attachments"]] == ["题目.zip", "题目.zip"]
    assert client.get(f"/projects/{pid}").json()["attachments"] == project["attachments"]
    response = client.get(f"/projects/{pid}/attachments/{first['id']}")
    assert response.content == b"\x00\xffbinary\x00"
    assert response.headers["content-type"] == "application/octet-stream"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    other = create(client).json()["project"]["id"]
    assert client.get(f"/projects/{other}/attachments/{first['id']}").status_code == 404
    assert client.get(f"/projects/{other}/attachments").json() == []
    assert client.get(f"/projects/missing/attachments/{first['id']}").status_code == 404
    # A pending-upload deletion must never delete a bound attachment.
    client.delete(f"/attachments/uploads/{first['id']}")
    assert client.get(f"/projects/{pid}/attachments/{first['id']}").status_code == 200


def test_bind_is_atomic_and_upload_can_be_retried(client):
    item = upload(client)
    assert create(client, [item["id"], "missing"]).status_code == 422
    assert client.get("/projects").json() == []
    created = create(client, [item["id"]])
    assert created.status_code == 201
    assert create(client, [item["id"]]).status_code == 422
    assert len(client.get("/projects").json()) == 1
    pid = created.json()["project"]["id"]
    assert client.delete(f"/projects/{pid}").status_code == 204
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_attachments").fetchone()[0] == 0


@pytest.mark.parametrize("name", ["../escape", "C:\\escape", ".", "..", "bad\nname"])
def test_reject_directory_and_control_names(client, name):
    assert client.post("/attachments/uploads", params={"name": name}, content=b"x").status_code == 422


def test_limits_expiry_and_discard(client, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_FILE_SIZE", 4)
    assert client.post("/attachments/uploads?name=large", content=b"12345").status_code == 413
    item = upload(client, content=b"1234")
    assert create(client, [item["id"], item["id"]]).status_code == 422
    monkeypatch.setattr(attachments, "MAX_PROJECT_SIZE", 3)
    assert create(client, [item["id"]]).status_code == 413
    with db.get_conn() as conn:
        conn.execute("UPDATE project_attachments SET created_at=0")
    assert create(client, [item["id"]]).status_code == 422
    fresh = upload(client, content=b"")
    client.delete("/attachments/uploads/" + fresh["id"])
    assert create(client, [fresh["id"]]).status_code == 422
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_attachments").fetchone()[0] == 0


def test_child_inherits_parent_files_but_not_sibling_files(client):
    parent_file, child_file = upload(client), upload(client)
    parent = create(client, [parent_file["id"]]).json()["project"]["id"]
    child = create(client, [child_file["id"]]).json()["project"]["id"]
    sibling = create(client).json()["project"]["id"]
    with db.get_conn() as conn:
        conn.execute("UPDATE projects SET kind='parent' WHERE id=?", (parent,))
        conn.execute("UPDATE projects SET parent_project_id=? WHERE id IN (?,?)", (parent, child, sibling))
    assert len(client.get(f"/projects/{child}").json()["attachments"]) == 2
    assert len(client.get(f"/projects/{sibling}").json()["attachments"]) == 1
    assert client.get(f"/projects/{child}/attachments/{parent_file['id']}").status_code == 200
    assert client.get(f"/projects/{sibling}/attachments/{child_file['id']}").status_code == 404


def test_local_worker_reads_uploaded_archive_and_cached_original(client, tmp_path, monkeypatch):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("clue.txt", "attachment evidence")
    content = archive.getvalue()
    item = upload(client, content=content)
    project = ProjectDetail.model_validate(create(client, [item["id"]]).json())
    runtime = CairnClient("http://testserver")
    monkeypatch.setattr(runtime, "_session", lambda: client)
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "runs")))
    workspace = backend.ensure_running(project.project.id)
    prompt = prepare_attachments(runtime, backend, workspace, project)
    path = next((Path(workspace) / "attachments").glob("*.zip"))
    assert path.read_bytes() == content
    assert str(path).replace("\\", "\\\\") in prompt and "Inspect their file types" in prompt
    process = backend.build_exec_process(workspace, {}, [sys.executable, "-c",
        "import zipfile,sys; print(zipfile.ZipFile(sys.argv[1]).read('clue.txt').decode())", str(path)], timeout_seconds=10)
    process.start()
    result = process.communicate(timeout=15)
    assert result.returncode == 0 and "attachment evidence" in result.stdout
    monkeypatch.setattr(runtime, "download_attachment", lambda *_: pytest.fail("Unchanged files should be reused"))
    assert prepare_attachments(runtime, backend, workspace, project) == prompt
    path.write_bytes(b"modified")
    monkeypatch.setattr(runtime, "download_attachment", lambda *_: b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        prepare_attachments(runtime, backend, workspace, project)


def test_container_binary_delivery_and_local_path_boundary(client, tmp_path):
    item = upload(client)
    project = ProjectDetail.model_validate(create(client, [item["id"]]).json())
    writes = []
    backend = SimpleNamespace(write_bytes_file=lambda *args: writes.append(args))
    runtime = SimpleNamespace(download_attachment=lambda *_: b"\x00\xffbinary\x00")
    prompt = prepare_attachments(runtime, backend, "container-project", project)
    _, path, content = writes[0]
    assert path.startswith("/tmp/cairn-attachments/") and path in prompt
    base, archive = ContainerManager._text_file_archive(path, content)
    assert base == "/tmp"
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        files = [entry for entry in tar if entry.isfile()]
        assert len(files) == 1
        assert tar.extractfile(files[0]).read() == content
    local = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "runs")))
    workspace = local.ensure_running("p")
    with pytest.raises(ValueError, match="inside project"):
        local.write_bytes_file(workspace, str(tmp_path / "escaped.bin"), b"x")
    assert not (tmp_path / "escaped.bin").exists()


def test_schema_upgrade_is_idempotent(client):
    item = upload(client)
    with db.get_conn() as conn:
        conn.executescript(attachments.SCHEMA)
        conn.executescript(attachments.SCHEMA)
    assert create(client, [item["id"]]).status_code == 201
