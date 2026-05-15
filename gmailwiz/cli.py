"""click entry point for gmailwiz.

Two ways in:
  * ``python -m gmailwiz``           → interactive menu (the day-to-day UX)
  * ``python -m gmailwiz <command>`` → direct subcommands (for scripting)

Wired commands:
  * ``auth``    — run OAuth, print the authenticated email
  * ``report``  — read-only sender-grouped unread report (`--limit`, `--json`)
  * ``label``   — preview / commit a labeling run (Phase 2)

The menu and subcommands share the same internal helpers (`_run_auth`,
`_run_report`, `_run_label_plan`, `_run_label_commit`); the menu does **not**
shell out to subprocesses.
"""

from __future__ import annotations

import json as _json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import click

from gmailwiz import auth as gw_auth
from gmailwiz import db as gw_db
from gmailwiz import gmail_client
from gmailwiz import oneshot as gw_oneshot
from gmailwiz import planning
from gmailwiz.categories import CLASSIFIABLE_CATEGORIES, Category, gmail_label_name
from gmailwiz.classifier import (
    MissingAPIKeyError,
    SenderInput,
    SenderSample,
    classify_senders,
)
from gmailwiz.menu_text import find_option, render_menu


DEFAULT_REPORT_LIMIT = 100
DEFAULT_LABEL_LIMIT = 100
DEFAULT_ONESHOT_LIMIT = 1000


# ---------------------------------------------------------------------------
# Internal helpers — used by both subcommands and the menu
# ---------------------------------------------------------------------------


@dataclass
class _SenderRow:
    """One row in the rendered report."""

    email: str
    display_name: str
    unread: int
    category: Category
    newest_subject: str


def _run_auth(*, force_reauth: bool = False) -> Optional[str]:
    """Run (or refresh) OAuth, then print and return the authenticated email."""
    creds = gw_auth.get_credentials(force_reauth=force_reauth)
    email = gw_auth.get_authenticated_email(creds)
    if email:
        click.echo(f"Authenticated as: {email}")
        with gw_db.open_db() as conn:
            gw_db.set_app_state(conn, "last_auth_email", email)
    else:
        click.echo("Authenticated, but could not fetch the account email from userinfo.")
    return email


def _aggregate_senders(messages: list[dict]) -> tuple[
    list[SenderInput],
    dict[str, list[dict]],
    int,
]:
    """Group fetched messages by sender email.

    Returns ``(inputs, by_email, unparseable_count)``:

    - ``inputs`` — one ``SenderInput`` per unique parseable sender, ready for
      the classifier.
    - ``by_email`` — lowercased email → original message dicts (excludes
      messages with unparseable senders, so the report doesn't render a
      mystery row).
    - ``unparseable_count`` — number of messages whose ``From:`` header
      couldn't be parsed into a valid email address. Surfaced as a footer
      note in the report so the user knows some messages were dropped.
    """
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
                samples.append(SenderSample(subject=m.get("subject", ""), snippet=m.get("snippet", "")))
        inputs.append(SenderInput(email=email, display_name=display, samples=samples))
    return inputs, by_email, unparseable


def _internal_date_sort_key(m: dict) -> int:
    """Internal-date as int for sorting; 0 if missing/unparseable."""
    try:
        return int(m.get("internal_date") or 0)
    except (TypeError, ValueError):
        return 0


def _build_report_rows(
    by_email: dict[str, list[dict]],
    classifications: dict[str, Any],
) -> list[_SenderRow]:
    """Produce one `_SenderRow` per sender, sorted by unread count descending."""
    rows: list[_SenderRow] = []
    for email, msgs in by_email.items():
        newest = max(msgs, key=_internal_date_sort_key)
        result = classifications.get(email)
        category = getattr(result, "category", Category.UNKNOWN)
        # Pick the first non-empty display name across all messages from this
        # sender. Matches what `_aggregate_senders` shows the classifier — an
        # empty `sender_name` on the newest message shouldn't blank out a
        # display the user has actually seen on earlier messages.
        display = ""
        for m in msgs:
            name = m.get("sender_name") or ""
            if name:
                display = name
                break
        rows.append(
            _SenderRow(
                email=email,
                display_name=display,
                unread=len(msgs),
                category=category,
                newest_subject=newest.get("subject", "") or "",
            )
        )
    rows.sort(key=lambda r: (-r.unread, r.email))
    return rows


def _render_report_table(rows: list[_SenderRow]) -> str:
    """Render the report as a fixed-width table similar to the README sample.

    Returns an empty string when there are no rows — the caller is expected
    to print a contextual message (e.g. "all senders unparseable") since
    rows-empty doesn't necessarily mean messages-empty. We can't say "no
    unread messages found" here because the caller already short-circuited
    that case before reaching us.
    """
    if not rows:
        return ""

    sender_width = min(40, max(len("SENDER"), max(len(r.email) for r in rows)))
    cat_width = max(len("CATEGORY"), max(len(r.category.value) for r in rows))
    unread_width = max(len("UNREAD"), max(len(str(r.unread)) for r in rows))

    header = (
        f"{'SENDER':<{sender_width}}  "
        f"{'UNREAD':>{unread_width}}  "
        f"{'CATEGORY':<{cat_width}}  "
        f"NEWEST SUBJECT"
    )
    lines = [header]
    for r in rows:
        sender_cell = r.email if len(r.email) <= sender_width else r.email[: sender_width - 1] + "…"
        subject = r.newest_subject
        # Soft-cap subject to keep terminal output manageable; full data is in --json.
        if len(subject) > 60:
            subject = subject[:59] + "…"
        lines.append(
            f"{sender_cell:<{sender_width}}  "
            f"{r.unread:>{unread_width}}  "
            f"{r.category.value:<{cat_width}}  "
            f"{subject}"
        )
    return "\n".join(lines)


def _progress(msg: str, *, output_json: bool) -> None:
    """Print a progress note to stderr unless we're emitting machine-readable JSON.

    Going to stderr keeps `--json` output clean while still giving the user
    feedback during slow phases. Suppressed entirely under `--json` so scripts
    that consume the JSON aren't surprised by chatter on stderr.
    """
    if output_json:
        return
    click.echo(msg, err=True)


def _run_report(*, limit: int, output_json: bool) -> int:
    """Build and print the unread sender report. Returns process exit code."""
    if limit <= 0:
        click.echo("--limit must be greater than zero.", err=True)
        return 2

    creds = gw_auth.get_credentials()

    _progress(f"Fetching up to {limit} unread message IDs...", output_json=output_json)
    ids = gmail_client.list_unread_message_ids(creds, max_results=limit)
    if not ids:
        if output_json:
            # Keep the JSON shape stable across empty-vs-populated outcomes
            # so downstream scripts don't have to special-case it.
            click.echo(
                _json.dumps(
                    {
                        "run_id": None,
                        "message_count": 0,
                        "fetch_failure_count": 0,
                        "unparseable_sender_count": 0,
                        "classification_failures": [],
                        "senders": [],
                    },
                    indent=2,
                )
            )
        else:
            click.echo("No unread messages found in your inbox.")
        return 0

    _progress(f"Reading metadata for {len(ids)} messages...", output_json=output_json)
    messages = gmail_client.get_message_metadata(creds, ids)
    # If `list_unread_message_ids` returned IDs but `get_message_metadata`
    # produced nothing, every per-message fetch failed (revoked auth, quota,
    # systemic 5xx). Treat that as a hard failure rather than reporting an
    # empty inbox — otherwise the user thinks they have zero unread when
    # really we couldn't talk to Gmail.
    if not messages:
        click.echo(
            f"Failed to fetch metadata for any of the {len(ids)} message(s) Gmail listed. "
            "Likely auth revoked or transient API errors — see stderr for per-message details.",
            err=True,
        )
        return 3
    fetch_failure_count = len(ids) - len(messages)
    sender_inputs, by_email, unparseable_count = _aggregate_senders(messages)
    _progress(
        f"Found {len(sender_inputs)} unique senders. Classifying with Claude...",
        output_json=output_json,
    )

    def _classifier_progress(stage: str, info: dict) -> None:
        if stage == "cache_done":
            hits = info.get("hits", 0)
            todo = info.get("to_classify", 0)
            if hits and todo:
                _progress(
                    f"  {hits} cached, {todo} new to classify.",
                    output_json=output_json,
                )
            elif hits:
                _progress(f"  All {hits} senders already cached.", output_json=output_json)
            elif todo:
                _progress(f"  {todo} senders to classify (none cached yet).", output_json=output_json)
        elif stage == "batch_start":
            i, total, size = info["index"], info["total"], info["size"]
            if total > 1:
                _progress(
                    f"  Batch {i}/{total} ({size} senders)...",
                    output_json=output_json,
                )
        elif stage == "batch_done":
            err = info.get("error")
            if err:
                _progress(f"  Batch {info['index']}/{info['total']} failed: {err}", output_json=output_json)
        elif stage == "batch_truncated":
            _progress(
                f"  WARNING: Batch {info['index']}/{info['total']} hit max_tokens — "
                "some senders may have been omitted from the response. "
                "Reduce SENDERS_PER_BATCH or raise MAX_OUTPUT_TOKENS_PER_BATCH.",
                output_json=output_json,
            )

    with gw_db.open_db() as conn:
        # The discriminator between report runs and Phase-2/3 mutation runs is
        # the `phase` column (`report` vs `label`/`archive`/`undo`), NOT the
        # `status` column. So we keep `status` within the spec's enum
        # (`planned, committed, partially_failed, undone, failed`) — Phase 2
        # queries for committed mutations should always filter on phase too.
        run_id = gw_db.create_run(
            conn,
            phase="report",
            limit_count=limit,
            dry_run=True,
            status="planned",
        )
        try:
            classifications = classify_senders(
                sender_inputs,
                conn=conn,
                on_progress=_classifier_progress,
            )
        except BaseException:
            # Mark the run as failed before re-raising so an unexpected exit
            # doesn't leave a `planned` row dangling for a Phase-2 query to
            # mistake for actionable work.
            #
            # Bookkeeping must never shadow the original exception: cs.md
            # mandates raw error pass-through. `update_run_status` raises
            # KeyError if the row doesn't exist (e.g., a KeyboardInterrupt
            # landed inside `create_run`'s `with conn:` and rolled back
            # the INSERT). Swallow any error from the bookkeeping call
            # specifically so the original exception always reaches the
            # caller.
            try:
                gw_db.update_run_status(conn, run_id, "failed")
            except Exception:
                pass
            raise
        else:
            # Reflect partial failures in the status. ANY gap counts as
            # partial failure — whether the gap was a Gmail metadata fetch
            # that 5xx'd, an unparseable From: header, or a classification
            # failure. Otherwise a 1-of-100 successful fetch would log as
            # `committed` and a Phase-2 query would treat the report as
            # authoritative.
            had_classification_failure = any(
                getattr(r, "source", "") == "unknown" for r in classifications.values()
            )
            had_partial = (
                fetch_failure_count > 0
                or unparseable_count > 0
                or had_classification_failure
            )
            terminal = "partially_failed" if had_partial else "committed"
            gw_db.update_run_status(conn, run_id, terminal)

    rows = _build_report_rows(by_email, classifications)
    failed_classifications = [
        (email, getattr(result, "error", None))
        for email, result in classifications.items()
        if getattr(result, "source", "") == "unknown"
    ]

    if output_json:
        payload = {
            "run_id": run_id,
            "message_count": len(messages),
            "fetch_failure_count": fetch_failure_count,
            "unparseable_sender_count": unparseable_count,
            "classification_failures": [
                {"email": email, "error": err} for email, err in failed_classifications
            ],
            "senders": [
                {
                    "email": r.email,
                    "display_name": r.display_name,
                    "unread": r.unread,
                    "category": r.category.value,
                    "newest_subject": r.newest_subject,
                }
                for r in rows
            ],
        }
        click.echo(_json.dumps(payload, indent=2))
    else:
        if rows:
            click.echo(_render_report_table(rows))
            click.echo("")
        else:
            # `messages` is non-empty (we'd have early-returned otherwise),
            # but every From: header was unparseable. Don't render an empty
            # table or the misleading "no unread mail" line.
            click.echo(
                f"All {len(messages)} fetched message(s) had unparseable From: headers — "
                "nothing to display in the report.",
            )
            click.echo("")
        click.echo(f"{len(messages)} unread messages across {len(rows)} senders. (run id: {run_id})")
        if fetch_failure_count:
            click.echo(
                f"Note: {fetch_failure_count} of {len(ids)} message(s) couldn't be "
                "fetched from Gmail (see stderr for details)."
            )
        if unparseable_count:
            click.echo(
                f"Note: {unparseable_count} message(s) had unparseable From: headers and were skipped."
            )
        if failed_classifications:
            click.echo(
                f"Note: {len(failed_classifications)} sender(s) could not be classified by Claude:"
            )
            # Group identical errors so a single failed batch doesn't list 25
            # near-identical lines.
            by_error: dict[str, list[str]] = defaultdict(list)
            for email, err in failed_classifications:
                by_error[err or "(no error message)"].append(email)
            for err, emails in by_error.items():
                preview = ", ".join(emails[:3])
                more = f" (+{len(emails) - 3} more)" if len(emails) > 3 else ""
                click.echo(f"  - {err}: {preview}{more}")
    return 0


# ---------------------------------------------------------------------------
# Phase 2 — label plan / commit
# ---------------------------------------------------------------------------


def _print_label_plan_preview(plan: planning.LabelPlan, *, output_json: bool) -> None:
    """Render a built `LabelPlan` for the user. JSON or table form."""
    if output_json:
        payload = {
            "run_id": plan.run_id,
            "category": plan.category.value,
            "label_name": plan.label_name,
            "target_label_id": plan.target_label_id,
            "fetched_message_count": plan.fetched_message_count,
            "fetch_failure_count": plan.fetch_failure_count,
            "candidates": [
                {
                    "message_id": c.message_id,
                    "thread_id": c.thread_id,
                    "sender_email": c.sender_email,
                    "subject": c.subject,
                    "before_label_ids": c.before_label_ids,
                    "after_label_ids": c.after_label_ids,
                }
                for c in plan.candidates
            ],
            "skipped": {
                "unparseable": plan.skipped_unparseable,
                "uncached": plan.skipped_uncached,
                "unknown": plan.skipped_unknown,
                "other_category": plan.skipped_other_category,
                "already_labeled": plan.skipped_already_labeled,
            },
        }
        click.echo(_json.dumps(payload, indent=2))
        return

    click.echo("")
    click.echo(
        f"Plan: add label '{plan.label_name}' to {len(plan.candidates)} message(s)."
    )
    click.echo(f"Run id: {plan.run_id}")
    click.echo("")

    if plan.candidates:
        sender_w = min(
            40,
            max(len("SENDER"), max(len(c.sender_email) for c in plan.candidates)),
        )
        msgid_w = max(len("MESSAGE ID"), max(len(c.message_id) for c in plan.candidates))
        click.echo(f"{'MESSAGE ID':<{msgid_w}}  {'SENDER':<{sender_w}}  SUBJECT")
        for c in plan.candidates:
            subj = c.subject if len(c.subject) <= 60 else c.subject[:59] + "…"
            sender_cell = (
                c.sender_email
                if len(c.sender_email) <= sender_w
                else c.sender_email[: sender_w - 1] + "…"
            )
            click.echo(f"{c.message_id:<{msgid_w}}  {sender_cell:<{sender_w}}  {subj}")
        click.echo("")

    if plan.fetch_failure_count:
        click.echo(
            f"Note: {plan.fetch_failure_count} message(s) listed by Gmail "
            "could not be fetched (see stderr). The plan may be incomplete; "
            "re-run preview to retry."
        )
        click.echo("")

    if plan.total_skipped:
        click.echo(f"Skipped {plan.total_skipped} message(s):")
        if plan.skipped_unparseable:
            click.echo(f"  - {plan.skipped_unparseable} with unparseable From: header")
        if plan.skipped_uncached:
            click.echo(
                f"  - {plan.skipped_uncached} from senders not yet classified "
                "(run option 1 / `report` to populate the cache)"
            )
        if plan.skipped_unknown:
            click.echo(f"  - {plan.skipped_unknown} from senders classified as 'unknown'")
        if plan.skipped_other_category:
            click.echo(
                f"  - {plan.skipped_other_category} from senders classified as "
                "a different category"
            )
        if plan.skipped_already_labeled:
            click.echo(
                f"  - {plan.skipped_already_labeled} already carry the "
                f"'{plan.label_name}' label"
            )
        click.echo("")

    if plan.candidates:
        click.echo(
            f"Apply with: gmailwiz label --commit --run-id {plan.run_id}"
        )
        click.echo(
            "(Or pick this run from the menu's "
            "'Apply a previewed labeling run' option.)"
        )
    else:
        click.echo("Nothing to label. No run to apply.")


def _run_label_plan(
    *,
    category: Category,
    limit: int,
    output_json: bool,
) -> int:
    """Build and persist a label plan, then print the preview."""
    if limit <= 0:
        click.echo("--limit must be greater than zero.", err=True)
        return 2
    if category not in CLASSIFIABLE_CATEGORIES:
        click.echo(
            f"Cannot plan for non-classifiable category {category.value!r}.",
            err=True,
        )
        return 2

    creds = gw_auth.get_credentials()
    _progress(
        f"Building label plan for '{category.value}' (scan up to {limit} unread)...",
        output_json=output_json,
    )
    with gw_db.open_db() as conn:
        plan = planning.build_label_plan(
            creds=creds,
            conn=conn,
            category=category,
            limit=limit,
        )
    _print_label_plan_preview(plan, output_json=output_json)
    return 0


def _label_commit_progress(stage: str, info: dict[str, Any], *, output_json: bool) -> None:
    """Stderr progress output for `apply_label_plan`. Suppressed under --json."""
    if output_json:
        return
    if stage == "apply_begin":
        click.echo(
            f"Applying label '{info['label_name']}' to {info['total']} message(s)...",
            err=True,
        )
    elif stage == "apply_done":
        click.echo(
            f"  [{info['index']}/{info['total']}] applied to {info['message_id']}",
            err=True,
        )
    elif stage == "apply_failed":
        click.echo(
            f"  [{info['index']}/{info['total']}] FAILED on {info['message_id']}: "
            f"{info['error']}",
            err=True,
        )


def _run_label_commit(
    *,
    run_id: str,
    output_json: bool,
    assume_yes: bool = False,
) -> int:
    """Apply a previously-built label plan by run id."""
    # Defense-in-depth: `label_cmd` already rejects --commit --json without
    # --yes upstream, but any other caller (menu, future code) reaching this
    # function with `output_json=True, assume_yes=False` would have skipped
    # the prompt silently and mutated Gmail with no confirmation. Refuse.
    if output_json and not assume_yes:
        click.echo(
            "Refusing to commit under JSON output without explicit --yes "
            "(prompt would corrupt the JSON payload).",
            err=True,
        )
        return 2

    # Validate the run row + count planned audit rows BEFORE triggering
    # OAuth — a typo'd `--run-id` shouldn't force a needless token
    # refresh (and a possible browser flow if the token is expired).
    with gw_db.open_db() as conn:
        # Auto-heal a stale runs.status before reading. Covers the
        # rare-but-real case where a prior apply's `update_run_status`
        # write failed AFTER Gmail mutations had been recorded as
        # terminal in audit_log — without heal, the run row would stay
        # `planned` and lock the user out of both retry (helper short-
        # circuits on no-planned-rows) AND undo (rejects non-terminal
        # status). Audit log is the source of truth.
        run = planning.heal_run_status_from_audit_log(conn, run_id)
        if run is None:
            click.echo(f"No run with id {run_id!r}.", err=True)
            return 2
        if run.get("phase") != "label":
            click.echo(
                f"Run {run_id!r} has phase={run.get('phase')!r}, expected 'label'.",
                err=True,
            )
            return 2
        if run.get("status") != "planned":
            click.echo(
                f"Run {run_id!r} has status={run.get('status')!r}; only "
                "'planned' runs can be applied.",
                err=True,
            )
            return 2
        # Defense-in-depth pre-OAuth UNKNOWN refusal: `apply_label_plan`
        # rejects non-classifiable category runs, but doing it BEFORE
        # `gw_auth.get_credentials` saves a needless token refresh on
        # corrupted DB rows.
        raw_category = run.get("category_filter")
        try:
            cat = Category(raw_category or "")
        except ValueError:
            click.echo(
                f"Run {run_id!r} category_filter {raw_category!r} is not a "
                "valid Category — cannot apply.",
                err=True,
            )
            return 2
        if cat not in CLASSIFIABLE_CATEGORIES:
            click.echo(
                f"Run {run_id!r} has non-classifiable category "
                f"{cat.value!r}; cannot apply a label run for UNKNOWN.",
                err=True,
            )
            return 2

        # Filter on action='add_label' too, so the count matches the set
        # `apply_label_plan` will actually process (a hand-edited row with a
        # foreign action would otherwise inflate this count and mislead the
        # user about how many messages will be touched).
        planned_count = gw_db.count_audit_entries(
            conn, run_id, status="planned", action="add_label"
        )
        if planned_count == 0:
            if output_json:
                # Stay JSON-shaped under --json so consumers don't have
                # to special-case stdout-vs-stderr or text-vs-payload.
                click.echo(
                    _json.dumps(
                        {
                            "run_id": run_id,
                            "label_id": "",
                            "applied": 0,
                            "failed": 0,
                            "errors": [],
                            "run_status": "planned",
                            "note": "no planned audit entries — nothing to do",
                        },
                        indent=2,
                    )
                )
            else:
                click.echo(
                    f"Run {run_id!r} has no planned audit entries — nothing to do."
                )
            return 0

        if not assume_yes and not output_json:
            # Resolve the label name through the canonical mapping so this
            # string can never drift from `gmail_label_name`.
            raw_category = run.get("category_filter")
            try:
                label_name = gmail_label_name(Category(raw_category or ""))
            except ValueError:
                # Defensive: a hand-edited DB could land here. Render the
                # raw column value rather than crashing — and avoid the
                # ugly `gmailwiz/None` string when the column is NULL by
                # showing `<unknown>` instead.
                label_name = (
                    f"gmailwiz/{raw_category}"
                    if raw_category
                    else "gmailwiz/<unknown>"
                )
            click.echo(
                f"About to add label '{label_name}' to "
                f"{planned_count} message(s) in Gmail."
            )
            confirm = _prompt(
                "Type 'yes' to proceed (anything else cancels): "
            ).strip().lower()
            if confirm != "yes":
                click.echo("Cancelled.")
                # Cancel is not an error — user explicitly chose not to
                # mutate. Returning 0 keeps the documented exit-code set
                # (0 / 2 / 4 / 5) tight.
                return 0

        # Validation passed and the user confirmed — now do the OAuth dance.
        # Doing it here (rather than at the top of the function) avoids a
        # needless token refresh / browser flow when the user typo'd their
        # `--run-id`.
        creds = gw_auth.get_credentials()
        result = planning.apply_label_plan(
            creds=creds,
            conn=conn,
            run_id=run_id,
            on_progress=lambda stage, info: _label_commit_progress(
                stage, info, output_json=output_json
            ),
        )

    if output_json:
        click.echo(
            _json.dumps(
                {
                    "run_id": result.run_id,
                    "label_id": result.label_id,
                    "applied": result.applied,
                    "failed": result.failed,
                    "errors": [{"message_id": mid, "error": err} for mid, err in result.errors],
                    "run_status": result.run_status,
                },
                indent=2,
            )
        )
    else:
        click.echo("")
        click.echo(
            f"Run {result.run_id}: {result.applied} applied, "
            f"{result.failed} failed. status={result.run_status}"
        )
        if result.errors:
            click.echo("Failures:")
            for mid, err in result.errors:
                click.echo(f"  - {mid}: {err}")

    # Exit code:
    #   committed              → 0
    #   planned                → 0  (vacuous: nothing actually happened.
    #                                Reachable only if the audit rows
    #                                fail the action='add_label' filter
    #                                inside apply_label_plan despite
    #                                count_audit_entries(status='planned')
    #                                being non-zero — i.e., a hand-edited
    #                                archive row in a label run. No Gmail
    #                                mutation occurred; not a failure.)
    #   partially_failed       → 4 (some succeeded; signal partial)
    #   failed                 → 5 (none succeeded)
    if result.run_status in {"committed", "planned"}:
        return 0
    if result.run_status == "partially_failed":
        return 4
    return 5


# ---------------------------------------------------------------------------
# Phase 3 — archive plan / commit
# ---------------------------------------------------------------------------


def _print_archive_plan_preview(
    plan: planning.ArchivePlan, *, output_json: bool
) -> None:
    """Render a built `ArchivePlan` for the user. JSON or table form."""
    if output_json:
        payload = {
            "run_id": plan.run_id,
            "source_run_id": plan.source_run_id,
            "category": plan.source_category.value,
            "fetched_message_count": plan.fetched_message_count,
            "fetch_failure_count": plan.fetch_failure_count,
            "candidates": [
                {
                    "message_id": c.message_id,
                    "thread_id": c.thread_id,
                    "sender_email": c.sender_email,
                    "subject": c.subject,
                    "before_label_ids": c.before_label_ids,
                    "after_label_ids": c.after_label_ids,
                }
                for c in plan.candidates
            ],
            "skipped": {
                "already_archived": plan.skipped_already_archived,
                "source_failed": plan.skipped_source_failed,
            },
        }
        click.echo(_json.dumps(payload, indent=2))
        return

    click.echo("")
    click.echo(
        f"Plan: archive (remove INBOX from) {len(plan.candidates)} message(s) "
        f"from label run {plan.source_run_id} ({plan.source_category.value})."
    )
    click.echo(f"Archive run id: {plan.run_id}")
    click.echo("")

    if plan.candidates:
        sender_w = min(
            40,
            max(len("SENDER"), max(len(c.sender_email) for c in plan.candidates)),
        )
        msgid_w = max(
            len("MESSAGE ID"), max(len(c.message_id) for c in plan.candidates)
        )
        click.echo(f"{'MESSAGE ID':<{msgid_w}}  {'SENDER':<{sender_w}}  SUBJECT")
        for c in plan.candidates:
            subj = c.subject if len(c.subject) <= 60 else c.subject[:59] + "…"
            sender_cell = (
                c.sender_email
                if len(c.sender_email) <= sender_w
                else c.sender_email[: sender_w - 1] + "…"
            )
            click.echo(f"{c.message_id:<{msgid_w}}  {sender_cell:<{sender_w}}  {subj}")
        click.echo("")

    if plan.fetch_failure_count:
        click.echo(
            f"Note: {plan.fetch_failure_count} message(s) listed by the source "
            "run could not be fetched (deleted in Gmail since labeling, or "
            "transient API error). They will not be archived."
        )
        click.echo("")

    if plan.total_skipped:
        click.echo(f"Skipped {plan.total_skipped} message(s):")
        if plan.skipped_already_archived:
            click.echo(
                f"  - {plan.skipped_already_archived} already archived "
                "(no INBOX label, presumably user-archived after labeling)"
            )
        if plan.skipped_source_failed:
            click.echo(
                f"  - {plan.skipped_source_failed} from source run rows that "
                "didn't successfully apply (failed/reverted)"
            )
        click.echo("")

    if plan.candidates:
        click.echo(
            f"Apply with: gmailwiz archive --commit --run-id {plan.run_id}"
        )
        click.echo(
            "(Or pick this run from the menu's "
            "'Apply a previewed archive run' option.)"
        )
    else:
        click.echo("Nothing to archive. No run to apply.")


def _run_archive_plan(
    *, source_run_id: str, output_json: bool
) -> int:
    """Build and persist an archive plan, then print the preview."""
    # Validate the source label run BEFORE triggering OAuth — a typo'd
    # `--from-run-id` shouldn't force a needless token refresh / browser
    # flow. Mirrors the pattern in `_run_label_commit`,
    # `_run_archive_commit`, and `_run_undo`.
    with gw_db.open_db() as conn:
        # Heal the source run's status before reading — a label run
        # whose `update_run_status` post-apply failed would otherwise
        # be invisible as an archive source (status='planned' fails
        # the committed/partially_failed check).
        source = planning.heal_run_status_from_audit_log(conn, source_run_id)
        if source is None:
            click.echo(f"No run with id {source_run_id!r}.", err=True)
            return 2
        if source.get("phase") != planning.PHASE_LABEL:
            click.echo(
                f"Run {source_run_id!r} has phase={source.get('phase')!r}; "
                "archive source must be a 'label' run.",
                err=True,
            )
            return 2
        if source.get("status") not in {"committed", "partially_failed"}:
            click.echo(
                f"Run {source_run_id!r} has status={source.get('status')!r}; "
                "archive source must be 'committed' or 'partially_failed'.",
                err=True,
            )
            return 2
        # Also validate the category_filter parses BEFORE OAuth — a
        # corrupted source row would otherwise pass phase/status but
        # fail inside `build_archive_plan` after the user has paid the
        # OAuth round-trip cost.
        raw_category = source.get("category_filter")
        try:
            cat = Category(raw_category or "")
        except ValueError:
            click.echo(
                f"Run {source_run_id!r} category_filter "
                f"{raw_category!r} is not a valid Category — cannot "
                "use as archive source.",
                err=True,
            )
            return 2
        # Match the menu's option 6 picker filter — UNKNOWN sources
        # never drive mutations per the Phase 2 rules. Defense-in-depth
        # symmetric with `_run_label_commit` and `_run_undo`.
        if cat not in CLASSIFIABLE_CATEGORIES:
            click.echo(
                f"Run {source_run_id!r} has non-classifiable category "
                f"{cat.value!r}; cannot use as archive source.",
                err=True,
            )
            return 2
        # Source must have at least one applied audit row. Otherwise
        # `build_archive_plan` would create an empty archive run row
        # AND pay the OAuth cost. Cheap pre-check saves both. Acceptable
        # output: rc=0 (no work to do is not a failure) with a
        # human-readable message; in JSON mode, emit a structured shape
        # consistent with other "nothing to do" returns.
        applied_source_count = gw_db.count_audit_entries(
            conn,
            source_run_id,
            status="applied",
            action=planning.ACTION_ADD_LABEL,
        )
        if applied_source_count == 0:
            if output_json:
                click.echo(
                    _json.dumps(
                        {
                            "run_id": None,
                            "source_run_id": source_run_id,
                            "candidates": [],
                            "fetched_message_count": 0,
                            "fetch_failure_count": 0,
                            "skipped": {
                                "already_archived": 0,
                                "source_failed": 0,
                            },
                            "note": (
                                "source label run has no applied audit rows — "
                                "nothing to archive"
                            ),
                        },
                        indent=2,
                    )
                )
            else:
                click.echo(
                    f"Source label run {source_run_id} has no applied "
                    "audit rows — nothing to archive."
                )
            return 0

        creds = gw_auth.get_credentials()
        _progress(
            f"Building archive plan from label run {source_run_id}...",
            output_json=output_json,
        )
        plan = planning.build_archive_plan(
            creds=creds, conn=conn, source_run_id=source_run_id
        )
    _print_archive_plan_preview(plan, output_json=output_json)
    return 0


def _archive_commit_progress(
    stage: str, info: dict[str, Any], *, output_json: bool
) -> None:
    if output_json:
        return
    if stage == "apply_begin":
        click.echo(
            f"Archiving {info['total']} message(s) (removing INBOX)...",
            err=True,
        )
    elif stage == "apply_done":
        click.echo(
            f"  [{info['index']}/{info['total']}] archived {info['message_id']}",
            err=True,
        )
    elif stage == "apply_failed":
        click.echo(
            f"  [{info['index']}/{info['total']}] FAILED on "
            f"{info['message_id']}: {info['error']}",
            err=True,
        )


def _run_archive_commit(
    *, run_id: str, output_json: bool, assume_yes: bool = False
) -> int:
    """Apply a previously-built archive plan by run id.

    Note: no UNKNOWN-category guard here (unlike `_run_label_commit` and
    `_run_undo` for label phase). Archive operates on a fixed set of
    `message_ids` recorded in the audit_log and removes ``INBOX`` —
    the operation is not category-derived, so a corrupted
    ``category_filter`` is purely informational and doesn't break apply.
    """
    if output_json and not assume_yes:
        click.echo(
            "Refusing to commit under JSON output without explicit --yes "
            "(prompt would corrupt the JSON payload).",
            err=True,
        )
        return 2

    with gw_db.open_db() as conn:
        # Auto-heal a stale runs.status before reading. Covers the
        # rare-but-real case where a prior apply's `update_run_status`
        # write failed AFTER Gmail mutations had been recorded as
        # terminal in audit_log — without heal, the run row would stay
        # `planned` and lock the user out of both retry (helper short-
        # circuits on no-planned-rows) AND undo (rejects non-terminal
        # status). Audit log is the source of truth.
        run = planning.heal_run_status_from_audit_log(conn, run_id)
        if run is None:
            click.echo(f"No run with id {run_id!r}.", err=True)
            return 2
        if run.get("phase") != planning.PHASE_ARCHIVE:
            click.echo(
                f"Run {run_id!r} has phase={run.get('phase')!r}, expected 'archive'.",
                err=True,
            )
            return 2
        if run.get("status") != "planned":
            click.echo(
                f"Run {run_id!r} has status={run.get('status')!r}; only "
                "'planned' runs can be applied.",
                err=True,
            )
            return 2

        planned_count = gw_db.count_audit_entries(
            conn, run_id, status="planned", action=planning.ACTION_ARCHIVE
        )
        if planned_count == 0:
            if output_json:
                click.echo(
                    _json.dumps(
                        {
                            "run_id": run_id,
                            "applied": 0,
                            "failed": 0,
                            "errors": [],
                            "run_status": "planned",
                            "note": "no planned audit entries — nothing to do",
                        },
                        indent=2,
                    )
                )
            else:
                click.echo(
                    f"Run {run_id!r} has no planned audit entries — nothing to do."
                )
            return 0

        if not assume_yes and not output_json:
            click.echo(
                f"About to remove INBOX from {planned_count} message(s) in Gmail."
            )
            click.echo(
                "(Messages disappear from Inbox view but stay in All Mail. "
                "Reversible via menu option 4.)"
            )
            confirm = _prompt(
                "Type 'yes' to proceed (anything else cancels): "
            ).strip().lower()
            if confirm != "yes":
                click.echo("Cancelled.")
                return 0

        creds = gw_auth.get_credentials()
        result = planning.apply_archive_plan(
            creds=creds,
            conn=conn,
            run_id=run_id,
            on_progress=lambda stage, info: _archive_commit_progress(
                stage, info, output_json=output_json
            ),
        )

    if output_json:
        click.echo(
            _json.dumps(
                {
                    "run_id": result.run_id,
                    "applied": result.applied,
                    "failed": result.failed,
                    "errors": [
                        {"message_id": mid, "error": err} for mid, err in result.errors
                    ],
                    "run_status": result.run_status,
                },
                indent=2,
            )
        )
    else:
        click.echo("")
        click.echo(
            f"Run {result.run_id}: {result.applied} archived, "
            f"{result.failed} failed. status={result.run_status}"
        )
        if result.errors:
            click.echo("Failures:")
            for mid, err in result.errors:
                click.echo(f"  - {mid}: {err}")

    if result.run_status in {"committed", "planned"}:
        return 0
    if result.run_status == "partially_failed":
        return 4
    return 5


# ---------------------------------------------------------------------------
# Phase 2 chunk 2 / Phase 3 — undo
# ---------------------------------------------------------------------------


def _undo_progress(
    stage: str, info: dict[str, Any], *, output_json: bool
) -> None:
    if output_json:
        return
    if stage == "undo_begin":
        click.echo(
            f"Undoing {info['total']} {info['phase']} mutation(s)...",
            err=True,
        )
    elif stage == "undo_done":
        click.echo(
            f"  [{info['index']}/{info['total']}] reverted {info['message_id']}",
            err=True,
        )
    elif stage == "undo_failed":
        click.echo(
            f"  [{info['index']}/{info['total']}] FAILED on "
            f"{info['message_id']}: {info['error']}",
            err=True,
        )


def _run_undo(
    *, run_id: str, output_json: bool, assume_yes: bool = False
) -> int:
    """Reverse a previously-applied label or archive run."""
    if output_json and not assume_yes:
        click.echo(
            "Refusing to undo under JSON output without explicit --yes "
            "(prompt would corrupt the JSON payload).",
            err=True,
        )
        return 2

    with gw_db.open_db() as conn:
        # Auto-heal a stale runs.status before reading. Covers the
        # rare-but-real case where a prior apply's `update_run_status`
        # write failed AFTER Gmail mutations had been recorded as
        # terminal in audit_log — without heal, the run row would stay
        # `planned` and lock the user out of both retry (helper short-
        # circuits on no-planned-rows) AND undo (rejects non-terminal
        # status). Audit log is the source of truth.
        run = planning.heal_run_status_from_audit_log(conn, run_id)
        if run is None:
            click.echo(f"No run with id {run_id!r}.", err=True)
            return 2
        if run.get("phase") not in {planning.PHASE_LABEL, planning.PHASE_ARCHIVE}:
            click.echo(
                f"Run {run_id!r} has phase={run.get('phase')!r}; "
                "only label and archive runs can be undone.",
                err=True,
            )
            return 2
        if run.get("status") not in {"committed", "partially_failed"}:
            click.echo(
                f"Run {run_id!r} has status={run.get('status')!r}; only "
                "'committed' and 'partially_failed' runs can be undone.",
                err=True,
            )
            return 2
        # Defense-in-depth pre-OAuth UNKNOWN refusal — only relevant
        # for label runs (archive runs don't need a label_name lookup,
        # so UNKNOWN is harmless there). `apply_undo_plan` rejects this
        # too; doing it before OAuth saves a needless token refresh on
        # corrupted DB rows.
        if run.get("phase") == planning.PHASE_LABEL:
            raw_category = run.get("category_filter")
            try:
                cat = Category(raw_category or "")
            except ValueError:
                click.echo(
                    f"Run {run_id!r} category_filter {raw_category!r} is "
                    "not a valid Category — cannot undo.",
                    err=True,
                )
                return 2
            if cat not in CLASSIFIABLE_CATEGORIES:
                click.echo(
                    f"Run {run_id!r} has non-classifiable category "
                    f"{cat.value!r}; cannot undo a label run for UNKNOWN.",
                    err=True,
                )
                return 2

        # Count rows that will actually be reverted (action depends on phase).
        action_filter = (
            planning.ACTION_ADD_LABEL
            if run.get("phase") == planning.PHASE_LABEL
            else planning.ACTION_ARCHIVE
        )
        applied_count = gw_db.count_audit_entries(
            conn, run_id, status="applied", action=action_filter
        )
        if applied_count == 0:
            if output_json:
                click.echo(
                    _json.dumps(
                        {
                            "run_id": run_id,
                            "phase": run.get("phase"),
                            "reverted": 0,
                            "failed": 0,
                            "errors": [],
                            "run_status": run.get("status"),
                            "note": "no applied audit entries — nothing to undo",
                        },
                        indent=2,
                    )
                )
            else:
                click.echo(
                    f"Run {run_id!r} has no applied audit entries — "
                    "nothing to undo."
                )
            return 0

        if not assume_yes and not output_json:
            phase = run.get("phase")
            if phase == planning.PHASE_LABEL:
                # Resolve the label name through the canonical mapping.
                raw_category = run.get("category_filter")
                try:
                    label_name = gmail_label_name(Category(raw_category or ""))
                except ValueError:
                    label_name = (
                        f"gmailwiz/{raw_category}"
                        if raw_category
                        else "gmailwiz/<unknown>"
                    )
                click.echo(
                    f"About to remove label '{label_name}' from "
                    f"{applied_count} message(s) in Gmail."
                )
            else:
                click.echo(
                    f"About to re-add INBOX to {applied_count} previously-"
                    "archived message(s) in Gmail."
                )
            confirm = _prompt(
                "Type 'yes' to proceed (anything else cancels): "
            ).strip().lower()
            if confirm != "yes":
                click.echo("Cancelled.")
                return 0

        creds = gw_auth.get_credentials()
        result = planning.apply_undo_plan(
            creds=creds,
            conn=conn,
            run_id=run_id,
            on_progress=lambda stage, info: _undo_progress(
                stage, info, output_json=output_json
            ),
        )

    if output_json:
        click.echo(
            _json.dumps(
                {
                    "run_id": result.run_id,
                    "phase": result.phase,
                    "reverted": result.reverted,
                    "failed": result.failed,
                    "errors": [
                        {"message_id": mid, "error": err} for mid, err in result.errors
                    ],
                    "run_status": result.run_status,
                },
                indent=2,
            )
        )
    else:
        click.echo("")
        click.echo(
            f"Run {result.run_id}: {result.reverted} reverted, "
            f"{result.failed} failed. status={result.run_status}"
        )
        if result.errors:
            click.echo("Failures:")
            for mid, err in result.errors:
                click.echo(f"  - {mid}: {err}")

    # Exit code (mirrors apply_label_plan / apply_archive_plan):
    #   undone (every applicable row reverted)  → 0
    #   partial revert (some reverted, some failed)  → 4
    #   no reverts and >=1 failure (all-failed undo)  → 5
    #   anything else (defensive, unreachable)  → 0
    if result.run_status == "undone":
        return 0
    if result.reverted == 0 and result.failed > 0:
        return 5
    if result.failed > 0:
        return 4
    return 0


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------


def _prompt(prompt_text: str) -> str:
    """``input()`` wrapper that turns Ctrl-D / Ctrl-C into a clean quit.

    The plan requires every prompt to handle Ctrl-C cleanly without orphaning
    a partially-built run. Catching KeyboardInterrupt here means a single
    Ctrl-C at any prompt is treated the same as typing ``q``: it cancels the
    current action and returns to the menu (or exits, if at the top-level
    prompt).
    """
    try:
        return input(prompt_text)
    except (EOFError, KeyboardInterrupt):
        # Move to a fresh line so the next prompt isn't appended to a `^C`.
        click.echo("")
        return "q"


def _menu_show_report() -> None:
    """Menu option 1: prompt for a limit, then run the read-only report."""
    opt = find_option("1")
    if opt:
        click.echo("")
        click.echo(opt.detail)
        click.echo("")
    raw = _prompt(
        f"How many unread messages to scan? [{DEFAULT_REPORT_LIMIT}] (or 'q' to cancel) "
    ).strip()
    if raw.lower() in {"q", "quit"}:
        return
    if not raw:
        limit = DEFAULT_REPORT_LIMIT
    else:
        try:
            limit = int(raw)
        except ValueError:
            click.echo(f"'{raw}' is not a number; using default ({DEFAULT_REPORT_LIMIT}).")
            limit = DEFAULT_REPORT_LIMIT
        else:
            if limit <= 0:
                click.echo(
                    f"Limit must be positive; using default ({DEFAULT_REPORT_LIMIT})."
                )
                limit = DEFAULT_REPORT_LIMIT

    try:
        _run_report(limit=limit, output_json=False)
    except KeyboardInterrupt:
        # Ctrl-C *during* the report (mid-API-call) shouldn't show a traceback
        # to an interactive user. `_run_report`'s BaseException handler has
        # already marked the run as `failed` in the DB before re-raising.
        click.echo("\nCancelled.")
    except Exception as exc:  # surface raw errors per cs.md
        click.echo(f"Report failed: {type(exc).__name__}: {exc}", err=True)


def _menu_pick_int(prompt_text: str, *, lo: int, hi: int) -> Optional[int]:
    """Prompt for an integer in ``[lo, hi]``. Returns ``None`` on cancel.

    Used by the menu pickers. Empty input or ``q``/``quit`` cancels;
    out-of-range or non-numeric input prints a note and also cancels (the
    user is one keystroke from re-opening the picker — no recovery loop).
    """
    raw = _prompt(prompt_text).strip().lower()
    if not raw or raw in {"q", "quit"}:
        return None
    try:
        idx = int(raw)
    except ValueError:
        click.echo(f"'{raw}' is not a number; cancelled.")
        return None
    if not (lo <= idx <= hi):
        click.echo(f"Out of range; cancelled.")
        return None
    return idx


def _menu_preview_label() -> None:
    """Menu option 2: pick category, build a label plan, show preview."""
    opt = find_option("2")
    if opt:
        click.echo("")
        click.echo(opt.detail)
        click.echo("")

    classifiable = list(CLASSIFIABLE_CATEGORIES)
    click.echo("Pick a category to label:")
    for i, c in enumerate(classifiable, start=1):
        click.echo(f"  {i}. {c.value}")
    click.echo("  q. Cancel")
    idx = _menu_pick_int("> ", lo=1, hi=len(classifiable))
    if idx is None:
        return
    category = classifiable[idx - 1]

    raw = _prompt(
        f"How many unread messages to scan? [{DEFAULT_LABEL_LIMIT}] (or 'q' to cancel) "
    ).strip()
    if raw.lower() in {"q", "quit"}:
        return
    if not raw:
        limit = DEFAULT_LABEL_LIMIT
    else:
        try:
            limit = int(raw)
        except ValueError:
            click.echo(f"'{raw}' is not a number; using default ({DEFAULT_LABEL_LIMIT}).")
            limit = DEFAULT_LABEL_LIMIT
        else:
            if limit <= 0:
                click.echo(
                    f"Limit must be positive; using default ({DEFAULT_LABEL_LIMIT})."
                )
                limit = DEFAULT_LABEL_LIMIT

    try:
        _run_label_plan(category=category, limit=limit, output_json=False)
    except KeyboardInterrupt:
        click.echo("\nCancelled.")
    except Exception as exc:  # surface raw errors per cs.md
        click.echo(f"Plan failed: {type(exc).__name__}: {exc}", err=True)


def _format_run_timestamp(iso_ts: str) -> str:
    """Prettify the DB's UTC ISO ``created_at`` into local-time ``YYYY-MM-DD HH:MM``.

    The DB stores ``datetime.now(timezone.utc).isoformat(timespec="seconds")``
    (UTC). The user reading the picker is on local time — calling the
    no-arg form of ``astimezone()`` picks up the system tz and converts.
    A naive datetime (only possible from a hand-edited DB row) is
    *interpreted* as local time by ``astimezone()`` per Python 3.6+
    semantics — that's a slight mismatch with the project's UTC-by-
    default storage convention, but it's preferable to special-casing
    legacy/malformed input. Falls back to the raw string if it's not
    parseable at all.
    """
    try:
        dt = datetime.fromisoformat(iso_ts)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return iso_ts


def _menu_apply_label() -> None:
    """Menu option 3: list planned label runs, pick one, confirm, apply."""
    opt = find_option("3")
    if opt:
        click.echo("")
        click.echo(opt.detail)
        click.echo("")

    with gw_db.open_db() as conn:
        all_runs = gw_db.list_runs(conn, phase="label", status="planned", limit=20)
        # Hydrate with planned-audit-row counts and drop:
        #   * runs with zero planned rows — `build_label_plan` writes a
        #     run row even when no messages match (audit trail), but
        #     surfacing those in the apply picker is just noise.
        #   * runs whose `category_filter` doesn't parse to a valid
        #     `Category` — apply would error out with `ValueError` mid-
        #     flight; better to hide them than to let the user pick a
        #     row that's guaranteed to fail.
        #   * runs that auto-heal flips out of `planned` (stale-status
        #     case where a prior apply's bookkeeping write failed). The
        #     heal call updates the persistent state too, so the run
        #     becomes visible to the undo picker on the next refresh.
        runs = []
        for r in all_runs:
            healed = planning.heal_run_status_from_audit_log(conn, r["id"])
            if healed is None or healed.get("status") != "planned":
                continue
            r = healed
            # Match the apply filter (status='planned' AND action='add_label')
            # so a contaminated run can't show a misleading count here.
            r["count"] = gw_db.count_audit_entries(
                conn, r["id"], status="planned", action="add_label"
            )
            if r["count"] == 0:
                continue
            try:
                cat = Category(r.get("category_filter") or "")
            except ValueError:
                continue
            # Skip UNKNOWN — `apply_label_plan` rejects it as defense-in-
            # depth, so showing it in the picker would only let the user
            # pick a row that's guaranteed to fail mid-apply.
            if cat is Category.UNKNOWN:
                continue
            runs.append(r)

    if not runs:
        click.echo("No previewed labeling runs to apply.")
        click.echo("Use option 2 (Preview a labeling run) to create one first.")
        return

    click.echo("Which previewed labeling run do you want to apply?")
    for i, r in enumerate(runs, start=1):
        click.echo(
            f"  {i}. {_format_run_timestamp(r['created_at'])}  — "
            f"{r.get('category_filter') or '?'}, "
            f"{r['count']} message(s), planned"
        )
    click.echo("  q. Cancel")
    idx = _menu_pick_int("> ", lo=1, hi=len(runs))
    if idx is None:
        return
    chosen = runs[idx - 1]

    try:
        _run_label_commit(
            run_id=chosen["id"], output_json=False, assume_yes=False
        )
    except KeyboardInterrupt:
        click.echo("\nCancelled.")
    except Exception as exc:  # surface raw errors per cs.md
        click.echo(f"Apply failed: {type(exc).__name__}: {exc}", err=True)


def _menu_undo_run() -> None:
    """Menu option 4: list undoable runs (label or archive), pick, confirm, undo."""
    opt = find_option("4")
    if opt:
        click.echo("")
        click.echo(opt.detail)
        click.echo("")

    with gw_db.open_db() as conn:
        # Include `planned` in the status filter so auto-heal can rescue
        # stale-status runs (a prior apply's bookkeeping write failed
        # post-mutation, leaving the run row at `planned` despite real
        # Gmail mutations). After heal, those runs flip to
        # `committed`/`partially_failed` and become legitimate undo
        # targets.
        all_runs = gw_db.list_runs(
            conn,
            phases=(planning.PHASE_LABEL, planning.PHASE_ARCHIVE),
            statuses=("committed", "partially_failed", "planned"),
            limit=20,
        )
        runs = []
        for r in all_runs:
            healed = planning.heal_run_status_from_audit_log(conn, r["id"])
            if healed is None or healed.get("status") not in {
                "committed", "partially_failed"
            }:
                continue
            r = healed
            phase = r.get("phase")
            action = (
                planning.ACTION_ADD_LABEL
                if phase == planning.PHASE_LABEL
                else planning.ACTION_ARCHIVE
            )
            r["count"] = gw_db.count_audit_entries(
                conn, r["id"], status="applied", action=action
            )
            if r["count"] == 0:
                # No applied rows means there's nothing to revert.
                continue
            # For label runs, ensure the category is still resolvable so
            # `apply_undo_plan` won't ValueError mid-flight.
            if phase == planning.PHASE_LABEL:
                try:
                    cat = Category(r.get("category_filter") or "")
                except ValueError:
                    continue
                if cat is Category.UNKNOWN:
                    continue
            runs.append(r)

    if not runs:
        click.echo("No previously-applied runs to undo.")
        click.echo(
            "(Only committed or partially-failed label / archive runs can be undone.)"
        )
        return

    click.echo("Which run do you want to undo?")
    for i, r in enumerate(runs, start=1):
        phase = r.get("phase") or "?"
        cat = r.get("category_filter") or ""
        # Compose a tag like "label/promotional" or "archive/promotional"
        # so the user can tell at a glance what each row represents.
        if cat:
            tag = f"{phase}/{cat}"
        else:
            tag = phase
        # `count` is the number of currently `applied` rows (i.e., the
        # work the undo would actually do). For a `partially_failed`
        # run that was applied with some failures, this is the count
        # of successful applies that remain to be reverted —
        # disambiguate so the user doesn't read "X message(s)" as
        # "X original mutations".
        count_label = (
            f"{r['count']} still applied"
            if r["status"] == "partially_failed"
            else f"{r['count']} message(s)"
        )
        click.echo(
            f"  {i}. {_format_run_timestamp(r['created_at'])}  — "
            f"{tag}, {count_label}, {r['status']}"
        )
    click.echo("  q. Cancel")
    idx = _menu_pick_int("> ", lo=1, hi=len(runs))
    if idx is None:
        return
    chosen = runs[idx - 1]

    try:
        _run_undo(run_id=chosen["id"], output_json=False, assume_yes=False)
    except KeyboardInterrupt:
        click.echo("\nCancelled.")
    except Exception as exc:  # surface raw errors per cs.md
        click.echo(f"Undo failed: {type(exc).__name__}: {exc}", err=True)


def _menu_preview_archive() -> None:
    """Menu option 6: pick a label run, build an archive plan, show preview."""
    opt = find_option("6")
    if opt:
        click.echo("")
        click.echo(opt.detail)
        click.echo("")

    with gw_db.open_db() as conn:
        # Include `planned` so auto-heal can rescue stale-status label
        # runs as legitimate archive sources.
        all_runs = gw_db.list_runs(
            conn,
            phase=planning.PHASE_LABEL,
            statuses=("committed", "partially_failed", "planned"),
            limit=20,
        )
        runs = []
        for r in all_runs:
            healed = planning.heal_run_status_from_audit_log(conn, r["id"])
            if healed is None or healed.get("status") not in {
                "committed", "partially_failed"
            }:
                continue
            r = healed
            r["count"] = gw_db.count_audit_entries(
                conn, r["id"], status="applied", action=planning.ACTION_ADD_LABEL
            )
            if r["count"] == 0:
                continue
            try:
                cat = Category(r.get("category_filter") or "")
            except ValueError:
                continue
            if cat is Category.UNKNOWN:
                continue
            runs.append(r)

    if not runs:
        click.echo("No applied label runs to archive from.")
        click.echo(
            "(Run option 2 to preview a labeling run, then option 3 to apply it. "
            "Then come back here to archive.)"
        )
        return

    click.echo("Pick a previously-applied label run to archive:")
    for i, r in enumerate(runs, start=1):
        click.echo(
            f"  {i}. {_format_run_timestamp(r['created_at'])}  — "
            f"label/{r.get('category_filter') or '?'}, "
            f"{r['count']} message(s), {r['status']}"
        )
    click.echo("  q. Cancel")
    idx = _menu_pick_int("> ", lo=1, hi=len(runs))
    if idx is None:
        return
    chosen = runs[idx - 1]

    try:
        _run_archive_plan(source_run_id=chosen["id"], output_json=False)
    except KeyboardInterrupt:
        click.echo("\nCancelled.")
    except Exception as exc:
        click.echo(f"Archive plan failed: {type(exc).__name__}: {exc}", err=True)


def _menu_apply_archive() -> None:
    """Menu option 7: list planned archive runs, pick one, confirm, apply."""
    opt = find_option("7")
    if opt:
        click.echo("")
        click.echo(opt.detail)
        click.echo("")

    with gw_db.open_db() as conn:
        all_runs = gw_db.list_runs(
            conn, phase=planning.PHASE_ARCHIVE, status="planned", limit=20
        )
        runs = []
        for r in all_runs:
            # Heal in case a prior apply's bookkeeping write failed —
            # if heal flips the run out of `planned`, it doesn't belong
            # in the apply picker (it belongs in option 4 for undo).
            healed = planning.heal_run_status_from_audit_log(conn, r["id"])
            if healed is None or healed.get("status") != "planned":
                continue
            r = healed
            r["count"] = gw_db.count_audit_entries(
                conn, r["id"], status="planned", action=planning.ACTION_ARCHIVE
            )
            if r["count"] == 0:
                continue
            runs.append(r)

    if not runs:
        click.echo("No previewed archive runs to apply.")
        click.echo("Use option 6 (Preview an archive run) to create one first.")
        return

    click.echo("Which previewed archive run do you want to apply?")
    for i, r in enumerate(runs, start=1):
        click.echo(
            f"  {i}. {_format_run_timestamp(r['created_at'])}  — "
            f"archive/{r.get('category_filter') or '?'}, "
            f"{r['count']} message(s), planned"
        )
    click.echo("  q. Cancel")
    idx = _menu_pick_int("> ", lo=1, hi=len(runs))
    if idx is None:
        return
    chosen = runs[idx - 1]

    try:
        _run_archive_commit(
            run_id=chosen["id"], output_json=False, assume_yes=False
        )
    except KeyboardInterrupt:
        click.echo("\nCancelled.")
    except Exception as exc:
        click.echo(f"Apply failed: {type(exc).__name__}: {exc}", err=True)


def _format_oneshot_summary(result: gw_oneshot.OneShotResult) -> str:
    """Human-readable rollup of a OneShotResult for menu / non-JSON output."""
    lines = [
        f"status: {result.status}"
        + (f"  ({result.error_code})" if result.error_code else ""),
        f"snapshot: {result.snapshot_size} message(s) scanned",
        f"classified senders: {result.classified_sender_count}",
        f"wall: {result.wall_seconds:.1f}s",
    ]
    for cat in result.categories:
        seg = (
            f"  {cat.category:<14} "
            f"label: {cat.labels_applied} applied / {cat.labels_failed} failed"
        )
        if cat.archive_run_id is not None:
            seg += (
                f"   archive: {cat.archive_applied} applied / "
                f"{cat.archive_failed} failed"
            )
        if cat.error:
            seg += f"   ERROR: {cat.error}"
        lines.append(seg)
    if result.notes:
        lines.append("notes:")
        for n in result.notes:
            lines.append(f"  - {n}")
    return "\n".join(lines)


def _run_one_pass(
    *,
    limit: int,
    archive: bool,
    output_json: bool,
    interactive_auth: bool = True,
) -> int:
    """Drive `oneshot.run_one_pass`, render output, return exit code."""
    if limit <= 0:
        click.echo("--limit must be greater than zero.", err=True)
        return 2

    try:
        creds = gw_auth.get_credentials(interactive=interactive_auth)
    except gw_oneshot.AuthRequired as exc:
        payload = {
            "status": "auth_required",
            "error_code": "auth_required",
            "reason": exc.reason,
        }
        if output_json:
            click.echo(_json.dumps(payload, indent=2))
        else:
            click.echo(
                f"auth_required: {exc.reason}\n"
                "Re-authenticate on M4 (menu option 5) and retry.",
                err=True,
            )
        return 2

    def _progress(stage: str, info: dict[str, Any]) -> None:
        if output_json:
            return
        # Concise stderr progress — one line per phase boundary.
        if stage == "snapshot_done":
            click.echo(
                f"Snapshot: {info['metadata_fetched']} message(s) "
                f"({info['fetch_failures']} fetch failure(s)).",
                err=True,
            )
        elif stage == "classify_done":
            click.echo(
                f"Classified {info['classified']} sender(s).",
                err=True,
            )
        elif stage == "category_begin":
            click.echo(f"--- {info['category']} ---", err=True)
        elif stage == "label_done":
            click.echo(
                f"  label: {info['applied']} applied, {info['failed']} failed.",
                err=True,
            )
        elif stage == "archive_done":
            click.echo(
                f"  archive: {info['applied']} applied, {info['failed']} failed.",
                err=True,
            )

    try:
        result = gw_oneshot.run_one_pass(
            creds=creds,
            limit=limit,
            archive=archive,
            on_progress=_progress,
        )
    except MissingAPIKeyError as exc:
        click.echo(str(exc), err=True)
        return 2

    if output_json:
        click.echo(_json.dumps(result.to_dict(), indent=2))
    else:
        click.echo("")
        click.echo(_format_oneshot_summary(result))

    if result.status == "success":
        return 0
    if result.status == "auth_required":
        return 2
    if result.status == "partial_failure":
        return 4
    return 5


def _menu_run_one_pass() -> None:
    """Menu option 8: report → label-all → archive-all, prompt only for limit."""
    opt = find_option("8")
    if opt:
        click.echo("")
        click.echo(opt.detail)
        click.echo("")
    raw = _prompt(
        f"How many unread messages to scan? [{DEFAULT_ONESHOT_LIMIT}] (or 'q' to cancel) "
    ).strip()
    if raw.lower() in {"q", "quit"}:
        return
    if not raw:
        limit = DEFAULT_ONESHOT_LIMIT
    else:
        try:
            limit = int(raw)
        except ValueError:
            click.echo(
                f"'{raw}' is not a number; using default ({DEFAULT_ONESHOT_LIMIT})."
            )
            limit = DEFAULT_ONESHOT_LIMIT
        else:
            if limit <= 0:
                click.echo(
                    f"Limit must be positive; using default ({DEFAULT_ONESHOT_LIMIT})."
                )
                limit = DEFAULT_ONESHOT_LIMIT

    try:
        _run_one_pass(
            limit=limit,
            archive=True,
            output_json=False,
            interactive_auth=True,
        )
    except KeyboardInterrupt:
        click.echo("\nCancelled.")
    except Exception as exc:  # surface raw errors per cs.md
        click.echo(f"Run failed: {type(exc).__name__}: {exc}", err=True)


def _menu_reauth() -> None:
    """Menu option 5: re-run OAuth, refreshing the token."""
    opt = find_option("5")
    if opt:
        click.echo("")
        click.echo(opt.detail)
        click.echo("")
    confirm = _prompt("Open the browser to re-authenticate now? [y/N] ").strip().lower()
    if confirm not in {"y", "yes"}:
        click.echo("Cancelled.")
        return
    try:
        _run_auth(force_reauth=True)
    except Exception as exc:
        click.echo(f"Re-authentication failed: {type(exc).__name__}: {exc}", err=True)


def menu() -> None:
    """Interactive menu loop. Runs until the user picks ``q``.

    `_prompt` translates Ctrl-C/EOF at a prompt into a clean "q" cancel,
    so a Ctrl-C *while* a sub-flow is prompting cancels that sub-flow
    and returns here. A Ctrl-C between iterations (e.g., during the
    `click.echo(render_menu())` print, or while a sub-flow's `try/except`
    is unwinding) would otherwise propagate as a traceback. Catch it
    and exit cleanly.
    """
    while True:
        try:
            click.echo("")
            click.echo(render_menu())
            choice = _prompt("> ").strip().lower()
        except KeyboardInterrupt:
            click.echo("")
            click.echo("Bye.")
            return

        if not choice:
            # Empty input → redisplay the menu, don't exit. Quitting requires
            # an explicit `q` (or Ctrl-D / Ctrl-C, which `_prompt` translates
            # to `q`). Removes the footgun of accidentally hitting Enter.
            continue

        if choice in {"q", "quit"}:
            click.echo("Bye.")
            return

        opt = find_option(choice)
        if opt is None:
            click.echo(f"Unknown option: {choice!r}. Pick one of the listed numbers, or 'q' to quit.")
            continue

        if opt.key == "1":
            _menu_show_report()
        elif opt.key == "2":
            _menu_preview_label()
        elif opt.key == "3":
            _menu_apply_label()
        elif opt.key == "4":
            _menu_undo_run()
        elif opt.key == "5":
            _menu_reauth()
        elif opt.key == "6":
            _menu_preview_archive()
        elif opt.key == "7":
            _menu_apply_archive()
        elif opt.key == "8":
            _menu_run_one_pass()
        else:
            # The "q" option is handled by the early-out above, so reaching
            # here means a future option leaked into the menu without a
            # dispatcher branch. Surface loudly rather than silently ignoring.
            click.echo(f"Option '{opt.key}' is not implemented yet.")


# ---------------------------------------------------------------------------
# click group + subcommands
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.pass_context
def main(ctx: click.Context) -> None:
    """gmailwiz — AI-assisted Gmail inbox triage. Read, preview, label."""
    if ctx.invoked_subcommand is None:
        menu()


@main.command("auth")
def auth_cmd() -> None:
    """Run the Google OAuth flow and print the authenticated email."""
    _run_auth(force_reauth=False)


@main.command("report")
@click.option(
    "--limit",
    type=int,
    default=DEFAULT_REPORT_LIMIT,
    show_default=True,
    help="Maximum number of unread messages to scan.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit the report as JSON instead of a table.",
)
def report_cmd(limit: int, output_json: bool) -> None:
    """Group unread mail by sender + Claude category. Read-only."""
    try:
        rc = _run_report(limit=limit, output_json=output_json)
    except MissingAPIKeyError as exc:
        # Plain error, no traceback — user just needs to set the env var.
        click.echo(str(exc), err=True)
        sys.exit(2)
    if rc != 0:
        sys.exit(rc)


@main.command("label")
@click.option(
    "--category",
    type=click.Choice([c.value for c in CLASSIFIABLE_CATEGORIES]),
    default=None,
    help="Category to label (required for preview; ignored for --commit).",
)
@click.option(
    "--limit",
    type=int,
    default=DEFAULT_LABEL_LIMIT,
    show_default=True,
    help="Maximum number of unread messages to scan when previewing.",
)
@click.option(
    "--commit",
    is_flag=True,
    default=False,
    help="Apply a previously-previewed plan. Requires --run-id.",
)
@click.option(
    "--run-id",
    "run_id",
    type=str,
    default=None,
    help="Run id of a previously-previewed plan (required with --commit).",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit JSON instead of a human-readable table.",
)
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    default=False,
    help="Skip the 'yes' confirmation prompt at commit time.",
)
def label_cmd(
    category: Optional[str],
    limit: int,
    commit: bool,
    run_id: Optional[str],
    output_json: bool,
    assume_yes: bool,
) -> None:
    """Preview or apply a labeling run.

    Default mode is preview (dry-run): builds a plan, persists it, prints it.
    No Gmail mutations occur. Apply later with `--commit --run-id <id>`.
    """
    if commit:
        if not run_id:
            click.echo("--commit requires --run-id <id>.", err=True)
            sys.exit(2)
        if output_json and not assume_yes:
            # Confirmation prompts on stdout would corrupt the JSON payload
            # downstream consumers expect. Rather than silently mutating
            # without a prompt, force the user to be explicit: pass `--yes`
            # to skip the prompt OR drop `--json` to get the prompt.
            click.echo(
                "--commit with --json requires --yes (cannot prompt under "
                "JSON output mode without corrupting the payload).",
                err=True,
            )
            sys.exit(2)
        if category:
            click.echo(
                "--category is ignored with --commit (the run id determines "
                "the target label).",
                err=True,
            )
        try:
            rc = _run_label_commit(
                run_id=run_id, output_json=output_json, assume_yes=assume_yes
            )
        except FileNotFoundError as exc:
            # Most common cause: missing credentials.json. Mirror
            # report_cmd's clean-exit-without-traceback pattern for
            # well-known user errors.
            click.echo(f"{type(exc).__name__}: {exc}", err=True)
            sys.exit(2)
        except ValueError as exc:
            click.echo(f"{exc}", err=True)
            sys.exit(2)
        except KeyboardInterrupt:
            click.echo("\nCancelled.", err=True)
            sys.exit(130)
    else:
        if not category:
            click.echo(
                "--category is required for preview mode (omit --commit, "
                "supply --category).",
                err=True,
            )
            sys.exit(2)
        if run_id:
            click.echo(
                "--run-id is only meaningful with --commit; ignoring.",
                err=True,
            )
        if assume_yes:
            click.echo(
                "--yes is only meaningful with --commit; ignoring.",
                err=True,
            )
        try:
            rc = _run_label_plan(
                category=Category(category), limit=limit, output_json=output_json
            )
        except FileNotFoundError as exc:
            click.echo(f"{type(exc).__name__}: {exc}", err=True)
            sys.exit(2)
        except ValueError as exc:
            click.echo(f"{exc}", err=True)
            sys.exit(2)
        except KeyboardInterrupt:
            click.echo("\nCancelled.", err=True)
            sys.exit(130)
    if rc != 0:
        sys.exit(rc)


@main.command("archive")
@click.option(
    "--from-run-id",
    "source_run_id",
    type=str,
    default=None,
    help="Source label run id (required for preview).",
)
@click.option(
    "--commit",
    is_flag=True,
    default=False,
    help="Apply a previously-previewed archive plan. Requires --run-id.",
)
@click.option(
    "--run-id",
    "run_id",
    type=str,
    default=None,
    help="Run id of a previously-previewed archive plan (required with --commit).",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit JSON instead of a human-readable table.",
)
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    default=False,
    help="Skip the 'yes' confirmation prompt at commit time.",
)
def archive_cmd(
    source_run_id: Optional[str],
    commit: bool,
    run_id: Optional[str],
    output_json: bool,
    assume_yes: bool,
) -> None:
    """Preview or apply an archive run.

    Default mode is preview (dry-run): builds a plan from a previously-applied
    label run, persists it, prints it. No Gmail mutations occur. Apply later
    with `--commit --run-id <archive-run-id>`.
    """
    if commit:
        if not run_id:
            click.echo("--commit requires --run-id <id>.", err=True)
            sys.exit(2)
        if output_json and not assume_yes:
            click.echo(
                "--commit with --json requires --yes (cannot prompt under "
                "JSON output mode without corrupting the payload).",
                err=True,
            )
            sys.exit(2)
        if source_run_id:
            click.echo(
                "--from-run-id is ignored with --commit (the --run-id is the "
                "previewed archive run).",
                err=True,
            )
        try:
            rc = _run_archive_commit(
                run_id=run_id, output_json=output_json, assume_yes=assume_yes
            )
        except FileNotFoundError as exc:
            click.echo(f"{type(exc).__name__}: {exc}", err=True)
            sys.exit(2)
        except ValueError as exc:
            # build_archive_plan / apply_archive_plan validation errors
            # (non-label phase, undone source, malformed category_filter,
            # etc.). Surface as a clean rc=2 user-facing message rather
            # than letting a Python traceback escape click.
            click.echo(f"{exc}", err=True)
            sys.exit(2)
        except KeyboardInterrupt:
            click.echo("\nCancelled.", err=True)
            sys.exit(130)
    else:
        if not source_run_id:
            click.echo(
                "--from-run-id is required for preview mode (omit --commit, "
                "supply --from-run-id <label-run-id>).",
                err=True,
            )
            sys.exit(2)
        if run_id:
            click.echo(
                "--run-id is only meaningful with --commit; ignoring.",
                err=True,
            )
        if assume_yes:
            click.echo(
                "--yes is only meaningful with --commit; ignoring.",
                err=True,
            )
        try:
            rc = _run_archive_plan(
                source_run_id=source_run_id, output_json=output_json
            )
        except FileNotFoundError as exc:
            click.echo(f"{type(exc).__name__}: {exc}", err=True)
            sys.exit(2)
        except ValueError as exc:
            click.echo(f"{exc}", err=True)
            sys.exit(2)
        except KeyboardInterrupt:
            click.echo("\nCancelled.", err=True)
            sys.exit(130)
    if rc != 0:
        sys.exit(rc)


@main.command("undo")
@click.option(
    "--run-id",
    "run_id",
    type=str,
    required=True,
    help="Run id (label or archive) to reverse.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit JSON instead of a human-readable table.",
)
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    default=False,
    help="Skip the 'yes' confirmation prompt.",
)
def undo_cmd(run_id: str, output_json: bool, assume_yes: bool) -> None:
    """Reverse a previously-applied label or archive run.

    Walks the run's audit_log rows and dispatches the inverse mutation
    (label run → remove the gmailwiz/<category> label; archive run → re-add
    INBOX). Requires the run to be in `committed` or `partially_failed`
    state. Operates only on messages from that specific run — manually-
    labeled / archived messages are untouched.
    """
    if output_json and not assume_yes:
        click.echo(
            "undo with --json requires --yes (cannot prompt under "
            "JSON output mode without corrupting the payload).",
            err=True,
        )
        sys.exit(2)
    try:
        rc = _run_undo(
            run_id=run_id, output_json=output_json, assume_yes=assume_yes
        )
    except FileNotFoundError as exc:
        click.echo(f"{type(exc).__name__}: {exc}", err=True)
        sys.exit(2)
    except ValueError as exc:
        click.echo(f"{exc}", err=True)
        sys.exit(2)
    except KeyboardInterrupt:
        click.echo("\nCancelled.", err=True)
        sys.exit(130)
    if rc != 0:
        sys.exit(rc)


@main.command("run")
@click.option(
    "--limit",
    type=int,
    default=DEFAULT_ONESHOT_LIMIT,
    show_default=True,
    help="Maximum number of unread inbox messages to scan in one pass.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit the result as JSON instead of a human summary.",
)
@click.option(
    "--no-archive",
    "no_archive",
    is_flag=True,
    default=False,
    help="Label all four categories but skip the archive phase.",
)
@click.option(
    "--headless",
    "headless",
    is_flag=True,
    default=False,
    help=(
        "Fail with auth_required instead of opening the browser if the "
        "stored token is expired/invalid. Used by the M2 trigger service."
    ),
)
def run_cmd(limit: int, output_json: bool, no_archive: bool, headless: bool) -> None:
    """One-pass: classify → label all 4 categories → archive all 4.

    The non-interactive, scriptable counterpart of menu option 8. Uses a
    single inbox snapshot for all four categories so the four label runs
    operate on the same message set (no drift between them). Exit codes:
    0 success, 2 bad input / missing API key / auth_required, 4 partial
    failure, 5 total failure.
    """
    try:
        rc = _run_one_pass(
            limit=limit,
            archive=not no_archive,
            output_json=output_json,
            interactive_auth=not headless,
        )
    except KeyboardInterrupt:
        click.echo("\nCancelled.", err=True)
        sys.exit(130)
    if rc != 0:
        sys.exit(rc)
