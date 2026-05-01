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


def test_unimplemented_phase2_subcommand_errors():
    """`label` / `archive` / `undo` must NOT be registered in Phase 1."""
    runner = CliRunner()
    for sub in ("label", "archive", "undo"):
        result = runner.invoke(cli.main, [sub, "--help"])
        assert result.exit_code != 0, f"Phase 2/3 subcommand '{sub}' should not be registered yet"


# ---------------------------------------------------------------------------
# Menu rendering
# ---------------------------------------------------------------------------


def test_menu_text_only_has_phase1_options():
    keys = {opt.key for opt in MENU_OPTIONS}
    assert keys == {"1", "5", "q"}


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
