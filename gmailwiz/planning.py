"""Persisted execution plans for mutating commands.

A plan is a ``runs`` row plus N ``audit_log`` rows, all with ``status='planned'``.
Preview reads from this; commit walks the same rows and flips them. The whole
point is to prevent the dreaded "preview shows X but commit acts on Y" drift
caused by recomputing the candidate set against fresh Gmail state at commit
time.

Phase 2 implements label plans only. Archive is Phase 3.
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

    # Re-entry safety: if any row is still `planned` after the loop
    # (shouldn't normally happen — we only return early via vacuous-
    # apply, which is handled above), leave the run at `planned` so a
    # future apply can finish it. Don't flip `dry_run` either.
    if cum_planned > 0:
        result.run_status = "planned"
    elif cum_failed == 0:
        result.run_status = "committed"
    elif cum_applied == 0:
        result.run_status = "failed"
    else:
        result.run_status = "partially_failed"

    if result.run_status != "planned":
        # Flip dry_run to 0 alongside the terminal status — a
        # committed/partially_failed/failed run with dry_run=1 is
        # contradictory and would mislead any consumer that filtered
        # by `dry_run=0` to find real-world mutations.
        gw_db.update_run_status(conn, run_id, result.run_status, dry_run=False)

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
