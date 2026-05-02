"""Tests for `gmailwiz.gmail_client` — focused on sender header parsing.

The Gmail API call layer is mocked at the boundary; these tests cover the
header-parsing helper that's pure logic.
"""

from __future__ import annotations

import pytest

from gmailwiz.gmail_client import _decode_rfc2047, _parse_from_header, _truncate_snippet


@pytest.mark.parametrize(
    "raw, expected_name, expected_email",
    [
        ('"Display Name" <foo@example.com>', "Display Name", "foo@example.com"),
        ("Display Name <foo@example.com>", "Display Name", "foo@example.com"),
        ("foo@example.com", "", "foo@example.com"),
        ("<foo@example.com>", "", "foo@example.com"),
        ("FOO@EXAMPLE.COM", "", "foo@example.com"),
        ("  foo@example.com  ", "", "foo@example.com"),
    ],
)
def test_parse_from_header_valid_addresses(raw, expected_name, expected_email):
    name, addr = _parse_from_header(raw)
    assert name == expected_name
    assert addr == expected_email


@pytest.mark.parametrize(
    "raw",
    [
        "",  # empty header
        "no-reply <noreply>",  # display has hyphen, no @ in addr
        "Just Some Text",  # no angle brackets, no @
        "<>",  # empty angle brackets
        "@example.com",  # missing local part
        "foo@",  # missing domain
        "foo@bar@baz.com",  # multiple @
        "  ",  # whitespace only
    ],
)
def test_parse_from_header_rejects_invalid_addresses(raw):
    """Bogus addresses must NOT pollute the senders cache.

    The pre-fix bug: `parseaddr("no-reply <noreply>")` returned
    `("no-reply", "noreply")`, which the classifier then keyed off as if it
    were a real email address. Fix is to require `local@domain` shape.
    """
    name, addr = _parse_from_header(raw)
    assert addr == "", f"expected empty email for {raw!r}, got {addr!r}"


def test_truncate_snippet_caps_at_200_chars():
    long = "x" * 500
    out = _truncate_snippet(long)
    # 200 chars + "..." suffix.
    assert len(out) <= 203
    assert out.endswith("...")


def test_truncate_snippet_short_passes_through():
    assert _truncate_snippet("short text") == "short text"


def test_truncate_snippet_handles_none_and_empty():
    assert _truncate_snippet(None) == ""
    assert _truncate_snippet("") == ""


# ---------------------------------------------------------------------------
# RFC 2047 decoding (used for both From display names and Subject headers)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Plain ASCII passes through.
        ("Hello world", "Hello world"),
        ("", ""),
        # B-encoded UTF-8 (base64).
        ("=?UTF-8?B?SGVsbG8sIFdvcmxkIQ==?=", "Hello, World!"),
        # Q-encoded UTF-8 (quoted-printable).
        ("=?UTF-8?Q?Caf=C3=A9_=E2=98=95?=", "Café ☕"),
        # Mixed ASCII + encoded chunks.
        ("From =?UTF-8?B?Sm9zw6k=?= directly", "From José directly"),
    ],
)
def test_decode_rfc2047_roundtrips(raw, expected):
    assert _decode_rfc2047(raw) == expected


def test_decode_rfc2047_falls_back_on_decode_header_exception(monkeypatch):
    """If `email.header.decode_header` itself raises, fall back to the raw
    string verbatim — the user still sees an identifying token rather than
    losing data."""
    raw = "=?UTF-8?B?some-input?="

    def _boom(_: str):
        raise RuntimeError("decode_header exploded")

    # Patch the decode_header symbol bound inside gmail_client module.
    monkeypatch.setattr(gmail_client, "decode_header", _boom)
    out = _decode_rfc2047(raw)
    # Exact-match the raw input — fallback must preserve it verbatim.
    assert out == raw


def test_decode_rfc2047_handles_undecodable_bytes_via_replace():
    """Bytes that can't be decoded under the announced charset must use
    `errors='replace'` (U+FFFD replacement char) rather than raising.

    Uses UTF-8 as the announced charset with bytes that are invalid in
    UTF-8 (a lone continuation byte 0x80 / a 0xff). ISO-8859-1 wouldn't
    work — every byte is valid in ISO-8859-1 — so we'd never hit the
    replace path.
    """
    # Base64-encoded bytes [0xff, 0xff, 0xff] = "////". 0xff is invalid
    # as a UTF-8 leading byte, so utf-8 decode would raise without
    # errors='replace'.
    raw = "=?UTF-8?B?////?="
    out = _decode_rfc2047(raw)
    # The replacement character must appear (proves errors='replace' fired).
    assert "�" in out, f"expected replacement chars in {out!r}"


def test_subject_decoded_in_get_message_metadata(monkeypatch):
    """Encoded Subject headers must be decoded before reaching downstream
    consumers (report rendering and classifier prompts)."""
    payloads = {
        "encoded": {
            "id": "encoded",
            "threadId": "t1",
            "snippet": "preview",
            "internalDate": "1700000000000",
            "labelIds": ["INBOX"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "Sender <a@b.com>"},
                    {"name": "Subject", "value": "=?UTF-8?B?SGVsbG8sIFdvcmxkIQ==?="},
                ]
            },
        }
    }
    fake = _build_fake_service(get_responses=payloads)
    monkeypatch.setattr(gmail_client, "_build_service", lambda creds: fake)

    results = gmail_client.get_message_metadata(creds=MagicMock(), message_ids=["encoded"])
    assert results[0]["subject"] == "Hello, World!"


# ---------------------------------------------------------------------------
# list_unread_message_ids — pagination + edge cases
# ---------------------------------------------------------------------------


from unittest.mock import MagicMock, patch

from gmailwiz import gmail_client


def _build_fake_service(list_responses=None, get_responses=None):
    """Stand-in for `googleapiclient.build("gmail", ...)`.

    `list_responses` is a list of dicts returned in order from
    `users().messages().list(...).execute()`. `get_responses` is keyed by
    message id and dispatched by `users().messages().get(id=...).execute()`.
    """
    list_iter = iter(list_responses or [])
    get_responses = get_responses or {}

    list_call = MagicMock()
    list_call.execute.side_effect = lambda: next(list_iter)

    def get_factory(*, userId, id, format, metadataHeaders):  # noqa: N803
        call = MagicMock()
        if isinstance(get_responses.get(id), Exception):
            call.execute.side_effect = get_responses[id]
        else:
            call.execute.return_value = get_responses.get(id, {})
        return call

    messages = MagicMock()
    messages.list.return_value = list_call
    messages.get.side_effect = get_factory

    users = MagicMock()
    users.messages.return_value = messages

    service = MagicMock()
    service.users.return_value = users
    return service


def test_list_unread_paginates_until_max_results(monkeypatch):
    pages = [
        {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "tok1"},
        {"messages": [{"id": "c"}, {"id": "d"}], "nextPageToken": "tok2"},
        {"messages": [{"id": "e"}]},  # no nextPageToken → loop terminates
    ]
    fake = _build_fake_service(list_responses=pages)
    monkeypatch.setattr(gmail_client, "_build_service", lambda creds: fake)

    ids = gmail_client.list_unread_message_ids(creds=MagicMock(), max_results=10)
    assert ids == ["a", "b", "c", "d", "e"]


def test_list_unread_stops_at_max_results(monkeypatch):
    pages = [{"messages": [{"id": str(i)} for i in range(50)], "nextPageToken": "tok"}]
    fake = _build_fake_service(list_responses=pages)
    monkeypatch.setattr(gmail_client, "_build_service", lambda creds: fake)

    ids = gmail_client.list_unread_message_ids(creds=MagicMock(), max_results=3)
    assert ids == ["0", "1", "2"]


def test_list_unread_breaks_on_empty_page_with_token(monkeypatch):
    """An empty page with a nextPageToken must NOT loop forever."""
    pages = [{"messages": [], "nextPageToken": "tok-but-no-messages"}]
    fake = _build_fake_service(list_responses=pages)
    monkeypatch.setattr(gmail_client, "_build_service", lambda creds: fake)

    ids = gmail_client.list_unread_message_ids(creds=MagicMock(), max_results=10)
    assert ids == []


def test_list_unread_returns_empty_for_zero_max():
    """max_results=0 short-circuits with no API call."""
    assert gmail_client.list_unread_message_ids(creds=MagicMock(), max_results=0) == []


# ---------------------------------------------------------------------------
# get_message_metadata — HttpError tolerance
# ---------------------------------------------------------------------------


def test_get_message_metadata_skips_http_errors(monkeypatch, capsys):
    """A 404 on one ID must not abort the rest of the batch."""
    from googleapiclient.errors import HttpError

    # Build a stub HttpError. The constructor wants a response and content;
    # we just need an instance that's truthy and stringifiable.
    fake_resp = MagicMock(status=404, reason="Not Found")
    fake_resp.__getitem__ = lambda self, k: "404"
    err = HttpError(fake_resp, b'{"error":"not found"}')

    payloads = {
        "alive": {
            "id": "alive",
            "threadId": "t1",
            "snippet": "hi",
            "internalDate": "1700000000000",
            "labelIds": ["INBOX"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "Alive Sender <alive@example.com>"},
                    {"name": "Subject", "value": "Hello"},
                ]
            },
        },
        "deleted": err,  # raises on .execute()
    }
    fake = _build_fake_service(get_responses=payloads)
    monkeypatch.setattr(gmail_client, "_build_service", lambda creds: fake)

    results = gmail_client.get_message_metadata(creds=MagicMock(), message_ids=["alive", "deleted"])

    # The deleted ID was skipped; the alive one came through.
    assert len(results) == 1
    assert results[0]["id"] == "alive"
    assert results[0]["sender_email"] == "alive@example.com"
    assert results[0]["sender_name"] == "Alive Sender"

    # The skip should have been logged to stderr.
    captured = capsys.readouterr()
    assert "Skipping message deleted" in captured.err


def test_get_message_metadata_handles_missing_headers(monkeypatch):
    """Messages with no From: header should produce empty sender fields."""
    payloads = {
        "no-from": {
            "id": "no-from",
            "threadId": "t1",
            "snippet": "hi",
            "internalDate": "1700000000000",
            "labelIds": ["INBOX"],
            "payload": {"headers": [{"name": "Subject", "value": "Headerless"}]},
        }
    }
    fake = _build_fake_service(get_responses=payloads)
    monkeypatch.setattr(gmail_client, "_build_service", lambda creds: fake)

    results = gmail_client.get_message_metadata(creds=MagicMock(), message_ids=["no-from"])
    assert results[0]["sender_email"] == ""
    assert results[0]["sender_name"] == ""
    assert results[0]["subject"] == "Headerless"


# ---------------------------------------------------------------------------
# Phase 2 — labels.list / labels.create / messages.modify
# ---------------------------------------------------------------------------


def _build_fake_labels_service(*, list_response=None, create_responses=None, modify_responses=None):
    """Stand-in for the gmail service that supports labels.list,
    labels.create, and messages.modify.
    """
    list_response = list_response or {"labels": []}
    create_responses = create_responses or {}
    modify_responses = modify_responses or {}
    create_calls: list[dict] = []
    modify_calls: list[dict] = []

    labels = MagicMock()

    list_call = MagicMock()
    list_call.execute.return_value = list_response
    labels.list.return_value = list_call

    def create_factory(*, userId, body):  # noqa: N803
        create_calls.append({"userId": userId, "body": body})
        call = MagicMock()
        name = body.get("name")
        resp = create_responses.get(name) or {"id": f"Label_for_{name}", "name": name}
        if isinstance(resp, Exception):
            call.execute.side_effect = resp
        else:
            call.execute.return_value = resp
        return call

    labels.create.side_effect = create_factory

    messages = MagicMock()

    def modify_factory(*, userId, id, body):  # noqa: N803
        modify_calls.append({"userId": userId, "id": id, "body": body})
        call = MagicMock()
        resp = modify_responses.get(id) or {"id": id, "labelIds": []}
        if isinstance(resp, Exception):
            call.execute.side_effect = resp
        else:
            call.execute.return_value = resp
        return call

    messages.modify.side_effect = modify_factory

    users = MagicMock()
    users.labels.return_value = labels
    users.messages.return_value = messages

    service = MagicMock()
    service.users.return_value = users
    return service, create_calls, modify_calls


def test_list_existing_labels_returns_name_to_id_map(monkeypatch):
    fake, _, _ = _build_fake_labels_service(
        list_response={"labels": [
            {"id": "INBOX", "name": "INBOX"},
            {"id": "Label_42", "name": "gmailwiz/promotional"},
            {"id": "no_name"},  # malformed — should be skipped
            {"name": "no_id"},  # malformed — should be skipped
        ]}
    )
    monkeypatch.setattr(gmail_client, "_build_service", lambda creds: fake)

    out = gmail_client.list_existing_labels(creds=MagicMock())
    assert out == {"INBOX": "INBOX", "gmailwiz/promotional": "Label_42"}


def test_ensure_labels_exist_returns_existing_id_without_create(monkeypatch):
    fake, create_calls, _ = _build_fake_labels_service(
        list_response={"labels": [
            {"id": "Label_42", "name": "gmailwiz/promotional"},
        ]}
    )
    monkeypatch.setattr(gmail_client, "_build_service", lambda creds: fake)

    out = gmail_client.ensure_labels_exist(
        creds=MagicMock(),
        label_names=["gmailwiz/promotional"],
    )
    assert out == {"gmailwiz/promotional": "Label_42"}
    assert create_calls == [], "should not have called labels.create"


def test_ensure_labels_exist_creates_missing_label(monkeypatch):
    fake, create_calls, _ = _build_fake_labels_service(
        list_response={"labels": []},
        create_responses={"gmailwiz/promotional": {"id": "Label_NEW", "name": "gmailwiz/promotional"}},
    )
    monkeypatch.setattr(gmail_client, "_build_service", lambda creds: fake)

    out = gmail_client.ensure_labels_exist(
        creds=MagicMock(),
        label_names=["gmailwiz/promotional"],
    )
    assert out == {"gmailwiz/promotional": "Label_NEW"}
    assert len(create_calls) == 1
    body = create_calls[0]["body"]
    assert body["name"] == "gmailwiz/promotional"
    assert body["labelListVisibility"] == "labelShow"
    assert body["messageListVisibility"] == "show"


def test_ensure_labels_exist_mixed(monkeypatch):
    """One label exists, one needs creating — both come back in the result."""
    fake, create_calls, _ = _build_fake_labels_service(
        list_response={"labels": [
            {"id": "Label_42", "name": "gmailwiz/promotional"},
        ]}
    )
    monkeypatch.setattr(gmail_client, "_build_service", lambda creds: fake)

    out = gmail_client.ensure_labels_exist(
        creds=MagicMock(),
        label_names=["gmailwiz/promotional", "gmailwiz/newsletter"],
    )
    assert out["gmailwiz/promotional"] == "Label_42"
    assert out["gmailwiz/newsletter"] == "Label_for_gmailwiz/newsletter"
    assert [c["body"]["name"] for c in create_calls] == ["gmailwiz/newsletter"]


def test_ensure_labels_exist_raises_on_missing_id_in_create_response(monkeypatch):
    """If Gmail's create response omits an id, surface it loudly — don't silently
    cache an empty string and pollute downstream calls."""
    fake, _, _ = _build_fake_labels_service(
        list_response={"labels": []},
        create_responses={"gmailwiz/x": {"name": "gmailwiz/x"}},  # no id
    )
    monkeypatch.setattr(gmail_client, "_build_service", lambda creds: fake)

    with pytest.raises(RuntimeError, match="no id"):
        gmail_client.ensure_labels_exist(creds=MagicMock(), label_names=["gmailwiz/x"])


def test_modify_labels_sends_add_only(monkeypatch):
    fake, _, modify_calls = _build_fake_labels_service(
        modify_responses={"m1": {"id": "m1", "labelIds": ["INBOX", "Label_42"]}},
    )
    monkeypatch.setattr(gmail_client, "_build_service", lambda creds: fake)

    out = gmail_client.modify_labels(
        creds=MagicMock(),
        message_id="m1",
        add_label_ids=["Label_42"],
    )
    assert out == {"id": "m1", "labelIds": ["INBOX", "Label_42"]}
    assert len(modify_calls) == 1
    assert modify_calls[0]["body"] == {"addLabelIds": ["Label_42"]}


def test_modify_labels_sends_remove_only(monkeypatch):
    fake, _, modify_calls = _build_fake_labels_service(
        modify_responses={"m1": {"id": "m1", "labelIds": []}},
    )
    monkeypatch.setattr(gmail_client, "_build_service", lambda creds: fake)

    gmail_client.modify_labels(
        creds=MagicMock(),
        message_id="m1",
        remove_label_ids=["INBOX"],
    )
    assert modify_calls[0]["body"] == {"removeLabelIds": ["INBOX"]}


def test_modify_labels_sends_add_and_remove(monkeypatch):
    fake, _, modify_calls = _build_fake_labels_service()
    monkeypatch.setattr(gmail_client, "_build_service", lambda creds: fake)

    gmail_client.modify_labels(
        creds=MagicMock(),
        message_id="m1",
        add_label_ids=["Label_42"],
        remove_label_ids=["INBOX"],
    )
    body = modify_calls[0]["body"]
    assert body == {"addLabelIds": ["Label_42"], "removeLabelIds": ["INBOX"]}


def test_modify_labels_short_circuits_on_empty(monkeypatch):
    """No add and no remove → no API call, return {}."""
    sentinel = object()

    def boom(creds):
        raise AssertionError("should not have been called")

    monkeypatch.setattr(gmail_client, "_build_service", boom)
    out = gmail_client.modify_labels(creds=sentinel, message_id="m1")
    assert out == {}
