"""Tests for `gmailwiz.classifier`.

All tests mock the Anthropic client — no real API calls are made.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gmailwiz import db as gw_db
from gmailwiz import classifier
from gmailwiz.categories import Category
from gmailwiz.classifier import (
    PROMPT_VERSION,
    MissingAPIKeyError,
    SenderInput,
    SenderSample,
    _require_api_key,
    build_user_prompt,
    classify_senders,
    parse_classifier_response,
)


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "state.db"
    c = gw_db.connect(db_path)
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Prompt shape
# ---------------------------------------------------------------------------


def test_build_user_prompt_includes_email_and_subject():
    senders = [
        SenderInput(
            email="news@example.com",
            display_name="Example News",
            samples=[SenderSample(subject="Daily digest", snippet="Top stories today...")],
        )
    ]
    prompt = build_user_prompt(senders)
    assert "news@example.com" in prompt
    assert "Daily digest" in prompt
    assert "Top stories today" in prompt
    # The prompt asks for JSON conforming to the documented schema.
    assert "JSON" in prompt or "json" in prompt


def test_build_user_prompt_truncates_long_snippet():
    long_snippet = "x" * 500
    senders = [
        SenderInput(
            email="a@b.com",
            samples=[SenderSample(subject="hi", snippet=long_snippet)],
        )
    ]
    prompt = build_user_prompt(senders)
    # 200-char cap + "..." marker
    assert "x" * 200 + "..." in prompt
    assert "x" * 201 not in prompt


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_parse_valid_json():
    text = '{"results":[{"email":"a@b.com","category":"promotional"},{"email":"c@d.com","category":"newsletter"}]}'
    out = parse_classifier_response(text)
    assert out == {"a@b.com": Category.PROMOTIONAL, "c@d.com": Category.NEWSLETTER}


def test_parse_strips_markdown_fences():
    text = "```json\n{\"results\":[{\"email\":\"a@b.com\",\"category\":\"personal\"}]}\n```"
    out = parse_classifier_response(text)
    assert out == {"a@b.com": Category.PERSONAL}


def test_parse_extracts_json_object_from_prose():
    text = (
        "Sure! Here is the JSON: "
        '{"results":[{"email":"x@y.com","category":"transactional"}]} '
        "Hope that helps."
    )
    out = parse_classifier_response(text)
    assert out == {"x@y.com": Category.TRANSACTIONAL}


def test_parse_malformed_json_returns_empty():
    assert parse_classifier_response("not json at all") == {}
    assert parse_classifier_response("") == {}
    assert parse_classifier_response("{ broken") == {}


def test_parse_unknown_category_marked_unknown():
    # The model is forbidden to return "unknown", but if it does, surface it.
    text = '{"results":[{"email":"a@b.com","category":"spam"},{"email":"c@d.com","category":"unknown"}]}'
    out = parse_classifier_response(text)
    assert out == {"a@b.com": Category.UNKNOWN, "c@d.com": Category.UNKNOWN}


def test_parse_lowercases_email_keys():
    text = '{"results":[{"email":"FOO@Bar.com","category":"promotional"}]}'
    out = parse_classifier_response(text)
    assert out == {"foo@bar.com": Category.PROMOTIONAL}


def test_parse_skips_non_dict_entries():
    text = '{"results":["nope",{"email":"a@b.com","category":"newsletter"}]}'
    out = parse_classifier_response(text)
    assert out == {"a@b.com": Category.NEWSLETTER}


# ---------------------------------------------------------------------------
# End-to-end classify_senders, with mocked Anthropic
# ---------------------------------------------------------------------------


def _fake_anthropic_response(text: str):
    """Build a minimal stand-in that matches the SDK's content[0].text shape."""
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


def test_classify_senders_calls_model_then_caches(conn):
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_anthropic_response(
        '{"results":[{"email":"a@b.com","category":"newsletter"}]}'
    )

    senders = [SenderInput(email="a@b.com", samples=[SenderSample(subject="hi")])]
    out = classify_senders(senders, conn=conn, client=fake_client)

    assert fake_client.messages.create.call_count == 1
    assert out["a@b.com"].category is Category.NEWSLETTER
    assert out["a@b.com"].source == "model"

    # Second call should hit cache and not invoke the API again.
    fake_client.messages.create.reset_mock()
    out2 = classify_senders(senders, conn=conn, client=fake_client)
    assert fake_client.messages.create.call_count == 0
    assert out2["a@b.com"].category is Category.NEWSLETTER
    assert out2["a@b.com"].source == "cache"


def test_classify_senders_marks_unknown_on_malformed_response(conn):
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_anthropic_response("not json")

    senders = [SenderInput(email="a@b.com", samples=[SenderSample(subject="hi")])]
    out = classify_senders(senders, conn=conn, client=fake_client)

    assert out["a@b.com"].category is Category.UNKNOWN
    assert out["a@b.com"].source == "unknown"
    # Unknown senders are NOT cached — verify by checking the senders table.
    assert gw_db.get_sender(conn, "a@b.com") is None


def test_classify_senders_marks_unknown_on_out_of_vocab(conn):
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_anthropic_response(
        '{"results":[{"email":"a@b.com","category":"spam"}]}'
    )
    senders = [SenderInput(email="a@b.com", samples=[SenderSample(subject="hi")])]
    out = classify_senders(senders, conn=conn, client=fake_client)
    assert out["a@b.com"].category is Category.UNKNOWN
    assert out["a@b.com"].source == "unknown"


def test_classify_senders_surfaces_api_errors(conn):
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = RuntimeError("boom")

    senders = [SenderInput(email="a@b.com", samples=[SenderSample(subject="hi")])]
    out = classify_senders(senders, conn=conn, client=fake_client)

    assert out["a@b.com"].category is Category.UNKNOWN
    assert out["a@b.com"].error is not None
    assert "RuntimeError" in out["a@b.com"].error


def test_classify_senders_dedupes_by_normalised_email(conn):
    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_anthropic_response(
        '{"results":[{"email":"a@b.com","category":"personal"}]}'
    )
    senders = [
        SenderInput(email="A@B.com", samples=[SenderSample(subject="one")]),
        SenderInput(email="a@b.com", samples=[SenderSample(subject="two")]),
    ]
    out = classify_senders(senders, conn=conn, client=fake_client)
    # Only one entry, only one API call regardless of input case.
    assert list(out.keys()) == ["a@b.com"]
    assert fake_client.messages.create.call_count == 1


def test_classify_senders_only_calls_for_uncached(conn):
    # Pre-populate cache for one of two senders.
    gw_db.upsert_sender(
        conn,
        email="cached@example.com",
        display_name=None,
        category=Category.PROMOTIONAL,
        prompt_version=PROMPT_VERSION,
        model="claude-test",
    )

    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_anthropic_response(
        '{"results":[{"email":"new@example.com","category":"transactional"}]}'
    )

    senders = [
        SenderInput(email="cached@example.com"),
        SenderInput(email="new@example.com", samples=[SenderSample(subject="hi")]),
    ]
    out = classify_senders(senders, conn=conn, client=fake_client)

    # Only the uncached one was sent to the model.
    assert fake_client.messages.create.call_count == 1
    prompt_arg = fake_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "new@example.com" in prompt_arg
    assert "cached@example.com" not in prompt_arg

    assert out["cached@example.com"].source == "cache"
    assert out["cached@example.com"].category is Category.PROMOTIONAL
    assert out["new@example.com"].source == "model"
    assert out["new@example.com"].category is Category.TRANSACTIONAL


# ---------------------------------------------------------------------------
# API key plumbing
# ---------------------------------------------------------------------------


def test_require_api_key_raises_missing_api_key_error_when_unset(monkeypatch):
    """When the env var is missing, raise the domain exception (NOT sys.exit).

    SystemExit is a BaseException — the interactive menu can't catch it via
    `except Exception`, which would terminate the whole process. Using a
    RuntimeError subclass lets the menu recover.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(MissingAPIKeyError) as excinfo:
        _require_api_key()
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


def test_require_api_key_returns_value_when_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert _require_api_key() == "sk-ant-test"


# ---------------------------------------------------------------------------
# Cache invalidation paths
# ---------------------------------------------------------------------------


def test_classify_senders_reclassifies_on_stale_ttl(conn):
    """Cache rows older than the TTL must trigger a fresh API call."""
    from datetime import datetime, timedelta, timezone

    # Pre-populate cache with a stale timestamp (35 days ago, past 30-day TTL).
    stale = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat(timespec="seconds")
    gw_db.upsert_sender(
        conn,
        email="stale@example.com",
        display_name="Stale Sender",
        category=Category.PROMOTIONAL,
        prompt_version=PROMPT_VERSION,
        model="claude-test",
        classified_at=stale,
    )

    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_anthropic_response(
        '{"results":[{"email":"stale@example.com","category":"newsletter"}]}'
    )

    senders = [SenderInput(email="stale@example.com", samples=[SenderSample(subject="hi")])]
    out = classify_senders(senders, conn=conn, client=fake_client)

    # API was called (stale cache miss).
    assert fake_client.messages.create.call_count == 1
    # New classification overrode the old one.
    assert out["stale@example.com"].category is Category.NEWSLETTER
    assert out["stale@example.com"].source == "model"


def test_classify_senders_reclassifies_on_prompt_version_bump(conn):
    """Cache rows from an old prompt_version must trigger fresh classification."""
    gw_db.upsert_sender(
        conn,
        email="old-prompt@example.com",
        display_name="Old Prompt Sender",
        category=Category.PROMOTIONAL,
        prompt_version="p0-old-version",  # different from current
        model="claude-test",
    )

    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_anthropic_response(
        '{"results":[{"email":"old-prompt@example.com","category":"transactional"}]}'
    )

    senders = [SenderInput(email="old-prompt@example.com", samples=[SenderSample(subject="hi")])]
    out = classify_senders(senders, conn=conn, client=fake_client)

    assert fake_client.messages.create.call_count == 1
    assert out["old-prompt@example.com"].category is Category.TRANSACTIONAL


def test_classify_senders_skips_unknown_cache_rows(conn):
    """Cache rows that parse as UNKNOWN should NOT count as cache hits.

    Pre-fix bug: a legacy/hand-edited row containing 'spam' would be parsed
    to UNKNOWN by `db.get_sender`, accepted as fresh, and the user would
    perpetually see UNKNOWN with no opportunity for re-classification.
    """
    # Bypass upsert_sender's category enum and write a raw bad value.
    conn.execute(
        "INSERT INTO senders (email, display_name, category, classified_at, prompt_version, model) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("bad@example.com", "", "spam", "2026-04-30T12:00:00+00:00", PROMPT_VERSION, "claude-test"),
    )
    conn.commit()

    fake_client = MagicMock()
    fake_client.messages.create.return_value = _fake_anthropic_response(
        '{"results":[{"email":"bad@example.com","category":"newsletter"}]}'
    )
    senders = [SenderInput(email="bad@example.com", samples=[SenderSample(subject="hi")])]
    out = classify_senders(senders, conn=conn, client=fake_client)

    assert fake_client.messages.create.call_count == 1
    assert out["bad@example.com"].category is Category.NEWSLETTER
