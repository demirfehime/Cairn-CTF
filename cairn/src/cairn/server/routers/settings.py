from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.models import Settings

router = APIRouter(tags=["settings"])


@router.get("/settings", response_model=Settings)
def get_settings():
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM settings WHERE rowid = 1").fetchone()
        return Settings(**dict(row))


@router.put("/settings", response_model=Settings)
def update_settings(body: Settings):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE settings
            SET intent_timeout = ?, reason_timeout = ?,
                agent_reassignment_cooldown = ?, parent_review_debounce = ?
            WHERE rowid = 1
            """,
            (
                body.intent_timeout,
                body.reason_timeout,
                body.agent_reassignment_cooldown,
                body.parent_review_debounce,
            ),
        )
        return body
