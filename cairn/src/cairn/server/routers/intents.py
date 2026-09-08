from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.models import (
    ConcludeRequest,
    ConcludeResponse,
    CreateIntentRequest,
    Fact,
    HeartbeatRequest,
    Intent,
)
from cairn.server.services import (
    add_blackboard_entry,
    add_blackboard_entry_once,
    check_project_active,
    get_claimable_open_intent_or_404,
    get_releasable_open_intent_or_404,
    intent_to_model,
    next_fact_id,
    next_intent_id,
    utcnow,
    validate_facts_exist,
    validate_intent_creator_worker,
    validate_project_fact_scope,
    validate_goal_not_in_sources,
)

router = APIRouter(tags=["intents"])


@router.post(
    "/projects/{project_id}/intents",
    response_model=Intent,
    status_code=201,
)
def create_intent(project_id: str, body: CreateIntentRequest):
    with get_conn() as conn:
        project = check_project_active(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)
        validate_intent_creator_worker(body.creator, body.worker)

        now = utcnow()
        iid = next_intent_id(conn, project_id)
        claimed = body.worker is not None
        conn.execute(
            """
            INSERT INTO intents
                (id, project_id, to_fact_id, description, creator, worker,
                 last_heartbeat_at, created_at, concluded_at,
                 child_title, child_origin, child_goal, priority)
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                iid,
                project_id,
                body.description,
                body.creator,
                body.worker,
                now if claimed else None,
                now,
                body.child_title,
                body.child_origin,
                body.child_goal,
                body.priority,
            ),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, fid),
            )

        add_blackboard_entry(
            conn,
            project_id,
            "intent_created",
            body.description,
            child_project_id=project_id if project["kind"] == "child" else None,
            metadata={
                "intent_id": iid,
                "from": body.from_,
                "creator": body.creator,
                "worker": body.worker,
            },
        )

        return Intent(
            id=iid,
            **{"from": body.from_},
            to=None,
            description=body.description,
            creator=body.creator,
            worker=body.worker,
            last_heartbeat_at=now if claimed else None,
            created_at=now,
            concluded_at=None,
            child_title=body.child_title,
            child_origin=body.child_origin,
            child_goal=body.child_goal,
            priority=body.priority,
        )


@router.post(
    "/projects/{project_id}/intents/{intent_id}/heartbeat",
    response_model=Intent,
)
def heartbeat(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        project = check_project_active(conn, project_id)
        intent = get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        now = utcnow()
        conn.execute(
            "UPDATE intents SET worker = ?, last_heartbeat_at = ? WHERE id = ? AND project_id = ?",
            (body.worker, now, intent_id, project_id),
        )

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        if intent["worker"] != body.worker:
            add_blackboard_entry_once(
                conn,
                project_id,
                "intent_claimed",
                intent["description"],
                child_project_id=project_id if project["kind"] == "child" else None,
                metadata={"intent_id": intent_id, "worker": body.worker},
                unique_by=("intent_id", "worker"),
            )
        return intent_to_model(conn, updated, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/release",
    response_model=Intent,
)
def release(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        project = check_project_active(conn, project_id)
        row = get_releasable_open_intent_or_404(conn, project_id, intent_id, body.worker)

        if row["worker"] == body.worker:
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE id = ? AND project_id = ?",
                (intent_id, project_id),
            )
            row = conn.execute(
                "SELECT * FROM intents WHERE id = ? AND project_id = ?",
                (intent_id, project_id),
            ).fetchone()
            add_blackboard_entry(
                conn,
                project_id,
                "intent_released",
                row["description"],
                child_project_id=project_id if project["kind"] == "child" else None,
                metadata={"intent_id": intent_id, "worker": body.worker},
            )

        return intent_to_model(conn, row, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/conclude",
    response_model=ConcludeResponse,
)
def conclude(project_id: str, intent_id: str, body: ConcludeRequest):
    with get_conn() as conn:
        project = check_project_active(conn, project_id)
        get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)
        validate_project_fact_scope(conn, project_id, body.description)

        now = utcnow()
        fid = next_fact_id(conn, project_id)

        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (fid, project_id, body.description),
        )
        conn.execute(
            "UPDATE intents SET to_fact_id = ?, worker = ?, last_heartbeat_at = ?, concluded_at = ? WHERE id = ? AND project_id = ?",
            (fid, body.worker, now, now, intent_id, project_id),
        )

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()

        board_child_id = project_id if project["kind"] == "child" else None
        add_blackboard_entry(
            conn,
            project_id,
            "fact",
            body.description,
            child_project_id=board_child_id,
            metadata={"fact_id": fid, "intent_id": intent_id, "worker": body.worker},
        )
        add_blackboard_entry(
            conn,
            project_id,
            "intent_concluded",
            updated["description"],
            child_project_id=board_child_id,
            metadata={"intent_id": intent_id, "fact_id": fid, "worker": body.worker},
        )

        from cairn.server.routers.projects import report_fact_flags
        report_fact_flags(conn, project_id, fid, body.description, body.worker)

        return ConcludeResponse(
            fact=Fact(id=fid, description=body.description),
            intent=intent_to_model(conn, updated, project_id),
        )
