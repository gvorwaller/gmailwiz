"""SQLite-backed state for gmailwiz.

Stores:
  * `senders`     — sender-level classifier cache (the hot path in Phase 1)
  * `runs`        — one row per dry-run / commit plan (created by Phase 1
                    `report` runs, used heavily by Phase 2/3)
  * `audit_log`   — one row per attempted Gmail mutation. Phase 1 writes none
                    of these; the table exists so Phase 2 doesn't need a schema
                    migration.
  * `app_state`   — generic key/value bag (e.g. `last_auth_email`).

Defaults to ``data/db/state.db`` inside the repo (gitignored). Tests pass an
explicit path so they never touch the user's real database.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from gmailwiz.categories import Category, parse_category

# Anchored to the repo root (one dir above this module). Lives inside the
# project at `data/db/state.db` so it's visible in Finder and easy to find
# with the rest of the project state. `data/` is gitignored — never committed.
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "db" / "state.db"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Spec enums (docs/2026-04-28_implementation-plan-Codex.md § Data Model).
# Encoded as CHECK constraints so a typo in a future caller can't silently
# poison the table with an out-of-vocab status that breaks Phase 2 queries.
RUN_STATUSES: tuple[str, ...] = (
    "planned",
    "committed",
    "partially_failed",
    "undone",
    "failed",
)
AUDIT_STATUSES: tuple[str, ...] = ("planned", "applied", "failed", "reverted")


def _sql_status_check(column: str, allowed: tuple[str, ...]) -> str:
    quoted = ",".join(f"'{v}'" for v in allowed)
    return f"CHECK ({column} IN ({quoted}))"


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS senders (
        email TEXT PRIMARY KEY,
        display_name TEXT,
        category TEXT NOT NULL,
        classified_at TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        model TEXT NOT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        phase TEXT NOT NULL,
        created_at TEXT NOT NULL,
        query TEXT,
        limit_count INTEGER,
        category_filter TEXT,
        dry_run INTEGER NOT NULL,
        status TEXT NOT NULL {_sql_status_check("status", RUN_STATUSES)}
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        ts TEXT NOT NULL,
        action TEXT NOT NULL,
        message_id TEXT NOT NULL,
        thread_id TEXT,
        sender_email TEXT,
        before_label_ids TEXT NOT NULL,
        after_label_ids TEXT NOT NULL,
        status TEXT NOT NULL {_sql_status_check("status", AUDIT_STATUSES)},
        error TEXT,
        FOREIGN KEY (run_id) REFERENCES runs(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_parent_dir(path: Path) -> None:
    """Create the parent dir with restrictive permissions.

    `data/db/` (for state.db) and `data/` (for token.json) hold project
    state; either module may be the first to materialize either dir, so
    both apply 0o700 best-effort. `auth._ensure_parent_dir` does the same —
    keep the policies in sync.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection to the gmailwiz SQLite database, creating it if missing.

    Schema is applied (idempotently) on every connect — cheap and avoids
    needing a separate ``init`` step.
    """
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    _ensure_parent_dir(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Self-use is mostly single-process, but a user running gmailwiz in two
    # terminals shouldn't see "database is locked" if writes briefly overlap.
    # 5s of grace is more than enough for any current operation.
    conn.execute("PRAGMA busy_timeout = 5000")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables if they don't already exist."""
    with conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)


@contextmanager
def open_db(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Context manager that opens a DB connection and closes it on exit."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# `senders` table
# ---------------------------------------------------------------------------


def upsert_sender(
    conn: sqlite3.Connection,
    *,
    email: str,
    display_name: Optional[str],
    category: Category,
    prompt_version: str,
    model: str,
    classified_at: Optional[str] = None,
) -> None:
    """Insert or update the cached classification for a sender."""
    classified_at = classified_at or _utcnow_iso()
    with conn:
        conn.execute(
            """
            INSERT INTO senders (email, display_name, category, classified_at, prompt_version, model)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                -- COALESCE preserves a previously-stored display_name when
                -- the current run sees a From: header without one. Some
                -- senders sometimes send with a display name and sometimes
                -- without; we don't want a re-classification to blank out
                -- a good name we already had.
                display_name = COALESCE(excluded.display_name, senders.display_name),
                category = excluded.category,
                classified_at = excluded.classified_at,
                prompt_version = excluded.prompt_version,
                model = excluded.model
            """,
            (
                email.lower(),
                display_name,
                category.value,
                classified_at,
                prompt_version,
                model,
            ),
        )


def get_sender(conn: sqlite3.Connection, email: str) -> Optional[dict[str, Any]]:
    """Return the cached row for a sender, or `None` if absent."""
    row = conn.execute(
        "SELECT email, display_name, category, classified_at, prompt_version, model FROM senders WHERE email = ?",
        (email.lower(),),
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["category"] = parse_category(data["category"])
    return data


def get_senders(conn: sqlite3.Connection, emails: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Bulk lookup: return ``{email_lower: row}`` for any cached emails in `emails`."""
    out: dict[str, dict[str, Any]] = {}
    for email in emails:
        row = get_sender(conn, email)
        if row is not None:
            out[email.lower()] = row
    return out


# ---------------------------------------------------------------------------
# `runs` table
# ---------------------------------------------------------------------------


def create_run(
    conn: sqlite3.Connection,
    *,
    phase: str,
    query: Optional[str] = None,
    limit_count: Optional[int] = None,
    category_filter: Optional[str] = None,
    dry_run: bool = True,
    status: str = "planned",
    run_id: Optional[str] = None,
    created_at: Optional[str] = None,
) -> str:
    """Insert a new run row and return its id."""
    rid = run_id or uuid.uuid4().hex
    created = created_at or _utcnow_iso()
    with conn:
        conn.execute(
            """
            INSERT INTO runs (id, phase, created_at, query, limit_count, category_filter, dry_run, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (rid, phase, created, query, limit_count, category_filter, 1 if dry_run else 0, status),
        )
    return rid


def get_run(conn: sqlite3.Connection, run_id: str) -> Optional[dict[str, Any]]:
    """Fetch a run row by id."""
    row = conn.execute(
        "SELECT id, phase, created_at, query, limit_count, category_filter, dry_run, status FROM runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    return dict(row) if row else None


def update_run_status(conn: sqlite3.Connection, run_id: str, status: str) -> None:
    """Update the ``status`` column on an existing run."""
    with conn:
        conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))


# ---------------------------------------------------------------------------
# `audit_log` table — Phase 2/3 will use these. Phase 1 only needs them to exist.
# ---------------------------------------------------------------------------


def append_audit_entry(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    action: str,
    message_id: str,
    before_label_ids: list[str],
    after_label_ids: list[str],
    thread_id: Optional[str] = None,
    sender_email: Optional[str] = None,
    status: str = "planned",
    error: Optional[str] = None,
    ts: Optional[str] = None,
) -> int:
    """Append a row to the audit log. Returns the new row id."""
    timestamp = ts or _utcnow_iso()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO audit_log (
                run_id, ts, action, message_id, thread_id, sender_email,
                before_label_ids, after_label_ids, status, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                timestamp,
                action,
                message_id,
                thread_id,
                sender_email,
                json.dumps(before_label_ids),
                json.dumps(after_label_ids),
                status,
                error,
            ),
        )
        return int(cur.lastrowid)


def get_audit_entries(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """Return all audit-log rows for a given run, oldest first."""
    rows = conn.execute(
        """
        SELECT id, run_id, ts, action, message_id, thread_id, sender_email,
               before_label_ids, after_label_ids, status, error
        FROM audit_log
        WHERE run_id = ?
        ORDER BY id ASC
        """,
        (run_id,),
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["before_label_ids"] = json.loads(d["before_label_ids"])
        d["after_label_ids"] = json.loads(d["after_label_ids"])
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# `app_state` table
# ---------------------------------------------------------------------------


def set_app_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Insert/replace a key/value pair in `app_state`."""
    with conn:
        conn.execute(
            """
            INSERT INTO app_state (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def get_app_state(conn: sqlite3.Connection, key: str) -> Optional[str]:
    """Fetch a value from `app_state`, or `None` if missing."""
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None
