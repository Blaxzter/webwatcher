"""SQLite persistence: last known snapshot per site plus a check history."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    key                  TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    url                  TEXT NOT NULL,
    content              TEXT,
    content_hash         TEXT,
    screenshot_path      TEXT,
    last_checked_at      TEXT,
    last_changed_at      TEXT,
    next_due_at          TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    error_notified       INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT
);

CREATE TABLE IF NOT EXISTS checks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    site_key        TEXT NOT NULL,
    checked_at      TEXT NOT NULL,
    status          TEXT NOT NULL,
    content_hash    TEXT,
    added_lines     INTEGER NOT NULL DEFAULT 0,
    removed_lines   INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER,
    http_status     INTEGER,
    error           TEXT,
    screenshot_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_checks_site_time ON checks (site_key, checked_at DESC);

-- Die gewünschte Konfiguration einer Seite, als rohes YAML/JSON-Mapping.
-- Bewusst getrennt von `sites`: dort steht der beobachtete Zustand, hier der
-- Sollzustand. Das Mapping bleibt ungeparst, damit `site_from_mapping()` die
-- einzige Validierung im Programm bleibt - egal ob es aus der config.yaml
-- oder aus dem Web-UI kommt.
CREATE TABLE IF NOT EXISTS site_configs (
    key        TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Jede Änderung an site_configs zählt hoch. Der Runner vergleicht nur diese Zahl
# und baut die SiteConfigs (inkl. Regex-Kompilierung) nur dann neu.
REVISION_KEY = "sites_revision"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # -- site state ---------------------------------------------------------

    def ensure_site(self, key: str, name: str, url: str) -> None:
        """Insert the site row on first sight; keep name/url in sync afterwards."""
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO sites (key, name, url) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET name = excluded.name, url = excluded.url
                """,
                (key, name, url),
            )

    def get_state(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM sites WHERE key = ?", (key,)).fetchone()

    def all_states(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM sites ORDER BY name").fetchall()

    def save_snapshot(
        self,
        key: str,
        content: str,
        content_hash: str,
        screenshot_path: str | None,
        changed: bool,
    ) -> None:
        now = to_iso(utcnow())
        with self.conn:
            self.conn.execute(
                """
                UPDATE sites
                   SET content = ?,
                       content_hash = ?,
                       screenshot_path = COALESCE(?, screenshot_path),
                       last_checked_at = ?,
                       last_changed_at = CASE WHEN ? THEN ? ELSE last_changed_at END,
                       consecutive_failures = 0,
                       error_notified = 0,
                       last_error = NULL
                 WHERE key = ?
                """,
                (content, content_hash, screenshot_path, now, 1 if changed else 0, now, key),
            )

    def touch_checked(self, key: str, screenshot_path: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                """
                UPDATE sites
                   SET last_checked_at = ?,
                       screenshot_path = COALESCE(?, screenshot_path),
                       consecutive_failures = 0,
                       error_notified = 0,
                       last_error = NULL
                 WHERE key = ?
                """,
                (to_iso(utcnow()), screenshot_path, key),
            )

    def record_failure(self, key: str, error: str) -> int:
        """Bump the failure counter and return its new value."""
        with self.conn:
            self.conn.execute(
                """
                UPDATE sites
                   SET consecutive_failures = consecutive_failures + 1,
                       last_checked_at = ?,
                       last_error = ?
                 WHERE key = ?
                """,
                (to_iso(utcnow()), error, key),
            )
        row = self.conn.execute(
            "SELECT consecutive_failures FROM sites WHERE key = ?", (key,)
        ).fetchone()
        return int(row["consecutive_failures"]) if row else 0

    def mark_error_notified(self, key: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE sites SET error_notified = 1 WHERE key = ?", (key,))

    def set_next_due(self, key: str, when: datetime) -> None:
        with self.conn:
            self.conn.execute("UPDATE sites SET next_due_at = ? WHERE key = ?", (to_iso(when), key))

    def reset_site(self, key: str) -> None:
        """Forget the baseline so the next check starts over."""
        with self.conn:
            self.conn.execute(
                """
                UPDATE sites
                   SET content = NULL, content_hash = NULL, last_changed_at = NULL,
                       next_due_at = NULL, consecutive_failures = 0, error_notified = 0,
                       last_error = NULL
                 WHERE key = ?
                """,
                (key,),
            )

    # -- site configuration -------------------------------------------------

    def sites_revision(self) -> int:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (REVISION_KEY,)).fetchone()
        return int(row["value"]) if row else 0

    def _bump_revision(self) -> None:
        """Caller must already hold the `with self.conn` transaction."""
        self.conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, '1')
            ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)
            """,
            (REVISION_KEY,),
        )

    def site_configs(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM site_configs ORDER BY position, key").fetchall()

    def get_site_config(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM site_configs WHERE key = ?", (key,)).fetchone()

    def next_position(self) -> int:
        row = self.conn.execute("SELECT MAX(position) AS top FROM site_configs").fetchone()
        return (row["top"] + 1) if row and row["top"] is not None else 0

    def put_site_config(
        self,
        key: str,
        data: str,
        position: int | None = None,
        actor: str | None = None,
    ) -> None:
        now = to_iso(utcnow())
        slot = self.next_position() if position is None else position
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO site_configs (key, data, position, created_at, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    data = excluded.data,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (key, data, slot, now, now, actor),
            )
            self._bump_revision()

    def delete_site_config(self, key: str) -> None:
        """Drop the config together with its state and history - no orphans."""
        with self.conn:
            self.conn.execute("DELETE FROM site_configs WHERE key = ?", (key,))
            self.conn.execute("DELETE FROM sites WHERE key = ?", (key,))
            self.conn.execute("DELETE FROM checks WHERE site_key = ?", (key,))
            self._bump_revision()

    def rename_site(self, old_key: str, new_key: str) -> None:
        """Carry state and history over to the new key, so a rename keeps the baseline."""
        with self.conn:
            self.conn.execute("UPDATE site_configs SET key = ? WHERE key = ?", (new_key, old_key))
            # Ein Zustand unter dem neuen Key kann existieren, wenn es die Seite
            # dort schon einmal gab - dann gewinnt der mitgebrachte.
            self.conn.execute("DELETE FROM sites WHERE key = ?", (new_key,))
            self.conn.execute("UPDATE sites SET key = ? WHERE key = ?", (new_key, old_key))
            self.conn.execute(
                "UPDATE checks SET site_key = ? WHERE site_key = ?", (new_key, old_key)
            )
            self._bump_revision()

    def set_positions(self, keys: list[str]) -> None:
        with self.conn:
            for index, key in enumerate(keys):
                self.conn.execute(
                    "UPDATE site_configs SET position = ? WHERE key = ?", (index, key)
                )
            self._bump_revision()

    # -- history ------------------------------------------------------------

    def record_check(
        self,
        site_key: str,
        status: str,
        content_hash: str | None = None,
        added_lines: int = 0,
        removed_lines: int = 0,
        duration_ms: int | None = None,
        http_status: int | None = None,
        error: str | None = None,
        screenshot_path: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO checks (site_key, checked_at, status, content_hash, added_lines,
                                    removed_lines, duration_ms, http_status, error, screenshot_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    site_key,
                    to_iso(utcnow()),
                    status,
                    content_hash,
                    added_lines,
                    removed_lines,
                    duration_ms,
                    http_status,
                    error,
                    screenshot_path,
                ),
            )

    def recent_checks(self, site_key: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
        if site_key:
            return self.conn.execute(
                "SELECT * FROM checks WHERE site_key = ? ORDER BY id DESC LIMIT ?",
                (site_key, limit),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM checks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def prune_history(self, days: int) -> int:
        cutoff = to_iso(utcnow() - timedelta(days=days))
        with self.conn:
            cursor = self.conn.execute("DELETE FROM checks WHERE checked_at < ?", (cutoff,))
        return cursor.rowcount or 0
