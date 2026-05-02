"""Persisted execution plans for mutating commands.

A plan is a ``runs`` row plus N ``audit_log`` rows, all with ``status='planned'``.
Preview reads from this; commit walks the same rows and flips them. The whole
point is to prevent the dreaded "preview shows X but commit acts on Y" drift
caused by recomputing the candidate set against fresh Gmail state at commit
time.

Phases supported here:
  * Label  — `build_label_plan` / `apply_label_plan`
  * Archive — `build_archive_plan` / `apply_archive_plan` (scoped to a
    previously-applied label run; archive candidates are the message_ids
    from that run, not "every message currently labeled gmailwiz/<cat>")
  * Undo   — `apply_undo_plan` reverses a previously-applied label or
    archive run by walking the persisted audit_log rows. There's no
    separate "build undo plan" step because the audit_log IS the plan
    (it already has every message_id and the pre-mutation state).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from google.oauth2.credentials import Credentials

from gmailwiz import db as gw_db
from gmailwiz import gmail_client
from gmailwiz.categories import (
    CLASSIFIABLE_CATEGORIES,
    Category,
    gmail_label_name,
)


# ---------------------------------------------------------------------------
# Phase + action constants (used across all chunks)
# ---------------------------------------------------------------------------

# In Gmail, "archive" means removing the `INBOX` system label from a
# message. The message stays in All Mail (searchable, accessible), but
# is no longer in the Inbox view. Nothing is deleted.
INBOX_LABEL_ID = "INBOX"

# `runs.phase` values in use across all chunks.
PHASE_LABEL = "label"
PHASE_ARCHIVE = "archive"

# `audit_log.action` values in use.
ACTION_ADD_LABEL = "add_label"
ACTION_ARCHIVE = "archive"

# Map a run's `phase` to the audit_log `action` it expects.
_PHASE_TO_ACTION = {
    PHASE_LABEL: ACTION_ADD_LABEL,
    PHASE_ARCHIVE: ACTION_ARCHIVE,
}


# ---------------------------------------------------------------------------
# Auto-heal: stale `runs.status` recovery from cumulative audit_log state
# ---------------------------------------------------------------------------


def heal_run_status_from_audit_log(
    conn,
    run_id: str,
) -> Optional[dict]:
    """Re-derive ``runs.status`` from the audit_log if the cached status
    is inconsistent with what's actually been mutated.

    The ``runs.status`` column is effectively a cache of "what does the
    audit_log say about this run". Most of the time it's authoritative
    because the apply functions write it after the loop. But if the
    final ``update_run_status`` call FAILS (DB locked, disk full, etc.)
    after Gmail mutations have already been recorded as `applied`/
    `failed` in audit_log, the cache goes stale: run row stays
    `status='planned'` while the audit log shows the work is done.

    Without auto-heal, the user is stranded: ``_run_label_commit`` would
    short-circuit ("no planned audit entries — nothing to do") and
    ``_run_undo`` / the undo picker would skip the run because its
    status isn't `committed`/`partially_failed`.

    This helper, called at the start of every helper that reads a run,
    re-derives the correct status from cumulative audit_log state and
    persists the fix. Audit log is the single source of truth.

    Returns the (possibly-updated) run row, or ``None`` if the run
    doesn't exist. Phases without a recognized action (e.g., 'report')
    are returned untouched — only label and archive runs are healed.
    """
    run = gw_db.get_run(conn, run_id)
    if run is None:
        return None
    phase = run.get("phase")
    action = _PHASE_TO_ACTION.get(phase)
    if action is None:
        # 'report' or unrecognized phase — nothing to heal.
        return run
    if run.get("status") != "planned":
        # Already in a terminal/non-planned state — trust the cache.
        return run

    audit_rows = gw_db.get_audit_entries(conn, run_id)
    relevant = [r for r in audit_rows if r.get("action") == action]
    cum_applied = sum(1 for r in relevant if r.get("status") == "applied")
    cum_failed = sum(1 for r in relevant if r.get("status") == "failed")
    cum_planned = sum(1 for r in relevant if r.get("status") == "planned")

    if cum_planned > 0:
        # Genuinely still planned (some rows untouched) — even if a few
        # are terminal, the run isn't fully done. Leave at planned but
        # at least flip dry_run if any real mutation occurred.
        if cum_applied > 0 or cum_failed > 0:
            try:
                gw_db.update_run_status(conn, run_id, "planned", dry_run=False)
                run["dry_run"] = 0
            except Exception:
                # Heal is best-effort; if the same DB problem recurs
                # we'd just re-heal next read. Don't mask the read.
                pass
        return run

    if cum_applied == 0 and cum_failed == 0:
        # No action rows at all — genuinely planned (vacuous). Untouched.
        return run

    # All rows are terminal — derive the correct cumulative status.
    if cum_failed == 0:
        derived = "committed"
    elif cum_applied == 0:
        derived = "failed"
    else:
        derived = "partially_failed"

    try:
        gw_db.update_run_status(conn, run_id, derived, dry_run=False)
        run["status"] = derived
        run["dry_run"] = 0
    except Exception:
        # Best-effort; same rationale as above.
        pass
    return run


# ---------------------------------------------------------------------------
# Pending-label marker
# ---------------------------------------------------------------------------

# Stored in `after_label_ids` at plan time when the target gmailwiz/<cat>
# label hasn't been created in Gmail yet. The commit step calls
# `ensure_labels_exist` to create the label, then overwrites the audit row's
# `after_label_ids` with the actual label_ids returned by Gmail's modify
# response. The marker is intentionally non-Gmail-id-shaped (Gmail label_ids
# look like `Label_1234567890` or `INBOX`) so a future query can spot leftover
# planned rows by string match.
_PENDING_PREFIX = "pending:"


def _pending_label_marker(label_name: str) -> str:
    return f"{_PENDING_PREFIX}{label_name}"


# ---------------------------------------------------------------------------
# Plan dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LabelPlanCandidate:
    """A single message slated for labeling under a plan."""

    message_id: str
    thread_id: Optional[str]
    sender_email: str
    subject: str
    before_label_ids: list[str]
    after_label_ids: list[str]


@dataclass
class LabelPlan:
    """The fully persisted result of `build_label_plan`."""

    run_id: str
    category: Category
    label_name: str
    target_label_id: Optional[str]  # None if the gmailwiz/<cat> label doesn't exist yet
    candidates: list[LabelPlanCandidate] = field(default_factory=list)
    fetched_message_count: int = 0
    # Number of message IDs that Gmail listed but `get_message_metadata`
    # couldn't fetch (per-message HttpError → logged + skipped). A
    # transient Gmail read failure shrinks the candidate set silently
    # without this signal; the preview surfaces it so the user knows
    # the plan is incomplete and can re-run.
    fetch_failure_count: int = 0
    skipped_unparseable: int = 0
    skipped_uncached: int = 0
    skipped_unknown: int = 0
    skipped_other_category: int = 0
    skipped_already_labeled: int = 0

    @property
    def total_skipped(self) -> int:
        return (
            self.skipped_unparseable
            + self.skipped_uncached
            + self.skipped_unknown
            + self.skipped_other_category
            + self.skipped_already_labeled
        )


@dataclass
class LabelApplyResult:
    """Outcome of applying a previously-built label plan."""

    run_id: str
    label_id: str
    applied: int = 0
    failed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    run_status: str = "planned"


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------


def build_label_plan(
    *,
    creds: Credentials,
    conn: sqlite3.Connection,
    category: Category,
    limit: int = 100,
    query: str = "is:unread in:inbox",
) -> LabelPlan:
    """Build a labeling plan for ``category`` from the current inbox state.

    Persists a ``runs`` row + one ``audit_log`` row per candidate
    (``status='planned'``). The audit_log rows are the source of truth for
    the commit step — never a recomputed query.

    Filters out:
      * messages whose ``From:`` we couldn't parse into an email
      * senders not yet in the ``senders`` cache (the user hasn't reported them)
      * senders cached as ``UNKNOWN``
      * senders cached as a different category
      * messages that already carry the target ``gmailwiz/<category>`` label

    Does **not** mutate Gmail. Label creation is deferred to commit time.
    """
    if category not in CLASSIFIABLE_CATEGORIES:
        raise ValueError(
            f"Refusing to plan a label run for non-classifiable category: "
            f"{category!r}. Allowed: {[c.value for c in CLASSIFIABLE_CATEGORIES]}"
        )
    if limit <= 0:
        raise ValueError("limit must be positive")

    label_name = gmail_label_name(category)

    # Read-only: find the target label's Gmail id IF it already exists. If
    # not, no message can have it, so skip the "already labeled" filter.
    existing_labels = gmail_client.list_existing_labels(creds)
    target_label_id: Optional[str] = existing_labels.get(label_name)

    ids = gmail_client.list_unread_message_ids(creds, max_results=limit, query=query)
    messages = gmail_client.get_message_metadata(creds, ids) if ids else []
    # Per-message HttpErrors inside `get_message_metadata` are logged to
    # stderr and the message is skipped (active inbox: messages can be
    # archived/deleted between list and get). Compute the gap so the
    # preview can surface it — a transient API issue could otherwise
    # silently shrink the candidate set without any user-visible signal.
    fetch_failure_count = max(0, len(ids) - len(messages))

    # Persist the run row before iterating, so a crash mid-plan still leaves
    # a discoverable record (the user sees a `planned` run with N audit rows
    # rather than zero state at all).
    run_id = gw_db.create_run(
        conn,
        phase="label",
        query=query,
        limit_count=limit,
        category_filter=category.value,
        dry_run=True,
        status="planned",
    )

    plan = LabelPlan(
        run_id=run_id,
        category=category,
        label_name=label_name,
        target_label_id=target_label_id,
        fetched_message_count=len(messages),
        fetch_failure_count=fetch_failure_count,
    )

    for msg in messages:
        sender = (msg.get("sender_email") or "").strip().lower()
        if not sender:
            plan.skipped_unparseable += 1
            continue
        cached = gw_db.get_sender(conn, sender)
        if not cached:
            plan.skipped_uncached += 1
            continue
        cached_category = cached["category"]
        if cached_category is Category.UNKNOWN:
            plan.skipped_unknown += 1
            continue
        if cached_category is not category:
            plan.skipped_other_category += 1
            continue
        before_ids = list(msg.get("label_ids") or [])
        if target_label_id is not None and target_label_id in before_ids:
            plan.skipped_already_labeled += 1
            continue

        # Project after-state. If the label already exists, append its real
        # id; if not, append a placeholder that the commit step will replace.
        if target_label_id is not None:
            after_ids = before_ids + [target_label_id]
        else:
            after_ids = before_ids + [_pending_label_marker(label_name)]

        plan.candidates.append(
            LabelPlanCandidate(
                message_id=msg["id"],
                thread_id=msg.get("thread_id"),
                sender_email=sender,
                subject=msg.get("subject", "") or "",
                before_label_ids=before_ids,
                after_label_ids=after_ids,
            )
        )
        gw_db.append_audit_entry(
            conn,
            run_id=run_id,
            action="add_label",
            message_id=msg["id"],
            thread_id=msg.get("thread_id"),
            sender_email=sender,
            before_label_ids=before_ids,
            after_label_ids=after_ids,
            status="planned",
        )

    return plan


# ---------------------------------------------------------------------------
# Plan applier
# ---------------------------------------------------------------------------


def apply_label_plan(
    *,
    creds: Credentials,
    conn: sqlite3.Connection,
    run_id: str,
    on_progress: Optional[Callable[[str, dict[str, Any]], None]] = None,
) -> LabelApplyResult:
    """Walk a planned label run and apply each ``add_label`` to Gmail.

    Pre-conditions (raises ``ValueError`` otherwise):
      * the run exists
      * its phase is ``label``
      * its status is ``planned`` (a `committed` / `partially_failed` /
        `failed` run can't be re-applied — that's what `undo` is for)

    Per-row outcome:
      * success → audit row flipped to ``applied`` with the actual
        ``after_label_ids`` returned by Gmail (overwrites the projected
        marker)
      * exception → audit row flipped to ``failed`` with the error string

    Run-level terminal status:
      * all rows applied            → ``committed``
      * some applied, some failed   → ``partially_failed``
      * none applied (all failed)   → ``failed``
    """
    run = gw_db.get_run(conn, run_id)
    if run is None:
        raise ValueError(f"No run with id {run_id!r}")
    if run.get("phase") != "label":
        raise ValueError(
            f"Run {run_id!r} has phase={run.get('phase')!r}; expected 'label'"
        )
    if run.get("status") != "planned":
        raise ValueError(
            f"Run {run_id!r} has status={run.get('status')!r}; only 'planned' "
            "runs can be applied"
        )

    category_value = run.get("category_filter")
    if not category_value:
        raise ValueError(
            f"Run {run_id!r} has no category_filter; cannot derive target label"
        )
    try:
        category = Category(category_value)
    except ValueError as exc:
        raise ValueError(
            f"Run {run_id!r} category_filter {category_value!r} is not a valid Category"
        ) from exc
    # Defense-in-depth: `build_label_plan` already rejects UNKNOWN, but a
    # hand-edited DB row could slip through and end up creating a
    # `gmailwiz/unknown` label in Gmail. Refuse here too.
    if category not in CLASSIFIABLE_CATEGORIES:
        raise ValueError(
            f"Run {run_id!r} has non-classifiable category {category.value!r}; "
            "cannot apply label run for UNKNOWN."
        )
    label_name = gmail_label_name(category)

    # Vacuous-apply guard runs BEFORE any Gmail call. `ensure_labels_exist`
    # is itself a Gmail mutation (it creates `gmailwiz/<category>` if
    # absent); calling it for a run with zero work to do would still leave
    # a permanent label artifact in the user's Gmail account despite no
    # message-level mutation occurring. Read audit rows from SQLite first;
    # if there's nothing to apply, return without ever touching Gmail.
    audit_rows = gw_db.get_audit_entries(conn, run_id)
    # Filter both on `status='planned'` (re-entry safety: skip rows already
    # touched by a previous interrupted apply) AND on `action='add_label'`
    # (defense-in-depth: a label run shouldn't apply rows whose action is
    # something else, e.g., a hand-edited or future-Phase-3 archive row
    # that landed in this run by mistake).
    planned_rows = [
        r for r in audit_rows
        if r.get("status") == "planned" and r.get("action") == "add_label"
    ]
    total = len(planned_rows)

    if total == 0:
        result = LabelApplyResult(run_id=run_id, label_id="")
        result.run_status = "planned"
        if on_progress is not None:
            on_progress(
                "apply_complete",
                {"applied": 0, "failed": 0, "run_status": "planned"},
            )
        return result

    # Now we know we have work to do — create the label if needed.
    label_map = gmail_client.ensure_labels_exist(creds, [label_name])
    target_label_id = label_map[label_name]

    result = LabelApplyResult(run_id=run_id, label_id=target_label_id)

    if on_progress is not None:
        on_progress("apply_begin", {"total": total, "label_name": label_name, "label_id": target_label_id})

    for i, row in enumerate(planned_rows, start=1):
        msg_id = row["message_id"]
        try:
            response = gmail_client.modify_labels(
                creds,
                message_id=msg_id,
                add_label_ids=[target_label_id],
            )
        except Exception as exc:  # surface verbatim per cs.md
            err = f"{type(exc).__name__}: {exc}"
            # Mutation never happened. The audit row's `after_label_ids`
            # was set at plan time to a *projection* (before + pending
            # marker / target id). Revert it to `before_label_ids` so a
            # later query reading after_label_ids on a failed row sees an
            # accurate "nothing changed" rather than a stale projection.
            gw_db.update_audit_entry(
                conn,
                audit_id=row["id"],
                status="failed",
                after_label_ids=list(row.get("before_label_ids") or []),
                error=err,
            )
            result.failed += 1
            result.errors.append((msg_id, err))
            if on_progress is not None:
                on_progress(
                    "apply_failed",
                    {
                        "index": i,
                        "total": total,
                        "message_id": msg_id,
                        "error": err,
                    },
                )
            continue

        # Gmail's `users.messages.modify` returns the full Message
        # resource — `labelIds` is the complete post-mutation label
        # list. A response missing the field entirely is a malformed
        # / intermediated response; conflating it with "message has
        # no labels post-modify" would persist a wrong empty list to
        # the audit row, which a future undo would interpret as
        # "remove no labels". Surface the bad shape verbatim instead
        # (per cs.md) and treat the row as failed.
        if "labelIds" not in response:
            err = (
                "Gmail messages.modify response missing 'labelIds' "
                f"for {msg_id}; cannot record post-mutation state. "
                f"Response: {response!r}"
            )
            gw_db.update_audit_entry(
                conn,
                audit_id=row["id"],
                status="failed",
                after_label_ids=list(row.get("before_label_ids") or []),
                error=err,
            )
            result.failed += 1
            result.errors.append((msg_id, err))
            if on_progress is not None:
                on_progress(
                    "apply_failed",
                    {
                        "index": i,
                        "total": total,
                        "message_id": msg_id,
                        "error": err,
                    },
                )
            continue
        actual_after = list(response.get("labelIds") or [])
        try:
            gw_db.update_audit_entry(
                conn,
                audit_id=row["id"],
                status="applied",
                after_label_ids=actual_after,
            )
        except Exception as exc:
            # Worst-case integrity gap: the Gmail mutation SUCCEEDED
            # (label is on the message) but the audit row is still
            # `planned`. Surface loudly and counted as `failed` so the
            # run doesn't claim a clean commit. A naive retry won't
            # help — `_run_label_commit`'s precondition rejects runs
            # whose status is anything other than `planned`, and after
            # this loop completes the run will be flipped to
            # `partially_failed`/`failed` and then refuse re-apply.
            # Manual reconciliation: either remove the label from the
            # message in Gmail, or open the SQLite DB and flip the row
            # to `applied` to match reality.
            err = (
                f"{type(exc).__name__} updating audit row {row['id']} "
                f"after successful Gmail modify on {msg_id}: {exc} — "
                "label was applied in Gmail but audit_log still says "
                "planned. Manual reconciliation required (the run "
                "row will close as partially_failed and is not "
                "automatically re-runnable)."
            )
            result.failed += 1
            result.errors.append((msg_id, err))
            if on_progress is not None:
                on_progress(
                    "apply_failed",
                    {
                        "index": i,
                        "total": total,
                        "message_id": msg_id,
                        "error": err,
                    },
                )
            continue

        result.applied += 1
        if on_progress is not None:
            on_progress(
                "apply_done",
                {"index": i, "total": total, "message_id": msg_id},
            )

    # Terminal status decided from CUMULATIVE audit-log state (not just
    # this pass's `result.applied` / `result.failed`). A run that was
    # interrupted mid-loop and resumed must reflect prior failed rows in
    # the final status — otherwise a re-run that processes only the
    # still-`planned` rows successfully would mark the run `committed`,
    # silently hiding failures from the prior pass.
    cumulative = gw_db.get_audit_entries(conn, run_id)
    add_label_rows = [r for r in cumulative if r.get("action") == "add_label"]
    cum_applied = sum(1 for r in add_label_rows if r.get("status") == "applied")
    cum_failed = sum(1 for r in add_label_rows if r.get("status") == "failed")
    cum_planned = sum(1 for r in add_label_rows if r.get("status") == "planned")

    # Re-entry safety: a row can land here still `planned` if Gmail's
    # `modify_labels` succeeded but the subsequent per-row
    # `update_audit_entry` raised (DB write failure after a real Gmail
    # mutation — the worst-case integrity gap). Leave the run at
    # `planned` so a future apply re-walks this row; Gmail's modify
    # is idempotent so the retry is safe.
    if cum_planned > 0:
        result.run_status = "planned"
    elif cum_failed == 0:
        result.run_status = "committed"
    elif cum_applied == 0:
        result.run_status = "failed"
    else:
        result.run_status = "partially_failed"

    # `dry_run` reflects "has any real-world Gmail mutation been
    # attempted on this run?" Once any audit row reaches a terminal
    # state (applied or failed), a real mutation was attempted and
    # `dry_run=0` is the truthful state — even if the run-level status
    # is still `planned` because some rows haven't reached a terminal
    # state yet (the worst-case integrity gap above). Flipping
    # `dry_run` independently of `run_status` is safer than letting
    # `(status='planned' AND dry_run=1)` mask a partial real mutation.
    if cum_applied > 0 or cum_failed > 0:
        gw_db.update_run_status(conn, run_id, result.run_status, dry_run=False)
    elif result.run_status != "planned":
        # No applied/failed rows but status changed (shouldn't happen
        # given the cumulative logic above, but defensive).
        gw_db.update_run_status(conn, run_id, result.run_status)

    if on_progress is not None:
        on_progress(
            "apply_complete",
            {
                "applied": result.applied,
                "failed": result.failed,
                "run_status": result.run_status,
            },
        )

    return result


# ---------------------------------------------------------------------------
# Phase 3 — archive
# ---------------------------------------------------------------------------


@dataclass
class ArchivePlanCandidate:
    """A single message slated for archiving under a plan."""

    message_id: str
    thread_id: Optional[str]
    sender_email: str
    subject: str
    before_label_ids: list[str]
    after_label_ids: list[str]


@dataclass
class ArchivePlan:
    """The fully persisted result of `build_archive_plan`."""

    run_id: str  # NEW archive run id (not the source label run)
    source_run_id: str  # the label run we're archiving from
    source_category: Category
    candidates: list[ArchivePlanCandidate] = field(default_factory=list)
    fetched_message_count: int = 0
    fetch_failure_count: int = 0
    skipped_already_archived: int = 0
    skipped_source_failed: int = 0  # rows in source run that themselves failed/reverted

    @property
    def total_skipped(self) -> int:
        return self.skipped_already_archived + self.skipped_source_failed


def _validate_label_source_for_archive(
    conn: sqlite3.Connection, source_run_id: str
) -> dict[str, Any]:
    """Validate that `source_run_id` is a label run usable as an archive source.

    Returns the run row on success. Raises ValueError otherwise.

    Acceptable source statuses: ``committed`` and ``partially_failed`` (a
    partially-failed label run still produced real labels we can archive).
    Rejected: ``planned`` (nothing applied), ``undone`` (the user has
    reversed it), ``failed`` (no rows applied), and any non-label phase.
    """
    source = gw_db.get_run(conn, source_run_id)
    if source is None:
        raise ValueError(f"No run with id {source_run_id!r}")
    if source.get("phase") != PHASE_LABEL:
        raise ValueError(
            f"Run {source_run_id!r} has phase={source.get('phase')!r}; "
            f"archive source must be a {PHASE_LABEL!r} run."
        )
    if source.get("status") not in {"committed", "partially_failed"}:
        raise ValueError(
            f"Run {source_run_id!r} has status={source.get('status')!r}; "
            "archive source must be 'committed' or 'partially_failed'."
        )
    return source


def build_archive_plan(
    *,
    creds: Credentials,
    conn: sqlite3.Connection,
    source_run_id: str,
) -> ArchivePlan:
    """Build an archive plan from a previously-applied label run.

    Walks the source run's audit_log rows (status='applied'), re-fetches
    each message's *current* label_ids from Gmail to record an accurate
    pre-archive state, and skips messages that no longer have ``INBOX``
    (the user already archived them manually) or that Gmail can't fetch
    (deleted / 404).

    Persists a new ``runs`` row (phase='archive', category_filter copied
    from the source) plus one ``audit_log`` row per candidate
    (action='archive', status='planned').

    The "no recomputation drift between preview and commit" rule applies
    here too: the candidate SET is fixed at this moment from the source
    run; commit walks these same persisted rows.
    """
    source = _validate_label_source_for_archive(conn, source_run_id)

    # Resolve the source category for category_filter on the new run.
    raw_category = source.get("category_filter")
    try:
        source_category = Category(raw_category or "")
    except ValueError as exc:
        raise ValueError(
            f"Source run {source_run_id!r} category_filter "
            f"{raw_category!r} is not a valid Category"
        ) from exc
    # Defense-in-depth symmetric with apply_label_plan / apply_undo_plan:
    # UNKNOWN-category source runs were never supposed to drive
    # mutations. Refuse here too in case _run_archive_plan's CLI gate
    # is bypassed.
    if source_category not in CLASSIFIABLE_CATEGORIES:
        raise ValueError(
            f"Source run {source_run_id!r} has non-classifiable category "
            f"{source_category.value!r}; cannot use as archive source."
        )

    # Pull message_ids from the source run's APPLIED audit rows. Skipping
    # `failed` / `reverted` / `planned` rows means we never try to archive
    # a message that the source run didn't successfully label.
    source_audit = gw_db.get_audit_entries(conn, source_run_id)
    applied_source_rows = [
        r for r in source_audit
        if r.get("action") == ACTION_ADD_LABEL and r.get("status") == "applied"
    ]
    skipped_source_failed = sum(
        1 for r in source_audit
        if r.get("action") == ACTION_ADD_LABEL and r.get("status") != "applied"
    )

    message_ids = [r["message_id"] for r in applied_source_rows]
    messages = (
        gmail_client.get_message_metadata(creds, message_ids)
        if message_ids
        else []
    )
    fetch_failure_count = max(0, len(message_ids) - len(messages))

    run_id = gw_db.create_run(
        conn,
        phase=PHASE_ARCHIVE,
        query=source.get("query"),
        limit_count=source.get("limit_count"),
        category_filter=source_category.value,
        dry_run=True,
        status="planned",
    )

    plan = ArchivePlan(
        run_id=run_id,
        source_run_id=source_run_id,
        source_category=source_category,
        fetched_message_count=len(messages),
        fetch_failure_count=fetch_failure_count,
        skipped_source_failed=skipped_source_failed,
    )

    for msg in messages:
        before_ids = list(msg.get("label_ids") or [])
        if INBOX_LABEL_ID not in before_ids:
            # User already archived this one manually. Don't re-archive
            # an already-archived message — the operation would be a
            # Gmail no-op but we'd record a misleading audit row.
            plan.skipped_already_archived += 1
            continue

        after_ids = [x for x in before_ids if x != INBOX_LABEL_ID]
        plan.candidates.append(
            ArchivePlanCandidate(
                message_id=msg["id"],
                thread_id=msg.get("thread_id"),
                sender_email=(msg.get("sender_email") or "").strip().lower(),
                subject=msg.get("subject", "") or "",
                before_label_ids=before_ids,
                after_label_ids=after_ids,
            )
        )
        gw_db.append_audit_entry(
            conn,
            run_id=run_id,
            action=ACTION_ARCHIVE,
            message_id=msg["id"],
            thread_id=msg.get("thread_id"),
            sender_email=(msg.get("sender_email") or "").strip().lower() or None,
            before_label_ids=before_ids,
            after_label_ids=after_ids,
            status="planned",
        )

    return plan


@dataclass
class ArchiveApplyResult:
    """Outcome of applying a previously-built archive plan."""

    run_id: str
    applied: int = 0
    failed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    run_status: str = "planned"


def apply_archive_plan(
    *,
    creds: Credentials,
    conn: sqlite3.Connection,
    run_id: str,
    on_progress: Optional[Callable[[str, dict[str, Any]], None]] = None,
) -> ArchiveApplyResult:
    """Walk a planned archive run and remove ``INBOX`` from each message.

    Mirrors :func:`apply_label_plan` (same drift-safe design — walks
    persisted audit_log rows, never re-fetches Gmail beyond the per-
    message ``modify_labels`` call). Per-row outcome and run-level
    terminal status follow the same rules: cumulative audit-log state
    (across resumed applies) determines the final run status.
    """
    run = gw_db.get_run(conn, run_id)
    if run is None:
        raise ValueError(f"No run with id {run_id!r}")
    if run.get("phase") != PHASE_ARCHIVE:
        raise ValueError(
            f"Run {run_id!r} has phase={run.get('phase')!r}; expected "
            f"{PHASE_ARCHIVE!r}"
        )
    if run.get("status") != "planned":
        raise ValueError(
            f"Run {run_id!r} has status={run.get('status')!r}; only 'planned' "
            "runs can be applied"
        )

    audit_rows = gw_db.get_audit_entries(conn, run_id)
    planned_rows = [
        r for r in audit_rows
        if r.get("status") == "planned" and r.get("action") == ACTION_ARCHIVE
    ]
    total = len(planned_rows)
    result = ArchiveApplyResult(run_id=run_id)

    if total == 0:
        # Vacuous-apply guard mirrors apply_label_plan: don't flip the
        # run row at all, leave it `planned` for re-runnability.
        result.run_status = "planned"
        if on_progress is not None:
            on_progress(
                "apply_complete",
                {"applied": 0, "failed": 0, "run_status": "planned"},
            )
        return result

    if on_progress is not None:
        on_progress(
            "apply_begin",
            {"total": total, "action": ACTION_ARCHIVE, "label_id": INBOX_LABEL_ID},
        )

    for i, row in enumerate(planned_rows, start=1):
        msg_id = row["message_id"]
        try:
            response = gmail_client.modify_labels(
                creds,
                message_id=msg_id,
                remove_label_ids=[INBOX_LABEL_ID],
            )
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            gw_db.update_audit_entry(
                conn,
                audit_id=row["id"],
                status="failed",
                after_label_ids=list(row.get("before_label_ids") or []),
                error=err,
            )
            result.failed += 1
            result.errors.append((msg_id, err))
            if on_progress is not None:
                on_progress(
                    "apply_failed",
                    {"index": i, "total": total, "message_id": msg_id, "error": err},
                )
            continue

        if "labelIds" not in response:
            err = (
                f"Gmail messages.modify response missing 'labelIds' "
                f"for {msg_id}; cannot record post-mutation state. "
                f"Response: {response!r}"
            )
            gw_db.update_audit_entry(
                conn,
                audit_id=row["id"],
                status="failed",
                after_label_ids=list(row.get("before_label_ids") or []),
                error=err,
            )
            result.failed += 1
            result.errors.append((msg_id, err))
            if on_progress is not None:
                on_progress(
                    "apply_failed",
                    {"index": i, "total": total, "message_id": msg_id, "error": err},
                )
            continue

        actual_after = list(response.get("labelIds") or [])
        try:
            gw_db.update_audit_entry(
                conn,
                audit_id=row["id"],
                status="applied",
                after_label_ids=actual_after,
            )
        except Exception as exc:
            err = (
                f"{type(exc).__name__} updating audit row {row['id']} "
                f"after successful Gmail modify on {msg_id}: {exc} — "
                "INBOX was removed in Gmail but audit_log still says "
                "planned. Manual reconciliation required."
            )
            result.failed += 1
            result.errors.append((msg_id, err))
            if on_progress is not None:
                on_progress(
                    "apply_failed",
                    {"index": i, "total": total, "message_id": msg_id, "error": err},
                )
            continue

        result.applied += 1
        if on_progress is not None:
            on_progress(
                "apply_done",
                {"index": i, "total": total, "message_id": msg_id},
            )

    # Cumulative terminal-status (matches apply_label_plan): consider all
    # archive audit rows for this run, not just this pass's outcomes,
    # so a resumed apply that succeeds on the still-planned rows can't
    # hide prior-pass failures by marking the run `committed`.
    cumulative = gw_db.get_audit_entries(conn, run_id)
    archive_rows = [r for r in cumulative if r.get("action") == ACTION_ARCHIVE]
    cum_applied = sum(1 for r in archive_rows if r.get("status") == "applied")
    cum_failed = sum(1 for r in archive_rows if r.get("status") == "failed")
    cum_planned = sum(1 for r in archive_rows if r.get("status") == "planned")

    if cum_planned > 0:
        result.run_status = "planned"
    elif cum_failed == 0:
        result.run_status = "committed"
    elif cum_applied == 0:
        result.run_status = "failed"
    else:
        result.run_status = "partially_failed"

    # See `apply_label_plan` for rationale: flip dry_run independently
    # of run_status whenever any real Gmail mutation has been attempted.
    if cum_applied > 0 or cum_failed > 0:
        gw_db.update_run_status(conn, run_id, result.run_status, dry_run=False)
    elif result.run_status != "planned":
        gw_db.update_run_status(conn, run_id, result.run_status)

    if on_progress is not None:
        on_progress(
            "apply_complete",
            {
                "applied": result.applied,
                "failed": result.failed,
                "run_status": result.run_status,
            },
        )

    return result


# ---------------------------------------------------------------------------
# Phase 2 chunk 2 / Phase 3 — undo
# ---------------------------------------------------------------------------


@dataclass
class UndoResult:
    """Outcome of applying an undo to a previously-committed run."""

    run_id: str
    phase: str  # 'label' or 'archive'
    reverted: int = 0
    failed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    run_status: str = "committed"  # final status of the original run


def _resolve_label_id_for_undo(
    creds: Credentials, source: dict[str, Any]
) -> str:
    """Look up the target label id for an undo of a label run.

    Reads the canonical name from `runs.category_filter` → `gmail_label_name`
    rather than deriving from `set(after) - set(before)` of a single audit
    row, which could include incidental Gmail state changes (e.g., UNREAD
    flipped because the user opened the message between apply and undo).
    Read-only — does NOT call `ensure_labels_exist`, so a missing label
    surfaces as a clear error rather than being silently re-created.
    """
    raw_category = source.get("category_filter")
    try:
        category = Category(raw_category or "")
    except ValueError as exc:
        raise ValueError(
            f"Run {source['id']!r} category_filter {raw_category!r} is not a "
            "valid Category — cannot derive target label for undo."
        ) from exc
    label_name = gmail_label_name(category)
    existing = gmail_client.list_existing_labels(creds)
    label_id = existing.get(label_name)
    if label_id is None:
        raise ValueError(
            f"Cannot undo run {source['id']!r}: Gmail label {label_name!r} "
            "no longer exists. (It may have been deleted manually in Gmail.) "
            "Re-create the label and retry, or hand-edit affected messages "
            "in Gmail directly."
        )
    return label_id


def apply_undo_plan(
    *,
    creds: Credentials,
    conn: sqlite3.Connection,
    run_id: str,
    on_progress: Optional[Callable[[str, dict[str, Any]], None]] = None,
) -> UndoResult:
    """Reverse a previously-applied label or archive run.

    Walks the run's audit_log rows where ``status='applied'`` (only those
    are real Gmail mutations to reverse) and dispatches the inverse:

      * ``phase='label'``  — remove the gmailwiz/<category> label
      * ``phase='archive'`` — re-add the ``INBOX`` label

    Updates each row in-place to ``status='reverted'`` (audit-log
    schema's CHECK enum) and the run row to ``status='undone'``. The
    audit row's ``after_label_ids`` is overwritten with Gmail's actual
    response so the post-undo state is recorded faithfully.

    A run that has any rows in non-applied state (e.g., a previously
    failed mutation) is partially undone: the failed rows are left
    alone (their original failure stands), only ``applied`` rows are
    reverted. Run-level terminal status is decided from CUMULATIVE
    audit_log state:

      * If any rows still show ``status='applied'`` after this pass
        (e.g., undo failed for some), the run stays at its original
        status (``committed`` / ``partially_failed``) so a future
        ``apply_undo_plan`` call can retry the still-applied rows.
      * Only when every applicable row has been reverted does the run
        flip to ``undone``.
      * If zero rows reverted (every applicable row failed undo this
        pass and there were no prior reverts), the run also stays at
        its original status — same retry-friendly contract.
    """
    run = gw_db.get_run(conn, run_id)
    if run is None:
        raise ValueError(f"No run with id {run_id!r}")
    phase = run.get("phase")
    if phase not in {PHASE_LABEL, PHASE_ARCHIVE}:
        raise ValueError(
            f"Run {run_id!r} has phase={phase!r}; only 'label' and 'archive' "
            "runs can be undone."
        )
    status = run.get("status")
    if status not in {"committed", "partially_failed"}:
        raise ValueError(
            f"Run {run_id!r} has status={status!r}; only 'committed' and "
            "'partially_failed' runs can be undone."
        )

    # Action filter depends only on phase — derive it without any Gmail
    # call so the vacuous-apply guard below can short-circuit before
    # touching Gmail.
    action_filter = (
        ACTION_ADD_LABEL if phase == PHASE_LABEL else ACTION_ARCHIVE
    )

    audit_rows = gw_db.get_audit_entries(conn, run_id)
    appliable_rows = [
        r for r in audit_rows
        if r.get("status") == "applied" and r.get("action") == action_filter
    ]
    total = len(appliable_rows)

    result = UndoResult(run_id=run_id, phase=phase)

    if total == 0:
        # Nothing to undo (e.g., a `partially_failed` run with zero
        # applied rows, or a label run whose label was already removed
        # by a prior partial undo). Short-circuit BEFORE any Gmail call
        # — `_resolve_label_id_for_undo` would otherwise hit
        # `list_existing_labels` and could spuriously fail with
        # "label no longer exists" when there is literally nothing to
        # undo. Don't flip the run row.
        result.run_status = status
        if on_progress is not None:
            on_progress(
                "undo_complete",
                {"reverted": 0, "failed": 0, "run_status": status},
            )
        return result

    # Now we know there's work — resolve the target label id (label phase
    # only). Read-only `list_existing_labels` call; raises ValueError if
    # the label was manually deleted in Gmail.
    add_label_ids: list[str]
    remove_label_ids: list[str]
    if phase == PHASE_LABEL:
        # Defense-in-depth: a hand-edited DB row with category_filter
        # set to a non-classifiable value (e.g., 'unknown') should not
        # reach `_resolve_label_id_for_undo`, which would look up
        # `gmailwiz/unknown` in Gmail and produce a confusing
        # "no longer exists" error. Refuse here with a clear message
        # symmetric to `apply_label_plan`'s and `build_label_plan`'s
        # UNKNOWN guards.
        try:
            category = Category(run.get("category_filter") or "")
        except ValueError as exc:
            raise ValueError(
                f"Run {run_id!r} category_filter "
                f"{run.get('category_filter')!r} is not a valid Category — "
                "cannot derive target label for undo."
            ) from exc
        if category not in CLASSIFIABLE_CATEGORIES:
            raise ValueError(
                f"Run {run_id!r} has non-classifiable category "
                f"{category.value!r}; cannot undo a label run for UNKNOWN."
            )
        target_label_id = _resolve_label_id_for_undo(creds, run)
        add_label_ids = []
        remove_label_ids = [target_label_id]
    else:  # PHASE_ARCHIVE
        add_label_ids = [INBOX_LABEL_ID]
        remove_label_ids = []

    if on_progress is not None:
        on_progress(
            "undo_begin",
            {
                "total": total,
                "phase": phase,
                "add_label_ids": add_label_ids,
                "remove_label_ids": remove_label_ids,
            },
        )

    for i, row in enumerate(appliable_rows, start=1):
        msg_id = row["message_id"]
        try:
            response = gmail_client.modify_labels(
                creds,
                message_id=msg_id,
                add_label_ids=add_label_ids,
                remove_label_ids=remove_label_ids,
            )
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            # On undo failure, DON'T touch after_label_ids (its current
            # value is the post-apply state, which is still accurate
            # since the undo mutation didn't happen). Just record the
            # error; the row stays `applied`.
            gw_db.update_audit_entry(
                conn,
                audit_id=row["id"],
                status="applied",
                error=err,
            )
            result.failed += 1
            result.errors.append((msg_id, err))
            if on_progress is not None:
                on_progress(
                    "undo_failed",
                    {"index": i, "total": total, "message_id": msg_id, "error": err},
                )
            continue

        if "labelIds" not in response:
            err = (
                f"Gmail messages.modify response missing 'labelIds' "
                f"for {msg_id}; cannot record post-undo state. "
                f"Response: {response!r}"
            )
            gw_db.update_audit_entry(
                conn,
                audit_id=row["id"],
                status="applied",
                error=err,
            )
            result.failed += 1
            result.errors.append((msg_id, err))
            if on_progress is not None:
                on_progress(
                    "undo_failed",
                    {"index": i, "total": total, "message_id": msg_id, "error": err},
                )
            continue

        actual_after = list(response.get("labelIds") or [])
        try:
            gw_db.update_audit_entry(
                conn,
                audit_id=row["id"],
                status="reverted",
                after_label_ids=actual_after,
                # Clear any stale error string from a prior failed undo
                # attempt — a successful retry should not leave a row
                # showing both `status='reverted'` and a stale error.
                clear_error=True,
            )
        except Exception as exc:
            err = (
                f"{type(exc).__name__} updating audit row {row['id']} "
                f"after successful Gmail undo on {msg_id}: {exc} — "
                "Gmail was reverted but audit_log still says "
                "applied. Manual reconciliation required."
            )
            result.failed += 1
            result.errors.append((msg_id, err))
            if on_progress is not None:
                on_progress(
                    "undo_failed",
                    {"index": i, "total": total, "message_id": msg_id, "error": err},
                )
            continue

        result.reverted += 1
        if on_progress is not None:
            on_progress(
                "undo_done",
                {"index": i, "total": total, "message_id": msg_id},
            )

    # Run-level terminal status from CUMULATIVE audit_log state (mirrors
    # the apply-side cumulative pattern that Codex caught for label
    # in Phase 2 chunk 1). The reasons:
    #
    #   * If any rows still show `status='applied'` for our action
    #     filter, the undo is incomplete — leave the run at its
    #     original status so the user can retry. Flipping to `undone`
    #     here would make `apply_undo_plan` reject a retry attempt
    #     (precondition rejects any non-committed/non-partially_failed
    #     run), stranding the user with Gmail labels still applied
    #     and no automated path to clean them up.
    #   * If every row in our action filter is `reverted`, the run is
    #     fully undone — flip to `undone`.
    #   * If zero rows were reverted (every applicable row failed
    #     mid-loop — distinct from the vacuous case which short-
    #     circuits earlier), leave the run at its original status so
    #     a retry remains possible.
    cumulative = gw_db.get_audit_entries(conn, run_id)
    relevant_rows = [r for r in cumulative if r.get("action") == action_filter]
    cum_remaining_applied = sum(
        1 for r in relevant_rows if r.get("status") == "applied"
    )
    cum_reverted = sum(1 for r in relevant_rows if r.get("status") == "reverted")

    if cum_remaining_applied > 0 or cum_reverted == 0:
        result.run_status = status  # original (committed / partially_failed)
    else:
        gw_db.update_run_status(conn, run_id, "undone")
        result.run_status = "undone"

    if on_progress is not None:
        on_progress(
            "undo_complete",
            {
                "reverted": result.reverted,
                "failed": result.failed,
                "run_status": result.run_status,
            },
        )

    return result
