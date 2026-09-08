from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import HTTPException

from cairn.server.models import (
    BlackboardEntry,
    FlagCandidate,
    Intent,
    ProjectAgentSelection,
    ProjectMeta,
    ProjectReason,
)

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _project_root_id(conn: sqlite3.Connection, project_id: str) -> str:
    row = conn.execute(
        "SELECT id, parent_project_id, kind FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        return project_id
    return row["parent_project_id"] if row["kind"] == "child" and row["parent_project_id"] else row["id"]


def validate_project_fact_scope(conn: sqlite3.Connection, project_id: str, description: str) -> None:
    """Reject facts that clearly import a known sibling project's Origin.

    Agents may discover additional in-scope hosts, but an exact hostname that is
    another Cairn Parent's Origin is a strong signal of stale cross-project data.
    Keeping this check at the write boundary prevents contaminated facts from
    reaching the Parent Blackboard even if a worker ignores workspace guidance.
    """
    root_id = _project_root_id(conn, project_id)
    origins = conn.execute(
        """
        SELECT p.id, f.description AS origin
        FROM projects p JOIN facts f ON f.project_id = p.id AND f.id = 'origin'
        WHERE p.parent_project_id IS NULL
        """
    ).fetchall()
    current_hosts: set[str] = set()
    sibling_hosts: set[str] = set()
    for row in origins:
        hosts = {
            match.casefold()
            for match in re.findall(r"https?://([^/\s)]+)", row["origin"] or "")
        }
        if row["id"] == root_id:
            current_hosts.update(hosts)
        else:
            sibling_hosts.update(hosts)
    leaked = sorted(host for host in sibling_hosts - current_hosts if host in description.casefold())
    if leaked:
        raise HTTPException(
            409,
            "Fact rejected: it references another project's Origin host "
            + ", ".join(leaked)
            + ". Re-verify the evidence against this project's Origin.",
        )


def next_project_id(conn: sqlite3.Connection) -> str:
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'project'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'project'").fetchone()
    return f"proj_{row['value']:03d}"


def next_agent_id(conn: sqlite3.Connection) -> str:
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'agent'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'agent'").fetchone()
    return f"agent_{row['value']:03d}"


def _agent_runtime_fields(
    conn: sqlite3.Connection,
    agent: sqlite3.Row,
    project: sqlite3.Row,
) -> dict[str, object]:
    connection_status = {
        "healthy": "connected",
        "unhealthy": "offline",
    }.get(agent["health_status"], "unknown")
    task_project = project
    assigned_child_id = None
    assigned_child_title = None

    if project["kind"] == "parent":
        assignment = conn.execute(
            """
            SELECT child.* FROM project_agents pa
            JOIN projects child ON child.id = pa.project_id
            WHERE child.parent_project_id = ? AND pa.agent_id = ?
              AND pa.role = 'assigned'
            ORDER BY pa.assigned_at DESC, child.created_at DESC LIMIT 1
            """,
            (project["id"], agent["agent_id"]),
        ).fetchone()
        if assignment is not None:
            task_project = assignment
            assigned_child_id = assignment["id"]
            assigned_child_title = assignment["title"]

    if task_project["reason_worker"] == agent["name"]:
        return {
            "health_status": agent["health_status"],
            "health_detail": agent["health_detail"],
            "health_checked_at": agent["health_checked_at"],
            "connection_status": connection_status,
            "task_status": "working",
            "current_task": f"Reason: {task_project['reason_trigger'] or 'graph update'}",
            "current_task_type": "reason",
            "current_intent_id": None,
            "task_started_at": task_project["reason_started_at"],
            "last_heartbeat_at": task_project["reason_last_heartbeat_at"],
            "assigned_child_id": assigned_child_id,
            "assigned_child_title": assigned_child_title,
        }

    intent = conn.execute(
        """
        SELECT * FROM intents
        WHERE project_id = ? AND worker = ? AND concluded_at IS NULL
        ORDER BY last_heartbeat_at DESC, created_at DESC LIMIT 1
        """,
        (task_project["id"], agent["name"]),
    ).fetchone()
    if intent is not None:
        is_bootstrap = intent["description"].strip().casefold() == "bootstrap"
        return {
            "health_status": agent["health_status"],
            "health_detail": agent["health_detail"],
            "health_checked_at": agent["health_checked_at"],
            "connection_status": connection_status,
            "task_status": "working",
            "current_task": intent["description"],
            "current_task_type": "bootstrap" if is_bootstrap else "explore",
            "current_intent_id": intent["id"],
            "task_started_at": intent["created_at"],
            "last_heartbeat_at": intent["last_heartbeat_at"],
            "assigned_child_id": assigned_child_id,
            "assigned_child_title": assigned_child_title,
        }

    task_status = "idle"
    if task_project["status"] == "paused":
        task_status = "paused"
    elif task_project["status"] == "completed":
        task_status = "completed"
    return {
        "health_status": agent["health_status"],
        "health_detail": agent["health_detail"],
        "health_checked_at": agent["health_checked_at"],
        "connection_status": connection_status,
        "task_status": task_status,
        "current_task": None,
        "current_task_type": None,
        "current_intent_id": None,
        "task_started_at": None,
        "last_heartbeat_at": None,
        "assigned_child_id": assigned_child_id,
        "assigned_child_title": assigned_child_title,
    }


def build_project_agents(conn: sqlite3.Connection, project_id: str) -> list[ProjectAgentSelection]:
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        return []
    rows = conn.execute(
        """
        SELECT a.id AS agent_id, a.name, a.model, pa.use_hints,
               pa.role, pa.assignment_epoch, a.health_status, a.health_detail,
               a.health_checked_at
        FROM project_agents pa
        JOIN agents a ON a.id = pa.agent_id
        WHERE pa.project_id = ?
        ORDER BY a.priority, a.name
        """,
        (project_id,),
    ).fetchall()
    return [
        ProjectAgentSelection(
            agent_id=row["agent_id"],
            name=row["name"],
            model=row["model"],
            use_hints=bool(row["use_hints"]),
            role=row["role"],
            assignment_epoch=row["assignment_epoch"],
            **_agent_runtime_fields(conn, row, project),
        )
        for row in rows
    ]


def build_parent_agent(conn: sqlite3.Connection, row: sqlite3.Row) -> ProjectAgentSelection | None:
    agent_id = row["parent_agent_id"] if "parent_agent_id" in row.keys() else None
    if not agent_id:
        return None
    agent = conn.execute(
        """
        SELECT id AS agent_id, name, model, health_status, health_detail,
               health_checked_at
        FROM agents WHERE id = ?
        """,
        (agent_id,),
    ).fetchone()
    if agent is None:
        return None
    return ProjectAgentSelection(
        agent_id=agent["agent_id"],
        name=agent["name"],
        model=agent["model"],
        use_hints=True,
        role="participant",
        **_agent_runtime_fields(conn, agent, row),
    )


def next_blackboard_id(conn: sqlite3.Connection, project_id: str) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO blackboard_counters (parent_project_id, value) VALUES (?, 0)",
        (project_id,),
    )
    conn.execute(
        "UPDATE blackboard_counters SET value = value + 1 WHERE parent_project_id = ?",
        (project_id,),
    )
    value = conn.execute(
        "SELECT value FROM blackboard_counters WHERE parent_project_id = ?",
        (project_id,),
    ).fetchone()["value"]
    return f"b{value:04d}"


def add_blackboard_entry(
    conn: sqlite3.Connection,
    project_id: str,
    kind: str,
    content: str,
    *,
    child_project_id: str | None = None,
    metadata: dict | None = None,
) -> BlackboardEntry:
    entry_id = next_blackboard_id(conn, project_id)
    created_at = utcnow()
    payload = metadata or {}
    conn.execute(
        """
        INSERT INTO blackboard
            (id, parent_project_id, child_project_id, kind, content, metadata, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry_id,
            project_id,
            child_project_id,
            kind,
            content,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            created_at,
        ),
    )
    return BlackboardEntry(
        id=entry_id,
        parent_project_id=project_id,
        child_project_id=child_project_id,
        kind=kind,
        content=content,
        metadata=payload,
        created_at=created_at,
    )


def build_blackboard(conn: sqlite3.Connection, project_id: str) -> list[BlackboardEntry]:
    rows = conn.execute(
        "SELECT * FROM blackboard WHERE parent_project_id = ? ORDER BY created_at, id",
        (project_id,),
    ).fetchall()
    return [
        BlackboardEntry(
            id=row["id"],
            parent_project_id=row["parent_project_id"],
            child_project_id=row["child_project_id"],
            kind=row["kind"],
            content=row["content"],
            metadata=json.loads(row["metadata"] or "{}"),
            created_at=row["created_at"],
        )
        for row in rows
    ]


def find_blackboard_entry(
    conn: sqlite3.Connection,
    project_id: str,
    kind: str,
    **metadata_match: object,
) -> BlackboardEntry | None:
    """Find an entry on one project's private board by stable metadata fields."""
    rows = conn.execute(
        "SELECT * FROM blackboard WHERE parent_project_id = ? AND kind = ? ORDER BY created_at, id",
        (project_id, kind),
    ).fetchall()
    for row in rows:
        metadata = json.loads(row["metadata"] or "{}")
        if all(metadata.get(key) == value for key, value in metadata_match.items()):
            return BlackboardEntry(
                id=row["id"],
                parent_project_id=row["parent_project_id"],
                child_project_id=row["child_project_id"],
                kind=row["kind"],
                content=row["content"],
                metadata=metadata,
                created_at=row["created_at"],
            )
    return None


def add_blackboard_entry_once(
    conn: sqlite3.Connection,
    project_id: str,
    kind: str,
    content: str,
    *,
    child_project_id: str | None = None,
    metadata: dict | None = None,
    unique_by: tuple[str, ...] = (),
) -> BlackboardEntry:
    payload = metadata or {}
    if unique_by:
        match = {key: payload.get(key) for key in unique_by}
        existing = find_blackboard_entry(conn, project_id, kind, **match)
        if existing is not None:
            return existing
    return add_blackboard_entry(
        conn,
        project_id,
        kind,
        content,
        child_project_id=child_project_id,
        metadata=payload,
    )


def ensure_project_blackboard(conn: sqlite3.Connection, project_id: str) -> None:
    """Backfill a project-local Blackboard for projects created before Child boards existed."""
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        return

    facts = conn.execute(
        "SELECT * FROM facts WHERE project_id = ? ORDER BY CASE id WHEN 'origin' THEN 0 WHEN 'goal' THEN 1 ELSE 2 END, id",
        (project_id,),
    ).fetchall()
    for fact in facts:
        kind = fact["id"] if fact["id"] in {"origin", "goal"} else "fact"
        add_blackboard_entry_once(
            conn,
            project_id,
            kind,
            fact["description"],
            child_project_id=project_id if project["kind"] == "child" else None,
            metadata={"fact_id": fact["id"], "backfilled": True},
            unique_by=("fact_id",),
        )

    intents = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at, id",
        (project_id,),
    ).fetchall()
    for intent in intents:
        source_rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE project_id = ? AND intent_id = ? ORDER BY rowid",
            (project_id, intent["id"]),
        ).fetchall()
        sources = [row["fact_id"] for row in source_rows]
        add_blackboard_entry_once(
            conn,
            project_id,
            "intent_created",
            intent["description"],
            child_project_id=project_id if project["kind"] == "child" else None,
            metadata={
                "intent_id": intent["id"],
                "from": sources,
                "creator": intent["creator"],
                "worker": intent["worker"],
                "backfilled": True,
            },
            unique_by=("intent_id",),
        )
        if intent["to_fact_id"] is not None:
            add_blackboard_entry_once(
                conn,
                project_id,
                "intent_concluded",
                intent["description"],
                child_project_id=project_id if project["kind"] == "child" else None,
                metadata={
                    "intent_id": intent["id"],
                    "fact_id": intent["to_fact_id"],
                    "worker": intent["worker"],
                    "backfilled": True,
                },
                unique_by=("intent_id",),
            )

    hints = conn.execute(
        "SELECT * FROM hints WHERE project_id = ? ORDER BY created_at, id",
        (project_id,),
    ).fetchall()
    for hint in hints:
        add_blackboard_entry_once(
            conn,
            project_id,
            "hint",
            hint["content"],
            child_project_id=project_id if project["kind"] == "child" else None,
            metadata={
                "hint_id": hint["id"],
                "creator": hint["creator"],
                "backfilled": True,
            },
            unique_by=("hint_id",),
        )

    if project["kind"] == "child":
        assignments = conn.execute(
            "SELECT agent_id, assignment_epoch FROM project_agents WHERE project_id = ? AND role = 'assigned' ORDER BY assigned_at, agent_id",
            (project_id,),
        ).fetchall()
        for assignment in assignments:
            add_blackboard_entry_once(
                conn,
                project_id,
                "agent_assigned",
                f"Agent {assignment['agent_id']} assigned to this Child",
                child_project_id=project_id,
                metadata={
                    "agent_id": assignment["agent_id"],
                    "assignment_epoch": assignment["assignment_epoch"],
                    "backfilled": True,
                },
                unique_by=("agent_id", "assignment_epoch"),
            )

        parent_id = project["parent_project_id"]
        if parent_id:
            published = conn.execute(
                """
                SELECT * FROM blackboard
                WHERE parent_project_id = ? AND child_project_id = ?
                  AND kind IN ('breakthrough', 'child_summary')
                ORDER BY created_at, id
                """,
                (parent_id, project_id),
            ).fetchall()
            for row in published:
                metadata = json.loads(row["metadata"] or "{}")
                if row["kind"] == "breakthrough":
                    unique_by = ("fact_id",)
                else:
                    unique_by = ("parent_fact_id",)
                add_blackboard_entry_once(
                    conn,
                    project_id,
                    row["kind"],
                    row["content"],
                    child_project_id=project_id,
                    metadata={**metadata, "backfilled": True},
                    unique_by=unique_by,
                )

        if project["process_status"] != "stopped" or project["process_error"]:
            add_blackboard_entry_once(
                conn,
                project_id,
                "process",
                f"Child process {project['process_status']}",
                child_project_id=project_id,
                metadata={
                    "status": project["process_status"],
                    "pid": project["process_pid"],
                    "error": project["process_error"],
                    "backfilled": True,
                },
                unique_by=("status", "pid", "error"),
            )


def _next_scoped_id(
    conn: sqlite3.Connection, kind: str, prefix: str, project_id: str
) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO scoped_counters (project_id, kind, value) VALUES (?, ?, 0)",
        (project_id, kind),
    )
    conn.execute(
        "UPDATE scoped_counters SET value = value + 1 WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    )
    row = conn.execute(
        "SELECT value FROM scoped_counters WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    ).fetchone()
    assert row is not None
    return f"{prefix}{row['value']:03d}"


def next_fact_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "fact", "f", project_id)


def next_intent_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "intent", "i", project_id)


def next_hint_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "hint", "h", project_id)


def next_flag_candidate_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "flag_candidate", "c", project_id)


def build_flag_candidates(conn: sqlite3.Connection, project_id: str) -> list[FlagCandidate]:
    rows = conn.execute(
        """
        SELECT * FROM flag_candidates
        WHERE project_id = ?
        ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'confirmed' THEN 1 ELSE 2 END,
                 created_at, id
        """,
        (project_id,),
    ).fetchall()
    return [FlagCandidate(**dict(row)) for row in rows]


def get_project_or_404(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def check_project_active(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "active":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_hint_writable(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] not in ("active", "paused", "stopped", "completed"):
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_completed(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "completed":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def validate_facts_exist(
    conn: sqlite3.Connection, project_id: str, fact_ids: list[str]
) -> None:
    for fid in fact_ids:
        row = conn.execute(
            "SELECT 1 FROM facts WHERE id = ? AND project_id = ?", (fid, project_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Fact {fid} not found")


def validate_goal_not_in_sources(fact_ids: list[str]) -> None:
    if "goal" in fact_ids:
        raise HTTPException(400, "goal cannot be used in from")


def validate_intent_creator_worker(creator: str, worker: str | None) -> None:
    if worker is not None and worker != creator:
        raise HTTPException(400, "worker must be null or equal to creator")


def get_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Intent not found")
    return row


def get_claimable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is not None and row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_releasable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is None:
        return row
    if row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_completion_intent_or_409(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND to_fact_id = 'goal'",
        (project_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(409, "Completed project is missing its completion intent")
    if len(rows) != 1:
        raise HTTPException(409, "Completed project has multiple completion intents")
    return rows[0]


def intent_to_model(conn: sqlite3.Connection, row: sqlite3.Row, project_id: str) -> Intent:
    sources = conn.execute(
        "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
        (row["id"], project_id),
    ).fetchall()
    return Intent(
        id=row["id"],
        **{"from": [s["fact_id"] for s in sources]},
        to=row["to_fact_id"],
        description=row["description"],
        creator=row["creator"],
        worker=row["worker"],
        last_heartbeat_at=row["last_heartbeat_at"],
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
        child_title=row["child_title"] if "child_title" in row.keys() else None,
        child_origin=row["child_origin"] if "child_origin" in row.keys() else None,
        child_goal=row["child_goal"] if "child_goal" in row.keys() else None,
        priority=row["priority"] if "priority" in row.keys() else 100,
    )


def build_intents(conn: sqlite3.Connection, project_id: str) -> list[Intent]:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    return [intent_to_model(conn, r, project_id) for r in rows]


def get_intent_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT intent_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["intent_timeout"]


def get_reason_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT reason_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["reason_timeout"]


def project_reason_from_row(row: sqlite3.Row) -> ProjectReason | None:
    if row["reason_worker"] is None:
        return None
    return ProjectReason(
        worker=row["reason_worker"],
        trigger=row["reason_trigger"],
        started_at=row["reason_started_at"],
        last_heartbeat_at=row["reason_last_heartbeat_at"],
    )


def project_meta_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> ProjectMeta:
    return ProjectMeta(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        bootstrap_enabled=bool(row["bootstrap_enabled"]),
        created_at=row["created_at"],
        reason=project_reason_from_row(row),
        agents=build_project_agents(conn, row["id"]),
        kind=row["kind"] if "kind" in row.keys() else "parent",
        parent_project_id=row["parent_project_id"] if "parent_project_id" in row.keys() else None,
        parent_intent_id=row["parent_intent_id"] if "parent_intent_id" in row.keys() else None,
        parent_agent=build_parent_agent(conn, row),
        priority=row["priority"] if "priority" in row.keys() else 0,
        summary=row["summary"] if "summary" in row.keys() else None,
        breakthrough=bool(row["breakthrough"]) if "breakthrough" in row.keys() else False,
        process_status=row["process_status"] if "process_status" in row.keys() else "stopped",
        process_pid=row["process_pid"] if "process_pid" in row.keys() else None,
    )


def clear_project_reason(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE id = ?
        """,
        (project_id,),
    )


def expire_workers(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_intent_timeout(conn)
    now = utcnow()
    query = """
        UPDATE intents
        SET worker = NULL
        WHERE to_fact_id IS NULL
          AND worker IS NOT NULL
          AND last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE project_id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)


def expire_reason_leases(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_reason_timeout(conn)
    now = utcnow()
    query = """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE reason_worker IS NOT NULL
          AND reason_last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(reason_last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)
