"""Minimal Telegram sender for completion notifications.

Best-effort by design: a failure to deliver the Telegram message must
never fail the underlying job (the actual Gmail mutations are already
persisted in state.db's audit_log). All errors are logged to stderr
and swallowed.

Env vars
--------
``TELEGRAM_BOT_TOKEN``  Bot token from BotFather. If unset, ``send_message``
                       is a silent no-op (so dev/test runs don't try to call
                       Telegram).
``TELEGRAM_CHAT_ID``     Recipient chat id. Required when token is set.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

import httpx


TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV = "TELEGRAM_CHAT_ID"

# 4096 is Telegram's documented hard limit; we ship some headroom.
_MAX_LEN = 3900


def _truncate(text: str) -> str:
    if len(text) <= _MAX_LEN:
        return text
    return text[: _MAX_LEN - 20] + "\n…(truncated)"


def send_message(text: str, *, timeout: float = 5.0) -> bool:
    """Send ``text`` to the configured Telegram chat.

    Returns True on a 2xx Telegram response, False otherwise (including
    "not configured"). Never raises — callers can fire-and-forget.
    """
    token = os.environ.get(TOKEN_ENV, "").strip()
    chat_id = os.environ.get(CHAT_ID_ENV, "").strip()
    if not token or not chat_id:
        # Silent no-op in dev/test. Operator can grep for this trace if
        # they want to know why notifications aren't firing.
        sys.stderr.write(
            f"[telegram] skipped: {TOKEN_ENV}/{CHAT_ID_ENV} not configured\n"
        )
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": _truncate(text),
        # plain text — avoid Markdown escaping issues from snapshot ids etc.
        "disable_notification": False,
    }
    try:
        resp = httpx.post(url, json=payload, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[telegram] post failed: {type(exc).__name__}: {exc}\n")
        return False

    if resp.status_code // 100 != 2:
        # Don't log full bodies; Telegram's error responses can echo the
        # chat_id (which is fine but noisy) or include the bot token in
        # rare cases. Status + first 120 chars is plenty for triage.
        body_preview = resp.text[:120].replace("\n", " ")
        sys.stderr.write(
            f"[telegram] HTTP {resp.status_code}: {body_preview}\n"
        )
        return False
    return True


__all__ = ["send_message", "TOKEN_ENV", "CHAT_ID_ENV"]
