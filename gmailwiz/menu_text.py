"""Menu copy lives here so it's easy to tune without touching CLI plumbing.

Phase 1 only exposes options 1, 5, and q. Phases 2 and 3 will add 2/3/4 (label,
apply, undo) and the archive equivalents — placeholders are intentionally
absent from `MENU_OPTIONS` until those phases land.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MenuOption:
    key: str
    label: str
    summary: str
    detail: str


MENU_HEADER = "gmailwiz — Gmail inbox triage"

MENU_PROMPT = "What do you want to do?"

# Order = display order in the menu.
MENU_OPTIONS: tuple[MenuOption, ...] = (
    MenuOption(
        key="1",
        label="Show unread report",
        summary="Group unread mail by sender, classified by Claude. No changes to Gmail.",
        detail=(
            "Fetches your unread inbox, groups messages by sender, asks Claude to "
            "categorize each sender (promotional, transactional, newsletter, "
            "personal), and prints a grouped report. Nothing in Gmail is "
            "modified. You'll be asked how many messages to scan."
        ),
    ),
    MenuOption(
        key="5",
        label="Re-authenticate with Google",
        summary="Refresh the OAuth token (needed every 7 days).",
        detail=(
            "Opens a browser window so you can sign in to Google again and "
            "regenerate the OAuth refresh token. Required roughly every 7 days "
            "because the OAuth project is in Google's Testing mode."
        ),
    ),
    MenuOption(
        key="q",
        label="Quit",
        summary="Exit the menu.",
        detail="Exits gmailwiz without doing anything.",
    ),
)


def render_menu() -> str:
    """Render the full menu text shown at each loop iteration."""
    lines = [MENU_HEADER, "", MENU_PROMPT, ""]
    for opt in MENU_OPTIONS:
        # Two-column-ish layout that matches the README sample.
        lines.append(f"  {opt.key}. {opt.label:<32} — {opt.summary}")
    lines.append("")
    return "\n".join(lines)


def find_option(key: str) -> MenuOption | None:
    """Look up a menu option by key (case-insensitive)."""
    target = (key or "").strip().lower()
    if not target:
        return None
    for opt in MENU_OPTIONS:
        if opt.key.lower() == target:
            return opt
    return None
