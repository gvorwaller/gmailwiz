"""End-to-end "one-pass" orchestration: classify → label all → archive all.

Driven by:
  * the ``run`` CLI subcommand (``python -m gmailwiz run``)
  * the interactive menu's option #8 ("Run full cycle")
  * Phase 2's FastAPI trigger service on M2 (Drafts → /run)

All three call ``run_one_pass`` with the same contract. The orchestrator is
deliberately decoupled from auth and from any open DB connection so it can
run inside a worker thread that owns its own ``sqlite3.Connection``
(``sqlite3`` connections are thread-affine).

Snapshot invariant
------------------
``run_one_pass`` fetches the inbox **once** and feeds the same set of
message metadata into every category's plan. Without this, the planning
helpers would each refetch ``list_unread_message_ids`` between phases —
once category 1 had been labeled and archived, category 2 would see a
different inbox under the same ``--limit``. This module's contract with
``planning.build_label_plan`` / ``planning.build_archive_plan`` is the
``messages=`` snapshot kwarg they accept.

Exit-code mapping (used by the CLI subcommand):
  * ``status="success"``      → 0
  * ``status="auth_required"`` → 2
  * ``status="partial_failure"`` → 4
  * ``status="failure"``      → 5
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from google.oauth2.credentials import Credentials

from gmailwiz import auth as gw_auth
from gmailwiz import db as gw_db
from gmailwiz import gmail_client, planning
from gmailwiz.categories import CLASSIFIABLE_CATEGORIES, Category
from gmailwiz.classifier import (
    MissingAPIKeyError,
    SenderInput,
    SenderSample,
    classify_senders,
)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CategoryResult:
    """Per-category outcome inside a one-pass run."""

    category: str
    label_run_id: Optional[str] = None
    labels_applied: int = 0
    labels_failed: int = 0
    label_run_status: Optional[str] = None  # planning's run.status post-apply
    archive_run_id: Optional[str] = None
    archive_applied: int = 0
    archive_failed: int = 0
    archive_run_status: Optional[str] = None
    error: Optional[str] = None  # per-category exception text, if any


@dataclass
class OneShotResult:
    """Full one-pass result. Serialisable to JSON via ``to_dict``."""

    status: str  # "success" | "partial_failure" | "failure" | "auth_required"
    error_code: Optional[str] = None
    report_run_id: Optional[str] = None
    report_status: Optional[str] = None  # "committed" | "partially_failed" | "failed"
    snapshot_size: int = 0
    snapshot_message_ids: list[str] = field(default_factory=list)
    classified_sender_count: int = 0
    fetch_failure_count: int = 0
    unparseable_sender_count: int = 0
    classification_failure_count: int = 0
    categories: list[CategoryResult] = field(default_factory=list)
    wall_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "error_code": self.error_code,
            "report_run_id": self.report_run_id,
            "report_status": self.report_status,
            "snapshot_size": self.snapshot_size,
            "snapshot_message_ids": list(self.snapshot_message_ids),
            "classified_sender_count": self.classified_sender_count,
            "fetch_failure_count": self.fetch_failure_count,
            "unparseable_sender_count": self.unparseable_sender_count,
            "classification_failure_count": self.classification_failure_count,
            "categories": [
                {
                    "category": c.category,
                    "label_run_id": c.label_run_id,
                    "labels_applied": c.labels_applied,
                    "labels_failed": c.labels_failed,
                    "label_run_status": c.label_run_status,
                    "archive_run_id": c.archive_run_id,
                    "archive_applied": c.archive_applied,
                    "archive_failed": c.archive_failed,
                    "archive_run_status": c.archive_run_status,
                    "error": c.error,
                }
                for c in self.categories
            ],
            "wall_seconds": round(self.wall_seconds, 3),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Helpers (shared with cli.py — kept here so oneshot is import-self-sufficient
# for the future serve.py worker thread; cli.py re-uses them via re-export)
# ---------------------------------------------------------------------------


def aggregate_senders(messages: list[dict]) -> tuple[
    list[SenderInput],
    dict[str, list[dict]],
    int,
]:
    """Same shape as cli._aggregate_senders; lifted here so oneshot doesn't
    depend on the CLI module. Returns (inputs, by_email, unparseable_count)."""
    from collections import defaultdict

    by_email: dict[str, list[dict]] = defaultdict(list)
    unparseable = 0
    for msg in messages:
        sender = (msg.get("sender_email") or "").strip().lower()
        if not sender:
            unparseable += 1
            continue
        by_email[sender].append(msg)

    inputs: list[SenderInput] = []
    for email, msgs in by_email.items():
        display = ""
        samples: list[SenderSample] = []
        for m in msgs:
            if not display and m.get("sender_name"):
                display = m["sender_name"]
            if len(samples) < 3:
                samples.append(
                    SenderSample(
                        subject=m.get("subject", ""),
                        snippet=m.get("snippet", ""),
                    )
                )
        inputs.append(SenderInput(email=email, display_name=display, samples=samples))
    return inputs, by_email, unparseable


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


ProgressFn = Callable[[str, dict[str, Any]], None]


def _noop_progress(_stage: str, _info: dict[str, Any]) -> None:
    pass


def run_one_pass(
    *,
    creds: Credentials,
    db_path: Optional[Path] = None,
    limit: int = 1000,
    archive: bool = True,
    on_progress: Optional[ProgressFn] = None,
) -> OneShotResult:
    """Run report → label-all-4 → archive-all-4 in one pass.

    Parameters
    ----------
    creds
        Already-valid Google credentials. Caller is responsible for obtaining
        them — typically via ``auth.get_credentials(interactive=True)`` on M4
        or ``auth.get_credentials(interactive=False)`` on M2. The Phase 2
        trigger service catches ``auth.AuthRequired`` itself and bypasses
        this function entirely with ``error_code="auth_required"``.
    db_path
        Optional explicit path to ``state.db``. Defaults to ``gw_db``'s
        configured location. Required so the worker thread (Phase 2) can
        open its own connection — never inherit one from the request handler.
    limit
        Maximum number of unread inbox messages to scan. Sets the size of
        the snapshot the four categories operate on.
    archive
        If False, label all four categories but skip the archive phase.
        Drives the CLI's ``--no-archive`` flag.
    on_progress
        Optional ``(stage: str, info: dict) -> None`` callback for UI plumbing.
        Stages: ``snapshot_begin``, ``snapshot_done``, ``classify_begin``,
        ``classify_done``, ``category_begin``, ``label_begin``, ``label_done``,
        ``archive_begin``, ``archive_done``, ``category_done``, ``all_done``.
    """
    if on_progress is None:
        on_progress = _noop_progress
    if limit <= 0:
        raise ValueError("limit must be positive")

    started_at = time.monotonic()
    result = OneShotResult(status="success")

    # ---- Snapshot: list + fetch metadata ONCE -------------------------------
    on_progress("snapshot_begin", {"limit": limit})
    ids = gmail_client.list_unread_message_ids(creds, max_results=limit)
    if not ids:
        result.snapshot_size = 0
        result.status = "success"
        result.wall_seconds = time.monotonic() - started_at
        result.notes.append("inbox empty — no unread messages")
        on_progress("all_done", {"snapshot_size": 0})
        return result

    messages = gmail_client.get_message_metadata(creds, ids)
    fetch_failure_count = max(0, len(ids) - len(messages))
    result.fetch_failure_count = fetch_failure_count
    result.snapshot_size = len(messages)
    result.snapshot_message_ids = [m["id"] for m in messages]
    on_progress("snapshot_done", {
        "ids_returned": len(ids),
        "metadata_fetched": len(messages),
        "fetch_failures": fetch_failure_count,
    })

    if not messages:
        # Every per-message fetch failed (likely revoked auth or a Gmail
        # 5xx storm). Surface as failure — there's nothing the rest of the
        # pipeline can do.
        result.status = "failure"
        result.error_code = "metadata_fetch_failed"
        result.notes.append(
            f"all {len(ids)} message metadata fetches failed"
        )
        result.wall_seconds = time.monotonic() - started_at
        return result

    sender_inputs, by_email, unparseable_count = aggregate_senders(messages)

    # ---- Classify all senders, persist a `report` run -----------------------
    on_progress("classify_begin", {"unique_senders": len(sender_inputs)})

    with gw_db.open_db(db_path) as conn:
        report_run_id = gw_db.create_run(
            conn,
            phase="report",
            limit_count=limit,
            dry_run=True,
            status="planned",
        )
        result.report_run_id = report_run_id

        try:
            classifications = classify_senders(
                sender_inputs,
                conn=conn,
                on_progress=lambda stage, info: on_progress(
                    f"classify_{stage}", info
                ),
            )
        except MissingAPIKeyError as exc:
            try:
                gw_db.update_run_status(conn, report_run_id, "failed")
            except Exception:
                pass
            result.status = "failure"
            result.error_code = "missing_api_key"
            result.notes.append(str(exc))
            result.wall_seconds = time.monotonic() - started_at
            return result
        except BaseException as exc:  # noqa: BLE001 — bookkeeping path
            try:
                gw_db.update_run_status(conn, report_run_id, "failed")
            except Exception:
                pass
            # Re-raise: callers (CLI, trigger service) translate to their
            # own surface. cs.md mandates raw error pass-through.
            result.wall_seconds = time.monotonic() - started_at
            raise

        classification_failure_count = sum(
            1 for r in classifications.values() if getattr(r, "source", "") == "unknown"
        )
        had_partial = (
            fetch_failure_count > 0
            or unparseable_count > 0
            or classification_failure_count > 0
        )
        report_terminal_status = "partially_failed" if had_partial else "committed"
        gw_db.update_run_status(conn, report_run_id, report_terminal_status)
        result.report_status = report_terminal_status
        result.classified_sender_count = len(classifications)
        result.unparseable_sender_count = unparseable_count
        result.classification_failure_count = classification_failure_count
        if fetch_failure_count:
            result.notes.append(
                f"{fetch_failure_count} message(s) skipped: Gmail metadata fetch failed"
            )
        if unparseable_count:
            result.notes.append(
                f"{unparseable_count} message(s) skipped: unparseable From header"
            )
        if classification_failure_count:
            result.notes.append(
                f"{classification_failure_count} sender(s) skipped: classifier returned UNKNOWN"
            )
        on_progress("classify_done", {
            "classified": len(classifications),
            "classification_failures": classification_failure_count,
        })

        # ---- Snapshot for archive (id → metadata) -------------------------
        messages_by_id: dict[str, dict] = {m["id"]: m for m in messages}

        # ---- Per-category label + archive ---------------------------------
        for category in CLASSIFIABLE_CATEGORIES:
            cat_result = CategoryResult(category=category.value)
            result.categories.append(cat_result)
            on_progress("category_begin", {"category": category.value})

            try:
                label_plan = planning.build_label_plan(
                    creds=creds,
                    conn=conn,
                    category=category,
                    limit=limit,
                    messages=messages,
                )
                cat_result.label_run_id = label_plan.run_id
                on_progress("label_begin", {
                    "category": category.value,
                    "candidates": len(label_plan.candidates),
                })

                if label_plan.candidates:
                    label_result = planning.apply_label_plan(
                        creds=creds,
                        conn=conn,
                        run_id=label_plan.run_id,
                        on_progress=lambda stage, info, _c=category: on_progress(
                            f"label_apply_{stage}",
                            {**info, "category": _c.value},
                        ),
                    )
                    cat_result.labels_applied = label_result.applied
                    cat_result.labels_failed = label_result.failed
                    cat_result.label_run_status = label_result.run_status
                else:
                    # No candidates → no apply call. The build set the run
                    # to 'planned'; leave it that way (a no-op terminal
                    # transition would mislead the audit log).
                    cat_result.label_run_status = "planned"
                on_progress("label_done", {
                    "category": category.value,
                    "applied": cat_result.labels_applied,
                    "failed": cat_result.labels_failed,
                })
            except Exception as exc:  # noqa: BLE001
                # One category's failure mustn't kill the run — record and
                # continue to the next category. The trigger service / JSON
                # output will surface per-category errors.
                cat_result.error = f"label: {type(exc).__name__}: {exc}"
                on_progress("category_done", {
                    "category": category.value,
                    "error": cat_result.error,
                })
                continue

            if not archive:
                on_progress("category_done", {"category": category.value})
                continue
            if cat_result.label_run_status not in {"committed", "partially_failed"}:
                # No applied labels → no source for archive. Skip silently.
                on_progress("category_done", {"category": category.value})
                continue

            try:
                archive_plan = planning.build_archive_plan(
                    creds=creds,
                    conn=conn,
                    source_run_id=cat_result.label_run_id,
                    messages=messages_by_id,
                )
                cat_result.archive_run_id = archive_plan.run_id
                on_progress("archive_begin", {
                    "category": category.value,
                    "candidates": len(archive_plan.candidates),
                })

                if archive_plan.candidates:
                    archive_result = planning.apply_archive_plan(
                        creds=creds,
                        conn=conn,
                        run_id=archive_plan.run_id,
                        on_progress=lambda stage, info, _c=category: on_progress(
                            f"archive_apply_{stage}",
                            {**info, "category": _c.value},
                        ),
                    )
                    cat_result.archive_applied = archive_result.applied
                    cat_result.archive_failed = archive_result.failed
                    cat_result.archive_run_status = archive_result.run_status
                else:
                    cat_result.archive_run_status = "planned"
                on_progress("archive_done", {
                    "category": category.value,
                    "applied": cat_result.archive_applied,
                    "failed": cat_result.archive_failed,
                })
            except Exception as exc:  # noqa: BLE001
                cat_result.error = (
                    (cat_result.error + " | " if cat_result.error else "")
                    + f"archive: {type(exc).__name__}: {exc}"
                )

            on_progress("category_done", {"category": category.value})

    # ---- Roll up final status -----------------------------------------------
    # Report-phase shortfall counts the same as a per-category failure: the
    # caller asked for ``--limit N`` and we silently processed fewer than N
    # messages. Without this, ``run_one_pass`` could exit 0 (and the Phase 2
    # trigger service could send a "success" Telegram) after dropping the
    # majority of the snapshot to fetch failures or UNKNOWN classifications.
    report_had_shortfall = (
        result.report_status == "partially_failed"
        or result.report_status == "failed"
        or result.fetch_failure_count > 0
        or result.unparseable_sender_count > 0
        or result.classification_failure_count > 0
    )

    any_applied = any(
        c.labels_applied or c.archive_applied for c in result.categories
    )
    any_per_category_failed = any(
        c.error or c.labels_failed or c.archive_failed
        or c.label_run_status == "failed"
        or c.archive_run_status == "failed"
        for c in result.categories
    )
    any_failed = any_per_category_failed or report_had_shortfall

    if any_failed and any_applied:
        result.status = "partial_failure"
    elif any_failed and not any_applied:
        result.status = "failure"
    else:
        result.status = "success"

    result.wall_seconds = time.monotonic() - started_at
    on_progress("all_done", {
        "status": result.status,
        "wall_seconds": result.wall_seconds,
    })
    return result


__all__ = [
    "AuthRequired",
    "CategoryResult",
    "OneShotResult",
    "aggregate_senders",
    "run_one_pass",
]


# Re-export so callers (CLI, future serve.py) can `from gmailwiz.oneshot
# import AuthRequired` without also importing auth.
AuthRequired = gw_auth.AuthRequired
