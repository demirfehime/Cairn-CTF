from __future__ import annotations

import sqlite3

from cairn.server import db


def test_legacy_database_gets_agent_project_and_orchestration_settings(tmp_path, monkeypatch) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE settings (
            intent_timeout INTEGER NOT NULL DEFAULT 15,
            reason_timeout INTEGER NOT NULL DEFAULT 15
        );
        INSERT INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 20, 25);

        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            reason_worker TEXT,
            reason_trigger TEXT,
            reason_started_at TEXT,
            reason_last_heartbeat_at TEXT
        );
        CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0);
        INSERT INTO counters (name, value) VALUES ('project', 0);
        """
    )
    conn.close()

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as migrated:
        settings_columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(settings)")
        }
        project_columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(projects)")
        }
        tables = {
            row["name"]
            for row in migrated.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        settings = migrated.execute("SELECT * FROM settings WHERE rowid = 1").fetchone()
        agent_counter = migrated.execute(
            "SELECT value FROM counters WHERE name = 'agent'"
        ).fetchone()

    assert {"agent_reassignment_cooldown", "parent_review_debounce"} <= settings_columns
    assert {"parent_agent_id", "kind", "parent_project_id"} <= project_columns
    assert {"agents", "project_agents"} <= tables
    assert settings["intent_timeout"] == 20
    assert settings["reason_timeout"] == 25
    assert settings["agent_reassignment_cooldown"] == 300
    assert settings["parent_review_debounce"] == 15
    assert agent_counter["value"] == 0
