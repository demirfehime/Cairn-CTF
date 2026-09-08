from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from cairn.server.db import get_conn
from cairn.server.models import (
    ChildCompletionSyncRequest,
    ChildFactPublishRequest,
    ChildProcessUpdateRequest,
    CreateChildRequest,
    Fact,
    MoveAgentRequest,
    ParentSyncResponse,
    ProjectMeta,
)
from cairn.server.services import (
    add_blackboard_entry,
    add_blackboard_entry_once,
    build_blackboard,
    build_project_agents,
    get_project_or_404,
    next_fact_id,
    next_hint_id,
    next_project_id,
    project_meta_from_row,
    utcnow,
    validate_project_fact_scope,
)

router = APIRouter(tags=["orchestration"])

BREAKTHROUGH_TERMS = (
    "flag{",
    "critical",
    "credential",
    "password",
    "secret",
    "token",
    "admin access",
    "remote code execution",
    "rce",
    "shell",
    "proof of concept",
)


def _require_parent(conn, parent_project_id: str):
    row = get_project_or_404(conn, parent_project_id)
    if row["kind"] != "parent":
        raise HTTPException(400, "Project is not a Parent")
    return row


def _require_child(conn, child_project_id: str, parent_project_id: str | None = None):
    row = get_project_or_404(conn, child_project_id)
    if row["kind"] != "child":
        raise HTTPException(400, "Project is not a Child")
    if parent_project_id is not None and row["parent_project_id"] != parent_project_id:
        raise HTTPException(404, "Child does not belong to Parent")
    return row


def _create_child(conn, parent_project_id: str, body: CreateChildRequest) -> ProjectMeta:
    parent = _require_parent(conn, parent_project_id)
    intent = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND id = ?",
        (parent_project_id, body.parent_intent_id),
    ).fetchone()
    if intent is None:
        raise HTTPException(404, "Parent intent not found")
    existing = conn.execute(
        "SELECT * FROM projects WHERE parent_project_id = ? AND parent_intent_id = ?",
        (parent_project_id, body.parent_intent_id),
    ).fetchone()
    if existing is not None:
        return project_meta_from_row(conn, existing)

    participant_count = conn.execute(
        "SELECT COUNT(*) AS count FROM project_agents WHERE project_id = ? AND role = 'participant'",
        (parent_project_id,),
    ).fetchone()["count"]
    unfinished_count = conn.execute(
        """
        SELECT COUNT(*) AS count FROM projects
        WHERE parent_project_id = ? AND status != 'completed'
        """,
        (parent_project_id,),
    ).fetchone()["count"]
    max_children = max(1, participant_count + 1)
    if unfinished_count >= max_children:
        raise HTTPException(409, f"Parent may have at most {max_children} unfinished Children")

    child_id = next_project_id(conn)
    now = utcnow()
    conn.execute(
        """
        INSERT INTO projects
            (id, title, status, bootstrap_enabled, created_at, kind,
             parent_project_id, parent_intent_id, priority)
        VALUES (?, ?, 'paused', ?, ?, 'child', ?, ?, ?)
        """,
        (
            child_id,
            body.title,
            parent["bootstrap_enabled"],
            now,
            parent_project_id,
            body.parent_intent_id,
            body.priority,
        ),
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description) VALUES ('origin', ?, ?)",
        (child_id, body.origin),
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description) VALUES ('goal', ?, ?)",
        (child_id, body.goal),
    )
    add_blackboard_entry(
        conn,
        parent_project_id,
        "child_created",
        f"Created {child_id}: {body.title}\nGoal: {body.goal}",
        child_project_id=child_id,
        metadata={"parent_intent_id": body.parent_intent_id, "priority": body.priority},
    )
    add_blackboard_entry(
        conn,
        child_id,
        "origin",
        body.origin,
        child_project_id=child_id,
        metadata={"fact_id": "origin"},
    )
    add_blackboard_entry(
        conn,
        child_id,
        "goal",
        body.goal,
        child_project_id=child_id,
        metadata={"fact_id": "goal", "parent_intent_id": body.parent_intent_id},
    )
    return project_meta_from_row(
        conn, conn.execute("SELECT * FROM projects WHERE id = ?", (child_id,)).fetchone()
    )


def _next_assignment_epoch(conn, parent_project_id: str, agent_id: str) -> int:
    conn.execute(
        """
        INSERT OR IGNORE INTO agent_assignment_counters
            (parent_project_id, agent_id, value, last_moved_at)
        VALUES (?, ?, 0, NULL)
        """,
        (parent_project_id, agent_id),
    )
    conn.execute(
        """
        UPDATE agent_assignment_counters
        SET value = value + 1, last_moved_at = ?
        WHERE parent_project_id = ? AND agent_id = ?
        """,
        (utcnow(), parent_project_id, agent_id),
    )
    return conn.execute(
        "SELECT value FROM agent_assignment_counters WHERE parent_project_id = ? AND agent_id = ?",
        (parent_project_id, agent_id),
    ).fetchone()["value"]


def _assign(conn, parent_project_id: str, child_id: str, agent_id: str) -> None:
    participant = conn.execute(
        """
        SELECT pa.use_hints, a.health_status FROM project_agents pa
        JOIN agents a ON a.id = pa.agent_id
        WHERE pa.project_id = ? AND pa.agent_id = ? AND pa.role = 'participant'
        """,
        (parent_project_id, agent_id),
    ).fetchone()
    if participant is None:
        raise HTTPException(400, "Agent is not in the Parent participant pool")
    if participant["health_status"] == "unhealthy":
        raise HTTPException(409, "Agent is unhealthy and cannot be assigned")
    epoch = _next_assignment_epoch(conn, parent_project_id, agent_id)
    conn.execute(
        """
        INSERT INTO project_agents
            (project_id, agent_id, use_hints, role, assignment_epoch, assigned_at)
        VALUES (?, ?, ?, 'assigned', ?, ?)
        """,
        (child_id, agent_id, participant["use_hints"], epoch, utcnow()),
    )
    add_blackboard_entry(
        conn,
        child_id,
        "agent_assigned",
        f"Agent {agent_id} assigned to this Child",
        child_project_id=child_id,
        metadata={"agent_id": agent_id, "assignment_epoch": epoch},
    )


def _rebalance(conn, parent_project_id: str) -> ParentSyncResponse:
    _require_parent(conn, parent_project_id)
    participants = conn.execute(
        """
        SELECT pa.agent_id FROM project_agents pa
        JOIN agents a ON a.id = pa.agent_id
        WHERE pa.project_id = ? AND pa.role = 'participant' AND a.enabled = 1
          AND a.health_status != 'unhealthy'
        ORDER BY a.priority, a.name
        """,
        (parent_project_id,),
    ).fetchall()
    agent_ids = [row["agent_id"] for row in participants]
    children = conn.execute(
        """
        SELECT * FROM projects
        WHERE parent_project_id = ? AND status NOT IN ('completed', 'stopped')
        ORDER BY priority, created_at, id
        """,
        (parent_project_id,),
    ).fetchall()

    active_ids = {row["id"] for row in children[: len(agent_ids)]}
    paused_ids = {row["id"] for row in children[len(agent_ids) :]}
    for child_id in active_ids:
        conn.execute("UPDATE projects SET status = 'active' WHERE id = ?", (child_id,))
    for child_id in paused_ids:
        conn.execute("UPDATE projects SET status = 'paused' WHERE id = ?", (child_id,))
        conn.execute(
            "UPDATE intents SET worker = NULL WHERE project_id = ? AND concluded_at IS NULL",
            (child_id,),
        )

    assignments = conn.execute(
        """
        SELECT pa.project_id, pa.agent_id FROM project_agents pa
        JOIN projects c ON c.id = pa.project_id
        WHERE c.parent_project_id = ? AND pa.role = 'assigned'
        """,
        (parent_project_id,),
    ).fetchall()
    assigned_by_agent = {row["agent_id"]: row["project_id"] for row in assignments}
    counts: dict[str, int] = {}
    for row in assignments:
        if row["agent_id"] not in agent_ids:
            conn.execute(
                "DELETE FROM project_agents WHERE project_id = ? AND agent_id = ? AND role = 'assigned'",
                (row["project_id"], row["agent_id"]),
            )
            assigned_by_agent.pop(row["agent_id"], None)
        elif row["project_id"] in active_ids:
            counts[row["project_id"]] = counts.get(row["project_id"], 0) + 1
        else:
            conn.execute(
                "DELETE FROM project_agents WHERE project_id = ? AND agent_id = ? AND role = 'assigned'",
                (row["project_id"], row["agent_id"]),
            )
            assigned_by_agent.pop(row["agent_id"], None)

    idle_agents = [agent_id for agent_id in agent_ids if agent_id not in assigned_by_agent]
    active_order = [row["id"] for row in children if row["id"] in active_ids]
    for child_id in active_order:
        if counts.get(child_id, 0) != 0:
            continue
        agent_id = idle_agents.pop(0) if idle_agents else None
        if agent_id is None:
            donor = next(
                (donor_id for donor_id in active_order if counts.get(donor_id, 0) > 1),
                None,
            )
            if donor is not None:
                donor_assignment = conn.execute(
                    """
                    SELECT agent_id FROM project_agents
                    WHERE project_id = ? AND role = 'assigned'
                    ORDER BY assigned_at DESC LIMIT 1
                    """,
                    (donor,),
                ).fetchone()
                if donor_assignment is not None:
                    agent_id = donor_assignment["agent_id"]
                    conn.execute(
                        "DELETE FROM project_agents WHERE project_id = ? AND agent_id = ? AND role = 'assigned'",
                        (donor, agent_id),
                    )
                    counts[donor] -= 1
        if agent_id is not None:
            _assign(conn, parent_project_id, child_id, agent_id)
            assigned_by_agent[agent_id] = child_id
            counts[child_id] = 1

    # Spare Agents reinforce the highest-priority active Child until Parent moves them.
    for agent_id in idle_agents:
        if not active_order:
            break
        target = min(active_order, key=lambda child_id: counts.get(child_id, 0))
        _assign(conn, parent_project_id, target, agent_id)
        counts[target] = counts.get(target, 0) + 1

    return ParentSyncResponse(
        active_children=sorted(active_ids),
        paused_children=sorted(paused_ids),
    )


@router.post("/projects/{parent_project_id}/children", response_model=ProjectMeta, status_code=201)
def create_child(parent_project_id: str, body: CreateChildRequest):
    with get_conn() as conn:
        child = _create_child(conn, parent_project_id, body)
        _rebalance(conn, parent_project_id)
        refreshed = conn.execute("SELECT * FROM projects WHERE id = ?", (child.id,)).fetchone()
        return project_meta_from_row(conn, refreshed)


@router.post("/projects/{parent_project_id}/children/materialize", response_model=ParentSyncResponse)
def materialize_parent_intents(parent_project_id: str):
    with get_conn() as conn:
        parent = _require_parent(conn, parent_project_id)
        participants = conn.execute(
            "SELECT COUNT(*) AS count FROM project_agents WHERE project_id = ? AND role = 'participant'",
            (parent_project_id,),
        ).fetchone()["count"]
        unfinished = conn.execute(
            "SELECT COUNT(*) AS count FROM projects WHERE parent_project_id = ? AND status != 'completed'",
            (parent_project_id,),
        ).fetchone()["count"]
        slots = max(0, max(1, participants + 1) - unfinished)
        intents = conn.execute(
            """
            SELECT i.* FROM intents i
            WHERE i.project_id = ? AND i.to_fact_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM projects c
                  WHERE c.parent_project_id = ? AND c.parent_intent_id = i.id
              )
            ORDER BY i.created_at, i.id
            LIMIT ?
            """,
            (parent_project_id, parent_project_id, slots),
        ).fetchall()
        created = []
        for offset, intent in enumerate(intents):
            source_rows = conn.execute(
                """
                SELECT f.id, f.description FROM intent_sources s
                JOIN facts f ON f.project_id = s.project_id AND f.id = s.fact_id
                WHERE s.project_id = ? AND s.intent_id = ? ORDER BY s.rowid
                """,
                (parent_project_id, intent["id"]),
            ).fetchall()
            source_text = "\n\n".join(
                f"[{source['id']}] {source['description']}" for source in source_rows
            )
            if not source_text:
                source_text = conn.execute(
                    "SELECT description FROM facts WHERE project_id = ? AND id = 'origin'",
                    (parent_project_id,),
                ).fetchone()["description"]
            child = _create_child(
                conn,
                parent_project_id,
                CreateChildRequest(
                    parent_intent_id=intent["id"],
                    title=intent["child_title"] or f"{intent['id']}: {intent['description'][:72]}",
                    origin=intent["child_origin"]
                    or f"Parent project: {parent['title']}\n\nStarting evidence:\n{source_text}",
                    goal=intent["child_goal"] or intent["description"],
                    priority=intent["priority"] if intent["priority"] is not None else 100 + unfinished + offset,
                ),
            )
            created.append(child)
        state = _rebalance(conn, parent_project_id)
        state.created_children = created
        return state


@router.post("/projects/{parent_project_id}/allocations/rebalance", response_model=ParentSyncResponse)
def rebalance_allocations(parent_project_id: str):
    with get_conn() as conn:
        return _rebalance(conn, parent_project_id)


@router.post("/projects/{parent_project_id}/allocations/move", response_model=ParentSyncResponse)
def move_agent(parent_project_id: str, body: MoveAgentRequest):
    with get_conn() as conn:
        _require_parent(conn, parent_project_id)
        target = _require_child(conn, body.target_child_id, parent_project_id)
        current = conn.execute(
            """
            SELECT pa.project_id, pa.assigned_at, c.priority, c.status
            FROM project_agents pa JOIN projects c ON c.id = pa.project_id
            WHERE c.parent_project_id = ? AND pa.agent_id = ? AND pa.role = 'assigned'
            """,
            (parent_project_id, body.agent_id),
        ).fetchone()
        if current and current["project_id"] == body.target_child_id:
            return _rebalance(conn, parent_project_id)

        settings = conn.execute("SELECT agent_reassignment_cooldown FROM settings WHERE rowid = 1").fetchone()
        counter = conn.execute(
            "SELECT last_moved_at FROM agent_assignment_counters WHERE parent_project_id = ? AND agent_id = ?",
            (parent_project_id, body.agent_id),
        ).fetchone()
        if counter and counter["last_moved_at"] and not body.breakthrough:
            moved_at = datetime.fromisoformat(counter["last_moved_at"].replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - moved_at).total_seconds()
            if elapsed < settings["agent_reassignment_cooldown"]:
                raise HTTPException(
                    409,
                    f"Agent reassignment cooldown has {int(settings['agent_reassignment_cooldown'] - elapsed)}s remaining",
                )

        if current:
            source_count = conn.execute(
                "SELECT COUNT(*) AS count FROM project_agents WHERE project_id = ? AND role = 'assigned'",
                (current["project_id"],),
            ).fetchone()["count"]
            if source_count <= 1 and current["status"] == "active":
                if target["priority"] < current["priority"]:
                    conn.execute("UPDATE projects SET status = 'paused' WHERE id = ?", (current["project_id"],))
                else:
                    raise HTTPException(409, "Cannot move the last Agent from an active Child")
            conn.execute(
                "DELETE FROM project_agents WHERE project_id = ? AND agent_id = ? AND role = 'assigned'",
                (current["project_id"], body.agent_id),
            )
            add_blackboard_entry(
                conn,
                current["project_id"],
                "agent_released",
                f"Agent {body.agent_id} moved to {body.target_child_id}",
                child_project_id=current["project_id"],
                metadata={"agent_id": body.agent_id, "target_child_id": body.target_child_id},
            )
        _assign(conn, parent_project_id, body.target_child_id, body.agent_id)
        conn.execute("UPDATE projects SET status = 'active' WHERE id = ?", (body.target_child_id,))
        add_blackboard_entry(
            conn,
            parent_project_id,
            "allocation",
            body.reason,
            child_project_id=body.target_child_id,
            metadata={
                "agent_id": body.agent_id,
                "from_child_id": current["project_id"] if current else None,
                "breakthrough": body.breakthrough,
            },
        )
        return _rebalance(conn, parent_project_id)


@router.post("/projects/{child_project_id}/blackboard/facts")
def publish_child_fact(child_project_id: str, body: ChildFactPublishRequest):
    with get_conn() as conn:
        child = _require_child(conn, child_project_id)
        validate_project_fact_scope(conn, child_project_id, body.description)
        parent_id = child["parent_project_id"]
        source = conn.execute("SELECT description FROM facts WHERE project_id = ? AND id = ?", (child_project_id, body.fact_id)).fetchone()
        if source is None:
            raise HTTPException(404, "Source Fact not found")
        from cairn.server.routers.projects import report_fact_flags
        report_fact_flags(conn, child_project_id, body.fact_id, source["description"], "child-publication")
        existing = conn.execute(
            "SELECT 1 FROM child_fact_publications WHERE child_project_id = ? AND fact_id = ?",
            (child_project_id, body.fact_id),
        ).fetchone()
        if existing:
            rows = conn.execute(
                "SELECT * FROM blackboard WHERE parent_project_id = ? AND child_project_id = ? ORDER BY created_at DESC",
                (parent_id, child_project_id),
            ).fetchall()
            for row in rows:
                if f'"fact_id":"{body.fact_id}"' in row["metadata"]:
                    return next(entry for entry in build_blackboard(conn, parent_id) if entry.id == row["id"])
        lowered = body.description.casefold()
        breakthrough = any(term in lowered for term in BREAKTHROUGH_TERMS)
        kind = "breakthrough" if breakthrough else "child_fact"
        if breakthrough:
            add_blackboard_entry_once(
                conn,
                child_project_id,
                "breakthrough",
                body.description,
                child_project_id=child_project_id,
                metadata={"fact_id": body.fact_id},
                unique_by=("fact_id",),
            )
        entry = add_blackboard_entry(
            conn,
            parent_id,
            kind,
            body.description,
            child_project_id=child_project_id,
            metadata={"fact_id": body.fact_id},
        )
        if breakthrough:
            conn.execute("UPDATE projects SET breakthrough = 1 WHERE id = ?", (child_project_id,))
        conn.execute(
            "INSERT INTO child_fact_publications (child_project_id, fact_id, published_at) VALUES (?, ?, ?)",
            (child_project_id, body.fact_id, utcnow()),
        )
        hint_id = next_hint_id(conn, parent_id)
        conn.execute(
            "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                hint_id,
                parent_id,
                f"Child {child_project_id} reported {body.fact_id}: {body.description}",
                f"blackboard.{child_project_id}",
                utcnow(),
            ),
        )
        return entry


@router.post("/projects/{child_project_id}/sync-completion", response_model=Fact)
def sync_child_completion(child_project_id: str, body: ChildCompletionSyncRequest):
    with get_conn() as conn:
        child = _require_child(conn, child_project_id)
        validate_project_fact_scope(conn, child_project_id, body.description)
        if child["status"] != "completed":
            raise HTTPException(409, "Child is not completed")
        parent_id = child["parent_project_id"]
        intent_id = child["parent_intent_id"]
        intent = conn.execute(
            "SELECT * FROM intents WHERE project_id = ? AND id = ?",
            (parent_id, intent_id),
        ).fetchone()
        if intent is None:
            raise HTTPException(409, "Parent intent is missing")
        if intent["to_fact_id"]:
            fact = conn.execute(
                "SELECT * FROM facts WHERE project_id = ? AND id = ?",
                (parent_id, intent["to_fact_id"]),
            ).fetchone()
            return Fact(**dict(fact))

        fact_id = next_fact_id(conn, parent_id)
        now = utcnow()
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (fact_id, parent_id, body.description),
        )
        conn.execute(
            """
            UPDATE intents SET to_fact_id = ?, worker = 'parent.child-sync',
                last_heartbeat_at = ?, concluded_at = ?
            WHERE project_id = ? AND id = ?
            """,
            (fact_id, now, now, parent_id, intent_id),
        )
        conn.execute(
            "UPDATE projects SET summary = ?, breakthrough = ? WHERE id = ?",
            (body.description, int(body.breakthrough), child_project_id),
        )
        conn.execute(
            "DELETE FROM project_agents WHERE project_id = ? AND role = 'assigned'",
            (child_project_id,),
        )
        add_blackboard_entry(
            conn,
            parent_id,
            "child_summary",
            body.description,
            child_project_id=child_project_id,
            metadata={"parent_fact_id": fact_id, "breakthrough": body.breakthrough},
        )
        add_blackboard_entry_once(
            conn,
            child_project_id,
            "child_summary",
            body.description,
            child_project_id=child_project_id,
            metadata={"parent_fact_id": fact_id, "breakthrough": body.breakthrough},
            unique_by=("parent_fact_id",),
        )
        _rebalance(conn, parent_id)
        from cairn.extensions.library import link_trace
        link_trace(conn, parent_id, fact_id, child_project_id, "goal")
        return Fact(id=fact_id, description=body.description)


@router.put("/projects/{child_project_id}/process", response_model=ProjectMeta)
def update_child_process(child_project_id: str, body: ChildProcessUpdateRequest):
    with get_conn() as conn:
        child = _require_child(conn, child_project_id)
        conn.execute(
            """
            UPDATE projects
            SET process_status = ?, process_pid = ?, process_log_dir = ?,
                process_error = ?, process_updated_at = ?
            WHERE id = ?
            """,
            (body.status, body.pid, body.log_dir, body.error, utcnow(), child_project_id),
        )
        process_changed = (
            child["process_status"] != body.status
            or child["process_pid"] != body.pid
            or child["process_error"] != body.error
        )
        if process_changed:
            metadata = {"status": body.status, "pid": body.pid, "error": body.error}
            add_blackboard_entry(
                conn,
                child_project_id,
                "process",
                f"Child process {body.status}",
                child_project_id=child_project_id,
                metadata=metadata,
            )
            add_blackboard_entry(
                conn,
                child["parent_project_id"],
                "process",
                f"Child process {body.status}",
                child_project_id=child_project_id,
                metadata=metadata,
            )
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (child_project_id,)).fetchone()
        return project_meta_from_row(conn, row)
