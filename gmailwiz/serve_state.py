"""Jobs table for the M2 trigger service.

Lives in a separate SQLite database (``data/db/trigger_jobs.db``) so the
gmailwiz triage state DB (``state.db``) stays focused on triage data. Each
``POST /run`` creates one row; the worker thread updates it as it
progresses.

States
------
``queued``        accepted, not yet picked up by the worker thread.
``running``       worker thread has started ``oneshot.run_one_pass``.
``done``          one-pass completed (status may still be partial / failure
                  per ``OneShotResult.status``; that's preserved in
                  ``result_json``).
``failed``        worker thread raised an unhandled exception.
``auth_required`` headless auth could not produce valid credentials. M4
                  re-auth + scp + retry is the documented recovery path.

The "in-flight" predicate used by the concurrency lock is "any row with
state in ('queued', 'running')". The lock is in-process; the jobs table
makes it observable across restarts (a crash mid-run leaves a stale
``running`` row that ``/jobs/{id}`` will still show, and the worker
should mark it ``failed`` on next startup — TODO when we add a janitor).
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

# Default path — co-located with gmailwiz's state.db so the same backup
# script catches both. Tests override via the explicit ``db_path`` arg.
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOBS_DB_PATH = _REPO_ROOT / "data" / "db" / "trigger_jobs.db"


JOB_STATES = ("queued", "running", "done", "failed", "auth_required")
IN_FLIGHT_STATES = ("queued", "running")


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    limit_count INTEGER,
    result_json TEXT,
    error       TEXT,
    CHECK (state IN ('queued','running','done','failed','auth_required'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at);
"""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection to the jobs DB, creating + migrating it as needed.

    Always opens a fresh connection — caller is responsible for closing.
    ``sqlite3`` connections are thread-affine so a worker thread must
    open its own (this is the documented contract between ``serve.py``
    and its background workers).
    """
    path = Path(db_path) if db_path is not None else DEFAULT_JOBS_DB_PATH
    _ensure_parent_dir(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    with conn:
        for stmt in SCHEMA.strip().split(";"):
            s = stmt.strip()
            if s:
                conn.execute(s)
    return conn


@contextmanager
def open_jobs_db(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def find_in_flight(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """Return the in-flight job row if any, else None.

    "In flight" means state ∈ {queued, running}. There should be at most
    one such row at any time — the API handler's in-process lock + this
    check together enforce single-job-at-a-time.
    """
    row = conn.execute(
        "SELECT * FROM jobs WHERE state IN ('queued','running') "
        "ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    return row


def create_queued_job(
    conn: sqlite3.Connection,
    *,
    limit_count: Optional[int] = None,
) -> str:
    """Insert a new queued job and return its id."""
    job_id = uuid.uuid4().hex
    with conn:
        conn.execute(
            "INSERT INTO jobs (id, state, created_at, limit_count) "
            "VALUES (?, 'queued', ?, ?)",
            (job_id, _now(), limit_count),
        )
    return job_id


def mark_running(conn: sqlite3.Connection, job_id: str) -> None:
    with conn:
        conn.execute(
            "UPDATE jobs SET state='running', started_at=? "
            "WHERE id=? AND state='queued'",
            (_now(), job_id),
        )


def mark_done(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    result: dict,
) -> None:
    with conn:
        conn.execute(
            "UPDATE jobs SET state='done', finished_at=?, result_json=?, error=NULL "
            "WHERE id=?",
            (_now(), json.dumps(result, default=str), job_id),
        )


def mark_failed(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    error: str,
    result: Optional[dict] = None,
) -> None:
    with conn:
        conn.execute(
            "UPDATE jobs SET state='failed', finished_at=?, error=?, result_json=? "
            "WHERE id=?",
            (
                _now(),
                error,
                json.dumps(result, default=str) if result is not None else None,
                job_id,
            ),
        )


def mark_auth_required(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    error: str,
) -> None:
    with conn:
        conn.execute(
            "UPDATE jobs SET state='auth_required', finished_at=?, error=? "
            "WHERE id=?",
            (_now(), error, job_id),
        )


def get_job(conn: sqlite3.Connection, job_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def row_to_dict(row: sqlite3.Row) -> dict:
    """Serialise a job row to a dict (decoding result_json)."""
    out = dict(row)
    raw = out.pop("result_json", None)
    if raw:
        try:
            out["result"] = json.loads(raw)
        except json.JSONDecodeError:
            out["result"] = None
            out["result_decode_error"] = True
    else:
        out["result"] = None
    return out
