"""Anthropic-backed sender classifier with on-disk cache.

The classifier operates on **senders, not messages** (per CLAUDE.md): the
input is one or more representative subjects/snippets per sender, and the
output is one of the four `Category` values. Results are cached in the
`senders` SQLite table; cache hits do not call the API.

Strict JSON output. Malformed or out-of-vocabulary responses are surfaced as
`Category.UNKNOWN` (never silent-coerced to a "default" category) so the user
sees them in the report and can investigate.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Sequence

from gmailwiz.categories import (
    CLASSIFIABLE_CATEGORIES,
    Category,
    parse_category,
)
from gmailwiz import db as gw_db

# ---------------------------------------------------------------------------
# Tuning knobs
# ---------------------------------------------------------------------------

# Bumped whenever the prompt shape changes — invalidates older cache rows.
PROMPT_VERSION = "p1-2026-04-28"

# Pinned Claude model. Defensible default for cost/quality on this task.
# Using the latest-version alias rather than a dated snapshot — the cache
# does NOT key off `model` for invalidation (it uses `prompt_version` + TTL),
# so an alias drift won't cause stale-cache bugs. If Phase 2 ever needs
# strict reproducibility of cached classifications, swap to a dated snapshot
# and bump `PROMPT_VERSION` to force re-classification.
DEFAULT_MODEL = "claude-sonnet-4-5"

# How long a cached classification is considered fresh before we re-classify.
CACHE_TTL_DAYS = 30

# How many sample subject/snippet pairs to send per sender. More context helps,
# but this is a token-cost tradeoff. Hold at 3 for Phase 1; revisit when we
# have real data.
MAX_SAMPLES_PER_SENDER = 3

# Senders per API call. Each entry produces ~60-80 output tokens of JSON; at
# this batch size, MAX_OUTPUT_TOKENS_PER_BATCH gives comfortable headroom even
# with verbose senders. Larger inboxes are split across multiple calls.
SENDERS_PER_BATCH = 25
MAX_OUTPUT_TOKENS_PER_BATCH = 4096

ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class SenderSample:
    """One unread message used as a classification hint for a sender."""

    subject: str = ""
    snippet: str = ""


@dataclass
class SenderInput:
    """Aggregated classification request for a single sender."""

    email: str
    display_name: str = ""
    samples: list[SenderSample] = field(default_factory=list)

    def normalised_email(self) -> str:
        return self.email.strip().lower()


@dataclass
class ClassificationResult:
    """Outcome for a single sender after classification (or cache lookup)."""

    email: str
    category: Category
    source: str  # "cache" | "model" | "unknown"
    raw_response: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You categorize email senders for a personal Gmail triage tool. "
    "For each sender you are given, output exactly one category from this "
    "fixed list:\n"
    "  - promotional: marketing, deals, sales, ads, retargeting.\n"
    "  - transactional: receipts, statements, shipping, account/security alerts, "
    "appointment confirmations, automated service notifications a human did not write to me.\n"
    "  - newsletter: periodic editorial content, digests, blog updates.\n"
    "  - personal: written by an actual human directly to the recipient.\n"
    "Respond with strict JSON only — no prose, no markdown fences. The schema is:\n"
    '  {"results": [{"email": "<lowercase>", "category": "<one of the four>"}]}\n'
    "Use lowercase emails. If you genuinely cannot tell, pick the closest fit; "
    "do not invent a fifth category."
)


def _format_sender_block(s: SenderInput) -> str:
    """Render one sender into the user-prompt body (sender id + samples)."""
    lines = [f"Sender: {s.email}"]
    if s.display_name:
        lines.append(f"Name: {s.display_name}")
    if not s.samples:
        lines.append("Samples: (none)")
    else:
        lines.append("Samples:")
        for i, sample in enumerate(s.samples[:MAX_SAMPLES_PER_SENDER], start=1):
            subj = (sample.subject or "").strip() or "(no subject)"
            # Subjects are NOT length-capped: per CLAUDE.md, the working
            # unit is "Sender + subject + first ~200 chars of snippet" —
            # only the snippet has the privacy cap. Subjects are short in
            # practice, and the full text is real classifier signal.
            snip = (sample.snippet or "").strip()
            # cs.md rule: never send full bodies — snippets are truncated to
            # 200 chars in `gmail_client._truncate_snippet`, which appends
            # "..." to make a 203-char string. Only truncate again if a
            # caller bypassed that (length > 203). Avoids double-truncating
            # the legitimate ellipsis suffix.
            if len(snip) > 203:
                snip = snip[:200].rstrip() + "..."
            lines.append(f"  {i}. subject: {subj}")
            if snip:
                lines.append(f"     snippet: {snip}")
    return "\n".join(lines)


def build_user_prompt(senders: Sequence[SenderInput]) -> str:
    """Build the user-message body for a batch of senders."""
    blocks = [_format_sender_block(s) for s in senders]
    return (
        "Classify each of the following senders. Return JSON exactly matching the schema.\n\n"
        + "\n\n".join(blocks)
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


_JSON_OBJECT_RE_GREEDY = re.compile(r"\{.*\}", re.DOTALL)
_JSON_OBJECT_RE_LAZY = re.compile(r"\{.*?\}", re.DOTALL)


def _extract_text_from_response(response: Any) -> str:
    """Pull the text body out of an Anthropic Messages API response.

    Tolerates both the SDK object shape (``response.content[0].text``) and the
    raw-dict shape used by tests.
    """
    if response is None:
        return ""
    content = getattr(response, "content", None)
    if content is None and isinstance(response, dict):
        content = response.get("content")
    if not content:
        return ""

    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(text)
    return "".join(parts)


def parse_classifier_response(text: str) -> dict[str, Category]:
    """Parse the model's JSON output into ``{email_lower: Category}``.

    Returns an empty dict on hard parse failure so the caller can mark every
    sender in the batch as `UNKNOWN` rather than crashing the whole report.
    """
    if not text:
        return {}

    # Strip markdown fences if the model ignored instructions.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        # Last-ditch: try to extract a `{...}` blob. First try the greedy
        # outermost match (handles nested objects in a clean response). If
        # that doesn't parse, fall back to the smallest possible blob — this
        # rescues the case where the model bracketed the JSON with prose like
        # "Sure! Here is `{"results":[...]}`. Hope that helps!".
        payload = None
        for pattern in (_JSON_OBJECT_RE_GREEDY, _JSON_OBJECT_RE_LAZY):
            match = pattern.search(cleaned)
            if not match:
                continue
            try:
                payload = json.loads(match.group(0))
                break
            except json.JSONDecodeError:
                continue
        if payload is None:
            return {}

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return {}

    out: dict[str, Category] = {}
    for entry in results:
        if not isinstance(entry, dict):
            continue
        email = entry.get("email")
        category_raw = entry.get("category")
        if not isinstance(email, str) or not isinstance(category_raw, str):
            continue
        normalised = email.strip().lower()
        if not normalised:
            # Skip blank emails — they'd never match a real sender's key and
            # would just clutter the result dict.
            continue
        category = parse_category(category_raw)
        # Only accept real categories — the model is forbidden to return UNKNOWN.
        # If it does, surface it as UNKNOWN so the caller sees the failure.
        if category not in CLASSIFIABLE_CATEGORIES:
            category = Category.UNKNOWN
        out[normalised] = category
    return out


# ---------------------------------------------------------------------------
# Anthropic client construction
# ---------------------------------------------------------------------------


class MissingAPIKeyError(RuntimeError):
    """Raised when ``ANTHROPIC_API_KEY`` isn't set in the environment.

    A subclass of ``RuntimeError`` (not ``SystemExit``) so the interactive
    menu can catch it via ``except Exception`` and return to the prompt
    instead of terminating the whole process. The CLI subcommand path
    converts it to a non-zero exit at the entry point.
    """


def _require_api_key() -> str:
    """Read `ANTHROPIC_API_KEY` from env or raise `MissingAPIKeyError`."""
    key = os.environ.get(ANTHROPIC_API_KEY_ENV)
    if not key:
        raise MissingAPIKeyError(
            f"Missing required environment variable: {ANTHROPIC_API_KEY_ENV}. "
            "Export it in your shell (see CLAUDE.md § Environment Variables)."
        )
    return key


def _build_client():
    """Construct an Anthropic SDK client. Imported lazily so tests can patch."""
    import anthropic  # noqa: WPS433 — local import is intentional

    return anthropic.Anthropic(api_key=_require_api_key())


def _call_anthropic(client: Any, *, model: str, prompt: str) -> Any:
    """Single Messages-API call. Pulled out so tests can stub the boundary."""
    return client.messages.create(
        model=model,
        max_tokens=MAX_OUTPUT_TOKENS_PER_BATCH,
        temperature=0,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )


# ---------------------------------------------------------------------------
# Cache freshness
# ---------------------------------------------------------------------------


def _is_cache_fresh(classified_at: str, *, ttl_days: int = CACHE_TTL_DAYS) -> bool:
    """True if the cached row is younger than `ttl_days`."""
    try:
        ts = datetime.fromisoformat(classified_at)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts > datetime.now(timezone.utc) - timedelta(days=ttl_days)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def classify_senders(
    senders: Sequence[SenderInput],
    *,
    conn: sqlite3.Connection,
    model: str = DEFAULT_MODEL,
    client: Optional[Any] = None,
    prompt_version: str = PROMPT_VERSION,
    cache_ttl_days: int = CACHE_TTL_DAYS,
    on_progress: Optional[Callable[[str, dict[str, Any]], None]] = None,
    batch_size: int = SENDERS_PER_BATCH,
) -> dict[str, ClassificationResult]:
    """Classify each sender, using the cache where possible.

    Senders that need API classification are split into chunks of `batch_size`
    so the model's response cannot exceed `MAX_OUTPUT_TOKENS_PER_BATCH`. A
    larger inbox simply takes more calls, not a truncated response.

    Args:
        senders: senders to classify (de-duplicated by lowercased email).
        conn: open SQLite connection (see `gmailwiz.db.connect`).
        model: Claude model id.
        client: optional pre-built Anthropic client. If `None`, one is built
            on demand only when uncached senders need classifying.
        prompt_version: bump to force re-classification.
        cache_ttl_days: refresh entries older than this.
        on_progress: optional callable ``(stage: str, info: dict) -> None``
            invoked at each phase boundary so CLIs can show progress without
            this module taking a UI dependency. Stages emitted:
              * ``"cache_done"``  info=`{"hits": N, "to_classify": M}`
              * ``"batch_start"`` info=`{"index": i, "total": T, "size": k}`
              * ``"batch_done"``  info=`{"index": i, "total": T,
                                          "parsed": p, "error": str|None}`
        batch_size: senders per API call. Override only for tests.

    Returns:
        Dict keyed by lowercased email → `ClassificationResult`.
    """
    # De-dup by normalised email, preserving first-seen samples/display_name.
    by_email: dict[str, SenderInput] = {}
    for s in senders:
        key = s.normalised_email()
        if not key:
            continue
        if key not in by_email:
            by_email[key] = SenderInput(email=key, display_name=s.display_name, samples=list(s.samples))
        else:
            existing = by_email[key]
            if not existing.display_name and s.display_name:
                existing.display_name = s.display_name
            for sample in s.samples:
                if len(existing.samples) >= MAX_SAMPLES_PER_SENDER:
                    break
                existing.samples.append(sample)

    results: dict[str, ClassificationResult] = {}
    to_classify: list[SenderInput] = []

    # 1. Cache lookup. Skip rows whose stored category parses as UNKNOWN —
    #    a legacy/hand-edited bad value would otherwise persist as an UNKNOWN
    #    cache hit forever, masking a re-classification that could fix it.
    for email, sender in by_email.items():
        cached = gw_db.get_sender(conn, email)
        if (
            cached
            and cached.get("prompt_version") == prompt_version
            and cached["category"] in CLASSIFIABLE_CATEGORIES
            and _is_cache_fresh(cached["classified_at"], ttl_days=cache_ttl_days)
        ):
            results[email] = ClassificationResult(
                email=email,
                category=cached["category"],
                source="cache",
            )
        else:
            to_classify.append(sender)

    if on_progress is not None:
        on_progress("cache_done", {"hits": len(results), "to_classify": len(to_classify)})

    if not to_classify:
        return results

    # 2. Call the model in chunks so the JSON response never exceeds
    #    MAX_OUTPUT_TOKENS_PER_BATCH. Build client lazily so unit tests of
    #    pure-cache paths never touch the network.
    if client is None:
        client = _build_client()

    chunks = [
        to_classify[i : i + batch_size]
        for i in range(0, len(to_classify), batch_size)
    ]
    total_batches = len(chunks)

    for batch_index, chunk in enumerate(chunks, start=1):
        if on_progress is not None:
            on_progress(
                "batch_start",
                {"index": batch_index, "total": total_batches, "size": len(chunk)},
            )

        prompt = build_user_prompt(chunk)
        raw_text = ""
        chunk_error: Optional[str] = None

        try:
            response = _call_anthropic(client, model=model, prompt=prompt)
            raw_text = _extract_text_from_response(response)
            # If the model hit max_tokens, the JSON tail is truncated and
            # only the leading senders will round-trip — flag it so the
            # operator can shrink SENDERS_PER_BATCH or grow the cap.
            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason == "max_tokens" and on_progress is not None:
                on_progress(
                    "batch_truncated",
                    {"index": batch_index, "total": total_batches},
                )
        except Exception as exc:  # surfaced verbatim per cs.md: never swallow API errors
            chunk_error = f"{type(exc).__name__}: {exc}"

        parsed = parse_classifier_response(raw_text) if raw_text else {}

        # 3. Persist + record per-sender outcome for this chunk. Per-sender
        #    error attribution distinguishes:
        #      - chunk-wide API failure (exception)
        #      - empty / unparseable model response
        #      - sender was simply absent from an otherwise-good response
        #    so the user can see why an UNKNOWN row is UNKNOWN.
        for sender in chunk:
            email = sender.normalised_email()
            category = parsed.get(email, Category.UNKNOWN)
            sender_error: Optional[str] = None
            if category in CLASSIFIABLE_CATEGORIES:
                gw_db.upsert_sender(
                    conn,
                    email=email,
                    display_name=sender.display_name or None,
                    category=category,
                    prompt_version=prompt_version,
                    model=model,
                )
                source = "model"
            else:
                source = "unknown"
                if chunk_error is not None:
                    sender_error = chunk_error
                elif not raw_text:
                    sender_error = "empty model response"
                elif not parsed:
                    sender_error = "model response was not valid JSON for this batch"
                elif email not in parsed:
                    sender_error = "model omitted this sender from the response"
                else:
                    sender_error = "model returned an unrecognized category"
            results[email] = ClassificationResult(
                email=email,
                category=category,
                source=source,
                raw_response=raw_text or None,
                error=sender_error,
            )

        if on_progress is not None:
            on_progress(
                "batch_done",
                {
                    "index": batch_index,
                    "total": total_batches,
                    "parsed": len(parsed),
                    "error": chunk_error,
                },
            )

    return results
