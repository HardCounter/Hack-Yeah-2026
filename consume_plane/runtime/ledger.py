"""Per-plugin completion ledger and dead letters (docs/consumer-plane.md section 7.3)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS plugin_runs (
    event_id TEXT NOT NULL,
    plugin TEXT NOT NULL,
    plugin_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('done', 'failed', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    duration_ms REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (event_id, plugin, plugin_version)
);
CREATE TABLE IF NOT EXISTS dead_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    plugin TEXT NOT NULL,
    plugin_version TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);
"""
MAX_ERROR_CHARS = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def state(self, event_id: str, plugin: str, version: str) -> tuple[str, int] | None:
        row = self._db.execute(
            "SELECT state, attempts FROM plugin_runs WHERE event_id=? AND plugin=? AND plugin_version=?",
            (event_id, plugin, version)).fetchone()
        return (row[0], row[1]) if row else None

    def is_settled(self, event_id: str, plugin: str, version: str) -> bool:
        s = self.state(event_id, plugin, version)
        return s is not None and s[0] in ("done", "dead")

    def mark_done(self, event_id: str, plugin: str, version: str, duration_ms: float) -> None:
        with self._db:
            self._db.execute(
                """INSERT INTO plugin_runs VALUES (?, ?, ?, 'done', 1, NULL, ?, ?)
                   ON CONFLICT(event_id, plugin, plugin_version) DO UPDATE SET state='done', attempts=attempts+1,
                       duration_ms=excluded.duration_ms, updated_at=excluded.updated_at""",
                (event_id, plugin, version, duration_ms, _now()))

    def record_failure(self, event_id: str, plugin: str, version: str, error: str, duration_ms: float) -> int:
        """Store a failed attempt and return the attempt count so far."""
        with self._db:
            self._db.execute(
                """INSERT INTO plugin_runs VALUES (?, ?, ?, 'failed', 1, ?, ?, ?)
                   ON CONFLICT(event_id, plugin, plugin_version) DO UPDATE SET state='failed', attempts=attempts+1,
                       last_error=excluded.last_error, duration_ms=excluded.duration_ms,
                       updated_at=excluded.updated_at""",
                (event_id, plugin, version, error[:MAX_ERROR_CHARS], duration_ms, _now()))
        return self.state(event_id, plugin, version)[1]

    def mark_dead(self, event_id: str, session_id: str, plugin: str, version: str, attempts: int, error: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE plugin_runs SET state='dead' WHERE event_id=? AND plugin=? AND plugin_version=?",
                (event_id, plugin, version))
            self._db.execute(
                "INSERT INTO dead_letters (event_id, session_id, plugin, plugin_version, attempts, error, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, session_id, plugin, version, attempts, error[:MAX_ERROR_CHARS], _now()))

    def dead_letters(self) -> list[dict]:
        cur = self._db.execute(
            "SELECT event_id, session_id, plugin, plugin_version, attempts, error FROM dead_letters ORDER BY id")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur]
