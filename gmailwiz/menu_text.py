"""Menu copy lives here so it's easy to tune without touching CLI plumbing.

Phases 1 + 2 (preview/apply) expose options 1, 2, 3, 5, q. Option 4 (undo)
ships with the next Phase 2 chunk; archive equivalents arrive in Phase 3.
Placeholders are intentionally absent until those phases land.
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
        key="2",
        label="Preview a labeling run",
        summary="See what would be labeled. Choose a category. Nothing is applied yet.",
        detail=(
            "Builds a labeling plan for a category you choose. Lists every "
            "message that would receive the gmailwiz/<category> label, plus a "
            "summary of why other messages were skipped. Saves the plan to "
            "local state but does NOT modify Gmail. Apply it later with menu "
            "option 3 (or `gmailwiz label --commit --run-id <id>`). "
            "Senders that haven't been classified yet are skipped — run "
            "option 1 (Show unread report) first to populate the cache."
        ),
    ),
    MenuOption(
        key="3",
        label="Apply a previewed labeling run",
        summary="Pick a previously-previewed run by date and apply it.",
        detail=(
            "Lists labeling plans you've previewed but not yet applied. Pick "
            "one and confirm with 'yes' to apply the labels in Gmail. The "
            "gmailwiz/<category> label is created in your Gmail account if it "
            "doesn't already exist. Each message is mutated independently, so "
            "a partial failure leaves a clear audit trail."
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
