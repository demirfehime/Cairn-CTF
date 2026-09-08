from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

DEFAULT_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"

_db_path: Path | None = None

SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15,
    agent_reassignment_cooldown INTEGER NOT NULL DEFAULT 300,
    parent_review_debounce INTEGER NOT NULL DEFAULT 15
);

INSERT OR IGNORE INTO settings
    (rowid, intent_timeout, reason_timeout, agent_reassignment_cooldown, parent_review_debounce)
VALUES (1, 15, 15, 300, 15);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT,
    kind TEXT NOT NULL DEFAULT 'parent',
    parent_project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    parent_intent_id TEXT,
    parent_agent_id TEXT REFERENCES agents(id) ON DELETE RESTRICT,
    priority INTEGER NOT NULL DEFAULT 0,
    summary TEXT,
    breakthrough INTEGER NOT NULL DEFAULT 0,
    parent_reviewed_fact_count INTEGER NOT NULL DEFAULT 0,
    process_status TEXT NOT NULL DEFAULT 'stopped',
    process_pid INTEGER,
    process_log_dir TEXT,
    process_error TEXT,
    process_updated_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    child_title TEXT,
    child_origin TEXT,
    child_goal TEXT,
    priority INTEGER NOT NULL DEFAULT 100,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);
INSERT OR IGNORE INTO counters (name, value) VALUES ('agent', 0);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    base_url TEXT NOT NULL,
    api_key TEXT NOT NULL,
    api_format TEXT NOT NULL DEFAULT 'auto',
    skill_paths TEXT NOT NULL DEFAULT '[]',
    mcp_servers TEXT NOT NULL DEFAULT '{}',
    model TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    task_types TEXT NOT NULL DEFAULT '["bootstrap","reason","explore"]',
    max_running INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 0,
    health_status TEXT NOT NULL DEFAULT 'unknown',
    health_detail TEXT,
    health_checked_at TEXT,
    health_failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_agents (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE RESTRICT,
    use_hints INTEGER NOT NULL DEFAULT 1,
    role TEXT NOT NULL DEFAULT 'participant',
    assignment_epoch INTEGER NOT NULL DEFAULT 0,
    assigned_at TEXT,
    PRIMARY KEY (project_id, agent_id)
);

CREATE TABLE IF NOT EXISTS agent_assignment_counters (
    parent_project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    value INTEGER NOT NULL DEFAULT 0,
    last_moved_at TEXT,
    PRIMARY KEY (parent_project_id, agent_id)
);

CREATE TABLE IF NOT EXISTS blackboard (
    id TEXT NOT NULL,
    parent_project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    child_project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, parent_project_id)
);

CREATE TABLE IF NOT EXISTS blackboard_counters (
    parent_project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    value INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS child_fact_publications (
    child_project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    fact_id TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (child_project_id, fact_id)
);

CREATE TABLE IF NOT EXISTS flag_candidates (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    value TEXT NOT NULL,
    source_fact_id TEXT NOT NULL,
    reported_source TEXT NOT NULL,
    description TEXT NOT NULL,
    worker TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision_note TEXT,
    PRIMARY KEY (id, project_id),
    UNIQUE (project_id, value)
);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);
"""


def configure(path: Path) -> None:
    global _db_path
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        # Older databases already have a settings table without the orchestration
        # tuning columns.  Add those columns before SCHEMA's default-row INSERT,
        # otherwise SQLite rejects the script before the normal migration runs.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                intent_timeout INTEGER NOT NULL DEFAULT 15,
                reason_timeout INTEGER NOT NULL DEFAULT 15
            )
            """
        )
        _ensure_settings_columns(conn)
        conn.executescript(SCHEMA)
        from cairn.server.routers.attachments import SCHEMA as ATTACHMENT_SCHEMA
        conn.executescript(ATTACHMENT_SCHEMA)
        _ensure_project_columns(conn)
        _ensure_orchestration_columns(conn)
        from cairn.extensions.library import initialize
        initialize(conn)


def configured_path() -> Path:
    assert _db_path is not None
    return _db_path


def _ensure_project_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    if "bootstrap_enabled" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN bootstrap_enabled INTEGER NOT NULL DEFAULT 1")
        if "bootstrap_mode" in columns:
            conn.execute(
                "UPDATE projects SET bootstrap_enabled = CASE WHEN bootstrap_mode = 'disabled' THEN 0 ELSE 1 END"
            )


def _add_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> bool:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name in columns:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    return True


def _ensure_settings_columns(conn: sqlite3.Connection) -> None:
    _add_column(conn, "settings", "agent_reassignment_cooldown", "INTEGER NOT NULL DEFAULT 300")
    _add_column(conn, "settings", "parent_review_debounce", "INTEGER NOT NULL DEFAULT 15")


def _ensure_orchestration_columns(conn: sqlite3.Connection) -> None:
    _add_column(conn, "projects", "kind", "TEXT NOT NULL DEFAULT 'parent'")
    _add_column(conn, "projects", "parent_project_id", "TEXT")
    _add_column(conn, "projects", "parent_intent_id", "TEXT")
    _add_column(conn, "projects", "parent_agent_id", "TEXT")
    _add_column(conn, "projects", "priority", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "projects", "summary", "TEXT")
    _add_column(conn, "projects", "breakthrough", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "projects", "parent_reviewed_fact_count", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "projects", "process_status", "TEXT NOT NULL DEFAULT 'stopped'")
    _add_column(conn, "projects", "process_pid", "INTEGER")
    _add_column(conn, "projects", "process_log_dir", "TEXT")
    _add_column(conn, "projects", "process_error", "TEXT")
    _add_column(conn, "projects", "process_updated_at", "TEXT")
    _add_column(conn, "project_agents", "role", "TEXT NOT NULL DEFAULT 'participant'")
    _add_column(conn, "project_agents", "assignment_epoch", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "project_agents", "assigned_at", "TEXT")
    _add_column(conn, "intents", "child_title", "TEXT")
    _add_column(conn, "intents", "child_origin", "TEXT")
    _add_column(conn, "intents", "child_goal", "TEXT")
    _add_column(conn, "intents", "priority", "INTEGER NOT NULL DEFAULT 100")
    _add_column(conn, "agents", "health_status", "TEXT NOT NULL DEFAULT 'unknown'")
    _add_column(conn, "agents", "health_detail", "TEXT")
    _add_column(conn, "agents", "health_checked_at", "TEXT")
    _add_column(conn, "agents", "health_failures", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "agents", "api_format", "TEXT NOT NULL DEFAULT 'auto'")
    _add_column(conn, "agents", "skill_paths", "TEXT NOT NULL DEFAULT '[]'")
    _add_column(conn, "agents", "mcp_servers", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_settings_columns(conn)

    # Preserve legacy graphs and their evidence. Schema upgrades must never
    # silently delete projects, facts, intents, or human review history.


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
