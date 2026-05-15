"""FastAPI trigger service for unattended one-pass runs.

Endpoints
---------

``GET /health``  Liveness only. 200 if the process is up. Never depends
                  on token/api-key state — used by Cloudflare Tunnel +
                  monitoring; flapping on real-run readiness would defeat
                  its purpose.

``GET /ready``   Preflight check. 200 with a JSON breakdown when
                  credentials.json + data/token.json + headless auth +
                  ANTHROPIC_API_KEY are all good. 503 with the failing
                  checks listed otherwise. Surfaces "re-auth on M4"
                  *before* the trigger fires a doomed run.

``POST /run``    Bearer-only auth. Optional ``{"limit": int}`` body.
                  Spawns one-pass in a background thread. 202 with
                  ``{"job_id": "<uuid>", "status": "queued"}``. 409
                  Conflict (not 429 — see plan revisions) when another
                  run is already in flight, with the in-flight job_id.

``GET /jobs/{job_id}``  Job state lookup. Returns the row from
                  trigger_jobs.db plus the parsed OneShotResult JSON.

Threading
---------
``sqlite3`` connections are thread-affine. The request handler MUST NOT
share its connection with the worker. The worker opens its OWN
``gmailwiz`` state.db and trigger_jobs.db connections inside the thread.
The request handler only touches the jobs DB to insert the queued row
and to read in-flight state; it closes its connection before the worker
starts.

Configuration (env vars; ``.env`` on M2, not committed)
-------------------------------------------------------
``GMAILWIZ_TRIGGER_TOKEN``  required — Bearer shared secret.
``TELEGRAM_BOT_TOKEN``       optional — completion notifications.
``TELEGRAM_CHAT_ID``         optional — completion notifications.
``ANTHROPIC_API_KEY``        required (checked at /ready time).
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from gmailwiz import auth as gw_auth
from gmailwiz import db as gw_db
from gmailwiz import oneshot as gw_oneshot
from gmailwiz import serve_state
from gmailwiz import telegram as gw_telegram


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


TRIGGER_TOKEN_ENV = "GMAILWIZ_TRIGGER_TOKEN"
DEFAULT_RUN_LIMIT = 1000
MAX_RUN_LIMIT = 5000


def _get_trigger_token() -> Optional[str]:
    """Read the token at request time, not at import time.

    Tests set the env var per-test; importing this module before they
    monkeypatch must not freeze a None.
    """
    tok = os.environ.get(TRIGGER_TOKEN_ENV, "").strip()
    return tok or None


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    limit: int = Field(default=DEFAULT_RUN_LIMIT, ge=1, le=MAX_RUN_LIMIT)
    archive: bool = True


class JobCreated(BaseModel):
    job_id: str
    status: str  # "queued"


class JobConflict(BaseModel):
    error: str
    in_flight_job_id: str
    in_flight_state: str


# ---------------------------------------------------------------------------
# App + concurrency lock
# ---------------------------------------------------------------------------


# The lock protects the "check in-flight → insert queued → start worker"
# critical section so two concurrent /run requests can't both win the
# race. It does NOT span the full run — the worker thread releases it
# implicitly by virtue of being spawned outside the lock.
_DISPATCH_LOCK = threading.Lock()


def create_app(
    *,
    jobs_db_path: Optional[Path] = None,
    state_db_path: Optional[Path] = None,
    runner: Optional[Any] = None,
) -> FastAPI:
    """Construct a fresh FastAPI app.

    Parameters allow the test suite to inject a tmp jobs DB, a tmp
    state DB, and an alternate runner (so tests can drive the worker
    synchronously and don't need to mock Gmail or Anthropic).

    ``runner`` is a callable with the signature
    ``(creds, state_db_path, limit, archive, on_progress) -> OneShotResult``;
    defaults to ``oneshot.run_one_pass``.
    """
    if runner is None:
        runner = gw_oneshot.run_one_pass

    app = FastAPI(
        title="gmailwiz trigger",
        version="0.2.0",
        # No /docs in prod since the OpenAPI listing leaks endpoint shape;
        # leave the inspection endpoints enabled for now (Bearer is still
        # required for /run). Toggle if/when this hostname is exposed
        # without Cloudflare Access.
    )

    # ---- /health ------------------------------------------------------
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok"}

    # ---- /ready -------------------------------------------------------
    @app.get("/ready")
    def ready() -> JSONResponse:
        checks: dict[str, Any] = {}
        ok = True

        creds_path = gw_auth.DEFAULT_CREDENTIALS_PATH
        checks["credentials_json"] = {
            "ok": creds_path.exists(),
            "path": str(creds_path),
        }
        if not creds_path.exists():
            ok = False

        token_path = gw_auth.DEFAULT_TOKEN_PATH
        checks["token_json"] = {
            "ok": token_path.exists(),
            "path": str(token_path),
        }
        if not token_path.exists():
            ok = False

        if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
            checks["anthropic_api_key"] = {"ok": False, "reason": "env var not set"}
            ok = False
        else:
            checks["anthropic_api_key"] = {"ok": True}

        if not _get_trigger_token():
            checks["trigger_token"] = {"ok": False, "reason": f"{TRIGGER_TOKEN_ENV} not set"}
            ok = False
        else:
            checks["trigger_token"] = {"ok": True}

        # Headless auth probe: try to load + (if expired) refresh the token
        # WITHOUT launching the browser flow. Catches the common "M4 didn't
        # scp the latest token" failure mode before Drafts triggers a run.
        if token_path.exists():
            try:
                gw_auth.get_credentials(
                    token_path=token_path, interactive=False
                )
                checks["headless_auth"] = {"ok": True}
            except gw_auth.AuthRequired as exc:
                checks["headless_auth"] = {
                    "ok": False,
                    "reason": exc.reason,
                }
                ok = False
            except Exception as exc:  # noqa: BLE001
                checks["headless_auth"] = {
                    "ok": False,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
                ok = False
        else:
            checks["headless_auth"] = {
                "ok": False,
                "reason": "token.json missing — see token_json check",
            }

        # In-flight job state — informational; doesn't fail readiness.
        try:
            with serve_state.open_jobs_db(jobs_db_path) as conn:
                in_flight = serve_state.find_in_flight(conn)
                checks["in_flight"] = (
                    {"job_id": in_flight["id"], "state": in_flight["state"]}
                    if in_flight
                    else None
                )
        except Exception as exc:  # noqa: BLE001
            checks["in_flight"] = {"error": f"{type(exc).__name__}: {exc}"}

        return JSONResponse(
            status_code=200 if ok else 503,
            content={"ready": ok, "checks": checks},
        )

    # ---- /run ---------------------------------------------------------
    @app.post("/run")
    def run(
        request: Request,
        authorization: Optional[str] = Header(default=None),
        body: Optional[RunRequest] = Body(default=None),
    ) -> JSONResponse:
        expected = _get_trigger_token()
        if not expected:
            # The server is misconfigured; fail loudly rather than letting
            # any caller through. 503 because this is a server-side issue.
            raise HTTPException(
                status_code=503,
                detail=f"{TRIGGER_TOKEN_ENV} not configured on the server",
            )

        # Header-only auth. We deliberately do NOT check ?token= — the plan
        # forbids query-string tokens (they leak to logs/history). A caller
        # using ?token= will simply get 401 here.
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing Bearer token")
        provided = authorization.removeprefix("Bearer ").strip()
        if provided != expected:
            raise HTTPException(status_code=401, detail="invalid Bearer token")

        # Defense-in-depth: reject any callers passing ?token= even with a
        # valid Bearer (catches an honest-mistake client that thinks query
        # tokens "also work").
        if "token" in request.query_params:
            raise HTTPException(
                status_code=400,
                detail="query-string token is not accepted; use Authorization: Bearer",
            )

        params = body or RunRequest()

        # Critical section: check-and-insert under the in-process lock so
        # two concurrent /run requests can't both pass the in-flight check.
        with _DISPATCH_LOCK:
            with serve_state.open_jobs_db(jobs_db_path) as conn:
                in_flight = serve_state.find_in_flight(conn)
                if in_flight is not None:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "error": "another one-pass is in flight",
                            "in_flight_job_id": in_flight["id"],
                            "in_flight_state": in_flight["state"],
                        },
                    )
                job_id = serve_state.create_queued_job(conn, limit_count=params.limit)

            # Spawn worker OUTSIDE the dispatch lock so a slow run doesn't
            # block subsequent /run-rejects from returning quickly.
            t = threading.Thread(
                target=_worker_entrypoint,
                args=(job_id, params, runner, jobs_db_path, state_db_path),
                daemon=True,
                name=f"gmailwiz-worker-{job_id[:8]}",
            )
            t.start()

        return JSONResponse(
            status_code=202,
            content={"job_id": job_id, "status": "queued"},
        )

    # ---- /jobs/{job_id} ----------------------------------------------
    @app.get("/jobs/{job_id}")
    def get_job(job_id: str) -> JSONResponse:
        with serve_state.open_jobs_db(jobs_db_path) as conn:
            row = serve_state.get_job(conn, job_id)
            if row is None:
                raise HTTPException(status_code=404, detail="job not found")
            return JSONResponse(status_code=200, content=serve_state.row_to_dict(row))

    return app


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _worker_entrypoint(
    job_id: str,
    params: RunRequest,
    runner: Any,
    jobs_db_path: Optional[Path],
    state_db_path: Optional[Path],
) -> None:
    """Worker-thread top: opens its own connections, runs one-pass, persists.

    Critically does NOT share any sqlite3.Connection with the request
    handler — both DBs are opened here, used here, closed here.
    """
    # Mark running first so /jobs/{id} shows progress.
    try:
        with serve_state.open_jobs_db(jobs_db_path) as conn:
            serve_state.mark_running(conn, job_id)
    except Exception as exc:  # noqa: BLE001
        # Can't even persist the running marker — the rest of the run
        # would be unobservable. Bail and try one last failure record.
        _persist_failure(job_id, jobs_db_path, f"could not mark running: {exc}")
        return

    # Obtain creds headlessly.
    try:
        creds = gw_auth.get_credentials(interactive=False)
    except gw_auth.AuthRequired as exc:
        msg = f"auth_required: {exc.reason}"
        try:
            with serve_state.open_jobs_db(jobs_db_path) as conn:
                serve_state.mark_auth_required(conn, job_id, error=exc.reason)
        finally:
            gw_telegram.send_message(
                f"gmailwiz {job_id[:8]}: {msg}\nRe-auth on M4 (menu 5) + scp token to M2."
            )
        return
    except Exception as exc:  # noqa: BLE001
        _persist_failure(
            job_id, jobs_db_path, f"auth error: {type(exc).__name__}: {exc}"
        )
        gw_telegram.send_message(
            f"gmailwiz {job_id[:8]}: auth error — {type(exc).__name__}: {exc}"
        )
        return

    # Run one-pass.
    try:
        result = runner(
            creds=creds,
            db_path=state_db_path,
            limit=params.limit,
            archive=params.archive,
        )
    except Exception as exc:  # noqa: BLE001
        tb = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        _persist_failure(job_id, jobs_db_path, f"run error: {tb}")
        gw_telegram.send_message(f"gmailwiz {job_id[:8]}: FAILED — {tb}")
        return

    payload = result.to_dict()
    try:
        with serve_state.open_jobs_db(jobs_db_path) as conn:
            serve_state.mark_done(conn, job_id, result=payload)
    except Exception as exc:  # noqa: BLE001
        # Worst case: the run actually succeeded against Gmail but we
        # couldn't record it. Still notify the user — the audit log in
        # state.db remains the source of truth for what was actually
        # mutated.
        sys.stderr.write(f"WARN: could not persist done state: {exc}\n")

    gw_telegram.send_message(_format_telegram_summary(job_id, payload))


def _persist_failure(
    job_id: str,
    jobs_db_path: Optional[Path],
    error: str,
) -> None:
    try:
        with serve_state.open_jobs_db(jobs_db_path) as conn:
            serve_state.mark_failed(conn, job_id, error=error)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"WARN: could not persist failure for {job_id}: {exc}\n")


def _format_telegram_summary(job_id: str, payload: dict) -> str:
    """One-line-per-category summary for the Telegram message body."""
    head = f"gmailwiz {job_id[:8]}: {payload.get('status')}"
    if payload.get("error_code"):
        head += f" ({payload['error_code']})"
    head += f" — {payload.get('snapshot_size', 0)} msg, "
    head += f"{payload.get('wall_seconds', 0):.1f}s"
    lines = [head]
    for cat in payload.get("categories") or []:
        seg = f"  {cat['category']}: label {cat['labels_applied']}"
        if cat.get("archive_applied") is not None:
            seg += f" / archive {cat['archive_applied']}"
        if cat.get("error"):
            seg += f"  ⚠ {cat['error']}"
        lines.append(seg)
    if payload.get("notes"):
        lines.append("notes:")
        for n in payload["notes"]:
            lines.append(f"  - {n}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module-level app for `uvicorn gmailwiz.serve:app`
# ---------------------------------------------------------------------------


app = create_app()
