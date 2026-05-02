"""Tests for `gmailwiz.db`.

Each test gets its own temp SQLite path via the `tmp_path` fixture so we never
touch the project's real `data/db/state.db`.
"""

from __future__ import annotations

import pytest

from gmailwiz import db as gw_db
from gmailwiz.categories import Category


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "state.db"
    connection = gw_db.connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


def _table_names(connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {r["name"] for r in rows}


def test_schema_creates_all_required_tables(conn):
    names = _table_names(conn)
    assert {"senders", "runs", "audit_log", "app_state"}.issubset(names)


def test_init_schema_is_idempotent(conn):
    # Calling init_schema again must not raise or duplicate anything.
    gw_db.init_schema(conn)
    gw_db.init_schema(conn)
    assert {"senders", "runs", "audit_log", "app_state"}.issubset(_table_names(conn))


def test_upsert_sender_inserts_then_updates(conn):
    gw_db.upsert_sender(
        conn,
        email="news@example.com",
        display_name="Example News",
        category=Category.NEWSLETTER,
        prompt_version="v1",
        model="claude-test",
    )
    row = gw_db.get_sender(conn, "news@example.com")
    assert row is not None
    assert row["category"] is Category.NEWSLETTER
    assert row["display_name"] == "Example News"

    # Update path: same email, new category. Email is normalised to lowercase.
    gw_db.upsert_sender(
        conn,
        email="News@Example.com",
        display_name="Example News",
        category=Category.PROMOTIONAL,
        prompt_version="v1",
        model="claude-test",
    )
    row = gw_db.get_sender(conn, "news@example.com")
    assert row["category"] is Category.PROMOTIONAL


def test_get_sender_returns_none_when_missing(conn):
    assert gw_db.get_sender(conn, "nobody@example.com") is None


def test_get_senders_bulk(conn):
    gw_db.upsert_sender(
        conn,
        email="a@example.com",
        display_name=None,
        category=Category.PERSONAL,
        prompt_version="v1",
        model="m",
    )
    gw_db.upsert_sender(
        conn,
        email="b@example.com",
        display_name=None,
        category=Category.TRANSACTIONAL,
        prompt_version="v1",
        model="m",
    )
    out = gw_db.get_senders(conn, ["a@example.com", "B@example.com", "missing@example.com"])
    assert set(out.keys()) == {"a@example.com", "b@example.com"}
    assert out["b@example.com"]["category"] is Category.TRANSACTIONAL


def test_create_run_returns_id_and_persists(conn):
    run_id = gw_db.create_run(conn, phase="report", limit_count=50, dry_run=True)
    assert isinstance(run_id, str) and run_id
    row = gw_db.get_run(conn, run_id)
    assert row is not None
    assert row["phase"] == "report"
    assert row["limit_count"] == 50
    assert row["dry_run"] == 1
    assert row["status"] == "planned"


def test_update_run_status(conn):
    run_id = gw_db.create_run(conn, phase="report")
    gw_db.update_run_status(conn, run_id, "committed")
    row = gw_db.get_run(conn, run_id)
    assert row["status"] == "committed"


def test_update_run_status_flips_dry_run(conn):
    run_id = gw_db.create_run(conn, phase="label", dry_run=True)
    gw_db.update_run_status(conn, run_id, "committed", dry_run=False)
    row = gw_db.get_run(conn, run_id)
    assert row["status"] == "committed"
    assert row["dry_run"] == 0


def test_update_run_status_raises_on_unknown_id(conn):
    """A typo'd run_id silently matching zero rows would let a caller
    believe state was persisted when it wasn't. Surface the miss loudly."""
    with pytest.raises(KeyError, match="No run with id"):
        gw_db.update_run_status(conn, "does-not-exist", "committed")


def test_update_audit_entry_raises_on_unknown_id(conn):
    with pytest.raises(KeyError, match="No audit_log row"):
        gw_db.update_audit_entry(conn, audit_id=9999, status="applied")


def test_audit_log_round_trip(conn):
    run_id = gw_db.create_run(conn, phase="label", dry_run=True)
    gw_db.append_audit_entry(
        conn,
        run_id=run_id,
        action="add_label",
        message_id="msg-1",
        before_label_ids=["INBOX", "UNREAD"],
        after_label_ids=["INBOX", "UNREAD", "Label_42"],
        sender_email="news@example.com",
        status="planned",
    )
    gw_db.append_audit_entry(
        conn,
        run_id=run_id,
        action="add_label",
        message_id="msg-2",
        before_label_ids=["INBOX"],
        after_label_ids=["INBOX", "Label_42"],
        sender_email="news@example.com",
        status="planned",
    )
    entries = gw_db.get_audit_entries(conn, run_id)
    assert len(entries) == 2
    assert entries[0]["before_label_ids"] == ["INBOX", "UNREAD"]
    assert entries[0]["after_label_ids"] == ["INBOX", "UNREAD", "Label_42"]
    assert entries[1]["message_id"] == "msg-2"


def test_app_state_set_and_get(conn):
    assert gw_db.get_app_state(conn, "missing") is None
    gw_db.set_app_state(conn, "last_auth_email", "gaylon@example.com")
    assert gw_db.get_app_state(conn, "last_auth_email") == "gaylon@example.com"
    # Overwrite path
    gw_db.set_app_state(conn, "last_auth_email", "other@example.com")
    assert gw_db.get_app_state(conn, "last_auth_email") == "other@example.com"
