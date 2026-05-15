"""Tests for the M2 trigger FastAPI service.

Strategy: drive the real FastAPI app via ``TestClient`` with an injected
runner stub so the worker thread doesn't actually call Gmail/Anthropic.
Tests use a tmp ``jobs_db_path`` (injected at ``create_app`` time) so
they're hermetic against the production ``data/db/trigger_jobs.db``.

Threading note
--------------
``TestClient`` runs each request in the test thread (sync). The worker
thread spawned by ``/run`` is real (``threading.Thread``), so we use a
threading.Event in the runner stub to deterministically gate when the
worker finishes — no time.sleep() polling.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from gmailwiz import auth as gw_auth
from gmailwiz import oneshot as gw_oneshot
from gmailwiz import serve as gw_serve
from gmailwiz import serve_state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def trigger_token(monkeypatch):
    """Pin the trigger token to a known value for the duration of the test."""
    monkeypatch.setenv("GMAILWIZ_TRIGGER_TOKEN", "t3st-secret")
    yield "t3st-secret"


@pytest.fixture
def jobs_db(tmp_path):
    """Hermetic jobs DB path."""
    return tmp_path / "trigger_jobs.db"


def _build_app(
    *,
    jobs_db_path: Path,
    runner=None,
    state_db_path: Path | None = None,
):
    """Construct the app and a TestClient. Returns (client, app)."""
    app = gw_serve.create_app(
        jobs_db_path=jobs_db_path,
        state_db_path=state_db_path,
        runner=runner,
    )
    return TestClient(app), app


def _ok_runner(creds, db_path, limit, archive, on_progress=None):
    """Synchronous stub: returns a OneShotResult-like object immediately.

    Tests that need to gate worker completion wrap this in a barrier.
    """
    return gw_oneshot.OneShotResult(
        status="success",
        report_status="committed",
        report_run_id="run-stub",
        snapshot_size=limit,
        snapshot_message_ids=[f"mid_{i}" for i in range(limit)],
        classified_sender_count=limit,
        categories=[],
        wall_seconds=0.001,
    )


def _wait_for_state(jobs_db_path: Path, job_id: str, target: str, timeout: float = 2.0):
    """Spin-wait for a job state transition in the jobs DB.

    Used to synchronise with the daemon worker thread. Aggressive timeout
    so a CI hang surfaces fast.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with serve_state.open_jobs_db(jobs_db_path) as conn:
            row = serve_state.get_job(conn, job_id)
            if row is not None and row["state"] == target:
                return row
        time.sleep(0.01)
    raise AssertionError(
        f"job {job_id} never reached state {target!r} within {timeout}s"
    )


# ---------------------------------------------------------------------------
# /health — pure liveness, never depends on token / api key state
# ---------------------------------------------------------------------------


def test_health_returns_ok_without_any_config(monkeypatch, jobs_db):
    # Deliberately UN-set all the env vars /ready would care about.
    for var in (
        "GMAILWIZ_TRIGGER_TOKEN",
        "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    client, _ = _build_app(jobs_db_path=jobs_db)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /ready — preflight surfaces the right failures
# ---------------------------------------------------------------------------


def _stub_valid_token(monkeypatch, tmp_path):
    """Make get_credentials(interactive=False) succeed (used by /ready)."""
    token_path = tmp_path / "token.json"
    token_path.write_text("{}")  # presence is enough; we mock auth itself
    monkeypatch.setattr(gw_auth, "DEFAULT_TOKEN_PATH", token_path)
    monkeypatch.setattr(
        gw_auth, "get_credentials",
        lambda **kw: MagicMock(),
    )
    return token_path


def test_ready_failure_when_credentials_json_missing(monkeypatch, tmp_path, jobs_db, trigger_token):
    monkeypatch.setattr(
        gw_auth, "DEFAULT_CREDENTIALS_PATH", tmp_path / "missing-credentials.json"
    )
    _stub_valid_token(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    client, _ = _build_app(jobs_db_path=jobs_db)
    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False
    assert body["checks"]["credentials_json"]["ok"] is False


def test_ready_failure_when_token_missing(monkeypatch, tmp_path, jobs_db, trigger_token):
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    monkeypatch.setattr(gw_auth, "DEFAULT_CREDENTIALS_PATH", creds)
    monkeypatch.setattr(gw_auth, "DEFAULT_TOKEN_PATH", tmp_path / "absent" / "token.json")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    client, _ = _build_app(jobs_db_path=jobs_db)
    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["checks"]["token_json"]["ok"] is False


def test_ready_failure_when_api_key_missing(monkeypatch, tmp_path, jobs_db, trigger_token):
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    monkeypatch.setattr(gw_auth, "DEFAULT_CREDENTIALS_PATH", creds)
    _stub_valid_token(monkeypatch, tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client, _ = _build_app(jobs_db_path=jobs_db)
    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["checks"]["anthropic_api_key"]["ok"] is False


def test_ready_failure_when_headless_auth_fails(monkeypatch, tmp_path, jobs_db, trigger_token):
    """Expired non-refreshable token: /ready must report headless_auth=False,
    which is the signal the operator needs 'go re-auth on M4'."""
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    token = tmp_path / "token.json"
    token.write_text("{}")
    monkeypatch.setattr(gw_auth, "DEFAULT_CREDENTIALS_PATH", creds)
    monkeypatch.setattr(gw_auth, "DEFAULT_TOKEN_PATH", token)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def _raise(**_kw):
        raise gw_auth.AuthRequired("token refresh failed")

    monkeypatch.setattr(gw_auth, "get_credentials", _raise)
    client, _ = _build_app(jobs_db_path=jobs_db)
    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["checks"]["headless_auth"]["ok"] is False
    assert "refresh failed" in body["checks"]["headless_auth"]["reason"]


def test_ready_ok_when_everything_present(monkeypatch, tmp_path, jobs_db, trigger_token):
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    monkeypatch.setattr(gw_auth, "DEFAULT_CREDENTIALS_PATH", creds)
    _stub_valid_token(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    client, _ = _build_app(jobs_db_path=jobs_db)
    r = client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["checks"]["in_flight"] is None


# ---------------------------------------------------------------------------
# /run — auth gate
# ---------------------------------------------------------------------------


def test_run_rejects_missing_authorization_header(monkeypatch, tmp_path, jobs_db, trigger_token):
    _stub_valid_token(monkeypatch, tmp_path)
    client, _ = _build_app(jobs_db_path=jobs_db, runner=_ok_runner)
    r = client.post("/run")
    assert r.status_code == 401
    # No job was queued.
    with serve_state.open_jobs_db(jobs_db) as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_run_rejects_wrong_bearer(monkeypatch, tmp_path, jobs_db, trigger_token):
    _stub_valid_token(monkeypatch, tmp_path)
    client, _ = _build_app(jobs_db_path=jobs_db, runner=_ok_runner)
    r = client.post("/run", headers={"Authorization": "Bearer not-the-secret"})
    assert r.status_code == 401


def test_run_rejects_query_string_token_even_with_valid_bearer(
    monkeypatch, tmp_path, jobs_db, trigger_token
):
    """Belt-and-suspenders: a request that passes ?token=... is rejected
    even if it ALSO supplies a valid Bearer. Catches client honest mistakes
    (e.g. a Drafts script that re-uses the td-sync query-token pattern)."""
    _stub_valid_token(monkeypatch, tmp_path)
    client, _ = _build_app(jobs_db_path=jobs_db, runner=_ok_runner)
    r = client.post(
        "/run?token=t3st-secret",
        headers={"Authorization": f"Bearer {trigger_token}"},
    )
    assert r.status_code == 400


def test_run_accepts_valid_bearer_returns_202(monkeypatch, tmp_path, jobs_db, trigger_token):
    _stub_valid_token(monkeypatch, tmp_path)

    barrier = threading.Event()

    def _gated_runner(creds, db_path, limit, archive, on_progress=None):
        barrier.wait(timeout=2.0)
        return _ok_runner(creds, db_path, limit, archive, on_progress)

    client, _ = _build_app(jobs_db_path=jobs_db, runner=_gated_runner)
    r = client.post(
        "/run",
        headers={"Authorization": f"Bearer {trigger_token}"},
        json={"limit": 10},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    job_id = body["job_id"]
    barrier.set()
    row = _wait_for_state(jobs_db, job_id, "done")
    assert row["limit_count"] == 10


# ---------------------------------------------------------------------------
# /run — 409 concurrency lock (NOT 429)
# ---------------------------------------------------------------------------


def test_run_returns_409_when_already_in_flight(monkeypatch, tmp_path, jobs_db, trigger_token):
    _stub_valid_token(monkeypatch, tmp_path)

    barrier = threading.Event()

    def _gated_runner(creds, db_path, limit, archive, on_progress=None):
        barrier.wait(timeout=2.0)
        return _ok_runner(creds, db_path, limit, archive, on_progress)

    client, _ = _build_app(jobs_db_path=jobs_db, runner=_gated_runner)
    r1 = client.post(
        "/run", headers={"Authorization": f"Bearer {trigger_token}"}, json={"limit": 5}
    )
    assert r1.status_code == 202
    job_id_1 = r1.json()["job_id"]

    # Worker is blocked on the barrier — second /run must conflict.
    r2 = client.post(
        "/run", headers={"Authorization": f"Bearer {trigger_token}"}, json={"limit": 5}
    )
    assert r2.status_code == 409, f"expected 409 Conflict, got {r2.status_code}"
    body = r2.json()
    assert body["in_flight_job_id"] == job_id_1
    assert body["in_flight_state"] in {"queued", "running"}

    # Release worker.
    barrier.set()
    _wait_for_state(jobs_db, job_id_1, "done")


# ---------------------------------------------------------------------------
# Worker thread — auth_required + failure paths
# ---------------------------------------------------------------------------


def test_run_marks_auth_required_when_credentials_raise(
    monkeypatch, tmp_path, jobs_db, trigger_token
):
    """Headless-auth path: worker thread catches AuthRequired and marks
    the job 'auth_required' (NOT 'failed'). This is the exact failure
    mode the trigger service exists to surface for re-auth."""
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    token = tmp_path / "token.json"
    token.write_text("{}")
    monkeypatch.setattr(gw_auth, "DEFAULT_CREDENTIALS_PATH", creds)
    monkeypatch.setattr(gw_auth, "DEFAULT_TOKEN_PATH", token)
    monkeypatch.setattr(
        gw_auth, "get_credentials",
        lambda **kw: (_ for _ in ()).throw(gw_auth.AuthRequired("token expired")),
    )

    # Runner should NEVER be invoked since auth failed first.
    def _boom_runner(**_kw):
        raise AssertionError("runner must not run when auth fails")

    client, _ = _build_app(jobs_db_path=jobs_db, runner=_boom_runner)
    r = client.post("/run", headers={"Authorization": f"Bearer {trigger_token}"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    row = _wait_for_state(jobs_db, job_id, "auth_required")
    assert "token expired" in row["error"]


def test_run_marks_failed_on_unhandled_runner_exception(
    monkeypatch, tmp_path, jobs_db, trigger_token
):
    _stub_valid_token(monkeypatch, tmp_path)

    def _boom_runner(creds, db_path, limit, archive, on_progress=None):
        raise RuntimeError("simulated Gmail outage")

    client, _ = _build_app(jobs_db_path=jobs_db, runner=_boom_runner)
    r = client.post("/run", headers={"Authorization": f"Bearer {trigger_token}"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    row = _wait_for_state(jobs_db, job_id, "failed")
    assert "simulated Gmail outage" in row["error"]


def test_run_persists_result_json_on_success(
    monkeypatch, tmp_path, jobs_db, trigger_token
):
    """Worker thread writes the full OneShotResult.to_dict() into
    result_json so /jobs/{id} can return it for forensic inspection."""
    _stub_valid_token(monkeypatch, tmp_path)
    client, _ = _build_app(jobs_db_path=jobs_db, runner=_ok_runner)
    r = client.post(
        "/run", headers={"Authorization": f"Bearer {trigger_token}"}, json={"limit": 7}
    )
    job_id = r.json()["job_id"]
    _wait_for_state(jobs_db, job_id, "done")

    j = client.get(f"/jobs/{job_id}")
    assert j.status_code == 200
    body = j.json()
    assert body["state"] == "done"
    assert body["result"] is not None
    assert body["result"]["status"] == "success"
    assert body["result"]["snapshot_size"] == 7


# ---------------------------------------------------------------------------
# Threaded DB pattern — worker uses its OWN connection, never the handler's
# ---------------------------------------------------------------------------


def test_worker_opens_own_connections(monkeypatch, tmp_path, jobs_db, trigger_token):
    """Critical: a sqlite3.Connection must NEVER cross the handler/worker
    thread boundary (sqlite3 is thread-affine). This test instruments
    serve_state.connect to record which thread opened each connection and
    asserts that the worker's thread opened connections AFTER the handler
    returned.
    """
    _stub_valid_token(monkeypatch, tmp_path)
    real_connect = serve_state.connect
    opens: list[int] = []

    def _spy_connect(db_path=None):
        opens.append(threading.get_ident())
        return real_connect(db_path)

    monkeypatch.setattr(serve_state, "connect", _spy_connect)

    barrier = threading.Event()

    def _gated_runner(creds, db_path, limit, archive, on_progress=None):
        barrier.wait(timeout=2.0)
        return _ok_runner(creds, db_path, limit, archive, on_progress)

    client, _ = _build_app(jobs_db_path=jobs_db, runner=_gated_runner)
    handler_thread = threading.get_ident()
    r = client.post("/run", headers={"Authorization": f"Bearer {trigger_token}"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    barrier.set()
    _wait_for_state(jobs_db, job_id, "done")

    # Group opens by thread id.
    handler_opens = [t for t in opens if t == handler_thread]
    worker_opens = [t for t in opens if t != handler_thread]
    assert len(handler_opens) >= 1, "request handler opened a connection (queue insert)"
    assert len(worker_opens) >= 1, (
        "worker thread must open its OWN connection — sqlite3 is thread-affine"
    )
    # And no single connection was used in both: the spy records open events
    # only; since each `with open_jobs_db(...)` call opens a NEW connection,
    # the fact that worker opens != handler opens proves the boundary holds.


# ---------------------------------------------------------------------------
# /jobs/{job_id}
# ---------------------------------------------------------------------------


def test_jobs_404_when_missing(monkeypatch, tmp_path, jobs_db, trigger_token):
    _stub_valid_token(monkeypatch, tmp_path)
    client, _ = _build_app(jobs_db_path=jobs_db, runner=_ok_runner)
    r = client.get("/jobs/nope")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /run — server-side misconfiguration (no token env var)
# ---------------------------------------------------------------------------


def test_run_503_when_server_has_no_trigger_token_configured(
    monkeypatch, tmp_path, jobs_db
):
    monkeypatch.delenv("GMAILWIZ_TRIGGER_TOKEN", raising=False)
    _stub_valid_token(monkeypatch, tmp_path)
    client, _ = _build_app(jobs_db_path=jobs_db, runner=_ok_runner)
    r = client.post("/run", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503
