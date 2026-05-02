"""Menu copy lives here so it's easy to tune without touching CLI plumbing.

All phases shipped:
  1 — show unread report
  2 — preview labeling run
  3 — apply previewed labeling run
  4 — undo a previous run (label or archive)
  5 — re-authenticate
  6 — preview archive run
  7 — apply previewed archive run
  q — quit
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
        key="4",
        label="Undo a previous run",
        summary="Reverse a previously-applied label or archive run by date.",
        detail=(
            "Lists previously-applied label and archive runs (committed or "
            "partially-failed). Pick one and confirm with 'yes' to reverse "
            "it: a label run gets the gmailwiz/<category> label removed from "
            "every message it labeled; an archive run gets INBOX re-added to "
            "every message it archived. Operates only on messages from that "
            "specific run — manually-labeled messages and other Gmail state "
            "are untouched."
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
        key="6",
        label="Preview an archive run",
        summary="From a previous labeling run, see which messages would be archived.",
        detail=(
            "Pick a previously-applied label run (committed or "
            "partially-failed). gmailwiz fetches the current Gmail state of "
            "those messages and builds an archive plan: every message that "
            "still has INBOX would have INBOX removed (Gmail's definition of "
            "'archive' — the message stays in All Mail and is fully "
            "searchable, but disappears from your Inbox view). Nothing is "
            "applied yet. Already-archived messages and ones Gmail can't "
            "fetch are skipped and reported."
        ),
    ),
    MenuOption(
        key="7",
        label="Apply a previewed archive run",
        summary="Pick a previously-previewed archive run by date and apply it.",
        detail=(
            "Lists archive plans you've previewed but not yet applied. Pick "
            "one and confirm with 'yes' to remove INBOX from every message "
            "in that plan. The messages disappear from your Inbox view but "
            "remain in All Mail. Reversible via menu option 4."
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
