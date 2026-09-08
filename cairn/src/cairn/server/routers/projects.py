import re

from cairn.server.routers.attachments import bind_to_project, list_for_project

from cairn.extensions.library import fact_trace, link_trace
from fastapi import APIRouter, HTTPException, Query, Response

from cairn.server.db import get_conn
from cairn.server.models import (
    CompleteRequest,
    CreateProjectRequest,
    Fact,
    FlagCandidate,
    FlagCandidateDecisionRequest,
    Hint,
    HeartbeatRequest,
    Intent,
    ProjectDetail,
    ProjectMeta,
    ProjectSummary,
    ReopenRequest,
    ReopenResponse,
    ReasonClaimRequest,
    UpdateProjectTitleRequest,
    UpdateProjectStatusRequest,
)
from cairn.server.services import (
    add_blackboard_entry,
    build_flag_candidates,
    build_intents,
    build_blackboard,
    build_project_agents,
    check_project_completed,
    check_project_active,
    clear_project_reason,
    expire_reason_leases,
    expire_workers,
    ensure_project_blackboard,
    get_completion_intent_or_409,
    get_project_or_404,
    intent_to_model,
    next_fact_id,
    next_flag_candidate_id,
    next_hint_id,
    next_intent_id,
    next_project_id,
    project_meta_from_row,
    project_reason_from_row,
    utcnow,
    validate_facts_exist,
    validate_goal_not_in_sources,
    validate_project_fact_scope,
)

router = APIRouter(tags=["projects"])

FLAG_PATTERN = re.compile(r"(?<![a-z0-9_])(?:[a-z0-9_-]*(?:flag|ctf))\{[^{}\s]{1,256}\}", re.IGNORECASE)
FLAG_GOAL_PATTERN = re.compile(r"(?<![a-z0-9_])flag(?![a-z0-9_])", re.IGNORECASE)


def _goal_requires_flag(conn, project_id: str) -> bool:
    goal_row = conn.execute(
        "SELECT description FROM facts WHERE project_id = ? AND id = 'goal'",
        (project_id,),
    ).fetchone()
    goal = goal_row["description"] if goal_row else ""
    return FLAG_GOAL_PATTERN.search(goal) is not None


def _validate_non_flag_parent_completion(conn, project_id: str) -> None:

    unfinished_children = conn.execute(
        """
        SELECT COUNT(*) AS count FROM projects
        WHERE parent_project_id = ? AND status != 'completed'
        """,
        (project_id,),
    ).fetchone()["count"]
    open_intents = conn.execute(
        "SELECT COUNT(*) AS count FROM intents WHERE project_id = ? AND to_fact_id IS NULL",
        (project_id,),
    ).fetchone()["count"]
    if unfinished_children or open_intents:
        raise HTTPException(
            409,
            "Parent cannot complete while Children or Parent intents are still unfinished",
        )


def _source_supports_flag(
    conn,
    project_id: str,
    source_fact_id: str,
    source_text: str,
    completion_description: str,
    flag_value: str,
) -> bool:
    supporting_texts = [source_text]
    blackboard = build_blackboard(conn, project_id)
    linked_child_ids: set[str] = set()

    for entry in blackboard:
        metadata = entry.metadata or {}
        if (
            entry.kind == "child_summary"
            and metadata.get("parent_fact_id") == source_fact_id
            and entry.child_project_id
        ):
            linked_child_ids.add(entry.child_project_id)
            supporting_texts.append(entry.content)
        elif entry.child_project_id is None and metadata.get("fact_id") == source_fact_id:
            supporting_texts.append(entry.content)

    if linked_child_ids:
        supporting_texts.extend(
            entry.content
            for entry in blackboard
            if entry.child_project_id in linked_child_ids
            and entry.kind in {"child_fact", "breakthrough"}
        )

    return any(flag_value in text for text in supporting_texts)


def _stage_flag_candidate(conn, project_id: str, body: CompleteRequest) -> FlagCandidate:
    evidence = body.evidence
    if evidence is None:
        raise HTTPException(409, "Parent Flag goal requires verified completion evidence")
    if evidence.kind != "flag":
        raise HTTPException(409, "Parent Flag goal requires verified Flag evidence")
    match = FLAG_PATTERN.fullmatch(evidence.value)
    if match is None:
        raise HTTPException(409, "Completion evidence does not contain a valid flag{...} or CTF-format value")
    flag_value = match.group(0)
    validate_project_fact_scope(conn, project_id, body.description)

    existing = conn.execute(
        "SELECT * FROM flag_candidates WHERE project_id = ? AND value = ?",
        (project_id, flag_value),
    ).fetchone()
    if existing is not None:
        return FlagCandidate(**dict(existing))

    fact_rows = conn.execute(
        """
        SELECT id, description FROM facts
        WHERE project_id = ?
        ORDER BY CASE id WHEN 'origin' THEN 2 WHEN 'goal' THEN 3 ELSE 0 END, id
        """,
        (project_id,),
    ).fetchall()
    facts = {row["id"]: row["description"] for row in fact_rows}
    source_fact_id = evidence.source if evidence.source in facts else None
    if source_fact_id is None:
        source_fact_id = next(
            (
                fact_id
                for fact_id, description in facts.items()
                if flag_value in description
            ),
            None,
        )
    if source_fact_id is None:
        source_fact_id = next((fact_id for fact_id in body.from_ if fact_id in facts), None)
    if source_fact_id is None:
        raise HTTPException(409, "Verified Flag has no valid source Fact")
    source_text = facts[source_fact_id]
    if not _source_supports_flag(
        conn,
        project_id,
        source_fact_id,
        source_text,
        body.description,
        flag_value,
    ):
        raise HTTPException(
            409,
            "Candidate Flag must appear in its source Fact "
            "or linked Child evidence",
        )

    candidate_id = next_flag_candidate_id(conn, project_id)
    now = utcnow()
    conn.execute(
        """
        INSERT INTO flag_candidates
            (id, project_id, value, source_fact_id, reported_source, description,
             worker, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            candidate_id,
            project_id,
            flag_value,
            source_fact_id,
            evidence.source,
            body.description,
            body.worker,
            now,
        ),
    )
    add_blackboard_entry(
        conn,
        project_id,
        "flag_candidate",
        f"Candidate Flag awaiting human confirmation: {flag_value}",
        metadata={
            "candidate_id": candidate_id,
            "value": flag_value,
            "source_fact_id": source_fact_id,
            "reported_source": evidence.source,
            "worker": body.worker,
            "status": "pending",
        },
    )
    row = conn.execute(
        "SELECT * FROM flag_candidates WHERE project_id = ? AND id = ?",
        (project_id, candidate_id),
    ).fetchone()
    assert row is not None
    return FlagCandidate(**dict(row))


def report_fact_flags(conn, project_id: str, fact_id: str, description: str, worker: str) -> list[FlagCandidate]:
    """Stage exact candidates as soon as evidence is saved, without completing work."""
    values = list(dict.fromkeys(match.group(0) for match in FLAG_PATTERN.finditer(description)))
    values = [value for value in values if not any(token in value.casefold() for token in
              ("...", "exact_verified_value", "exact_value", "placeholder", "example_flag"))]
    if not values or fact_id in {"origin", "goal"}:
        return []
    project = get_project_or_404(conn, project_id)
    parent_id = project["parent_project_id"] if project["kind"] == "child" else project_id
    parent = get_project_or_404(conn, parent_id)
    if parent["status"] == "completed":
        return []
    results = []
    for value in values:
        existing = conn.execute("SELECT * FROM flag_candidates WHERE project_id = ? AND value = ?", (parent_id, value)).fetchone()
        if existing:
            results.append(FlagCandidate(**dict(existing)))
            continue
        source = fact_id
        if parent_id != project_id:
            source = next_fact_id(conn, parent_id)
            conn.execute("INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
                         (source, parent_id, description))
            link_trace(conn, parent_id, source, project_id, fact_id)
            add_blackboard_entry(conn, parent_id, "child_fact", description, child_project_id=project_id,
                                 metadata={"fact_id": fact_id, "parent_fact_id": source, "worker": worker})
        body = CompleteRequest(**{"from": [source], "description": f"Possible Flag awaiting human review: {value}",
                               "worker": worker, "evidence": {"kind": "flag", "value": value,
                               "source": source, "verified": False}})
        candidate = _stage_flag_candidate(conn, parent_id, body)
        if parent_id != project_id:
            conn.execute("UPDATE flag_candidates SET reported_source = ? WHERE project_id = ? AND id = ?",
                         (f"{project_id}/{fact_id}", parent_id, candidate.id))
        results.append(candidate)
    return results


@router.get("/flag-candidates/pending")
def pending_flag_reports():
    with get_conn() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT c.*, p.title AS project_title FROM flag_candidates c JOIN projects p ON p.id = c.project_id "
            "WHERE c.status = 'pending' ORDER BY c.created_at, c.project_id, c.id"
        ).fetchall()]


def _complete_project_with_sources(
    conn,
    row,
    project_id: str,
    body: CompleteRequest,
    sources: list[str] | None = None,
) -> Intent:
    source_ids = sources if sources is not None else body.from_
    validate_facts_exist(conn, project_id, source_ids)
    validate_goal_not_in_sources(source_ids)
    for source_id in source_ids:
        link_trace(conn, project_id, "goal", project_id, source_id)
    now = utcnow()
    iid = next_intent_id(conn, project_id)

    conn.execute(
        "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, 'goal', ?, ?, ?, ?, ?, ?)",
        (iid, project_id, body.description, body.worker, body.worker, now, now, now),
    )
    for fid in source_ids:
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
            (iid, project_id, fid),
        )
    conn.execute(
        """
        UPDATE projects
        SET status = 'completed',
            reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE id = ?
        """,
        (project_id,),
    )
    if row["kind"] == "parent":
        conn.execute(
            "UPDATE projects SET status = 'stopped' WHERE parent_project_id = ? AND status != 'completed'",
            (project_id,),
        )
    else:
        conn.execute(
            "UPDATE projects SET summary = ? WHERE id = ?",
            (body.description, project_id),
        )

    add_blackboard_entry(
        conn,
        project_id,
        "completion",
        body.description,
        child_project_id=project_id if row["kind"] == "child" else None,
        metadata={
            "intent_id": iid,
            "from": source_ids,
            "worker": body.worker,
            "evidence": body.evidence.model_dump() if body.evidence else None,
        },
    )
    return Intent(
        id=iid,
        **{"from": source_ids},
        to="goal",
        description=body.description,
        creator=body.worker,
        worker=body.worker,
        last_heartbeat_at=now,
        created_at=now,
        concluded_at=now,
    )


@router.get("/projects", response_model=list[ProjectSummary])
def list_projects(
    kind: str | None = Query(default=None, pattern="^(parent|child)$"),
    parent_id: str | None = None,
):
    with get_conn() as conn:
        expire_workers(conn)
        expire_reason_leases(conn)
        filters = []
        params: list[str] = []
        if kind:
            filters.append("p.kind = ?")
            params.append(kind)
        if parent_id:
            filters.append("p.parent_project_id = ?")
            params.append(parent_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        rows = conn.execute(f"""
            SELECT p.*,
                (SELECT COUNT(*) FROM facts WHERE project_id = p.id) AS fact_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id) AS intent_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NOT NULL) AS working_intent_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NULL) AS unclaimed_intent_count,
                (SELECT COUNT(*) FROM hints WHERE project_id = p.id) AS hint_count
            FROM projects p
            {where}
            ORDER BY p.created_at
        """, params).fetchall()
        return [
            ProjectSummary(
                id=row["id"],
                title=row["title"],
                status=row["status"],
                bootstrap_enabled=bool(row["bootstrap_enabled"]),
                created_at=row["created_at"],
                reason=project_reason_from_row(row),
                agents=build_project_agents(conn, row["id"]),
                fact_count=row["fact_count"],
                intent_count=row["intent_count"],
                working_intent_count=row["working_intent_count"],
                unclaimed_intent_count=row["unclaimed_intent_count"],
                hint_count=row["hint_count"],
                kind=row["kind"],
                parent_project_id=row["parent_project_id"],
                parent_intent_id=row["parent_intent_id"],
                parent_agent=project_meta_from_row(conn, row).parent_agent,
                priority=row["priority"],
                summary=row["summary"],
                breakthrough=bool(row["breakthrough"]),
                process_status=row["process_status"],
                process_pid=row["process_pid"],
            )
            for row in rows
        ]


@router.post("/projects", response_model=ProjectDetail, status_code=201)
def create_project(body: CreateProjectRequest):
    with get_conn() as conn:
        agent_ids = [selection.agent_id for selection in body.agents]
        if agent_ids and not body.parent_agent_id:
            raise HTTPException(400, "parent_agent_id is required when participant Agents are selected")
        if body.parent_agent_id and not agent_ids:
            raise HTTPException(400, "At least one participating Agent is required")
        project_kind = "parent" if body.parent_agent_id else "child"
        if len(set(agent_ids)) != len(agent_ids):
            raise HTTPException(400, "Project agents must be unique")
        all_agent_ids = agent_ids + ([body.parent_agent_id] if body.parent_agent_id else [])
        if body.parent_agent_id and body.parent_agent_id in agent_ids:
            raise HTTPException(400, "Parent Agent must be dedicated and cannot join the participant pool")
        if all_agent_ids:
            placeholders = ",".join("?" for _ in all_agent_ids)
            rows = conn.execute(
                f"SELECT id, enabled FROM agents WHERE id IN ({placeholders})",
                all_agent_ids,
            ).fetchall()
            found = {row["id"]: bool(row["enabled"]) for row in rows}
            missing = [agent_id for agent_id in all_agent_ids if agent_id not in found]
            disabled = [agent_id for agent_id in all_agent_ids if agent_id in found and not found[agent_id]]
            if missing:
                raise HTTPException(400, f"Unknown agents: {', '.join(missing)}")
            if disabled:
                raise HTTPException(400, f"Disabled agents cannot join a project: {', '.join(disabled)}")
        pid = next_project_id(conn)
        now = utcnow()

        conn.execute(
            """
            INSERT INTO projects
                (id, title, status, bootstrap_enabled, created_at, kind, parent_agent_id)
            VALUES (?, ?, 'active', ?, ?, ?, ?)
            """,
            (pid, body.title, body.bootstrap_enabled, now, project_kind, body.parent_agent_id),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            ("origin", pid, body.origin),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            ("goal", pid, body.goal),
        )
        for selection in body.agents:
            conn.execute(
                """
                INSERT INTO project_agents
                    (project_id, agent_id, use_hints, role, assignment_epoch, assigned_at)
                VALUES (?, ?, ?, 'participant', 0, NULL)
                """,
                (pid, selection.agent_id, int(selection.use_hints)),
            )

        bind_to_project(conn, pid, body.attachment_ids)
        hints = []
        if body.hints:
            for h in body.hints:
                hid = next_hint_id(conn, pid)
                conn.execute(
                    "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
                    (hid, pid, h.content, h.creator, now),
                )
                hints.append(Hint(id=hid, content=h.content, creator=h.creator, created_at=now))

        board_child_id = pid if project_kind == "child" else None
        add_blackboard_entry(
            conn,
            pid,
            "origin",
            body.origin,
            child_project_id=board_child_id,
            metadata={"fact_id": "origin"},
        )
        add_blackboard_entry(
            conn,
            pid,
            "goal",
            body.goal,
            child_project_id=board_child_id,
            metadata={"fact_id": "goal"},
        )
        for hint in hints:
            add_blackboard_entry(
                conn,
                pid,
                "hint",
                hint.content,
                child_project_id=board_child_id,
                metadata={"hint_id": hint.id, "creator": hint.creator},
            )

        blackboard = build_blackboard(conn, pid)

        return ProjectDetail(
            attachments=list_for_project(conn, pid),
            project=ProjectMeta(
                id=pid,
                title=body.title,
                status="active",
                bootstrap_enabled=body.bootstrap_enabled,
                created_at=now,
                reason=None,
                agents=build_project_agents(conn, pid),
                kind=project_kind,
                parent_agent=project_meta_from_row(
                    conn, conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
                ).parent_agent,
            ),
            facts=[
                Fact(id="origin", description=body.origin),
                Fact(id="goal", description=body.goal),
            ],
            intents=[],
            hints=hints,
            blackboard=blackboard,
            flag_candidates=build_flag_candidates(conn, pid),
        )


@router.get("/projects/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str):
    with get_conn() as conn:
        expire_workers(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)

        facts = conn.execute(
            "SELECT * FROM facts WHERE project_id = ?", (project_id,)
        ).fetchall()
        hints = conn.execute(
            "SELECT * FROM hints WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()

        children = []
        if row["kind"] == "parent":
            child_rows = conn.execute(
                """
                SELECT p.*,
                    (SELECT COUNT(*) FROM facts WHERE project_id = p.id) AS fact_count,
                    (SELECT COUNT(*) FROM intents WHERE project_id = p.id) AS intent_count,
                    (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NOT NULL) AS working_intent_count,
                    (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NULL) AS unclaimed_intent_count,
                    (SELECT COUNT(*) FROM hints WHERE project_id = p.id) AS hint_count
                FROM projects p WHERE p.parent_project_id = ? ORDER BY p.priority, p.created_at
                """,
                (project_id,),
            ).fetchall()
            children = [
                ProjectSummary(
                    **project_meta_from_row(conn, child).model_dump(),
                    fact_count=child["fact_count"],
                    intent_count=child["intent_count"],
                    working_intent_count=child["working_intent_count"],
                    unclaimed_intent_count=child["unclaimed_intent_count"],
                    hint_count=child["hint_count"],
                )
                for child in child_rows
            ]
        ensure_project_blackboard(conn, project_id)
        blackboard = build_blackboard(conn, project_id)

        return ProjectDetail(
            attachments=list_for_project(conn, project_id),
            project=project_meta_from_row(conn, row),
            facts=[Fact(**dict(f), extension_trace=fact_trace(conn, project_id, f["id"])) for f in facts],
            intents=build_intents(conn, project_id),
            hints=[Hint(**dict(h)) for h in hints],
            children=children,
            blackboard=blackboard,
            flag_candidates=build_flag_candidates(conn, project_id),
        )


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute("DELETE FROM projects WHERE parent_project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


@router.put("/projects/{project_id}/title", response_model=ProjectMeta)
def update_project_title(project_id: str, body: UpdateProjectTitleRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute(
            "UPDATE projects SET title = ? WHERE id = ?",
            (body.title, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(conn, updated)


@router.put("/projects/{project_id}/status", response_model=ProjectMeta)
def update_project_status(project_id: str, body: UpdateProjectStatusRequest):
    with get_conn() as conn:
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_status = row["status"]
        if current_status == "completed":
            raise HTTPException(409, "Completed projects cannot change status")
        if current_status == body.status:
            return project_meta_from_row(conn, row)

        conn.execute(
            "UPDATE projects SET status = ? WHERE id = ?",
            (body.status, project_id),
        )
        if body.status in ("stopped", "paused"):
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE project_id = ? AND concluded_at IS NULL",
                (project_id,),
            )
            clear_project_reason(conn, project_id)
        if row["kind"] == "parent" and body.status == "stopped":
            conn.execute(
                "UPDATE projects SET status = 'stopped' WHERE parent_project_id = ? AND status != 'completed'",
                (project_id,),
            )
            conn.execute(
                """
                UPDATE intents SET worker = NULL
                WHERE project_id IN (SELECT id FROM projects WHERE parent_project_id = ?)
                  AND concluded_at IS NULL
                """,
                (project_id,),
            )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(conn, updated)


@router.post("/projects/{project_id}/reason/claim", response_model=ProjectMeta)
def claim_project_reason(project_id: str, body: ReasonClaimRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is not None and current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")
        if current_worker == body.worker:
            return project_meta_from_row(conn, row)

        now = utcnow()
        conn.execute(
            """
            UPDATE projects
            SET reason_worker = ?,
                reason_trigger = ?,
                reason_started_at = ?,
                reason_last_heartbeat_at = ?
            WHERE id = ?
            """,
            (body.worker, body.trigger, now, now, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(conn, updated)


@router.post("/projects/{project_id}/reason/heartbeat", response_model=ProjectMeta)
def heartbeat_project_reason(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is None:
            raise HTTPException(409, "Project reason is not currently claimed")
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")

        now = utcnow()
        conn.execute(
            "UPDATE projects SET reason_last_heartbeat_at = ? WHERE id = ?",
            (now, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(conn, updated)


@router.post("/projects/{project_id}/reason/release", response_model=ProjectMeta)
def release_project_reason(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is None:
            return project_meta_from_row(conn, row)
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")

        clear_project_reason(conn, project_id)
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(conn, updated)


@router.post("/projects/{project_id}/complete", response_model=Intent | FlagCandidate)
def complete_project(project_id: str, body: CompleteRequest, response: Response):
    with get_conn() as conn:
        row = check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)
        if row["kind"] == "parent":
            # A verified Flag submission always enters human review, regardless of
            # how the Goal was phrased (for example `flag.txt`, "read the flag", or
            # an explicit `flag{}` placeholder). Goal text still decides whether a
            # Parent that submits no evidence must be rejected as a Flag goal.
            if body.evidence is not None and body.evidence.kind == "flag":
                candidate = _stage_flag_candidate(conn, project_id, body)
                response.status_code = 202
                return candidate
            if _goal_requires_flag(conn, project_id):
                raise HTTPException(409, "Parent Flag goal requires verified completion evidence")
            if conn.execute("SELECT 1 FROM flag_candidates WHERE project_id = ? AND status = 'pending'", (project_id,)).fetchone():
                raise HTTPException(409, "Pending Flag candidates require human review before completion")
            _validate_non_flag_parent_completion(conn, project_id)
        elif FLAG_PATTERN.search(body.description):
            source = next_fact_id(conn, project_id)
            conn.execute("INSERT INTO facts(id, project_id, description) VALUES (?, ?, ?)", (source, project_id, body.description))
            report_fact_flags(conn, project_id, source, body.description, body.worker)
        return _complete_project_with_sources(conn, row, project_id, body)


@router.post(
    "/projects/{project_id}/flag-candidates/{candidate_id}/decision",
    response_model=Intent | FlagCandidate,
)
def decide_flag_candidate(
    project_id: str,
    candidate_id: str,
    body: FlagCandidateDecisionRequest,
):
    with get_conn() as conn:
        project = get_project_or_404(conn, project_id)
        if project["kind"] != "parent":
            raise HTTPException(409, "Flag candidates are only available for Parent projects")
        candidate_row = conn.execute(
            "SELECT * FROM flag_candidates WHERE project_id = ? AND id = ?",
            (project_id, candidate_id),
        ).fetchone()
        if candidate_row is None:
            raise HTTPException(404, "Flag candidate not found")

        target_status = "confirmed" if body.decision == "confirm" else "rejected"
        if candidate_row["status"] == target_status:
            return FlagCandidate(**dict(candidate_row))
        if candidate_row["status"] != "pending":
            raise HTTPException(409, f"Flag candidate is already {candidate_row['status']}")
        if project["status"] == "completed":
            raise HTTPException(409, "Completed project cannot accept another Flag decision")

        now = utcnow()
        conn.execute(
            """
            UPDATE flag_candidates
            SET status = ?, decided_at = ?, decided_by = ?, decision_note = ?
            WHERE project_id = ? AND id = ?
            """,
            (target_status, now, body.actor, body.note, project_id, candidate_id),
        )

        value = candidate_row["value"]
        source_fact_id = candidate_row["source_fact_id"]
        if body.decision == "confirm":
            add_blackboard_entry(
                conn,
                project_id,
                "flag_confirmed",
                f"Human confirmed candidate Flag: {value}",
                metadata={
                    "candidate_id": candidate_id,
                    "value": value,
                    "source_fact_id": source_fact_id,
                    "actor": body.actor,
                    "note": body.note,
                },
            )
            completion = CompleteRequest(
                **{
                    "from": [source_fact_id],
                    "description": candidate_row["description"],
                    "worker": body.actor,
                    "evidence": {
                        "kind": "flag",
                        "value": value,
                        "source": source_fact_id,
                        "verified": True,
                    },
                }
            )
            return _complete_project_with_sources(
                conn, project, project_id, completion, [source_fact_id]
            )

        add_blackboard_entry(
            conn,
            project_id,
            "flag_rejected",
            f"Human rejected candidate as Fake Flag: {value}",
            metadata={
                "candidate_id": candidate_id,
                "value": value,
                "source_fact_id": source_fact_id,
                "actor": body.actor,
                "note": body.note,
            },
        )
        hint_id = next_hint_id(conn, project_id)
        hint_content = (
            f"Human rejected {value} as a Fake Flag. Do not submit this value again; "
            "continue exploring for a different verified Flag."
        )
        if body.note:
            hint_content += f" Human note: {body.note}"
        conn.execute(
            "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
            (hint_id, project_id, hint_content, body.actor, now),
        )
        add_blackboard_entry(
            conn,
            project_id,
            "hint",
            hint_content,
            metadata={"hint_id": hint_id, "creator": body.actor, "candidate_id": candidate_id},
        )
        was_inactive = project["status"] in ("paused", "stopped")
        clear_project_reason(conn, project_id)
        conn.execute("UPDATE projects SET status = 'active' WHERE id = ?", (project_id,))
        if was_inactive:
            conn.execute(
                """
                UPDATE projects SET status = 'paused'
                WHERE parent_project_id = ? AND status = 'stopped'
                """,
                (project_id,),
            )
        updated = conn.execute(
            "SELECT * FROM flag_candidates WHERE project_id = ? AND id = ?",
            (project_id, candidate_id),
        ).fetchone()
        assert updated is not None
        return FlagCandidate(**dict(updated))


@router.post("/projects/{project_id}/reopen", response_model=ReopenResponse)
def reopen_project(project_id: str, body: ReopenRequest):
    with get_conn() as conn:
        expire_reason_leases(conn, project_id)
        check_project_completed(conn, project_id)
        completion = get_completion_intent_or_409(conn, project_id)

        source_rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (completion["id"], project_id),
        ).fetchall()
        source_ids = [row["fact_id"] for row in source_rows]
        if not source_ids:
            raise HTTPException(409, "Completion intent is missing its source facts")

        now = utcnow()
        fact_id = next_fact_id(conn, project_id)
        intent_id = next_intent_id(conn, project_id)
        description = body.description
        creator = body.creator

        conn.execute(
            "DELETE FROM intents WHERE id = ? AND project_id = ?",
            (completion["id"], project_id),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (fact_id, project_id, description),
        )
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (intent_id, project_id, fact_id, "external_feedback", creator, creator, now, now, now),
        )
        for source_id in source_ids:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (intent_id, project_id, source_id),
            )
        clear_project_reason(conn, project_id)
        conn.execute(
            "UPDATE projects SET status = 'active' WHERE id = ?",
            (project_id,),
        )
        confirmed_candidates = conn.execute(
            "SELECT id, value, source_fact_id FROM flag_candidates WHERE project_id = ? AND status = 'confirmed'",
            (project_id,),
        ).fetchall()
        if confirmed_candidates:
            conn.execute(
                """
                UPDATE flag_candidates
                SET status = 'rejected', decided_at = ?, decided_by = ?, decision_note = ?
                WHERE project_id = ? AND status = 'confirmed'
                """,
                (now, creator, "Project reopened after prior confirmation", project_id),
            )
            for candidate in confirmed_candidates:
                add_blackboard_entry(
                    conn,
                    project_id,
                    "flag_rejected",
                    f"Previously confirmed Flag invalidated by project reopen: {candidate['value']}",
                    metadata={
                        "candidate_id": candidate["id"],
                        "value": candidate["value"],
                        "source_fact_id": candidate["source_fact_id"],
                        "actor": creator,
                        "reason": "project_reopened",
                    },
                )
        updated_project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        updated_intent = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        assert updated_project is not None
        assert updated_intent is not None
        add_blackboard_entry(
            conn,
            project_id,
            "fact",
            description,
            child_project_id=project_id if updated_project["kind"] == "child" else None,
            metadata={"fact_id": fact_id, "intent_id": intent_id, "creator": creator},
        )
        return ReopenResponse(
            project=project_meta_from_row(conn, updated_project),
            fact=Fact(id=fact_id, description=description),
            intent=intent_to_model(conn, updated_intent, project_id),
        )
