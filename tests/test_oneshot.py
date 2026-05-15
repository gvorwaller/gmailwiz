"""Tests for `gmailwiz.oneshot.run_one_pass` — the end-to-end orchestrator.

Strategy: stub at module boundaries.

* `gmail_client.*` — list, metadata, label mutations.
* `oneshot.classify_senders` — we pre-seed the senders cache in the DB and
  use a no-op classifier stub, so the test doesn't need the Anthropic SDK.

The most important invariants verified here:

1. Snapshot stability — once the inbox is captured, neither
   ``list_unread_message_ids`` nor ``get_message_metadata`` may be called
   again, even when later categories operate on the snapshot.
2. Headless auth boundary — when ``auth.get_credentials(interactive=False)``
   raises ``AuthRequired``, ``oneshot.run_one_pass`` is never reached and
   the CLI helper translates the error to ``auth_required`` exit 2.
3. Partial failure isolation — one category's exception doesn't kill the
   others, and the final status reflects "partial_failure".
4. The shape of ``to_dict()`` so future JSON consumers (the M2 trigger
   service / Drafts → Telegram) have a stable contract.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from gmailwiz import auth as gw_auth
from gmailwiz import db as gw_db
from gmailwiz import gmail_client
from gmailwiz import oneshot as gw_oneshot
from gmailwiz import planning
from gmailwiz.categories import CLASSIFIABLE_CATEGORIES, Category
from gmailwiz.classifier import ClassificationResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    c = gw_db.connect()
    yield c
    c.close()


def _seed_senders(conn, *, by_email: dict[str, Category]) -> None:
    for email, cat in by_email.items():
        gw_db.upsert_sender(
            conn,
            email=email,
            display_name=None,
            category=cat,
            prompt_version="test",
            model="test-model",
        )


def _msg(
    *,
    mid: str,
    sender: str,
    subject: str = "subj",
    label_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": mid,
        "thread_id": "t",
        "sender_email": sender,
        "sender_name": "",
        "subject": subject,
        "snippet": "",
        "internal_date": "1700000000000",
        "label_ids": label_ids or ["INBOX", "UNREAD"],
    }


def _stub_classifier(monkeypatch, *, classifications: dict[str, Category]) -> None:
    """Replace ``oneshot.classify_senders`` with a no-op that returns
    classifications the caller specifies. The real cache is also seeded
    so ``planning.build_label_plan`` finds them when filtering candidates.
    """

    def _fake_classify(senders, *, conn, on_progress=None, **_kw):
        out = {}
        for s in senders:
            cat = classifications.get(s.email, Category.UNKNOWN)
            out[s.email] = ClassificationResult(
                email=s.email,
                category=cat,
                source="cache",
                error=None,
            )
        return out

    monkeypatch.setattr(gw_oneshot, "classify_senders", _fake_classify)


def _stub_gmail_basic(
    monkeypatch,
    *,
    ids: list[str],
    messages: list[dict],
    existing_labels: dict[str, str] | None = None,
    label_id: str = "Label_99",
) -> dict[str, list[str]]:
    """Stub the Gmail surface used end-to-end. Returns a dict that the test
    can inspect to see which messages were modified.
    """
    monkeypatch.setattr(
        gmail_client, "list_unread_message_ids",
        lambda creds, max_results=100, query="is:unread in:inbox": ids,
    )
    monkeypatch.setattr(
        gmail_client, "get_message_metadata",
        lambda creds, message_ids: messages,
    )
    monkeypatch.setattr(
        gmail_client, "list_existing_labels",
        lambda creds: dict(existing_labels or {}),
    )
    monkeypatch.setattr(
        gmail_client, "ensure_labels_exist",
        lambda creds, names: {n: label_id for n in names},
    )

    modify_log: dict[str, list[str]] = {"add": [], "remove": []}

    def fake_modify(creds, *, message_id, add_label_ids=(), remove_label_ids=()):
        # Each add gets logged; if INBOX is being removed, log that too.
        for lid in add_label_ids:
            modify_log["add"].append(f"{message_id}:{lid}")
        for lid in remove_label_ids:
            modify_log["remove"].append(f"{message_id}:{lid}")
        return {
            "id": message_id,
            "threadId": "t",
            "labelIds": [
                x for x in ["INBOX", "UNREAD", label_id] if x not in remove_label_ids
            ],
        }

    monkeypatch.setattr(gmail_client, "modify_labels", fake_modify)
    return modify_log


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_run_one_pass_happy_path(monkeypatch, conn):
    """Four senders, one per category, all label + archive successfully.

    Exit shape: status='success', one report run + 4 label runs + 4 archive
    runs in the DB; modify_log shows label-add for each of the 4 messages,
    and INBOX-remove for each archive.
    """
    senders = {
        "p@x.com": Category.PROMOTIONAL,
        "t@x.com": Category.TRANSACTIONAL,
        "n@x.com": Category.NEWSLETTER,
        "f@x.com": Category.PERSONAL,
    }
    _seed_senders(conn, by_email=senders)
    msgs = [_msg(mid=f"m_{e}", sender=e) for e in senders]

    modify_log = _stub_gmail_basic(monkeypatch, ids=[m["id"] for m in msgs], messages=msgs)
    _stub_classifier(monkeypatch, classifications=senders)

    result = gw_oneshot.run_one_pass(
        creds=MagicMock(),
        limit=100,
        archive=True,
    )

    assert result.status == "success"
    assert result.snapshot_size == 4
    assert sorted(result.snapshot_message_ids) == sorted(m["id"] for m in msgs)
    assert {c.category for c in result.categories} == {c.value for c in CLASSIFIABLE_CATEGORIES}
    for cat in result.categories:
        assert cat.labels_applied == 1, f"{cat.category}: expected 1 labeled"
        assert cat.labels_failed == 0
        assert cat.archive_applied == 1
        assert cat.archive_failed == 0
        assert cat.error is None

    # 4 add (one per message) + 4 remove (INBOX archive)
    assert len(modify_log["add"]) == 4
    assert len(modify_log["remove"]) == 4
    assert all(":INBOX" in entry for entry in modify_log["remove"])


def test_run_one_pass_empty_inbox(monkeypatch, conn):
    """No unread messages → status=success, snapshot_size=0, no runs."""
    _stub_gmail_basic(monkeypatch, ids=[], messages=[])
    _stub_classifier(monkeypatch, classifications={})

    result = gw_oneshot.run_one_pass(creds=MagicMock(), limit=100)

    assert result.status == "success"
    assert result.snapshot_size == 0
    assert result.categories == []
    assert "inbox empty" in " ".join(result.notes).lower()


def test_run_one_pass_no_archive_flag(monkeypatch, conn):
    """`archive=False`: label runs only, archive_run_id stays None for each."""
    senders = {"p@x.com": Category.PROMOTIONAL}
    _seed_senders(conn, by_email=senders)
    msgs = [_msg(mid="m1", sender="p@x.com")]
    modify_log = _stub_gmail_basic(monkeypatch, ids=["m1"], messages=msgs)
    _stub_classifier(monkeypatch, classifications=senders)

    result = gw_oneshot.run_one_pass(creds=MagicMock(), limit=10, archive=False)

    assert result.status == "success"
    promo = next(c for c in result.categories if c.category == "promotional")
    assert promo.labels_applied == 1
    assert promo.archive_run_id is None
    assert promo.archive_applied == 0
    assert modify_log["remove"] == []  # nothing archived


# ---------------------------------------------------------------------------
# Snapshot stability — the load-bearing invariant of the redesign
# ---------------------------------------------------------------------------


def test_run_one_pass_snapshot_only_fetches_once(monkeypatch, conn):
    """``list_unread_message_ids`` and ``get_message_metadata`` may each be
    called at most ONCE during a one-pass run.

    Pre-redesign, each category's ``build_*_plan`` refetched, so cat #2
    would see an inbox already mutated by cat #1 — a real drift bug. This
    test fails on that codepath.
    """
    senders = {
        "p@x.com": Category.PROMOTIONAL,
        "t@x.com": Category.TRANSACTIONAL,
        "n@x.com": Category.NEWSLETTER,
        "f@x.com": Category.PERSONAL,
    }
    _seed_senders(conn, by_email=senders)
    msgs = [_msg(mid=f"m_{e}", sender=e) for e in senders]

    list_calls: list[int] = []
    metadata_calls: list[int] = []

    def fake_list(creds, max_results=100, query="is:unread in:inbox"):
        list_calls.append(1)
        return [m["id"] for m in msgs]

    def fake_metadata(creds, message_ids):
        metadata_calls.append(1)
        return msgs

    monkeypatch.setattr(gmail_client, "list_unread_message_ids", fake_list)
    monkeypatch.setattr(gmail_client, "get_message_metadata", fake_metadata)
    monkeypatch.setattr(gmail_client, "list_existing_labels", lambda creds: {})
    monkeypatch.setattr(
        gmail_client, "ensure_labels_exist", lambda creds, names: {n: "Label_99" for n in names}
    )
    monkeypatch.setattr(
        gmail_client, "modify_labels",
        lambda creds, *, message_id, add_label_ids=(), remove_label_ids=(): {
            "id": message_id, "threadId": "t",
            "labelIds": [x for x in ["INBOX", "UNREAD", "Label_99"] if x not in remove_label_ids],
        },
    )
    _stub_classifier(monkeypatch, classifications=senders)

    result = gw_oneshot.run_one_pass(creds=MagicMock(), limit=100, archive=True)

    assert result.status == "success"
    assert len(list_calls) == 1, (
        f"list_unread_message_ids called {len(list_calls)}× — snapshot broken"
    )
    assert len(metadata_calls) == 1, (
        f"get_message_metadata called {len(metadata_calls)}× — snapshot broken"
    )


def test_run_one_pass_stable_when_live_inbox_shrinks(monkeypatch, conn):
    """If live Gmail state changes after the snapshot, later categories
    still operate on the original snapshot.

    Simulates: snapshot fetch returns m1-m4; subsequent fetches (which
    must NOT happen under the snapshot model) would only return m1. With
    snapshotting correct, all 4 categories still apply.
    """
    senders = {
        "p@x.com": Category.PROMOTIONAL,
        "t@x.com": Category.TRANSACTIONAL,
        "n@x.com": Category.NEWSLETTER,
        "f@x.com": Category.PERSONAL,
    }
    _seed_senders(conn, by_email=senders)
    msgs = [_msg(mid=f"m_{e}", sender=e) for e in senders]

    state = {"call_count": 0}

    def fake_list(creds, max_results=100, query="is:unread in:inbox"):
        state["call_count"] += 1
        # After the first call, pretend the inbox shrunk to just one msg.
        if state["call_count"] == 1:
            return [m["id"] for m in msgs]
        return [msgs[0]["id"]]

    def fake_metadata(creds, message_ids):
        # Same shape — second call would return a truncated set.
        if len(message_ids) == len(msgs):
            return msgs
        return [msgs[0]]

    monkeypatch.setattr(gmail_client, "list_unread_message_ids", fake_list)
    monkeypatch.setattr(gmail_client, "get_message_metadata", fake_metadata)
    monkeypatch.setattr(gmail_client, "list_existing_labels", lambda creds: {})
    monkeypatch.setattr(
        gmail_client, "ensure_labels_exist", lambda creds, names: {n: "Label_99" for n in names}
    )
    monkeypatch.setattr(
        gmail_client, "modify_labels",
        lambda creds, *, message_id, add_label_ids=(), remove_label_ids=(): {
            "id": message_id, "threadId": "t",
            "labelIds": [x for x in ["INBOX", "UNREAD", "Label_99"] if x not in remove_label_ids],
        },
    )
    _stub_classifier(monkeypatch, classifications=senders)

    result = gw_oneshot.run_one_pass(creds=MagicMock(), limit=100, archive=True)
    # All four categories should still apply because the snapshot was
    # captured before the inbox shrank.
    total_labeled = sum(c.labels_applied for c in result.categories)
    assert total_labeled == 4, (
        f"snapshot drift: only {total_labeled}/4 messages labeled"
    )


# ---------------------------------------------------------------------------
# Auth boundary
# ---------------------------------------------------------------------------


def test_cli_run_one_pass_headless_auth_required(monkeypatch, conn):
    """When ``get_credentials(interactive=False)`` raises AuthRequired,
    the CLI wrapper translates it to exit code 2 + auth_required JSON,
    and ``oneshot.run_one_pass`` is never reached.

    Critical because this is the *only* layer between the M2 trigger
    service and a hanging InstalledAppFlow browser launch on a headless box.
    """
    from gmailwiz import cli as gw_cli

    def _raise(**_kw):
        raise gw_auth.AuthRequired("token refresh failed (test)")

    monkeypatch.setattr(gw_auth, "get_credentials", _raise)

    def _boom_run(**_kw):
        raise AssertionError("run_one_pass must NOT be invoked when auth fails")

    monkeypatch.setattr(gw_oneshot, "run_one_pass", _boom_run)

    # JSON path so we can assert payload shape.
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = gw_cli._run_one_pass(
            limit=10, archive=True, output_json=True, interactive_auth=False
        )
    assert rc == 2
    payload = json.loads(buf.getvalue())
    assert payload["status"] == "auth_required"
    assert payload["error_code"] == "auth_required"
    assert "test" in payload["reason"]


# ---------------------------------------------------------------------------
# Partial failure isolation
# ---------------------------------------------------------------------------


def test_run_one_pass_one_category_fails_others_succeed(monkeypatch, conn):
    """``apply_label_plan`` raising mid-run for one category must not
    abort the other three. The result status flips to 'partial_failure'."""
    senders = {
        "p@x.com": Category.PROMOTIONAL,
        "t@x.com": Category.TRANSACTIONAL,
        "n@x.com": Category.NEWSLETTER,
        "f@x.com": Category.PERSONAL,
    }
    _seed_senders(conn, by_email=senders)
    msgs = [_msg(mid=f"m_{e}", sender=e) for e in senders]

    original_apply_label = planning.apply_label_plan
    call_state = {"calls": 0}

    def fake_apply_label(*, creds, conn, run_id, on_progress=None):
        call_state["calls"] += 1
        # Fail on the SECOND category (transactional, by CLASSIFIABLE_CATEGORIES order).
        if call_state["calls"] == 2:
            raise RuntimeError("simulated transient API failure")
        return original_apply_label(creds=creds, conn=conn, run_id=run_id, on_progress=on_progress)

    monkeypatch.setattr(planning, "apply_label_plan", fake_apply_label)
    _stub_gmail_basic(monkeypatch, ids=[m["id"] for m in msgs], messages=msgs)
    _stub_classifier(monkeypatch, classifications=senders)

    result = gw_oneshot.run_one_pass(creds=MagicMock(), limit=100, archive=True)

    assert result.status == "partial_failure", (
        f"expected partial_failure, got {result.status!r}"
    )
    errored = [c for c in result.categories if c.error]
    succeeded = [c for c in result.categories if c.labels_applied > 0]
    assert len(errored) == 1
    assert len(succeeded) == 3
    assert "simulated transient API failure" in errored[0].error


# ---------------------------------------------------------------------------
# JSON output schema (stable contract for the M2 trigger service)
# ---------------------------------------------------------------------------


def test_run_one_pass_unparseable_senders_demote_to_partial(monkeypatch, conn):
    """A snapshot with unparseable From: headers should NOT roll up to success.

    Pre-fix, the status rollup only inspected per-category label/archive
    failures and ignored the report-phase ``partially_failed`` status. A
    --limit 10 run that lost 4 messages to unparseable senders would have
    exited 0 even though 40% of the requested snapshot was silently
    dropped — the exact failure mode that makes unattended one-pass unsafe.
    """
    senders = {"p@x.com": Category.PROMOTIONAL}
    _seed_senders(conn, by_email=senders)
    msgs = [
        _msg(mid="m1", sender="p@x.com"),
        _msg(mid="m2", sender=""),  # unparseable
        _msg(mid="m3", sender=""),  # unparseable
    ]
    _stub_gmail_basic(monkeypatch, ids=[m["id"] for m in msgs], messages=msgs)
    _stub_classifier(monkeypatch, classifications=senders)

    result = gw_oneshot.run_one_pass(creds=MagicMock(), limit=10, archive=True)

    assert result.report_status == "partially_failed"
    assert result.unparseable_sender_count == 2
    # The promo message labeled+archived OK; but the snapshot shortfall
    # demotes the overall status to partial_failure.
    assert result.status == "partial_failure", (
        f"expected partial_failure (2 unparseable in snapshot); "
        f"got {result.status!r}"
    )
    assert any("unparseable" in n.lower() for n in result.notes)


def test_run_one_pass_classification_unknown_demotes_to_partial(monkeypatch, conn):
    """Classifier returning UNKNOWN for one sender must demote the rollup.

    UNKNOWN means that sender's messages will be silently skipped by every
    label phase (the planning helper filters them out). A 'success' rollup
    in that case is misleading — those messages are NOT archived but the
    caller has no signal to retry."""
    senders = {
        "p@x.com": Category.PROMOTIONAL,
        "mystery@x.com": Category.UNKNOWN,  # classifier fell back to unknown
    }
    _seed_senders(conn, by_email=senders)
    msgs = [_msg(mid=f"m_{e}", sender=e) for e in senders]
    _stub_gmail_basic(monkeypatch, ids=[m["id"] for m in msgs], messages=msgs)

    # Simulate classifier returning source='unknown' for mystery@x.com
    def _fake_classify(senders_arg, *, conn, on_progress=None, **_kw):
        out = {}
        for s in senders_arg:
            if s.email == "mystery@x.com":
                out[s.email] = ClassificationResult(
                    email=s.email,
                    category=Category.UNKNOWN,
                    source="unknown",
                    error="malformed response",
                )
            else:
                out[s.email] = ClassificationResult(
                    email=s.email,
                    category=Category.PROMOTIONAL,
                    source="cache",
                )
        return out

    monkeypatch.setattr(gw_oneshot, "classify_senders", _fake_classify)

    result = gw_oneshot.run_one_pass(creds=MagicMock(), limit=10, archive=True)

    assert result.classification_failure_count == 1
    assert result.report_status == "partially_failed"
    assert result.status == "partial_failure"


def test_test_suite_does_not_touch_real_state_db(monkeypatch):
    """Hermetic-guard: assert the conftest isolation is actually working.

    Belt-and-suspenders for the autouse ``_isolate_gmailwiz_state`` fixture.
    Snapshots the real DB path's mtime around a one-pass run and asserts
    it doesn't move. If this ever fires, either the conftest fixture
    broke or a future test bypassed it by calling ``gw_db.connect()``
    with no path before the fixture monkeypatched ``DEFAULT_DB_PATH``.
    """
    from pathlib import Path

    real_db = Path(__file__).resolve().parent.parent / "data" / "db" / "state.db"
    if not real_db.exists():
        # Fresh checkout — no state DB to protect, no possible regression.
        return

    before_mtime = real_db.stat().st_mtime_ns

    # Run a one-pass with fully-stubbed Gmail + classifier.
    senders = {"p@x.com": Category.PROMOTIONAL}
    # Open a temporary conn just to seed; uses the monkeypatched default.
    with gw_db.open_db() as c:
        _seed_senders(c, by_email=senders)
    msgs = [_msg(mid="m1", sender="p@x.com")]
    _stub_gmail_basic(monkeypatch, ids=["m1"], messages=msgs)
    _stub_classifier(monkeypatch, classifications=senders)
    gw_oneshot.run_one_pass(creds=MagicMock(), limit=10, archive=True)

    after_mtime = real_db.stat().st_mtime_ns
    assert after_mtime == before_mtime, (
        f"test run mutated real state DB at {real_db} "
        f"(mtime {before_mtime} -> {after_mtime}). Conftest isolation broken."
    )


def test_oneshot_to_dict_has_expected_keys(monkeypatch, conn):
    """Lock in the JSON shape: callers (Phase 2 trigger service, Drafts,
    Telegram formatter) will key off these exact field names."""
    senders = {"p@x.com": Category.PROMOTIONAL}
    _seed_senders(conn, by_email=senders)
    msgs = [_msg(mid="m1", sender="p@x.com")]
    _stub_gmail_basic(monkeypatch, ids=["m1"], messages=msgs)
    _stub_classifier(monkeypatch, classifications=senders)

    result = gw_oneshot.run_one_pass(creds=MagicMock(), limit=10, archive=True)
    payload = result.to_dict()

    # Top-level keys are stable.
    assert set(payload.keys()) == {
        "status", "error_code", "report_run_id", "report_status",
        "snapshot_size", "snapshot_message_ids", "classified_sender_count",
        "fetch_failure_count", "unparseable_sender_count",
        "classification_failure_count",
        "categories", "wall_seconds", "notes",
    }
    # Per-category keys.
    assert payload["categories"], "expected at least one category result"
    assert set(payload["categories"][0].keys()) == {
        "category", "label_run_id", "labels_applied", "labels_failed",
        "label_run_status", "archive_run_id", "archive_applied",
        "archive_failed", "archive_run_status", "error",
    }
    # Must be JSON-serialisable in full.
    json.dumps(payload)
