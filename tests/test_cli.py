"""Tests for `gmailwiz.cli`.

We use click's `CliRunner` for subcommand wiring tests and stub `input()` to
exercise the menu loop without any real Google or Anthropic calls.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from gmailwiz import cli
from gmailwiz.menu_text import MENU_HEADER, MENU_OPTIONS, find_option, render_menu


# ---------------------------------------------------------------------------
# Help / wiring
# ---------------------------------------------------------------------------


def test_top_level_help():
    runner = CliRunner()
    result = runner.invoke(cli.main, ["--help"])
    assert result.exit_code == 0
    assert "gmailwiz" in result.output.lower()
    # All Phase 1 subcommands appear in help.
    assert "auth" in result.output
    assert "report" in result.output


def test_report_help_lists_phase1_flags():
    runner = CliRunner()
    result = runner.invoke(cli.main, ["report", "--help"])
    assert result.exit_code == 0
    assert "--limit" in result.output
    assert "--json" in result.output


def test_auth_help_does_not_crash():
    runner = CliRunner()
    result = runner.invoke(cli.main, ["auth", "--help"])
    assert result.exit_code == 0


def test_unimplemented_phase23_subcommand_errors():
    """`archive` / `undo` must NOT be registered yet (Phase 3 / next chunk)."""
    runner = CliRunner()
    for sub in ("archive", "undo"):
        result = runner.invoke(cli.main, [sub, "--help"])
        assert result.exit_code != 0, f"Phase 3 subcommand '{sub}' should not be registered yet"


def test_label_subcommand_help_lists_phase2_flags():
    runner = CliRunner()
    result = runner.invoke(cli.main, ["label", "--help"])
    assert result.exit_code == 0, result.output
    assert "--category" in result.output
    assert "--commit" in result.output
    assert "--run-id" in result.output
    assert "--limit" in result.output


# ---------------------------------------------------------------------------
# Menu rendering
# ---------------------------------------------------------------------------


def test_menu_text_has_phase1_and_phase2_options():
    """Phase 1 (1, 5, q) + Phase 2 preview/apply (2, 3). Option 4 (undo)
    and Phase 3 archive options arrive in later chunks."""
    keys = {opt.key for opt in MENU_OPTIONS}
    assert keys == {"1", "2", "3", "5", "q"}


def test_render_menu_contains_header_and_all_options():
    text = render_menu()
    assert MENU_HEADER in text
    for opt in MENU_OPTIONS:
        assert opt.label in text


def test_find_option_case_insensitive():
    assert find_option("Q") is not None
    assert find_option("1") is not None
    assert find_option("99") is None
    assert find_option("") is None


# ---------------------------------------------------------------------------
# Menu loop — quit, unknown options, dispatch
# ---------------------------------------------------------------------------


def test_menu_quits_cleanly_on_q():
    runner = CliRunner()
    # No-arg invocation drops into the menu. Feed it "q\n" via stdin.
    result = runner.invoke(cli.main, [], input="q\n")
    assert result.exit_code == 0
    assert "gmailwiz" in result.output.lower()


def test_menu_quits_cleanly_on_eof():
    runner = CliRunner()
    # Empty stdin => EOFError on the first input() call => treated as quit.
    result = runner.invoke(cli.main, [], input="")
    assert result.exit_code == 0


def test_menu_handles_unknown_option_then_quits():
    runner = CliRunner()
    result = runner.invoke(cli.main, [], input="42\nq\n")
    assert result.exit_code == 0
    assert "Unknown option" in result.output


def test_menu_option_5_dispatches_to_reauth_when_confirmed():
    runner = CliRunner()
    with patch.object(cli, "_run_auth") as mock_auth:
        mock_auth.return_value = "gaylon@example.com"
        # 5 → "y" to confirm → q
        result = runner.invoke(cli.main, [], input="5\ny\nq\n")
    assert result.exit_code == 0
    mock_auth.assert_called_once_with(force_reauth=True)


def test_menu_option_5_cancels_when_user_declines():
    runner = CliRunner()
    with patch.object(cli, "_run_auth") as mock_auth:
        result = runner.invoke(cli.main, [], input="5\nn\nq\n")
    assert result.exit_code == 0
    assert "Cancelled" in result.output
    mock_auth.assert_not_called()


def test_menu_option_1_dispatches_to_report():
    runner = CliRunner()
    with patch.object(cli, "_run_report") as mock_report:
        mock_report.return_value = 0
        # 1 → accept default limit (empty line) → q
        result = runner.invoke(cli.main, [], input="1\n\nq\n")
    assert result.exit_code == 0
    mock_report.assert_called_once()
    kwargs = mock_report.call_args.kwargs
    assert kwargs["limit"] == cli.DEFAULT_REPORT_LIMIT
    assert kwargs["output_json"] is False


def test_menu_option_1_with_custom_limit():
    runner = CliRunner()
    with patch.object(cli, "_run_report") as mock_report:
        mock_report.return_value = 0
        result = runner.invoke(cli.main, [], input="1\n25\nq\n")
    assert result.exit_code == 0
    mock_report.assert_called_once()
    assert mock_report.call_args.kwargs["limit"] == 25


def test_menu_option_1_invalid_limit_falls_back_to_default():
    runner = CliRunner()
    with patch.object(cli, "_run_report") as mock_report:
        mock_report.return_value = 0
        result = runner.invoke(cli.main, [], input="1\nnot a number\nq\n")
    assert result.exit_code == 0
    mock_report.assert_called_once()
    assert mock_report.call_args.kwargs["limit"] == cli.DEFAULT_REPORT_LIMIT


# ---------------------------------------------------------------------------
# _build_report_rows / _render_report_table — pure logic, no IO
# ---------------------------------------------------------------------------


def test_build_report_rows_sorts_by_unread_desc():
    from gmailwiz.classifier import ClassificationResult
    from gmailwiz.categories import Category

    by_email = {
        "a@x.com": [
            {"sender_name": "A", "subject": "first", "internal_date": "100"},
            {"sender_name": "A", "subject": "second", "internal_date": "200"},
        ],
        "b@x.com": [
            {"sender_name": "B", "subject": "only", "internal_date": "150"},
        ],
    }
    classifications = {
        "a@x.com": ClassificationResult(email="a@x.com", category=Category.NEWSLETTER, source="model"),
        "b@x.com": ClassificationResult(email="b@x.com", category=Category.PROMOTIONAL, source="model"),
    }
    rows = cli._build_report_rows(by_email, classifications)
    assert [r.email for r in rows] == ["a@x.com", "b@x.com"]
    assert rows[0].unread == 2
    assert rows[0].newest_subject == "second"  # picked by max internal_date
    assert rows[1].category is Category.PROMOTIONAL


def test_render_report_table_includes_headers_and_rows():
    from gmailwiz.cli import _SenderRow
    from gmailwiz.categories import Category

    rows = [
        _SenderRow(
            email="news@nytimes.com",
            display_name="NYT",
            unread=47,
            category=Category.NEWSLETTER,
            newest_subject="The Morning",
        )
    ]
    text = cli._render_report_table(rows)
    assert "SENDER" in text
    assert "news@nytimes.com" in text
    assert "newsletter" in text
    assert "47" in text


def test_render_report_table_handles_empty_input():
    """Empty input must render empty (not a misleading 'No unread' line) —
    the caller is responsible for context-aware messaging when rows is
    empty but messages were fetched (e.g. all senders unparseable).
    """
    text = cli._render_report_table([])
    assert text == ""


# ---------------------------------------------------------------------------
# _run_report — empty/error edge cases (integration via stubs)
# ---------------------------------------------------------------------------


def test_run_report_all_unparseable_senders(monkeypatch, capsys):
    """Messages were fetched but every From: was unparseable.

    Must NOT print "No unread messages found." Must show the
    'all unparseable' banner and the 0-senders footer line, and the run
    must be marked partially_failed (not committed) in the DB.
    """
    from gmailwiz import auth as gw_auth
    from gmailwiz import db as gw_db
    from gmailwiz import gmail_client
    from gmailwiz import classifier as gw_classifier

    monkeypatch.setattr(gw_auth, "get_credentials", lambda **kw: object())
    monkeypatch.setattr(gmail_client, "list_unread_message_ids",
                        lambda creds, max_results: ["m1", "m2", "m3"])
    monkeypatch.setattr(gmail_client, "get_message_metadata",
                        lambda creds, ids: [
                            {"id": i, "thread_id": "t", "sender_email": "",
                             "sender_name": "", "subject": "?", "snippet": "",
                             "internal_date": "0", "label_ids": []}
                            for i in ids
                        ])

    rc = cli._run_report(limit=10, output_json=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "All 3 fetched message(s) had unparseable From: headers" in out
    assert "0 senders" in out
    assert "No unread messages found" not in out

    # The run must be marked `partially_failed` since the inbox WAS read,
    # but no senders made it to the report.
    with gw_db.open_db() as conn:
        row = conn.execute(
            "SELECT status FROM runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert row["status"] == "partially_failed"


# ---------------------------------------------------------------------------
# label subcommand — preview + commit wiring
# ---------------------------------------------------------------------------


def test_label_preview_requires_category():
    runner = CliRunner()
    result = runner.invoke(cli.main, ["label"])
    assert result.exit_code == 2
    assert "--category is required" in result.output


def test_label_commit_requires_run_id():
    runner = CliRunner()
    result = runner.invoke(cli.main, ["label", "--commit"])
    assert result.exit_code == 2
    assert "--commit requires --run-id" in result.output


def test_label_commit_json_requires_yes():
    """`--commit --json` without `--yes` is a footgun (would mutate without
    a prompt because we can't safely emit the prompt under JSON mode).
    Force the user to be explicit."""
    runner = CliRunner()
    result = runner.invoke(
        cli.main, ["label", "--commit", "--run-id", "x", "--json"]
    )
    assert result.exit_code == 2
    assert "--commit with --json requires --yes" in result.output


def test_run_label_commit_json_without_yes_refused_at_helper(capsys):
    """Defense-in-depth: even if a future caller bypasses the click
    upstream gate and invokes `_run_label_commit` directly with
    `output_json=True, assume_yes=False`, the helper itself must refuse
    to proceed (otherwise the prompt is silently skipped and Gmail is
    mutated with no confirmation).
    """
    rc = cli._run_label_commit(
        run_id="any", output_json=True, assume_yes=False
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "Refusing to commit under JSON" in err


def test_label_preview_invokes_run_label_plan(monkeypatch):
    runner = CliRunner()
    captured = {}

    def fake_plan(*, category, limit, output_json):
        captured["category"] = category
        captured["limit"] = limit
        captured["output_json"] = output_json
        return 0

    monkeypatch.setattr(cli, "_run_label_plan", fake_plan)
    result = runner.invoke(
        cli.main,
        ["label", "--category", "promotional", "--limit", "37"],
    )
    assert result.exit_code == 0, result.output
    from gmailwiz.categories import Category
    assert captured["category"] is Category.PROMOTIONAL
    assert captured["limit"] == 37
    assert captured["output_json"] is False


def test_label_commit_invokes_run_label_commit(monkeypatch):
    runner = CliRunner()
    captured = {}

    def fake_commit(*, run_id, output_json, assume_yes):
        captured.update(run_id=run_id, output_json=output_json, assume_yes=assume_yes)
        return 0

    monkeypatch.setattr(cli, "_run_label_commit", fake_commit)
    result = runner.invoke(
        cli.main, ["label", "--commit", "--run-id", "abc123", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert captured == {"run_id": "abc123", "output_json": False, "assume_yes": True}


def test_run_label_plan_writes_run_and_audit_rows(monkeypatch, capsys):
    """End-to-end through `_run_label_plan` against the autouse-isolated DB.

    Confirms preview-mode wiring: stub Gmail + auth, seed the senders cache,
    invoke `_run_label_plan`, then verify the DB has a `planned` run row and
    one audit_log row per candidate, and that the preview text printed.
    """
    from gmailwiz import auth as gw_auth
    from gmailwiz import db as gw_db
    from gmailwiz import gmail_client
    from gmailwiz.categories import Category

    # Seed the senders cache.
    with gw_db.open_db() as conn:
        gw_db.upsert_sender(
            conn,
            email="promo@x.com",
            display_name="Promo",
            category=Category.PROMOTIONAL,
            prompt_version="t",
            model="t",
        )

    monkeypatch.setattr(gw_auth, "get_credentials", lambda **kw: object())
    monkeypatch.setattr(
        gmail_client, "list_unread_message_ids",
        lambda creds, max_results, query="is:unread in:inbox": ["m1"],
    )
    monkeypatch.setattr(
        gmail_client, "get_message_metadata",
        lambda creds, ids: [{
            "id": "m1", "thread_id": "t", "sender_email": "promo@x.com",
            "sender_name": "Promo", "subject": "50% off", "snippet": "",
            "internal_date": "0", "label_ids": ["INBOX"],
        }],
    )
    monkeypatch.setattr(gmail_client, "list_existing_labels", lambda creds: {})

    rc = cli._run_label_plan(category=Category.PROMOTIONAL, limit=10, output_json=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "gmailwiz/promotional" in out
    assert "m1" in out
    assert "promo@x.com" in out

    with gw_db.open_db() as conn:
        runs = gw_db.list_runs(conn, phase="label")
        assert len(runs) == 1
        rid = runs[0]["id"]
        assert runs[0]["status"] == "planned"
        audit = gw_db.get_audit_entries(conn, rid)
        assert [r["message_id"] for r in audit] == ["m1"]
        assert audit[0]["status"] == "planned"


def test_run_label_commit_walks_audit_log(monkeypatch, capsys):
    """`_run_label_commit` reads the audit_log (NOT a fresh Gmail query) and
    flips each row's status. Drift between preview and commit must not change
    what gets mutated."""
    from gmailwiz import auth as gw_auth
    from gmailwiz import db as gw_db
    from gmailwiz import gmail_client
    from gmailwiz.categories import Category

    # Seed sender + plan.
    with gw_db.open_db() as conn:
        gw_db.upsert_sender(
            conn, email="promo@x.com", display_name="P",
            category=Category.PROMOTIONAL, prompt_version="t", model="t",
        )

    monkeypatch.setattr(gw_auth, "get_credentials", lambda **kw: object())
    monkeypatch.setattr(
        gmail_client, "list_unread_message_ids",
        lambda creds, max_results, query="is:unread in:inbox": ["m1", "m2"],
    )
    monkeypatch.setattr(
        gmail_client, "get_message_metadata",
        lambda creds, ids: [
            {"id": "m1", "thread_id": "t", "sender_email": "promo@x.com",
             "sender_name": "P", "subject": "a", "snippet": "",
             "internal_date": "0", "label_ids": ["INBOX"]},
            {"id": "m2", "thread_id": "t", "sender_email": "promo@x.com",
             "sender_name": "P", "subject": "b", "snippet": "",
             "internal_date": "0", "label_ids": ["INBOX"]},
        ],
    )
    monkeypatch.setattr(gmail_client, "list_existing_labels", lambda creds: {})

    rc = cli._run_label_plan(category=Category.PROMOTIONAL, limit=10, output_json=False)
    assert rc == 0

    # Find the planned run.
    with gw_db.open_db() as conn:
        runs = gw_db.list_runs(conn, phase="label", status="planned")
        rid = runs[0]["id"]

    # Stub mutations and commit.
    monkeypatch.setattr(
        gmail_client, "ensure_labels_exist",
        lambda creds, names: {n: "Label_99" for n in names},
    )
    modified: list[str] = []

    def fake_modify(creds, *, message_id, add_label_ids=(), remove_label_ids=()):
        modified.append(message_id)
        return {"id": message_id, "labelIds": ["INBOX", "Label_99"]}

    monkeypatch.setattr(gmail_client, "modify_labels", fake_modify)

    rc = cli._run_label_commit(run_id=rid, output_json=False, assume_yes=True)
    assert rc == 0
    assert modified == ["m1", "m2"]

    with gw_db.open_db() as conn:
        run = gw_db.get_run(conn, rid)
        assert run["status"] == "committed"
        for row in gw_db.get_audit_entries(conn, rid):
            assert row["status"] == "applied"


def test_run_label_commit_unknown_run_id_errors(monkeypatch, capsys):
    """A typo'd `--run-id` must NOT trigger OAuth before failing.

    Stub `get_credentials` with a side-effect that fails the test loudly
    if invoked — this catches a regression where the OAuth call lands
    BEFORE run-id validation, regardless of whether the dev's machine
    happens to have valid creds on disk.
    """
    from gmailwiz import auth as gw_auth

    def _must_not_be_called(**kw):
        raise AssertionError(
            "_run_label_commit triggered OAuth before validating "
            "the run id — validation order regression"
        )

    monkeypatch.setattr(gw_auth, "get_credentials", _must_not_be_called)
    rc = cli._run_label_commit(run_id="does-not-exist", output_json=False, assume_yes=True)
    assert rc == 2
    assert "No run with id" in capsys.readouterr().err


def test_run_label_commit_empty_plan_under_json_emits_json(monkeypatch, capsys):
    """Zero-planned-rows shortcut under `--json` MUST emit a JSON object,
    not a human-readable line — otherwise downstream JSON consumers
    get garbage on stdout."""
    import json as _json
    from gmailwiz import db as gw_db

    # Seed a `planned` label run with NO audit rows (zero-candidate).
    with gw_db.open_db() as conn:
        rid = gw_db.create_run(
            conn, phase="label", category_filter="promotional",
            limit_count=10, dry_run=True, status="planned",
        )

    rc = cli._run_label_commit(run_id=rid, output_json=True, assume_yes=True)
    assert rc == 0
    out = capsys.readouterr().out
    payload = _json.loads(out)  # must parse cleanly as JSON
    assert payload["run_id"] == rid
    assert payload["applied"] == 0
    assert payload["failed"] == 0
    assert payload["run_status"] == "planned"
    assert "no planned audit entries" in payload.get("note", "")


def test_run_report_baseexception_handler_does_not_shadow_original_error(
    monkeypatch, capsys
):
    """When `_run_report`'s BaseException handler runs the bookkeeping
    `update_run_status` call and that call raises (e.g. KeyError from
    rowcount==0 because the row never committed), the bookkeeping
    failure must NOT shadow the original exception. cs.md mandates raw
    error pass-through.
    """
    from gmailwiz import auth as gw_auth
    from gmailwiz import db as gw_db
    from gmailwiz import gmail_client
    from gmailwiz import classifier as gw_classifier

    monkeypatch.setattr(gw_auth, "get_credentials", lambda **kw: object())
    monkeypatch.setattr(
        gmail_client, "list_unread_message_ids",
        lambda creds, max_results: ["m1"],
    )
    monkeypatch.setattr(
        gmail_client, "get_message_metadata",
        lambda creds, ids: [{
            "id": "m1", "thread_id": "t", "sender_email": "p@x.com",
            "sender_name": "P", "subject": "x", "snippet": "",
            "internal_date": "0", "label_ids": [],
        }],
    )

    class OriginalError(RuntimeError):
        pass

    def boom_classify(*args, **kwargs):
        raise OriginalError("the real reason classification failed")

    # `cli` does `from gmailwiz.classifier import classify_senders`, so
    # we have to patch the symbol the cli module already bound at import
    # time, not the source module.
    monkeypatch.setattr(cli, "classify_senders", boom_classify)

    # Force the bookkeeping `update_run_status` call to itself raise — the
    # handler must swallow the bookkeeping error so the OriginalError
    # surfaces as the actual exception the caller sees.
    def boom_update(conn, run_id, status, *, dry_run=None):
        raise KeyError("simulated rowcount==0")

    monkeypatch.setattr(gw_db, "update_run_status", boom_update)

    with pytest.raises(OriginalError, match="the real reason"):
        cli._run_report(limit=10, output_json=False)


def test_run_label_commit_partial_failure_returns_4(monkeypatch, capsys):
    from gmailwiz import auth as gw_auth
    from gmailwiz import db as gw_db
    from gmailwiz import gmail_client
    from gmailwiz.categories import Category

    with gw_db.open_db() as conn:
        gw_db.upsert_sender(
            conn, email="p@x.com", display_name="P",
            category=Category.PROMOTIONAL, prompt_version="t", model="t",
        )

    monkeypatch.setattr(gw_auth, "get_credentials", lambda **kw: object())
    monkeypatch.setattr(
        gmail_client, "list_unread_message_ids",
        lambda creds, max_results, query="is:unread in:inbox": ["m1", "m2"],
    )
    monkeypatch.setattr(
        gmail_client, "get_message_metadata",
        lambda creds, ids: [
            {"id": mid, "thread_id": "t", "sender_email": "p@x.com",
             "sender_name": "P", "subject": "x", "snippet": "",
             "internal_date": "0", "label_ids": ["INBOX"]}
            for mid in ids
        ],
    )
    monkeypatch.setattr(gmail_client, "list_existing_labels", lambda creds: {})
    cli._run_label_plan(category=Category.PROMOTIONAL, limit=10, output_json=False)

    with gw_db.open_db() as conn:
        rid = gw_db.list_runs(conn, phase="label", status="planned")[0]["id"]

    monkeypatch.setattr(
        gmail_client, "ensure_labels_exist",
        lambda creds, names: {n: "Label_99" for n in names},
    )

    def half_failing(creds, *, message_id, add_label_ids=(), remove_label_ids=()):
        if message_id == "m2":
            raise RuntimeError("simulated quota")
        return {"id": message_id, "labelIds": ["INBOX", "Label_99"]}

    monkeypatch.setattr(gmail_client, "modify_labels", half_failing)
    rc = cli._run_label_commit(run_id=rid, output_json=False, assume_yes=True)
    assert rc == 4  # partially_failed exit code


# ---------------------------------------------------------------------------
# Menu — option 2 and 3 dispatch
# ---------------------------------------------------------------------------


def test_menu_option_2_dispatches_to_preview_label():
    runner = CliRunner()
    with patch.object(cli, "_run_label_plan") as mock_plan:
        mock_plan.return_value = 0
        # 2 → category 1 (promotional) → accept default limit → q
        result = runner.invoke(cli.main, [], input="2\n1\n\nq\n")
    assert result.exit_code == 0, result.output
    mock_plan.assert_called_once()
    kwargs = mock_plan.call_args.kwargs
    from gmailwiz.categories import Category
    assert kwargs["category"] is Category.PROMOTIONAL
    assert kwargs["limit"] == cli.DEFAULT_LABEL_LIMIT
    assert kwargs["output_json"] is False


def test_menu_option_2_cancel_via_q():
    runner = CliRunner()
    with patch.object(cli, "_run_label_plan") as mock_plan:
        result = runner.invoke(cli.main, [], input="2\nq\nq\n")
    assert result.exit_code == 0, result.output
    mock_plan.assert_not_called()


def test_menu_option_3_no_planned_runs_message():
    runner = CliRunner()
    result = runner.invoke(cli.main, [], input="3\nq\n")
    assert result.exit_code == 0, result.output
    assert "No previewed labeling runs to apply" in result.output


def test_menu_option_3_filters_unknown_category_runs():
    """A run with `category_filter='unknown'` would parse as `Category.UNKNOWN`
    and reach the picker. `apply_label_plan` rejects UNKNOWN as defense-
    in-depth, so showing it would only let the user pick a row that's
    guaranteed to fail mid-apply. Filter it out."""
    from gmailwiz import db as gw_db

    runner = CliRunner()
    with gw_db.open_db() as conn:
        rid = gw_db.create_run(
            conn, phase="label", category_filter="unknown",
            limit_count=10, dry_run=True, status="planned",
        )
        gw_db.append_audit_entry(
            conn, run_id=rid, action="add_label", message_id="m1",
            before_label_ids=["INBOX"],
            after_label_ids=["INBOX", "pending:gmailwiz/unknown"],
            status="planned",
        )

    result = runner.invoke(cli.main, [], input="3\nq\n")
    assert result.exit_code == 0, result.output
    # The corrupt UNKNOWN run is filtered — picker reports nothing to apply.
    assert "No previewed labeling runs to apply" in result.output


def test_menu_option_3_picks_planned_run_and_commits(monkeypatch):
    """Seed a planned label run, then exercise the picker."""
    from gmailwiz import db as gw_db
    from gmailwiz.categories import Category

    runner = CliRunner()

    # Seed: create a planned label run + one audit row.
    with gw_db.open_db() as conn:
        rid = gw_db.create_run(
            conn, phase="label", category_filter="promotional",
            limit_count=10, dry_run=True, status="planned",
        )
        gw_db.append_audit_entry(
            conn, run_id=rid, action="add_label", message_id="m1",
            before_label_ids=["INBOX"], after_label_ids=["INBOX", "pending:gmailwiz/promotional"],
            status="planned",
        )

    with patch.object(cli, "_run_label_commit") as mock_commit:
        mock_commit.return_value = 0
        # 3 → 1 (pick first listed run) → q
        result = runner.invoke(cli.main, [], input="3\n1\nq\n")
    assert result.exit_code == 0, result.output
    mock_commit.assert_called_once()
    assert mock_commit.call_args.kwargs["run_id"] == rid


def test_run_report_all_unparseable_senders_json(monkeypatch, capsys):
    """Same scenario, JSON output — payload must include the unparseable
    count and an empty senders list."""
    import json as _json

    from gmailwiz import auth as gw_auth
    from gmailwiz import gmail_client

    monkeypatch.setattr(gw_auth, "get_credentials", lambda **kw: object())
    monkeypatch.setattr(gmail_client, "list_unread_message_ids",
                        lambda creds, max_results: ["m1", "m2"])
    monkeypatch.setattr(gmail_client, "get_message_metadata",
                        lambda creds, ids: [
                            {"id": i, "thread_id": "t", "sender_email": "",
                             "sender_name": "", "subject": "?", "snippet": "",
                             "internal_date": "0", "label_ids": []}
                            for i in ids
                        ])

    rc = cli._run_report(limit=10, output_json=True)
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["message_count"] == 2
    assert payload["unparseable_sender_count"] == 2
    assert payload["senders"] == []
    assert payload["run_id"]
