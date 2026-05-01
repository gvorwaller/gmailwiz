"""click entry point for gmailwiz.

Two ways in:
  * ``python -m gmailwiz``           → interactive menu (the day-to-day UX)
  * ``python -m gmailwiz <command>`` → direct subcommands (for scripting)

Phase 1 wires:
  * ``auth``    — run OAuth, print the authenticated email
  * ``report``  — read-only sender-grouped unread report (`--limit`, `--json`)

The menu and subcommands share the same internal helpers (`_run_auth`,
`_run_report`); the menu does **not** shell out to subprocesses.
"""

from __future__ import annotations

import json as _json
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Optional

import click

from gmailwiz import auth as gw_auth
from gmailwiz import db as gw_db
from gmailwiz import gmail_client
from gmailwiz.categories import Category
from gmailwiz.classifier import (
    MissingAPIKeyError,
    SenderInput,
    SenderSample,
    classify_senders,
)
from gmailwiz.menu_text import find_option, render_menu


DEFAULT_REPORT_LIMIT = 100


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
            gw_db.update_run_status(conn, run_id, "failed")
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
    """Interactive menu loop. Runs until the user picks ``q``."""
    while True:
        click.echo("")
        click.echo(render_menu())
        choice = _prompt("> ").strip().lower()

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
        elif opt.key == "5":
            _menu_reauth()
        elif opt.key == "q":
            click.echo("Bye.")
            return
        else:
            # Defensive: a Phase 2/3 option somehow leaked in.
            click.echo(f"Option '{opt.key}' is not implemented in Phase 1.")


# ---------------------------------------------------------------------------
# click group + subcommands
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.pass_context
def main(ctx: click.Context) -> None:
    """gmailwiz — AI-assisted Gmail inbox triage (Phase 1: read-only)."""
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
