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
