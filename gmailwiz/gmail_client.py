"""Thin wrapper over googleapiclient for the Gmail operations gmailwiz needs.

Phase 1 reads (list unread, fetch metadata). Phase 2 adds label mutations
(``list_existing_labels`` is read-only; ``ensure_labels_exist`` and
``modify_labels`` mutate). All mutations surface raw ``HttpError`` verbatim
per cs.md — never swallowed into a generic message.
"""

from __future__ import annotations

import sys
from email.header import decode_header
from email.utils import parseaddr
from typing import Any, Iterable, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Headers we want from each message — keep the payload tiny (no bodies).
_METADATA_HEADERS: tuple[str, ...] = ("From", "Subject", "Date")

# Cap snippet length stored / passed to classifier. Per cs.md privacy rule:
# never log full bodies; sender + subject + first ~200 chars of snippet only.
_SNIPPET_MAX_CHARS = 200


def _build_service(creds: Credentials):
    """Build a Gmail API client. Cache discovery off — quieter logs, no warning."""
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _truncate_snippet(snippet: Optional[str]) -> str:
    if not snippet:
        return ""
    s = snippet.strip()
    if len(s) <= _SNIPPET_MAX_CHARS:
        return s
    return s[:_SNIPPET_MAX_CHARS].rstrip() + "..."


def _decode_rfc2047(raw: str) -> str:
    """Decode RFC 2047 encoded-word display names (e.g. ``=?UTF-8?B?...?=``).

    Falls back to the raw string on any decoding error so we never lose
    data — the encoded form is at least readable as a unique identifier.
    """
    if not raw or "=?" not in raw:
        return raw
    try:
        parts = decode_header(raw)
    except Exception:
        return raw
    decoded: list[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            try:
                decoded.append(chunk.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                decoded.append(chunk.decode("utf-8", errors="replace"))
        else:
            decoded.append(chunk)
    return "".join(decoded)


def _parse_from_header(value: str) -> tuple[str, str]:
    """Split a Gmail ``From:`` header into ``(display_name, email_lower)``.

    Returns ``("", "")`` if the address is missing or malformed (no ``@``).
    ``email.utils.parseaddr`` is permissive — it'll happily return
    ``("display", "noreply")`` from a header like ``no-reply <noreply>``,
    which would pollute the senders cache with bogus keys. Require the
    parsed address to look like an email (single ``@``, non-empty halves).

    Display names that arrive in RFC 2047 encoded-word form (common for
    non-ASCII names) are decoded so the report shows ``"José Pérez"``
    instead of ``"=?UTF-8?B?Sm9zw6kgUMOpcmV6?="``.
    """
    if not value:
        return "", ""
    name, addr = parseaddr(value)
    addr_lower = (addr or "").strip().lower()
    # Reject anything that doesn't look like an email: must contain exactly one
    # '@' with non-empty local-part and domain. parseaddr also returns the raw
    # input when it can't split, so this guards against that too.
    local, sep, domain = addr_lower.partition("@")
    if not sep or not local or not domain or "@" in domain:
        return _decode_rfc2047((name or "").strip()), ""
    return _decode_rfc2047((name or "").strip()), addr_lower


def list_unread_message_ids(
    creds: Credentials,
    *,
    max_results: int = 100,
    query: str = "is:unread in:inbox",
) -> list[str]:
    """Return message IDs matching the unread/inbox query, up to `max_results`.

    Paginates through ``users.messages.list`` until ``max_results`` is reached
    or Gmail runs out of pages.
    """
    if max_results <= 0:
        return []

    service = _build_service(creds)
    ids: list[str] = []
    page_token: Optional[str] = None

    while len(ids) < max_results:
        remaining = max_results - len(ids)
        # Gmail caps the page size at 500.
        page_size = min(remaining, 500)
        resp = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=page_size,
                pageToken=page_token,
            )
            .execute()
        )
        page_messages = resp.get("messages", [])
        # Defensive: if Gmail ever returns an empty page with a nextPageToken
        # set, blindly looping would never terminate. Break out instead.
        if not page_messages:
            break
        for msg in page_messages:
            mid = msg.get("id")
            if mid:
                ids.append(mid)
            if len(ids) >= max_results:
                break

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return ids


def get_message_metadata(
    creds: Credentials,
    message_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """Fetch lightweight metadata dicts for a batch of message IDs.

    Each dict contains: ``id``, ``thread_id``, ``sender_email``, ``sender_name``,
    ``subject``, ``snippet`` (truncated to ~200 chars), ``internal_date``,
    ``label_ids``.

    Issues one ``messages.get`` per ID with ``format='metadata'`` and a tight
    headers list, so per-message payload stays small. (Phase 1 doesn't need
    Gmail's batch endpoint — keep it simple until we measure pain.)
    """
    service = _build_service(creds)
    results: list[dict[str, Any]] = []

    for mid in message_ids:
        if not mid:
            continue
        try:
            msg = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=list(_METADATA_HEADERS),
                )
                .execute()
            )
        except HttpError as exc:
            # Active inboxes drop messages between `list` and `get` (the user
            # archived/deleted on their phone, etc.). Per-message HTTP errors
            # shouldn't abort the whole report — log to stderr and skip.
            # Auth/quota failures will recur on the next ID and the operator
            # will see them clearly. cs.md "surface raw errors" still holds:
            # we surface, just don't kill the whole run.
            sys.stderr.write(f"  Skipping message {mid}: {type(exc).__name__}: {exc}\n")
            continue

        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in msg.get("payload", {}).get("headers", [])
        }
        sender_name, sender_email = _parse_from_header(headers.get("from", ""))
        # Subject lines also come RFC 2047 encoded for non-ASCII content
        # (Asian language senders, accented characters, emoji-tag prefixes).
        # Decode so the report and classifier prompt show readable text.
        subject = _decode_rfc2047(headers.get("subject", ""))

        results.append(
            {
                "id": msg.get("id"),
                "thread_id": msg.get("threadId"),
                "sender_email": sender_email,
                "sender_name": sender_name,
                "subject": subject,
                "snippet": _truncate_snippet(msg.get("snippet")),
                "internal_date": msg.get("internalDate"),
                "label_ids": list(msg.get("labelIds", [])),
            }
        )

    return results


# ---------------------------------------------------------------------------
# Phase 2 — label mutations
# ---------------------------------------------------------------------------


def list_existing_labels(creds: Credentials) -> dict[str, str]:
    """Read-only listing of every label in the user's account: ``{name: id}``.

    Used at plan time to learn whether the target ``gmailwiz/<category>`` label
    already exists. If it doesn't, the planner skips the "already labeled"
    filter (a label that doesn't exist can't be on any message) and the
    commit step creates it on demand via :func:`ensure_labels_exist`.
    """
    service = _build_service(creds)
    resp = service.users().labels().list(userId="me").execute()
    out: dict[str, str] = {}
    for lbl in resp.get("labels", []):
        name = lbl.get("name")
        lid = lbl.get("id")
        if name and lid:
            out[name] = lid
    return out


def ensure_labels_exist(
    creds: Credentials,
    label_names: Iterable[str],
) -> dict[str, str]:
    """Return ``{name: id}`` for each requested label, creating any missing ones.

    Mutating: invokes ``users.labels.create`` for absent names. Callers should
    only invoke at commit time. New labels are visible in the label list and
    in the message list (``labelShow`` / ``show``) so the user can see and
    manually un-label from Gmail's UI if needed.
    """
    # Reuse `list_existing_labels` so the listing/parsing rules stay in one
    # place. The extra service-build cost is one HTTP-less object construction.
    by_name = list_existing_labels(creds)

    needed = [name for name in label_names if name not in by_name]
    if not needed:
        return {name: by_name[name] for name in label_names}

    service = _build_service(creds)
    out: dict[str, str] = {name: by_name[name] for name in label_names if name in by_name}
    for name in needed:
        created = (
            service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        new_id = created.get("id")
        if not new_id:
            # Surface the unexpected response shape rather than papering over it.
            raise RuntimeError(
                f"Gmail labels.create returned no id for {name!r}: {created!r}"
            )
        out[name] = new_id
    return out


def modify_labels(
    creds: Credentials,
    *,
    message_id: str,
    add_label_ids: Iterable[str] = (),
    remove_label_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Apply a label mutation to a single message. Returns Gmail's response.

    Idempotent: adding a label that's already present is a no-op for Gmail,
    as is removing one that isn't. The returned ``labelIds`` reflect the
    message's state *after* the modify (used by the audit_log to record the
    real post-state, not just our projection).

    A call with both add and remove empty short-circuits without an API
    request and returns ``{}`` — the caller had nothing to do.
    """
    add_list = list(add_label_ids)
    remove_list = list(remove_label_ids)
    body: dict[str, Any] = {}
    if add_list:
        body["addLabelIds"] = add_list
    if remove_list:
        body["removeLabelIds"] = remove_list
    if not body:
        return {}
    service = _build_service(creds)
    return (
        service.users()
        .messages()
        .modify(userId="me", id=message_id, body=body)
        .execute()
    )
