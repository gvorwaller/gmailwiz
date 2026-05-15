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


def test_archive_subcommand_help_lists_flags():
    """Phase 3 archive subcommand is registered and exposes --from-run-id /
    --commit / --run-id / --json / --yes."""
    runner = CliRunner()
    result = runner.invoke(cli.main, ["archive", "--help"])
    assert result.exit_code == 0, result.output
    for flag in ("--from-run-id", "--commit", "--run-id", "--json", "--yes"):
        assert flag in result.output


def test_undo_subcommand_help_requires_run_id():
    runner = CliRunner()
    result = runner.invoke(cli.main, ["undo", "--help"])
    assert result.exit_code == 0, result.output
    for flag in ("--run-id", "--json", "--yes"):
        assert flag in result.output


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


def test_menu_text_full_phase_options():
    """All phases shipped: 1 (report), 2/3 (label preview/apply),
    4 (undo any), 5 (re-auth), 6/7 (archive preview/apply),
    8 (run full cycle one-pass), q (quit)."""
    keys = {opt.key for opt in MENU_OPTIONS}
    assert keys == {"1", "2", "3", "4", "5", "6", "7", "8", "q"}


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


# ---------------------------------------------------------------------------
# Menu — option 4 (undo) cross-phase picker
# ---------------------------------------------------------------------------


def test_menu_option_4_no_undoable_runs():
    runner = CliRunner()
    result = runner.invoke(cli.main, [], input="4\nq\n")
    assert result.exit_code == 0, result.output
    assert "No previously-applied runs to undo" in result.output


def test_menu_option_4_lists_label_and_archive_runs(monkeypatch):
    """Picker for option 4 must surface BOTH label and archive runs in
    `committed`/`partially_failed` status, with the phase displayed so
    the user knows what each row is."""
    from gmailwiz import db as gw_db

    runner = CliRunner()
    with gw_db.open_db() as conn:
        # Committed label run with one applied row.
        label_rid = gw_db.create_run(
            conn, phase="label", category_filter="promotional",
            limit_count=10, dry_run=False, status="committed",
        )
        gw_db.append_audit_entry(
            conn, run_id=label_rid, action="add_label", message_id="m_lbl",
            before_label_ids=["INBOX"], after_label_ids=["INBOX", "Label_99"],
            status="applied",
        )
        # Committed archive run with one applied row.
        archive_rid = gw_db.create_run(
            conn, phase="archive", category_filter="promotional",
            limit_count=10, dry_run=False, status="committed",
        )
        gw_db.append_audit_entry(
            conn, run_id=archive_rid, action="archive", message_id="m_arc",
            before_label_ids=["INBOX", "Label_99"], after_label_ids=["Label_99"],
            status="applied",
        )

    with patch.object(cli, "_run_undo") as mock_undo:
        mock_undo.return_value = 0
        # 4 → q (cancel after seeing the picker, just to inspect output)
        result = runner.invoke(cli.main, [], input="4\nq\nq\n")
    assert result.exit_code == 0, result.output
    out = result.output
    # Both runs surface; tags differentiate.
    assert "label/promotional" in out
    assert "archive/promotional" in out


def test_menu_option_4_dispatches_to_run_undo(monkeypatch):
    from gmailwiz import db as gw_db

    runner = CliRunner()
    with gw_db.open_db() as conn:
        rid = gw_db.create_run(
            conn, phase="label", category_filter="promotional",
            limit_count=10, dry_run=False, status="committed",
        )
        gw_db.append_audit_entry(
            conn, run_id=rid, action="add_label", message_id="m1",
            before_label_ids=["INBOX"], after_label_ids=["INBOX", "Label_99"],
            status="applied",
        )

    with patch.object(cli, "_run_undo") as mock_undo:
        mock_undo.return_value = 0
        result = runner.invoke(cli.main, [], input="4\n1\nq\n")
    assert result.exit_code == 0, result.output
    mock_undo.assert_called_once()
    assert mock_undo.call_args.kwargs["run_id"] == rid


def test_menu_option_4_picker_heals_stale_planned_run(monkeypatch):
    """Codex P1 regression coverage at the menu layer: a run whose
    post-apply `update_run_status` write FAILED (audit log terminal
    but runs.status stuck at 'planned') must show up in option 4
    after auto-heal — the user must NOT be stranded."""
    from gmailwiz import db as gw_db

    runner = CliRunner()
    with gw_db.open_db() as conn:
        # Hand-construct a stale-status run: status='planned' but all
        # audit rows are 'applied' (the worst-case integrity gap).
        rid = gw_db.create_run(
            conn, phase="label", category_filter="promotional",
            limit_count=10, dry_run=True, status="planned",
        )
        for mid in ("m1", "m2"):
            audit_id = gw_db.append_audit_entry(
                conn, run_id=rid, action="add_label", message_id=mid,
                before_label_ids=["INBOX"],
                after_label_ids=["INBOX", "Label_99"],
                status="planned",
            )
            gw_db.update_audit_entry(conn, audit_id=audit_id, status="applied")

    # Open option 4 and inspect the picker. After auto-heal, the run
    # should be visible as committed/promotional and selectable.
    with patch.object(cli, "_run_undo") as mock_undo:
        mock_undo.return_value = 0
        result = runner.invoke(cli.main, [], input="4\n1\nq\n")
    assert result.exit_code == 0, result.output
    assert "label/promotional" in result.output
    mock_undo.assert_called_once()
    assert mock_undo.call_args.kwargs["run_id"] == rid

    # Persisted state was healed.
    with gw_db.open_db() as conn:
        run = gw_db.get_run(conn, rid)
    assert run["status"] == "committed"
    assert run["dry_run"] == 0


def test_menu_option_4_partially_failed_picker_shows_still_applied(monkeypatch):
    """Picker disambiguation: a `partially_failed` run shows
    'X still applied' instead of 'X message(s)' so the user reads the
    count as remaining work, not original mutation count."""
    from gmailwiz import db as gw_db

    runner = CliRunner()
    with gw_db.open_db() as conn:
        rid = gw_db.create_run(
            conn, phase="label", category_filter="promotional",
            limit_count=10, dry_run=False, status="partially_failed",
        )
        # 2 applied + 1 failed = "2 still applied" displayed.
        for mid in ("m1", "m2"):
            gw_db.append_audit_entry(
                conn, run_id=rid, action="add_label", message_id=mid,
                before_label_ids=["INBOX"],
                after_label_ids=["INBOX", "Label_99"],
                status="applied",
            )
        gw_db.append_audit_entry(
            conn, run_id=rid, action="add_label", message_id="m3",
            before_label_ids=["INBOX"], after_label_ids=["INBOX"],
            status="failed", error="apply failed earlier",
        )

    result = runner.invoke(cli.main, [], input="4\nq\nq\n")
    assert result.exit_code == 0, result.output
    assert "2 still applied" in result.output
    # And NOT "2 message(s)" (the disambiguation point).
    assert "2 message(s)" not in result.output


def test_menu_option_4_filters_uncommitable_runs(monkeypatch):
    """Planned, undone, failed runs must NOT show in the undo picker."""
    from gmailwiz import db as gw_db

    runner = CliRunner()
    with gw_db.open_db() as conn:
        for status in ("planned", "undone", "failed"):
            rid = gw_db.create_run(
                conn, phase="label", category_filter="promotional",
                limit_count=10, dry_run=True, status=status,
            )
            gw_db.append_audit_entry(
                conn, run_id=rid, action="add_label", message_id=f"m_{status}",
                before_label_ids=["INBOX"], after_label_ids=["INBOX", "Label_99"],
                # Even applied rows shouldn't make non-committed runs visible.
                status="applied" if status != "planned" else "planned",
            )

    result = runner.invoke(cli.main, [], input="4\nq\n")
    assert result.exit_code == 0, result.output
    assert "No previously-applied runs to undo" in result.output


# ---------------------------------------------------------------------------
# Menu — options 6 (preview archive) + 7 (apply archive)
# ---------------------------------------------------------------------------


def test_menu_option_6_no_label_runs_message():
    runner = CliRunner()
    result = runner.invoke(cli.main, [], input="6\nq\n")
    assert result.exit_code == 0, result.output
    assert "No applied label runs to archive from" in result.output


def test_menu_option_6_dispatches_to_run_archive_plan(monkeypatch):
    from gmailwiz import db as gw_db

    runner = CliRunner()
    with gw_db.open_db() as conn:
        rid = gw_db.create_run(
            conn, phase="label", category_filter="promotional",
            limit_count=10, dry_run=False, status="committed",
        )
        gw_db.append_audit_entry(
            conn, run_id=rid, action="add_label", message_id="m1",
            before_label_ids=["INBOX"], after_label_ids=["INBOX", "Label_99"],
            status="applied",
        )

    with patch.object(cli, "_run_archive_plan") as mock_plan:
        mock_plan.return_value = 0
        result = runner.invoke(cli.main, [], input="6\n1\nq\n")
    assert result.exit_code == 0, result.output
    mock_plan.assert_called_once()
    assert mock_plan.call_args.kwargs["source_run_id"] == rid


def test_menu_option_7_no_planned_archive_runs():
    runner = CliRunner()
    result = runner.invoke(cli.main, [], input="7\nq\n")
    assert result.exit_code == 0, result.output
    assert "No previewed archive runs to apply" in result.output


def test_menu_option_7_dispatches_to_run_archive_commit(monkeypatch):
    from gmailwiz import db as gw_db

    runner = CliRunner()
    with gw_db.open_db() as conn:
        rid = gw_db.create_run(
            conn, phase="archive", category_filter="promotional",
            limit_count=10, dry_run=True, status="planned",
        )
        gw_db.append_audit_entry(
            conn, run_id=rid, action="archive", message_id="m1",
            before_label_ids=["INBOX", "Label_99"], after_label_ids=["Label_99"],
            status="planned",
        )

    with patch.object(cli, "_run_archive_commit") as mock_commit:
        mock_commit.return_value = 0
        result = runner.invoke(cli.main, [], input="7\n1\nq\n")
    assert result.exit_code == 0, result.output
    mock_commit.assert_called_once()
    assert mock_commit.call_args.kwargs["run_id"] == rid


def test_archive_commit_json_requires_yes():
    runner = CliRunner()
    result = runner.invoke(
        cli.main, ["archive", "--commit", "--run-id", "x", "--json"]
    )
    assert result.exit_code == 2
    assert "--commit with --json requires --yes" in result.output


def test_undo_subcommand_json_requires_yes():
    runner = CliRunner()
    result = runner.invoke(cli.main, ["undo", "--run-id", "x", "--json"])
    assert result.exit_code == 2
    # Tight match: prevent regression of the prior `--undo` typo.
    assert "undo with --json requires --yes" in result.output
    assert "--undo with" not in result.output


def test_run_archive_commit_json_without_yes_refused_at_helper(capsys):
    """Defense-in-depth: even if a future caller bypasses the click
    upstream gate, `_run_archive_commit` itself must refuse json+notyes."""
    rc = cli._run_archive_commit(
        run_id="any", output_json=True, assume_yes=False
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "Refusing to commit under JSON" in err


def test_run_undo_json_without_yes_refused_at_helper(capsys):
    """Defense-in-depth: `_run_undo` must refuse json+notyes."""
    rc = cli._run_undo(run_id="any", output_json=True, assume_yes=False)
    assert rc == 2
    err = capsys.readouterr().err
    assert "Refusing to undo under JSON" in err


def test_run_archive_plan_unknown_source_run_id_errors(monkeypatch, capsys):
    """Archive PREVIEW path must defer OAuth until after source-run-id
    validation, just like the commit/undo helpers. A typo'd
    `--from-run-id` shouldn't trigger a token refresh."""
    from gmailwiz import auth as gw_auth

    def _must_not_be_called(**kw):
        raise AssertionError(
            "_run_archive_plan triggered OAuth before validating "
            "the source run id — validation order regression"
        )

    monkeypatch.setattr(gw_auth, "get_credentials", _must_not_be_called)
    rc = cli._run_archive_plan(
        source_run_id="does-not-exist", output_json=False
    )
    assert rc == 2
    assert "No run with id" in capsys.readouterr().err


def test_run_archive_plan_rejects_non_label_source(monkeypatch, capsys):
    """Source must be a label run (not a report or archive run)."""
    from gmailwiz import auth as gw_auth
    from gmailwiz import db as gw_db

    monkeypatch.setattr(gw_auth, "get_credentials",
                        lambda **kw: (_ for _ in ()).throw(
                            AssertionError("OAuth before validation")))

    with gw_db.open_db() as conn:
        rid = gw_db.create_run(
            conn, phase="report", limit_count=10, dry_run=True, status="committed"
        )

    rc = cli._run_archive_plan(source_run_id=rid, output_json=False)
    assert rc == 2
    err = capsys.readouterr().err
    assert "phase=" in err
    assert "label" in err


def test_run_archive_commit_unknown_run_id_errors(monkeypatch, capsys):
    """OAuth must NOT trigger before run-id validation (matches the
    label-commit pattern from Phase 2 chunk 1)."""
    from gmailwiz import auth as gw_auth

    def _must_not_be_called(**kw):
        raise AssertionError(
            "_run_archive_commit triggered OAuth before validating "
            "the run id — validation order regression"
        )

    monkeypatch.setattr(gw_auth, "get_credentials", _must_not_be_called)
    rc = cli._run_archive_commit(
        run_id="does-not-exist", output_json=False, assume_yes=True
    )
    assert rc == 2
    assert "No run with id" in capsys.readouterr().err


def test_archive_cmd_corrupted_category_validates_before_oauth(monkeypatch):
    """archive preview's pre-OAuth validation must reject a corrupted
    `category_filter` cleanly (rc=2 + clean message) without paying the
    OAuth round-trip cost."""
    from gmailwiz import auth as gw_auth
    from gmailwiz import db as gw_db

    runner = CliRunner()
    with gw_db.open_db() as conn:
        rid = gw_db.create_run(
            conn, phase="label", category_filter="garbage_value",
            limit_count=10, dry_run=False, status="committed",
        )
        gw_db.append_audit_entry(
            conn, run_id=rid, action="add_label", message_id="m1",
            before_label_ids=["INBOX"], after_label_ids=["INBOX", "Label_99"],
            status="applied",
        )

    def _no_oauth(**kw):
        raise AssertionError("OAuth fired before validation")
    monkeypatch.setattr(gw_auth, "get_credentials", _no_oauth)

    result = runner.invoke(cli.main, ["archive", "--from-run-id", rid])
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert "garbage_value" in result.output


def test_archive_cmd_catches_value_error_from_helper(monkeypatch):
    """Defense-in-depth: archive_cmd's `except ValueError` block must
    convert an unhandled ValueError from a helper into rc=2 + clean
    message, not let a traceback escape click. Stub the helper directly
    so the catch is exercised (the real helper now refuses these cases
    earlier with rc=2, so this code path is reachable only if a future
    refactor puts a ValueError back into the call chain)."""
    runner = CliRunner()

    def boom(**kwargs):
        raise ValueError("simulated downstream validation error")

    monkeypatch.setattr(cli, "_run_archive_plan", boom)
    result = runner.invoke(
        cli.main, ["archive", "--from-run-id", "any-id"]
    )
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert "simulated downstream validation error" in result.output


def test_undo_cmd_unknown_category_validates_before_oauth(monkeypatch):
    """undo's pre-OAuth UNKNOWN refusal must reject a corrupted (or
    legitimately UNKNOWN) `category_filter` cleanly without paying the
    OAuth round-trip cost."""
    from gmailwiz import auth as gw_auth
    from gmailwiz import db as gw_db

    runner = CliRunner()
    with gw_db.open_db() as conn:
        rid = gw_db.create_run(
            conn, phase="label", category_filter="unknown",
            limit_count=10, dry_run=False, status="committed",
        )
        gw_db.append_audit_entry(
            conn, run_id=rid, action="add_label", message_id="m1",
            before_label_ids=["INBOX"], after_label_ids=["INBOX", "Label_99"],
            status="applied",
        )

    def _no_oauth(**kw):
        raise AssertionError("OAuth fired before UNKNOWN validation")
    monkeypatch.setattr(gw_auth, "get_credentials", _no_oauth)

    result = runner.invoke(cli.main, ["undo", "--run-id", rid, "--yes"])
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert "non-classifiable" in result.output


def test_undo_cmd_catches_value_error_from_helper(monkeypatch):
    """Defense-in-depth: undo_cmd's `except ValueError` block must
    convert an unhandled ValueError into rc=2 + clean message."""
    runner = CliRunner()

    def boom(**kwargs):
        raise ValueError("simulated downstream undo error")

    monkeypatch.setattr(cli, "_run_undo", boom)
    result = runner.invoke(cli.main, ["undo", "--run-id", "any-id", "--yes"])
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert "simulated downstream undo error" in result.output


def test_label_cmd_catches_value_error_from_helper(monkeypatch):
    """Defense-in-depth: label_cmd's `except ValueError` blocks (preview
    + commit) must convert unhandled ValueError into rc=2 + clean."""
    runner = CliRunner()

    def boom(**kwargs):
        raise ValueError("simulated downstream label error")

    # Commit branch.
    monkeypatch.setattr(cli, "_run_label_commit", boom)
    result = runner.invoke(
        cli.main, ["label", "--commit", "--run-id", "any-id", "--yes"]
    )
    assert result.exit_code == 2, result.output
    assert "simulated downstream label error" in result.output

    # Preview branch.
    monkeypatch.setattr(cli, "_run_label_plan", boom)
    result = runner.invoke(
        cli.main, ["label", "--category", "promotional"]
    )
    assert result.exit_code == 2, result.output
    assert "simulated downstream label error" in result.output


def test_label_cmd_catches_keyboard_interrupt_cleanly(monkeypatch):
    """KbI raised mid-run from a helper must produce rc=130 + clean
    'Cancelled.' rather than a Python traceback. Not just for label —
    same pattern is exercised for archive and undo via the test below."""
    runner = CliRunner()

    def kbi(**kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "_run_label_plan", kbi)
    result = runner.invoke(cli.main, ["label", "--category", "promotional"])
    assert result.exit_code == 130, result.output
    assert "Cancelled." in result.output
    assert "Traceback" not in result.output


def test_archive_cmd_catches_keyboard_interrupt_cleanly(monkeypatch):
    runner = CliRunner()

    def kbi(**kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "_run_archive_plan", kbi)
    result = runner.invoke(cli.main, ["archive", "--from-run-id", "any-id"])
    assert result.exit_code == 130, result.output
    assert "Cancelled." in result.output


def test_undo_cmd_catches_keyboard_interrupt_cleanly(monkeypatch):
    runner = CliRunner()

    def kbi(**kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "_run_undo", kbi)
    result = runner.invoke(cli.main, ["undo", "--run-id", "any-id", "--yes"])
    assert result.exit_code == 130, result.output
    assert "Cancelled." in result.output


def test_label_cmd_catches_file_not_found_from_helper(monkeypatch):
    """FileNotFoundError (typically: missing credentials.json) → rc=2
    + clean type+message."""
    runner = CliRunner()

    def fnf(**kwargs):
        raise FileNotFoundError("credentials.json not found at /tmp/x")

    monkeypatch.setattr(cli, "_run_label_plan", fnf)
    result = runner.invoke(cli.main, ["label", "--category", "promotional"])
    assert result.exit_code == 2, result.output
    assert "FileNotFoundError" in result.output
    assert "credentials.json" in result.output


def test_archive_cmd_catches_file_not_found_from_helper(monkeypatch):
    runner = CliRunner()

    def fnf(**kwargs):
        raise FileNotFoundError("credentials.json missing")

    monkeypatch.setattr(cli, "_run_archive_plan", fnf)
    result = runner.invoke(cli.main, ["archive", "--from-run-id", "any-id"])
    assert result.exit_code == 2, result.output
    assert "FileNotFoundError" in result.output


def test_undo_cmd_catches_file_not_found_from_helper(monkeypatch):
    runner = CliRunner()

    def fnf(**kwargs):
        raise FileNotFoundError("credentials.json missing")

    monkeypatch.setattr(cli, "_run_undo", fnf)
    result = runner.invoke(cli.main, ["undo", "--run-id", "any-id", "--yes"])
    assert result.exit_code == 2, result.output
    assert "FileNotFoundError" in result.output


def test_run_undo_unknown_run_id_errors(monkeypatch, capsys):
    from gmailwiz import auth as gw_auth

    def _must_not_be_called(**kw):
        raise AssertionError(
            "_run_undo triggered OAuth before validating the run id"
        )

    monkeypatch.setattr(gw_auth, "get_credentials", _must_not_be_called)
    rc = cli._run_undo(
        run_id="does-not-exist", output_json=False, assume_yes=True
    )
    assert rc == 2
    assert "No run with id" in capsys.readouterr().err


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
