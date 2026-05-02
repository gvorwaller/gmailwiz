"""Tests for `gmailwiz.planning` — label plan build + apply.

Plan build is exercised end-to-end against a tmp SQLite DB; Gmail and the
classifier are stubbed at module boundaries (`gmail_client.list_unread_message_ids`,
`gmail_client.get_message_metadata`, `gmail_client.list_existing_labels`,
`gmail_client.ensure_labels_exist`, `gmail_client.modify_labels`). The senders
cache is pre-populated via `db.upsert_sender` so the plan filter logic has
real cache rows to consult.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from gmailwiz import db as gw_db
from gmailwiz import gmail_client
from gmailwiz import planning
from gmailwiz.categories import Category, gmail_label_name


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    """Open the autouse-isolated DB; closed at teardown."""
    c = gw_db.connect()
    yield c
    c.close()


def _seed_sender(
    conn,
    *,
    email: str,
    category: Category,
    display_name: str = "",
):
    gw_db.upsert_sender(
        conn,
        email=email,
        display_name=display_name or None,
        category=category,
        prompt_version="test",
        model="test-model",
    )


def _msg(
    *,
    mid: str,
    sender: str,
    subject: str = "subj",
    label_ids: list[str] | None = None,
    thread_id: str = "t",
    sender_name: str = "",
) -> dict[str, Any]:
    return {
        "id": mid,
        "thread_id": thread_id,
        "sender_email": sender,
        "sender_name": sender_name,
        "subject": subject,
        "snippet": "",
        "internal_date": "1700000000000",
        "label_ids": label_ids or ["INBOX", "UNREAD"],
    }


def _stub_gmail(
    monkeypatch,
    *,
    ids: list[str],
    messages: list[dict[str, Any]],
    existing_labels: dict[str, str] | None = None,
):
    """Stub the read-only Gmail surface used by `build_label_plan`."""
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


# ---------------------------------------------------------------------------
# build_label_plan — happy path + filters
# ---------------------------------------------------------------------------


def test_build_label_plan_categorises_promo_only(monkeypatch, conn):
    _seed_sender(conn, email="promo@x.com", category=Category.PROMOTIONAL)
    _seed_sender(conn, email="news@x.com", category=Category.NEWSLETTER)
    _seed_sender(conn, email="unknown@x.com", category=Category.UNKNOWN)

    msgs = [
        _msg(mid="m1", sender="promo@x.com", subject="50% off"),
        _msg(mid="m2", sender="news@x.com", subject="weekly"),
        _msg(mid="m3", sender="unknown@x.com", subject="?"),
        _msg(mid="m4", sender="never-seen@x.com", subject="who"),  # uncached
        _msg(mid="m5", sender="", subject="bad"),  # unparseable
        _msg(mid="m6", sender="promo@x.com", subject="another deal"),
    ]
    _stub_gmail(monkeypatch, ids=[m["id"] for m in msgs], messages=msgs)

    plan = planning.build_label_plan(
        creds=MagicMock(), conn=conn, category=Category.PROMOTIONAL, limit=10
    )

    assert plan.category is Category.PROMOTIONAL
    assert plan.label_name == "gmailwiz/promotional"
    assert plan.target_label_id is None  # label doesn't exist yet
    assert [c.message_id for c in plan.candidates] == ["m1", "m6"]
    assert plan.skipped_unknown == 1
    assert plan.skipped_other_category == 1
    assert plan.skipped_uncached == 1
    assert plan.skipped_unparseable == 1
    assert plan.skipped_already_labeled == 0


def test_build_label_plan_persists_run_and_audit_rows(monkeypatch, conn):
    _seed_sender(conn, email="promo@x.com", category=Category.PROMOTIONAL)
    msgs = [_msg(mid="m1", sender="promo@x.com")]
    _stub_gmail(monkeypatch, ids=["m1"], messages=msgs)

    plan = planning.build_label_plan(
        creds=MagicMock(), conn=conn, category=Category.PROMOTIONAL, limit=5
    )

    run = gw_db.get_run(conn, plan.run_id)
    assert run is not None
    assert run["phase"] == "label"
    assert run["status"] == "planned"
    assert run["category_filter"] == "promotional"
    assert run["dry_run"] == 1
    assert run["limit_count"] == 5

    audit = gw_db.get_audit_entries(conn, plan.run_id)
    assert len(audit) == 1
    row = audit[0]
    assert row["status"] == "planned"
    assert row["action"] == "add_label"
    assert row["message_id"] == "m1"
    assert row["sender_email"] == "promo@x.com"
    # Pending marker — the label doesn't exist yet, so it can't be a real id.
    assert any(x.startswith("pending:") for x in row["after_label_ids"])


def test_build_label_plan_uses_real_label_id_when_label_exists(monkeypatch, conn):
    _seed_sender(conn, email="promo@x.com", category=Category.PROMOTIONAL)
    msgs = [_msg(mid="m1", sender="promo@x.com")]
    _stub_gmail(
        monkeypatch,
        ids=["m1"],
        messages=msgs,
        existing_labels={"gmailwiz/promotional": "Label_42"},
    )

    plan = planning.build_label_plan(
        creds=MagicMock(), conn=conn, category=Category.PROMOTIONAL, limit=5
    )
    assert plan.target_label_id == "Label_42"
    assert plan.candidates[0].after_label_ids[-1] == "Label_42"
    audit = gw_db.get_audit_entries(conn, plan.run_id)
    assert "Label_42" in audit[0]["after_label_ids"]


def test_build_label_plan_skips_already_labeled(monkeypatch, conn):
    _seed_sender(conn, email="promo@x.com", category=Category.PROMOTIONAL)
    msgs = [
        _msg(mid="m1", sender="promo@x.com", label_ids=["INBOX", "UNREAD", "Label_42"]),
        _msg(mid="m2", sender="promo@x.com", label_ids=["INBOX", "UNREAD"]),
    ]
    _stub_gmail(
        monkeypatch,
        ids=["m1", "m2"],
        messages=msgs,
        existing_labels={"gmailwiz/promotional": "Label_42"},
    )

    plan = planning.build_label_plan(
        creds=MagicMock(), conn=conn, category=Category.PROMOTIONAL, limit=5
    )
    assert [c.message_id for c in plan.candidates] == ["m2"]
    assert plan.skipped_already_labeled == 1


def test_build_label_plan_no_messages_still_creates_run(monkeypatch, conn):
    _stub_gmail(monkeypatch, ids=[], messages=[])
    plan = planning.build_label_plan(
        creds=MagicMock(), conn=conn, category=Category.PROMOTIONAL, limit=5
    )
    assert plan.candidates == []
    run = gw_db.get_run(conn, plan.run_id)
    assert run is not None
    assert run["status"] == "planned"
    audit = gw_db.get_audit_entries(conn, plan.run_id)
    assert audit == []


def test_build_label_plan_rejects_unknown_category(monkeypatch, conn):
    with pytest.raises(ValueError, match="non-classifiable"):
        planning.build_label_plan(
            creds=MagicMock(), conn=conn, category=Category.UNKNOWN, limit=5
        )


def test_build_label_plan_rejects_nonpositive_limit(monkeypatch, conn):
    with pytest.raises(ValueError, match="limit"):
        planning.build_label_plan(
            creds=MagicMock(), conn=conn, category=Category.PROMOTIONAL, limit=0
        )


# ---------------------------------------------------------------------------
# apply_label_plan — happy / partial / total failure
# ---------------------------------------------------------------------------


def _stub_mutations(
    monkeypatch,
    *,
    label_id: str = "Label_99",
    modify_responses: dict[str, Any] | None = None,
):
    """Stub `ensure_labels_exist` + `modify_labels`.

    `modify_responses` maps message_id -> either a response dict (success) or
    an Exception instance (which gets raised).
    """
    monkeypatch.setattr(
        gmail_client,
        "ensure_labels_exist",
        lambda creds, names: {n: label_id for n in names},
    )
    responses = modify_responses or {}

    def fake_modify(creds, *, message_id, add_label_ids=(), remove_label_ids=()):
        # Default mirrors Gmail's real `messages.modify` shape — the
        # response is the FULL post-mutation Message resource, including
        # all existing labels (INBOX/UNREAD) plus whatever was added.
        # Tests that override per-message can pass an Exception (raised
        # on .execute()) or a custom dict.
        default = {
            "id": message_id,
            "threadId": "t",
            "labelIds": ["INBOX", "UNREAD", *list(add_label_ids)],
        }
        out = responses.get(message_id, default)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(gmail_client, "modify_labels", fake_modify)


def _build_planned_run(monkeypatch, conn, *, mids: list[str], category=Category.PROMOTIONAL):
    for mid in mids:
        _seed_sender(conn, email=f"{mid}@x.com", category=category)
    msgs = [_msg(mid=mid, sender=f"{mid}@x.com") for mid in mids]
    _stub_gmail(monkeypatch, ids=mids, messages=msgs)
    return planning.build_label_plan(
        creds=MagicMock(), conn=conn, category=category, limit=10
    )


def test_apply_label_plan_all_success(monkeypatch, conn):
    plan = _build_planned_run(monkeypatch, conn, mids=["m1", "m2", "m3"])
    _stub_mutations(monkeypatch)

    result = planning.apply_label_plan(creds=MagicMock(), conn=conn, run_id=plan.run_id)

    assert result.applied == 3
    assert result.failed == 0
    assert result.run_status == "committed"

    run = gw_db.get_run(conn, plan.run_id)
    assert run["status"] == "committed"
    # `dry_run` must flip to 0 after a real commit so consumers filtering on
    # `dry_run=0` to find real-world mutations actually find this row.
    assert run["dry_run"] == 0
    audit = gw_db.get_audit_entries(conn, plan.run_id)
    for row in audit:
        assert row["status"] == "applied"
        # Pending marker must be replaced with the real label id from
        # Gmail's response (no `pending:` strings should survive a successful
        # apply).
        assert not any(x.startswith("pending:") for x in row["after_label_ids"])


def test_apply_label_plan_partial_failure(monkeypatch, conn):
    plan = _build_planned_run(monkeypatch, conn, mids=["m1", "m2", "m3"])
    _stub_mutations(
        monkeypatch,
        modify_responses={
            "m1": {"labelIds": ["INBOX", "UNREAD", "Label_99"]},
            "m2": RuntimeError("simulated 404"),
            "m3": {"labelIds": ["INBOX", "UNREAD", "Label_99"]},
        },
    )

    result = planning.apply_label_plan(creds=MagicMock(), conn=conn, run_id=plan.run_id)

    assert result.applied == 2
    assert result.failed == 1
    assert result.run_status == "partially_failed"
    assert result.errors == [("m2", "RuntimeError: simulated 404")]

    audit = {r["message_id"]: r for r in gw_db.get_audit_entries(conn, plan.run_id)}
    assert audit["m1"]["status"] == "applied"
    assert audit["m2"]["status"] == "failed"
    assert audit["m2"]["error"] == "RuntimeError: simulated 404"
    assert audit["m3"]["status"] == "applied"


def test_apply_label_plan_all_failed(monkeypatch, conn):
    plan = _build_planned_run(monkeypatch, conn, mids=["m1", "m2"])
    _stub_mutations(
        monkeypatch,
        modify_responses={
            "m1": RuntimeError("a"),
            "m2": RuntimeError("b"),
        },
    )

    result = planning.apply_label_plan(creds=MagicMock(), conn=conn, run_id=plan.run_id)

    assert result.applied == 0
    assert result.failed == 2
    assert result.run_status == "failed"

    run = gw_db.get_run(conn, plan.run_id)
    assert run["status"] == "failed"


def test_apply_label_plan_rejects_missing_run(monkeypatch, conn):
    _stub_mutations(monkeypatch)
    with pytest.raises(ValueError, match="No run with id"):
        planning.apply_label_plan(creds=MagicMock(), conn=conn, run_id="does-not-exist")


def test_apply_label_plan_rejects_wrong_phase(monkeypatch, conn):
    _stub_mutations(monkeypatch)
    rid = gw_db.create_run(
        conn, phase="report", limit_count=10, dry_run=True, status="planned"
    )
    with pytest.raises(ValueError, match="phase="):
        planning.apply_label_plan(creds=MagicMock(), conn=conn, run_id=rid)


def test_apply_label_plan_rejects_already_committed_run(monkeypatch, conn):
    plan = _build_planned_run(monkeypatch, conn, mids=["m1"])
    _stub_mutations(monkeypatch)
    planning.apply_label_plan(creds=MagicMock(), conn=conn, run_id=plan.run_id)
    # Second apply should fail — the run is no longer 'planned'.
    with pytest.raises(ValueError, match="status="):
        planning.apply_label_plan(creds=MagicMock(), conn=conn, run_id=plan.run_id)


def test_apply_label_plan_zero_rows_does_not_flip_run(monkeypatch, conn):
    """A `planned` run with zero audit rows must NOT be flipped to
    `committed` with `dry_run=0` — that would mislead any future query
    that filters on (status='committed' AND dry_run=0) to find real
    Gmail mutations. Vacuous apply: leave the run alone, and crucially
    do NOT call any Gmail mutation API (including `ensure_labels_exist`,
    which would otherwise create a permanent `gmailwiz/<cat>` label
    artifact in the user's account despite no message-level mutation).
    """
    _stub_gmail(monkeypatch, ids=[], messages=[])
    plan = planning.build_label_plan(
        creds=MagicMock(), conn=conn, category=Category.PROMOTIONAL, limit=5
    )
    assert plan.candidates == []  # zero candidates

    # Track Gmail-mutation calls. The vacuous-apply guard MUST short-circuit
    # before any of these can fire.
    ensure_calls: list[tuple] = []
    modify_calls: list[str] = []

    def track_ensure(creds, names):
        ensure_calls.append(tuple(names))
        return {n: f"Label_{n}" for n in names}

    def track_modify(creds, *, message_id, add_label_ids=(), remove_label_ids=()):
        modify_calls.append(message_id)
        return {"id": message_id, "labelIds": []}

    monkeypatch.setattr(gmail_client, "ensure_labels_exist", track_ensure)
    monkeypatch.setattr(gmail_client, "modify_labels", track_modify)

    result = planning.apply_label_plan(creds=MagicMock(), conn=conn, run_id=plan.run_id)

    assert result.applied == 0
    assert result.failed == 0
    assert result.run_status == "planned"

    run = gw_db.get_run(conn, plan.run_id)
    assert run["status"] == "planned"
    assert run["dry_run"] == 1

    # No Gmail mutation occurred — neither label creation nor message modify.
    assert ensure_calls == [], (
        "Vacuous apply called ensure_labels_exist — would create a permanent "
        "Gmail label artifact for a run with no work to do"
    )
    assert modify_calls == []


def test_apply_label_plan_malformed_modify_response_marks_failed(monkeypatch, conn):
    """If Gmail's `messages.modify` response lacks `labelIds` entirely
    (malformed/intermediated), the row must be marked failed with a
    clear error rather than silently writing `[]` to `after_label_ids`
    (which a future undo would mistakenly read as 'message had no
    labels post-modify')."""
    _seed_sender(conn, email="promo@x.com", category=Category.PROMOTIONAL)
    msgs = [_msg(mid="m1", sender="promo@x.com")]
    _stub_gmail(monkeypatch, ids=["m1"], messages=msgs)
    plan = planning.build_label_plan(
        creds=MagicMock(), conn=conn, category=Category.PROMOTIONAL, limit=5
    )

    # Stub modify_labels to return a response WITHOUT the labelIds field.
    monkeypatch.setattr(
        gmail_client, "ensure_labels_exist",
        lambda creds, names: {n: "Label_99" for n in names},
    )

    def malformed_modify(creds, *, message_id, add_label_ids=(), remove_label_ids=()):
        return {"id": message_id, "threadId": "t"}  # no labelIds key

    monkeypatch.setattr(gmail_client, "modify_labels", malformed_modify)

    result = planning.apply_label_plan(creds=MagicMock(), conn=conn, run_id=plan.run_id)
    assert result.applied == 0
    assert result.failed == 1
    assert result.run_status == "failed"

    audit = gw_db.get_audit_entries(conn, plan.run_id)
    row = audit[0]
    assert row["status"] == "failed"
    assert "missing 'labelIds'" in (row["error"] or "")
    # Reverted to before-state.
    assert row["after_label_ids"] == row["before_label_ids"]


def test_apply_label_plan_rejects_unknown_category(monkeypatch, conn):
    """Defense-in-depth: a hand-edited DB row with `category_filter='unknown'`
    must NOT result in a `gmailwiz/unknown` Gmail label being created.
    `build_label_plan` rejects UNKNOWN at the entry path, but `apply_label_plan`
    should also refuse it on the way out so a corrupted row can't slip
    past."""
    rid = gw_db.create_run(
        conn, phase="label", category_filter="unknown",
        limit_count=10, dry_run=True, status="planned",
    )
    gw_db.append_audit_entry(
        conn, run_id=rid, action="add_label", message_id="m1",
        before_label_ids=["INBOX"], after_label_ids=["INBOX", "pending:gmailwiz/unknown"],
        status="planned",
    )

    ensure_calls: list = []
    monkeypatch.setattr(
        gmail_client, "ensure_labels_exist",
        lambda creds, names: ensure_calls.append(tuple(names)) or {n: "x" for n in names},
    )

    with pytest.raises(ValueError, match="non-classifiable"):
        planning.apply_label_plan(creds=MagicMock(), conn=conn, run_id=rid)

    # Crucially: ensure_labels_exist must NOT have been called.
    assert ensure_calls == []


def test_apply_label_plan_resumed_after_partial_failure_marks_partially_failed(
    monkeypatch, conn
):
    """Regression: a run that was interrupted with some rows already marked
    `failed` must NOT be flipped to `committed` when a later resumed apply
    succeeds on the still-`planned` rows. Terminal status reflects
    CUMULATIVE outcomes, not just the current pass.

    Scenario:
      1. First apply: row 1 succeeds, row 2 fails (status='failed' in
         place), Ctrl-C before row 3 (status stays 'planned').
      2. Run row stays 'planned' (never reached the terminal flip).
      3. Re-apply: only row 3 is in `planned_rows`. It succeeds.
      4. Terminal status MUST be `partially_failed` (row 2 still failed),
         NOT `committed`.
    """
    _seed_sender(conn, email="promo@x.com", category=Category.PROMOTIONAL)
    msgs = [_msg(mid=mid, sender="promo@x.com") for mid in ("m1", "m2", "m3")]
    _stub_gmail(monkeypatch, ids=["m1", "m2", "m3"], messages=msgs)
    plan = planning.build_label_plan(
        creds=MagicMock(), conn=conn, category=Category.PROMOTIONAL, limit=10
    )

    # First pass: simulate the partial-failure history by directly editing
    # the audit rows (this is what a real interrupted apply would have left
    # in the DB). m1 → applied, m2 → failed, m3 → planned (untouched).
    audit = gw_db.get_audit_entries(conn, plan.run_id)
    by_mid = {r["message_id"]: r for r in audit}
    gw_db.update_audit_entry(
        conn, audit_id=by_mid["m1"]["id"], status="applied",
        after_label_ids=["INBOX", "UNREAD", "Label_99"],
    )
    gw_db.update_audit_entry(
        conn, audit_id=by_mid["m2"]["id"], status="failed",
        after_label_ids=["INBOX", "UNREAD"], error="prior pass: simulated 500",
    )
    # Run row was never flipped — still `planned` — so it's re-applicable.
    run = gw_db.get_run(conn, plan.run_id)
    assert run["status"] == "planned"

    # Second pass: only m3 is still planned. Stub mutations to succeed.
    _stub_mutations(monkeypatch)
    result = planning.apply_label_plan(creds=MagicMock(), conn=conn, run_id=plan.run_id)

    # In THIS pass: 1 applied, 0 failed. But the run as a WHOLE has
    # 2 applied + 1 failed → must be `partially_failed`, not committed.
    assert result.applied == 1
    assert result.failed == 0
    assert result.run_status == "partially_failed"

    run = gw_db.get_run(conn, plan.run_id)
    assert run["status"] == "partially_failed"
    assert run["dry_run"] == 0


def test_apply_label_plan_skips_non_add_label_action_rows(monkeypatch, conn):
    """Audit rows with an action other than `add_label` (e.g., a future
    Phase 3 archive row that landed in this run by mistake) must NOT be
    walked by the label-apply loop."""
    _seed_sender(conn, email="promo@x.com", category=Category.PROMOTIONAL)
    msgs = [_msg(mid="m1", sender="promo@x.com")]
    _stub_gmail(monkeypatch, ids=["m1"], messages=msgs)
    plan = planning.build_label_plan(
        creds=MagicMock(), conn=conn, category=Category.PROMOTIONAL, limit=5
    )
    # Now hand-inject a planned row with the wrong action.
    gw_db.append_audit_entry(
        conn, run_id=plan.run_id, action="archive", message_id="m_archive",
        before_label_ids=["INBOX"], after_label_ids=[], status="planned",
    )

    modify_calls: list[str] = []

    def track_modify(creds, *, message_id, add_label_ids=(), remove_label_ids=()):
        modify_calls.append(message_id)
        return {"id": message_id, "threadId": "t",
                "labelIds": ["INBOX", "UNREAD", *list(add_label_ids)]}

    monkeypatch.setattr(
        gmail_client, "ensure_labels_exist",
        lambda creds, names: {n: "Label_99" for n in names},
    )
    monkeypatch.setattr(gmail_client, "modify_labels", track_modify)

    result = planning.apply_label_plan(creds=MagicMock(), conn=conn, run_id=plan.run_id)
    # The archive row was skipped; only the add_label row was processed.
    assert modify_calls == ["m1"]
    assert result.applied == 1


def test_apply_label_plan_failure_reverts_after_label_ids(monkeypatch, conn):
    """When `modify_labels` raises, the audit row's `after_label_ids` must
    revert to `before_label_ids` (since the mutation never happened),
    rather than keeping the stale plan-time projection. Otherwise a
    future query reading `after_label_ids` of a `failed` row sees a
    non-existent post-state."""
    _seed_sender(conn, email="promo@x.com", category=Category.PROMOTIONAL)
    msgs = [_msg(mid="m1", sender="promo@x.com", label_ids=["INBOX", "UNREAD"])]
    _stub_gmail(monkeypatch, ids=["m1"], messages=msgs)
    plan = planning.build_label_plan(
        creds=MagicMock(), conn=conn, category=Category.PROMOTIONAL, limit=5
    )
    # At plan time, after_label_ids includes the pending marker.
    audit_before_apply = gw_db.get_audit_entries(conn, plan.run_id)
    assert any(x.startswith("pending:") for x in audit_before_apply[0]["after_label_ids"])

    _stub_mutations(
        monkeypatch,
        modify_responses={"m1": RuntimeError("simulated 404")},
    )
    result = planning.apply_label_plan(creds=MagicMock(), conn=conn, run_id=plan.run_id)

    assert result.failed == 1
    audit = gw_db.get_audit_entries(conn, plan.run_id)
    row = audit[0]
    assert row["status"] == "failed"
    # Reverted to before-state — no projection survives.
    assert row["after_label_ids"] == ["INBOX", "UNREAD"]
    assert not any(x.startswith("pending:") for x in row["after_label_ids"])
    # `before_label_ids` MUST also be preserved untouched — a buggy
    # implementation that overwrote both fields would still pass the
    # after-only assertion above.
    assert row["before_label_ids"] == ["INBOX", "UNREAD"]


def test_apply_label_plan_no_drift_between_preview_and_commit(monkeypatch, conn):
    """The whole point of the persisted plan: if Gmail's state changes between
    preview and commit, the commit must still operate on the *previewed* set
    (the audit_log rows), not a re-fetched current state.

    Strong assertion: the read APIs (`list_unread_message_ids`,
    `get_message_metadata`) MUST NOT be called during apply. We replace
    them with side-effects that fail the test if invoked, then track
    exactly which message_ids `modify_labels` saw.
    """
    plan = _build_planned_run(monkeypatch, conn, mids=["m1", "m2"])

    def _read_must_not_be_called(*args, **kwargs):
        raise AssertionError(
            "apply_label_plan must not re-fetch Gmail at commit time — "
            "preview-vs-commit drift safety violation"
        )

    monkeypatch.setattr(gmail_client, "list_unread_message_ids", _read_must_not_be_called)
    monkeypatch.setattr(gmail_client, "get_message_metadata", _read_must_not_be_called)

    modify_calls: list[str] = []
    monkeypatch.setattr(
        gmail_client, "ensure_labels_exist",
        lambda creds, names: {n: "Label_99" for n in names},
    )

    def fake_modify(creds, *, message_id, add_label_ids=(), remove_label_ids=()):
        modify_calls.append(message_id)
        return {"id": message_id, "threadId": "t",
                "labelIds": ["INBOX", "UNREAD", *list(add_label_ids)]}

    monkeypatch.setattr(gmail_client, "modify_labels", fake_modify)

    result = planning.apply_label_plan(creds=MagicMock(), conn=conn, run_id=plan.run_id)

    assert result.applied == 2
    # Exactly the previewed messages — no recomputation.
    assert sorted(modify_calls) == ["m1", "m2"]
    audit = gw_db.get_audit_entries(conn, plan.run_id)
    assert {r["message_id"] for r in audit} == {"m1", "m2"}


# ---------------------------------------------------------------------------
# Archive — build_archive_plan + apply_archive_plan
# ---------------------------------------------------------------------------


def _commit_label_run(monkeypatch, conn, *, mids, category=Category.PROMOTIONAL):
    """Helper: build + apply a label run end-to-end so subsequent archive
    tests have a real source run with applied audit rows.
    """
    for mid in mids:
        _seed_sender(conn, email=f"{mid}@x.com", category=category)
    msgs = [_msg(mid=mid, sender=f"{mid}@x.com",
                 label_ids=["INBOX", "UNREAD"]) for mid in mids]
    _stub_gmail(monkeypatch, ids=mids, messages=msgs)
    plan = planning.build_label_plan(
        creds=MagicMock(), conn=conn, category=category, limit=10
    )
    _stub_mutations(monkeypatch)
    planning.apply_label_plan(creds=MagicMock(), conn=conn, run_id=plan.run_id)
    return plan.run_id


def _stub_archive_get_metadata(monkeypatch, messages):
    """Stub `gmail_client.get_message_metadata` to return a fixed list.
    Used at archive-plan-build time to seed the post-label state."""
    monkeypatch.setattr(
        gmail_client, "get_message_metadata",
        lambda creds, message_ids: messages,
    )


def test_build_archive_plan_persists_archive_run_and_audit_rows(monkeypatch, conn):
    label_run_id = _commit_label_run(monkeypatch, conn, mids=["m1", "m2"])

    # Now stub get_message_metadata to return the messages with INBOX still
    # present (i.e., they haven't been manually archived yet).
    archive_msgs = [
        _msg(mid="m1", sender="m1@x.com", label_ids=["INBOX", "UNREAD", "Label_99"]),
        _msg(mid="m2", sender="m2@x.com", label_ids=["INBOX", "UNREAD", "Label_99"]),
    ]
    _stub_archive_get_metadata(monkeypatch, archive_msgs)

    plan = planning.build_archive_plan(
        creds=MagicMock(), conn=conn, source_run_id=label_run_id
    )

    assert plan.source_run_id == label_run_id
    assert plan.source_category is Category.PROMOTIONAL
    assert len(plan.candidates) == 2
    assert plan.skipped_already_archived == 0

    # New runs row was persisted with phase='archive' status='planned'.
    archive_run = gw_db.get_run(conn, plan.run_id)
    assert archive_run["phase"] == "archive"
    assert archive_run["status"] == "planned"
    assert archive_run["category_filter"] == "promotional"
    assert archive_run["dry_run"] == 1

    # Audit rows for the archive run, action='archive'.
    rows = gw_db.get_audit_entries(conn, plan.run_id)
    assert len(rows) == 2
    for row in rows:
        assert row["action"] == "archive"
        assert row["status"] == "planned"
        assert "INBOX" in row["before_label_ids"]
        assert "INBOX" not in row["after_label_ids"]


def test_build_archive_plan_skips_already_archived_messages(monkeypatch, conn):
    label_run_id = _commit_label_run(monkeypatch, conn, mids=["m1", "m2"])
    archive_msgs = [
        _msg(mid="m1", sender="m1@x.com", label_ids=["INBOX", "UNREAD", "Label_99"]),
        # m2 has been manually archived (no INBOX).
        _msg(mid="m2", sender="m2@x.com", label_ids=["UNREAD", "Label_99"]),
    ]
    _stub_archive_get_metadata(monkeypatch, archive_msgs)

    plan = planning.build_archive_plan(
        creds=MagicMock(), conn=conn, source_run_id=label_run_id
    )
    assert [c.message_id for c in plan.candidates] == ["m1"]
    assert plan.skipped_already_archived == 1


def test_build_archive_plan_rejects_non_label_source(monkeypatch, conn):
    rid = gw_db.create_run(
        conn, phase="report", limit_count=5, dry_run=True, status="committed"
    )
    with pytest.raises(ValueError, match="archive source must be"):
        planning.build_archive_plan(creds=MagicMock(), conn=conn, source_run_id=rid)


def test_build_archive_plan_rejects_planned_source(monkeypatch, conn):
    """Source label run that was previewed but never applied has no
    `applied` audit rows — must not become an archive source."""
    rid = gw_db.create_run(
        conn, phase="label", category_filter="promotional",
        limit_count=5, dry_run=True, status="planned",
    )
    with pytest.raises(ValueError, match="committed"):
        planning.build_archive_plan(creds=MagicMock(), conn=conn, source_run_id=rid)


def test_build_archive_plan_rejects_undone_source(monkeypatch, conn):
    rid = gw_db.create_run(
        conn, phase="label", category_filter="promotional",
        limit_count=5, dry_run=True, status="undone",
    )
    with pytest.raises(ValueError, match="committed"):
        planning.build_archive_plan(creds=MagicMock(), conn=conn, source_run_id=rid)


def test_build_archive_plan_excludes_failed_source_audit_rows(monkeypatch, conn):
    """Source label run had some rows that failed; archive plan must
    only include messages that successfully applied (status='applied')."""
    for mid in ("m1", "m2"):
        _seed_sender(conn, email=f"{mid}@x.com", category=Category.PROMOTIONAL)
    msgs = [_msg(mid=mid, sender=f"{mid}@x.com") for mid in ("m1", "m2")]
    _stub_gmail(monkeypatch, ids=["m1", "m2"], messages=msgs)
    plan = planning.build_label_plan(
        creds=MagicMock(), conn=conn, category=Category.PROMOTIONAL, limit=10
    )
    _stub_mutations(
        monkeypatch,
        modify_responses={
            "m1": {"id": "m1", "threadId": "t",
                   "labelIds": ["INBOX", "UNREAD", "Label_99"]},
            "m2": RuntimeError("simulated 500"),
        },
    )
    planning.apply_label_plan(creds=MagicMock(), conn=conn, run_id=plan.run_id)

    # Only m1 was applied; m2 is failed.
    archive_msgs = [
        _msg(mid="m1", sender="m1@x.com", label_ids=["INBOX", "UNREAD", "Label_99"]),
    ]
    _stub_archive_get_metadata(monkeypatch, archive_msgs)
    archive_plan = planning.build_archive_plan(
        creds=MagicMock(), conn=conn, source_run_id=plan.run_id
    )
    assert [c.message_id for c in archive_plan.candidates] == ["m1"]
    assert archive_plan.skipped_source_failed == 1


def test_apply_archive_plan_no_drift_walks_audit_log(monkeypatch, conn):
    """Archive commit must walk the persisted audit_log, not refetch Gmail."""
    label_run_id = _commit_label_run(monkeypatch, conn, mids=["m1", "m2"])
    archive_msgs = [
        _msg(mid="m1", sender="m1@x.com", label_ids=["INBOX", "UNREAD", "Label_99"]),
        _msg(mid="m2", sender="m2@x.com", label_ids=["INBOX", "UNREAD", "Label_99"]),
    ]
    _stub_archive_get_metadata(monkeypatch, archive_msgs)
    archive_plan = planning.build_archive_plan(
        creds=MagicMock(), conn=conn, source_run_id=label_run_id
    )

    def _read_must_not_be_called(*args, **kwargs):
        raise AssertionError(
            "apply_archive_plan must not call list/get message read APIs"
        )
    monkeypatch.setattr(gmail_client, "list_unread_message_ids", _read_must_not_be_called)
    monkeypatch.setattr(gmail_client, "get_message_metadata", _read_must_not_be_called)

    modify_calls: list[dict] = []
    def fake_modify(creds, *, message_id, add_label_ids=(), remove_label_ids=()):
        modify_calls.append({
            "id": message_id,
            "add": list(add_label_ids),
            "remove": list(remove_label_ids),
        })
        # Mirror Gmail's response shape — full post-state label list.
        return {"id": message_id, "threadId": "t",
                "labelIds": ["UNREAD", "Label_99"]}  # INBOX removed
    monkeypatch.setattr(gmail_client, "modify_labels", fake_modify)

    result = planning.apply_archive_plan(
        creds=MagicMock(), conn=conn, run_id=archive_plan.run_id
    )
    assert result.applied == 2
    assert result.run_status == "committed"
    # Each call removed exactly INBOX, added nothing.
    for c in modify_calls:
        assert c["add"] == []
        assert c["remove"] == ["INBOX"]
    # Audit rows show INBOX gone in after_label_ids.
    rows = gw_db.get_audit_entries(conn, archive_plan.run_id)
    for r in rows:
        assert r["status"] == "applied"
        assert "INBOX" not in r["after_label_ids"]


def test_apply_archive_plan_resumed_after_partial_failure_marks_partially_failed(
    monkeypatch, conn
):
    """Codex P1 regression coverage for archive: a resumed apply that
    succeeds on still-`planned` rows must NOT mark the run `committed`
    when prior-pass failures exist. Terminal status reflects CUMULATIVE
    audit_log state."""
    label_run_id = _commit_label_run(monkeypatch, conn, mids=["m1", "m2", "m3"])
    archive_msgs = [
        _msg(mid=mid, sender=f"{mid}@x.com",
             label_ids=["INBOX", "UNREAD", "Label_99"])
        for mid in ("m1", "m2", "m3")
    ]
    _stub_archive_get_metadata(monkeypatch, archive_msgs)
    archive_plan = planning.build_archive_plan(
        creds=MagicMock(), conn=conn, source_run_id=label_run_id
    )

    # Simulate a partial-failure history by editing audit rows directly
    # (this is what a real interrupted apply would have left in the DB).
    audit = gw_db.get_audit_entries(conn, archive_plan.run_id)
    by_mid = {r["message_id"]: r for r in audit}
    gw_db.update_audit_entry(
        conn, audit_id=by_mid["m1"]["id"], status="applied",
        after_label_ids=["UNREAD", "Label_99"],
    )
    gw_db.update_audit_entry(
        conn, audit_id=by_mid["m2"]["id"], status="failed",
        after_label_ids=["INBOX", "UNREAD", "Label_99"],
        error="prior pass: simulated 500",
    )
    # m3 stays `planned`. Run row still 'planned' (never reached the flip).
    run = gw_db.get_run(conn, archive_plan.run_id)
    assert run["status"] == "planned"

    # Resume apply: only m3 is still planned; succeed on it.
    monkeypatch.setattr(
        gmail_client, "modify_labels",
        lambda creds, **kw: {
            "id": kw["message_id"], "threadId": "t",
            "labelIds": ["UNREAD", "Label_99"],  # INBOX removed
        },
    )
    result = planning.apply_archive_plan(
        creds=MagicMock(), conn=conn, run_id=archive_plan.run_id
    )

    # In THIS pass: 1 applied, 0 failed. But the run as a WHOLE has
    # 2 applied + 1 failed → must be `partially_failed`, not committed.
    assert result.applied == 1
    assert result.failed == 0
    assert result.run_status == "partially_failed"

    run = gw_db.get_run(conn, archive_plan.run_id)
    assert run["status"] == "partially_failed"
    assert run["dry_run"] == 0


def test_apply_archive_plan_malformed_modify_response_marks_failed(monkeypatch, conn):
    """A `messages.modify` response without `labelIds` is malformed and
    must be marked failed (mirrors the label-apply defense)."""
    label_run_id = _commit_label_run(monkeypatch, conn, mids=["m1"])
    archive_msgs = [_msg(mid="m1", sender="m1@x.com",
                         label_ids=["INBOX", "UNREAD", "Label_99"])]
    _stub_archive_get_metadata(monkeypatch, archive_msgs)
    archive_plan = planning.build_archive_plan(
        creds=MagicMock(), conn=conn, source_run_id=label_run_id
    )

    monkeypatch.setattr(
        gmail_client, "modify_labels",
        lambda creds, **kw: {"id": kw["message_id"], "threadId": "t"},  # no labelIds
    )
    result = planning.apply_archive_plan(
        creds=MagicMock(), conn=conn, run_id=archive_plan.run_id
    )
    assert result.applied == 0
    assert result.failed == 1
    assert result.run_status == "failed"
    rows = gw_db.get_audit_entries(conn, archive_plan.run_id)
    assert rows[0]["status"] == "failed"
    assert "missing 'labelIds'" in (rows[0]["error"] or "")
    # Reverted to before-state.
    assert rows[0]["after_label_ids"] == rows[0]["before_label_ids"]


def test_apply_undo_malformed_modify_response_records_error_keeps_applied(
    monkeypatch, conn
):
    """An undo modify_labels response missing `labelIds` is malformed —
    the row must record the error AND stay `status='applied'` so a
    retry can pick it up. Mirrors the label/archive defense but with
    the undo-specific contract that failures don't flip the row."""
    label_run_id = _commit_label_run(monkeypatch, conn, mids=["m1"])
    monkeypatch.setattr(
        gmail_client, "list_existing_labels",
        lambda creds: {"gmailwiz/promotional": "Label_99"},
    )
    monkeypatch.setattr(
        gmail_client, "modify_labels",
        lambda creds, **kw: {"id": kw["message_id"], "threadId": "t"},  # no labelIds
    )

    # Capture the post-apply after_label_ids BEFORE running undo so we
    # can verify it survives a failed undo.
    pre_undo_rows = gw_db.get_audit_entries(conn, label_run_id)
    pre_undo_after = pre_undo_rows[0]["after_label_ids"]

    result = planning.apply_undo_plan(
        creds=MagicMock(), conn=conn, run_id=label_run_id
    )
    assert result.reverted == 0
    assert result.failed == 1
    # Row stays 'applied' (failed undo doesn't flip the row to 'failed').
    rows = gw_db.get_audit_entries(conn, label_run_id)
    assert rows[0]["status"] == "applied"
    assert "missing 'labelIds'" in (rows[0]["error"] or "")
    # `after_label_ids` MUST be preserved (we did NOT mutate Gmail, so
    # the recorded post-apply state is still accurate). A regression
    # that overwrote after_label_ids on undo failure would be
    # catastrophic for a future retry that succeeds.
    assert rows[0]["after_label_ids"] == pre_undo_after
    # Run stays committed (or partially_failed) — NOT undone — so retry works.
    run = gw_db.get_run(conn, label_run_id)
    assert run["status"] != "undone"


def test_apply_undo_partial_failure_then_retry_succeeds(monkeypatch, conn):
    """Codex P1 regression coverage for undo: a partial-undo (some failed)
    must NOT mark the run `undone` while applied rows remain. A retry
    that succeeds on those still-applied rows then completes the undo.
    Stale error from the prior failed attempt must be cleared on
    successful retry."""
    label_run_id = _commit_label_run(monkeypatch, conn, mids=["m1", "m2"])
    monkeypatch.setattr(
        gmail_client, "list_existing_labels",
        lambda creds: {"gmailwiz/promotional": "Label_99"},
    )

    # First undo pass: m1 succeeds, m2 fails.
    pass_one_calls: list[str] = []
    def fail_modify_one(creds, *, message_id, add_label_ids=(), remove_label_ids=()):
        pass_one_calls.append(message_id)
        if message_id == "m2":
            raise RuntimeError("simulated transient 500")
        return {"id": message_id, "threadId": "t",
                "labelIds": ["INBOX", "UNREAD"]}
    monkeypatch.setattr(gmail_client, "modify_labels", fail_modify_one)
    result1 = planning.apply_undo_plan(
        creds=MagicMock(), conn=conn, run_id=label_run_id
    )
    assert result1.reverted == 1
    assert result1.failed == 1
    # Run must NOT be flipped to 'undone' — m2 still has the label.
    run = gw_db.get_run(conn, label_run_id)
    assert run["status"] in {"committed", "partially_failed"}

    # m2 still applied with stale error.
    rows_by_mid = {r["message_id"]: r for r in gw_db.get_audit_entries(conn, label_run_id)}
    assert rows_by_mid["m1"]["status"] == "reverted"
    assert rows_by_mid["m2"]["status"] == "applied"
    assert rows_by_mid["m2"]["error"] is not None  # stale error recorded

    # Second undo pass: the still-applied row succeeds.
    # Capture which row was still applied BEFORE pass 2 — the retry
    # must touch exactly that row, not the already-reverted one.
    pre_retry_rows = {
        r["message_id"]: r for r in gw_db.get_audit_entries(conn, label_run_id)
    }
    still_applied = [
        mid for mid, row in pre_retry_rows.items()
        if row["status"] == "applied"
    ]
    assert len(still_applied) == 1, (
        f"Expected exactly one still-applied row before retry, got {still_applied}"
    )
    expected_retry_mid = still_applied[0]

    pass_two_calls: list[str] = []
    def succeed_modify(creds, *, message_id, add_label_ids=(), remove_label_ids=()):
        pass_two_calls.append(message_id)
        return {"id": message_id, "threadId": "t",
                "labelIds": ["INBOX", "UNREAD"]}
    monkeypatch.setattr(gmail_client, "modify_labels", succeed_modify)
    result2 = planning.apply_undo_plan(
        creds=MagicMock(), conn=conn, run_id=label_run_id
    )
    assert result2.reverted == 1
    assert result2.failed == 0
    assert result2.run_status == "undone"
    # Tight: exactly the still-applied row, not the already-reverted one.
    # A regression that re-undid the already-`reverted` row (or hit both)
    # would have `pass_two_calls != [expected_retry_mid]`.
    assert pass_two_calls == [expected_retry_mid]

    # Stale error cleared on m2 after successful retry.
    rows_by_mid = {r["message_id"]: r for r in gw_db.get_audit_entries(conn, label_run_id)}
    assert rows_by_mid["m2"]["status"] == "reverted"
    assert rows_by_mid["m2"]["error"] is None  # cleared


def test_apply_undo_zero_applied_rows_does_not_call_gmail(monkeypatch, conn):
    """Vacuous-undo guard: a run with zero applied rows (e.g., a
    `partially_failed` run where every row failed during apply) must
    NOT call `list_existing_labels` or any other Gmail API."""
    # Create a partially_failed label run with no applied rows.
    rid = gw_db.create_run(
        conn, phase="label", category_filter="promotional",
        limit_count=10, dry_run=False, status="partially_failed",
    )
    gw_db.append_audit_entry(
        conn, run_id=rid, action="add_label", message_id="m1",
        before_label_ids=["INBOX"], after_label_ids=["INBOX"],
        status="failed", error="apply failed earlier",
    )

    list_calls: list = []
    modify_calls: list = []
    monkeypatch.setattr(
        gmail_client, "list_existing_labels",
        lambda creds: list_calls.append(("list_existing_labels",)) or {},
    )
    monkeypatch.setattr(
        gmail_client, "modify_labels",
        lambda creds, **kw: modify_calls.append(kw.get("message_id")) or {"labelIds": []},
    )

    result = planning.apply_undo_plan(
        creds=MagicMock(), conn=conn, run_id=rid
    )
    assert result.reverted == 0
    assert result.failed == 0
    assert result.run_status == "partially_failed"  # unchanged
    # CRITICAL: Gmail must not have been touched at all.
    assert list_calls == [], (
        "Vacuous undo called list_existing_labels — would spuriously "
        "fail with 'no longer exists' if the label was deleted, despite "
        "having no work to do"
    )
    assert modify_calls == []


def test_apply_archive_plan_zero_rows_does_not_call_gmail(monkeypatch, conn):
    """Vacuous-apply guard for archive: zero planned rows → no Gmail calls,
    run stays planned/dry_run=1."""
    rid = gw_db.create_run(
        conn, phase="archive", category_filter="promotional",
        limit_count=10, dry_run=True, status="planned",
    )
    modify_calls: list[str] = []
    monkeypatch.setattr(
        gmail_client, "modify_labels",
        lambda *a, **k: modify_calls.append(k.get("message_id")) or {"labelIds": []},
    )
    result = planning.apply_archive_plan(creds=MagicMock(), conn=conn, run_id=rid)
    assert result.applied == 0
    assert result.failed == 0
    assert result.run_status == "planned"
    assert modify_calls == []
    run = gw_db.get_run(conn, rid)
    assert run["dry_run"] == 1


# ---------------------------------------------------------------------------
# Undo — apply_undo_plan
# ---------------------------------------------------------------------------


def test_apply_undo_label_run_succeeds(monkeypatch, conn):
    """Undo of a committed label run removes the gmailwiz/<cat> label."""
    label_run_id = _commit_label_run(monkeypatch, conn, mids=["m1", "m2"])
    # The label was created at apply-time; for undo, list_existing_labels
    # must return it so _resolve_label_id_for_undo can look it up.
    monkeypatch.setattr(
        gmail_client, "list_existing_labels",
        lambda creds: {"gmailwiz/promotional": "Label_99"},
    )
    modify_calls: list[dict] = []
    def fake_modify(creds, *, message_id, add_label_ids=(), remove_label_ids=()):
        modify_calls.append({
            "id": message_id, "add": list(add_label_ids),
            "remove": list(remove_label_ids),
        })
        return {"id": message_id, "threadId": "t",
                "labelIds": ["INBOX", "UNREAD"]}  # label removed
    monkeypatch.setattr(gmail_client, "modify_labels", fake_modify)

    result = planning.apply_undo_plan(
        creds=MagicMock(), conn=conn, run_id=label_run_id
    )
    assert result.phase == "label"
    assert result.reverted == 2
    assert result.failed == 0
    assert result.run_status == "undone"

    for c in modify_calls:
        assert c["add"] == []
        assert c["remove"] == ["Label_99"]

    run = gw_db.get_run(conn, label_run_id)
    assert run["status"] == "undone"
    rows = gw_db.get_audit_entries(conn, label_run_id)
    for r in rows:
        assert r["status"] == "reverted"
        assert "Label_99" not in r["after_label_ids"]


def test_apply_undo_archive_run_succeeds(monkeypatch, conn):
    """Undo of a committed archive run re-adds INBOX."""
    label_run_id = _commit_label_run(monkeypatch, conn, mids=["m1"])
    archive_msgs = [
        _msg(mid="m1", sender="m1@x.com", label_ids=["INBOX", "UNREAD", "Label_99"]),
    ]
    _stub_archive_get_metadata(monkeypatch, archive_msgs)
    archive_plan = planning.build_archive_plan(
        creds=MagicMock(), conn=conn, source_run_id=label_run_id
    )
    # Archive apply (real Gmail removes INBOX).
    def fake_archive_modify(creds, *, message_id, add_label_ids=(), remove_label_ids=()):
        return {"id": message_id, "threadId": "t",
                "labelIds": ["UNREAD", "Label_99"]}
    monkeypatch.setattr(gmail_client, "modify_labels", fake_archive_modify)
    planning.apply_archive_plan(
        creds=MagicMock(), conn=conn, run_id=archive_plan.run_id
    )

    # Undo: should call modify_labels(add=['INBOX']).
    undo_calls: list[dict] = []
    def fake_undo_modify(creds, *, message_id, add_label_ids=(), remove_label_ids=()):
        undo_calls.append({
            "id": message_id, "add": list(add_label_ids),
            "remove": list(remove_label_ids),
        })
        return {"id": message_id, "threadId": "t",
                "labelIds": ["INBOX", "UNREAD", "Label_99"]}  # INBOX restored
    monkeypatch.setattr(gmail_client, "modify_labels", fake_undo_modify)

    result = planning.apply_undo_plan(
        creds=MagicMock(), conn=conn, run_id=archive_plan.run_id
    )
    assert result.phase == "archive"
    assert result.reverted == 1
    assert result.run_status == "undone"
    assert undo_calls[0]["add"] == ["INBOX"]
    assert undo_calls[0]["remove"] == []

    run = gw_db.get_run(conn, archive_plan.run_id)
    assert run["status"] == "undone"


def test_apply_undo_rejects_planned_run(monkeypatch, conn):
    rid = gw_db.create_run(
        conn, phase="label", category_filter="promotional",
        limit_count=5, dry_run=True, status="planned",
    )
    with pytest.raises(ValueError, match="committed"):
        planning.apply_undo_plan(creds=MagicMock(), conn=conn, run_id=rid)


def test_apply_undo_rejects_already_undone_run(monkeypatch, conn):
    rid = gw_db.create_run(
        conn, phase="label", category_filter="promotional",
        limit_count=5, dry_run=True, status="undone",
    )
    with pytest.raises(ValueError, match="committed"):
        planning.apply_undo_plan(creds=MagicMock(), conn=conn, run_id=rid)


def test_apply_undo_rejects_non_label_or_archive_phase(monkeypatch, conn):
    rid = gw_db.create_run(
        conn, phase="report", limit_count=5, dry_run=True, status="committed"
    )
    with pytest.raises(ValueError, match="label"):
        planning.apply_undo_plan(creds=MagicMock(), conn=conn, run_id=rid)


def test_apply_undo_label_only_reverts_applied_rows(monkeypatch, conn):
    """A `partially_failed` label run (some applied, some failed) should
    only revert the `applied` rows; the `failed` rows stay failed."""
    for mid in ("m1", "m2"):
        _seed_sender(conn, email=f"{mid}@x.com", category=Category.PROMOTIONAL)
    msgs = [_msg(mid=mid, sender=f"{mid}@x.com") for mid in ("m1", "m2")]
    _stub_gmail(monkeypatch, ids=["m1", "m2"], messages=msgs)
    plan = planning.build_label_plan(
        creds=MagicMock(), conn=conn, category=Category.PROMOTIONAL, limit=10
    )
    _stub_mutations(
        monkeypatch,
        modify_responses={
            "m1": {"id": "m1", "threadId": "t",
                   "labelIds": ["INBOX", "UNREAD", "Label_99"]},
            "m2": RuntimeError("apply failed"),
        },
    )
    planning.apply_label_plan(creds=MagicMock(), conn=conn, run_id=plan.run_id)
    # Run is partially_failed; m1 applied, m2 failed.

    monkeypatch.setattr(
        gmail_client, "list_existing_labels",
        lambda creds: {"gmailwiz/promotional": "Label_99"},
    )
    undo_calls: list[str] = []
    def fake_undo_modify(creds, *, message_id, add_label_ids=(), remove_label_ids=()):
        undo_calls.append(message_id)
        return {"id": message_id, "threadId": "t",
                "labelIds": ["INBOX", "UNREAD"]}
    monkeypatch.setattr(gmail_client, "modify_labels", fake_undo_modify)

    result = planning.apply_undo_plan(
        creds=MagicMock(), conn=conn, run_id=plan.run_id
    )
    # Only m1 was reverted; m2 was never applied so it's not touched.
    assert undo_calls == ["m1"]
    assert result.reverted == 1
    rows_by_id = {r["message_id"]: r for r in gw_db.get_audit_entries(conn, plan.run_id)}
    assert rows_by_id["m1"]["status"] == "reverted"
    assert rows_by_id["m2"]["status"] == "failed"


def test_apply_undo_drift_safety(monkeypatch, conn):
    """Undo must not call list_unread_message_ids or get_message_metadata."""
    label_run_id = _commit_label_run(monkeypatch, conn, mids=["m1"])
    monkeypatch.setattr(
        gmail_client, "list_existing_labels",
        lambda creds: {"gmailwiz/promotional": "Label_99"},
    )

    def _read_must_not_be_called(*args, **kwargs):
        raise AssertionError(
            "apply_undo_plan must not refetch Gmail beyond per-row modify"
        )
    monkeypatch.setattr(gmail_client, "list_unread_message_ids", _read_must_not_be_called)
    monkeypatch.setattr(gmail_client, "get_message_metadata", _read_must_not_be_called)

    monkeypatch.setattr(
        gmail_client, "modify_labels",
        lambda creds, **kw: {"id": kw["message_id"], "threadId": "t",
                             "labelIds": ["INBOX", "UNREAD"]},
    )
    result = planning.apply_undo_plan(
        creds=MagicMock(), conn=conn, run_id=label_run_id
    )
    assert result.reverted == 1


def test_heal_run_status_from_audit_log_promotes_stale_planned_to_committed(
    monkeypatch, conn
):
    """Codex P1 regression coverage: a run whose post-apply
    `update_run_status` write FAILED (Gmail mutations succeeded but
    audit log went terminal while runs.status stayed 'planned') must
    auto-heal on next read so the user can undo it."""
    # Simulate the worst-case integrity gap by hand: create a run at
    # status='planned, dry_run=1', then write audit rows as 'applied'
    # without flipping the run row.
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
        # Hand-flip to 'applied' to mimic Gmail succeeding before
        # update_run_status got a chance.
        gw_db.update_audit_entry(conn, audit_id=audit_id, status="applied")

    # Pre-heal: run is stuck at 'planned, dry_run=1'.
    pre = gw_db.get_run(conn, rid)
    assert pre["status"] == "planned"
    assert pre["dry_run"] == 1

    # Heal — should derive 'committed' from cumulative audit log and persist.
    healed = planning.heal_run_status_from_audit_log(conn, rid)
    assert healed is not None
    assert healed["status"] == "committed"
    assert healed["dry_run"] == 0

    # Persisted state matches.
    persisted = gw_db.get_run(conn, rid)
    assert persisted["status"] == "committed"
    assert persisted["dry_run"] == 0


def test_heal_run_status_partial_terminal_keeps_planned_but_flips_dry_run(
    monkeypatch, conn
):
    """Mid-loop crash case: some audit rows terminal, some still
    planned. Heal must keep run at 'planned' (re-runnable) but flip
    dry_run=0 since real mutations occurred."""
    rid = gw_db.create_run(
        conn, phase="label", category_filter="promotional",
        limit_count=10, dry_run=True, status="planned",
    )
    audit_id_applied = gw_db.append_audit_entry(
        conn, run_id=rid, action="add_label", message_id="m1",
        before_label_ids=["INBOX"], after_label_ids=["INBOX", "Label_99"],
        status="planned",
    )
    gw_db.update_audit_entry(conn, audit_id=audit_id_applied, status="applied")
    gw_db.append_audit_entry(
        conn, run_id=rid, action="add_label", message_id="m2",
        before_label_ids=["INBOX"], after_label_ids=["INBOX", "Label_99"],
        status="planned",
    )

    healed = planning.heal_run_status_from_audit_log(conn, rid)
    assert healed["status"] == "planned"  # still planned (m2 untouched)
    assert healed["dry_run"] == 0  # but real mutation occurred


def test_heal_run_status_no_op_for_already_terminal_run(monkeypatch, conn):
    """Heal must be a no-op for a run that's already in a terminal
    state — don't re-derive or re-write."""
    rid = gw_db.create_run(
        conn, phase="label", category_filter="promotional",
        limit_count=10, dry_run=False, status="committed",
    )
    healed = planning.heal_run_status_from_audit_log(conn, rid)
    assert healed["status"] == "committed"
    assert healed["dry_run"] == 0


def test_heal_run_status_returns_none_for_missing_run(monkeypatch, conn):
    assert planning.heal_run_status_from_audit_log(conn, "missing-rid") is None


def test_apply_undo_label_rejects_unknown_category(monkeypatch, conn):
    """Defense-in-depth: a hand-edited label run with category_filter='unknown'
    must NOT reach `_resolve_label_id_for_undo` (which would query Gmail
    for `gmailwiz/unknown` and fail with 'no longer exists'). Refuse
    here with a clean error symmetric to build_label_plan / apply_label_plan."""
    rid = gw_db.create_run(
        conn, phase="label", category_filter="unknown",
        limit_count=10, dry_run=False, status="committed",
    )
    gw_db.append_audit_entry(
        conn, run_id=rid, action="add_label", message_id="m1",
        before_label_ids=["INBOX"], after_label_ids=["INBOX", "Label_99"],
        status="applied",
    )

    list_calls: list = []
    monkeypatch.setattr(
        gmail_client, "list_existing_labels",
        lambda creds: list_calls.append("list") or {},
    )

    with pytest.raises(ValueError, match="non-classifiable"):
        planning.apply_undo_plan(creds=MagicMock(), conn=conn, run_id=rid)

    # Critically: list_existing_labels was NOT called — UNKNOWN is
    # rejected before any Gmail query.
    assert list_calls == []


def test_apply_undo_label_missing_label_in_gmail_errors(monkeypatch, conn):
    """If the gmailwiz/<cat> label was manually deleted in Gmail between
    apply and undo, the undo must surface a clear error (not silently
    no-op or re-create the label)."""
    label_run_id = _commit_label_run(monkeypatch, conn, mids=["m1"])
    monkeypatch.setattr(gmail_client, "list_existing_labels", lambda creds: {})
    with pytest.raises(ValueError, match="no longer exists"):
        planning.apply_undo_plan(
            creds=MagicMock(), conn=conn, run_id=label_run_id
        )
