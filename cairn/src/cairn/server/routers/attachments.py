"""Bounded, staged uploads attached atomically when a project is created."""
import hashlib
import re
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response

from cairn.server.db import get_conn
from cairn.server.services import get_project_or_404

router = APIRouter(tags=["attachments"])
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_PROJECT_SIZE = 200 * 1024 * 1024
MAX_FILES = 20
SCHEMA = """
CREATE TABLE IF NOT EXISTS project_attachments (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at REAL NOT NULL,
    content BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS attachments_project ON project_attachments(project_id);
"""


def public(row):
    return {key: row[key] for key in ("id", "name", "size", "sha256", "project_id")}


def list_for_project(conn, project_id):
    project = get_project_or_404(conn, project_id)
    # Children inherit only the attachments of their own Parent, never siblings.
    ids = [project_id]
    if project["parent_project_id"]:
        ids.append(project["parent_project_id"])
    marks = ",".join("?" for _ in ids)
    return [public(row) for row in conn.execute(
        f"SELECT id,name,size,sha256,project_id FROM project_attachments WHERE project_id IN ({marks}) ORDER BY created_at,id", ids
    )]


def bind_to_project(conn, project_id, attachment_ids):
    if len(attachment_ids) > MAX_FILES or len(set(attachment_ids)) != len(attachment_ids):
        raise HTTPException(422, "Select at most 20 unique attachments")
    if any(not re.fullmatch(r"att_[0-9a-f]{32}", key) for key in attachment_ids):
        raise HTTPException(422, "Invalid attachment identifier")
    total = 0
    for key in attachment_ids:
        row = conn.execute("SELECT size,project_id,created_at FROM project_attachments WHERE id=?", (key,)).fetchone()
        if row is None or row["project_id"] is not None or row["created_at"] < time.time() - 86400:
            raise HTTPException(422, "Attachment upload expired or already belongs to a project; upload again")
        total += row["size"]
    if total > MAX_PROJECT_SIZE:
        raise HTTPException(413, "Project attachments exceed 200 MiB")
    for key in attachment_ids:
        conn.execute("UPDATE project_attachments SET project_id=? WHERE id=?", (project_id, key))


@router.post("/attachments/uploads", status_code=201)
async def upload(request: Request, name: str = Query(min_length=1, max_length=240)):
    # Names are display metadata, never server filesystem paths.
    if not name.strip() or name in (".", "..") or re.search(r'[\\/\x00-\x1f\x7f]', name):
        raise HTTPException(422, "Attachment name must be a filename without directories")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_FILE_SIZE:
            raise HTTPException(413, "Each attachment must be at most 50 MiB")
        content.extend(chunk)
    key = "att_" + uuid4().hex
    with get_conn() as conn:
        conn.execute("DELETE FROM project_attachments WHERE project_id IS NULL AND created_at < ?", (time.time() - 86400,))
        pending = conn.execute("SELECT COALESCE(SUM(size),0) FROM project_attachments WHERE project_id IS NULL").fetchone()[0]
        if pending + len(content) > 512 * 1024 * 1024:
            raise HTTPException(413, "Pending uploads are full; remove unused uploads and retry")
        conn.execute("INSERT INTO project_attachments VALUES (?,?,?,?,?,?,?)", (
            key, None, name, len(content), hashlib.sha256(content).hexdigest(), time.time(), bytes(content)
        ))
        return public(conn.execute("SELECT * FROM project_attachments WHERE id=?", (key,)).fetchone())


@router.delete("/attachments/uploads/{attachment_id}", status_code=204)
def discard(attachment_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM project_attachments WHERE id=? AND project_id IS NULL", (attachment_id,))


@router.get("/projects/{project_id}/attachments")
def list_attachments(project_id: str):
    with get_conn() as conn:
        return list_for_project(conn, project_id)


@router.get("/projects/{project_id}/attachments/{attachment_id}")
def download(project_id: str, attachment_id: str):
    from urllib.parse import quote
    with get_conn() as conn:
        item = next((item for item in list_for_project(conn, project_id) if item["id"] == attachment_id), None)
        if item is None:
            raise HTTPException(404, "Attachment not found in this project")
        content = conn.execute("SELECT content FROM project_attachments WHERE id=?", (attachment_id,)).fetchone()[0]
    return Response(content, media_type="application/octet-stream", headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(item["name"], safe=""),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    })
