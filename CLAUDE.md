# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **DO NOT modify this file without explicitly asking the user first.**

## Session Startup (Do These First, In Order)

1. **Read `cs.md`** — hard rules on debugging methodology and project-specific constraints. Non-negotiable.
2. **Check recent devlog** — review the last few entries in `docs/devlog/` for recent decisions and work.
3. **Task management** — run `td usage --new-session` to see current work (after reading `cs.md`).

## Project Overview

Python CLI for AI-assisted Gmail inbox triage. **Self-use only** — runs on Gaylon's Mac against his personal Gmail account. Uses the Gmail API (`gmail.modify` scope) for reads and label/archive operations, and the Anthropic API to classify senders into categories (promotional, transactional, newsletter, personal).

Open scope question (not yet decided): fun project vs. SaneBox replacement. See `td-650d79`.

### Phased Delivery
1. **Phase 1 — read-only**: report grouping unread mail by sender + AI category. No mutations.
2. **Phase 2 — `--label`**: apply Gmail labels. Reversible.
3. **Phase 3 — `--archive`**: remove from inbox. Reversible but tedious.

Dry-run mode exists from day one. See `cs.md` § "Reversibility Ladder" and § "Dry-Run Is Default" for the rules.

## Commands

```bash
# Activate venv before any python/pip command
source .venv/bin/activate

# Install / update deps
pip install -r requirements.txt

# Re-freeze deps after a manual install
pip freeze > requirements.txt

# Run the CLI (entry point not yet implemented — TBD as Phase 1 lands)
python -m gmailwiz --help

# When user requests CC session status, use this script.
# Reads Claude Code session logs — concise timeline of recent activity.
# Useful for cross-session awareness across repos.
cc-status --project ~/gmailwiz                # last 30 min of this project's CC activity
cc-status --all-recent                        # last 15 min across all projects
cc-status --list                              # show all projects
cc-status --minutes 60 --project ~/giftlist   # last hour of giftlist activity
cc-status --sessions                          # list all named relay sessions (CC1, CC2, etc.)
cc-status --session CC2                       # last 30 min of CC2's activity
cc-status --session CC2 --minutes 60          # last hour of CC2
cc-status --lines 20                          # last 20 entries

# Note: --minutes is relative to the session's last activity, not current time. Session names are case-insensitive.
```

## Architecture

### Module Layout (planned)
```
gmailwiz/
├── __init__.py
├── __main__.py          # python -m gmailwiz
├── cli.py               # click entry; subcommands: auth, report, label, archive, undo
├── auth.py              # InstalledAppFlow, token persistence, refresh
├── gmail_client.py      # thin wrapper over googleapiclient: list, get, modify_labels
├── classifier.py        # Anthropic-based sender classification + cache
├── planning.py          # build & persist execution plans for mutating commands
├── db.py                # sqlite schema + accessors
└── categories.py        # Category enum + Gmail label-name mapping
tests/                   # pytest (planning, db, classifier, cli)
```

This is a target, not the current state. The actual layout emerges as `td-650d79` progresses. The plan of record is `docs/2026-04-28_implementation-plan-Codex.md`.

### OAuth Flow
- `google-auth-oauthlib` `InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)` handles the localhost callback automatically.
- Persist the refreshable token to `token.json` (gitignored).
- Subsequent runs: `Credentials.from_authorized_user_file('token.json', SCOPES)`; `creds.refresh(Request())` when expired.
- Critical OAuth flags (translated from giftlist's TS implementation):
  - `access_type='offline'` — required to get a refresh_token
  - `prompt='consent'` — forces consent screen so the refresh_token is returned every time
  - `include_granted_scopes=True`
- Without `prompt='consent'`, the second connect only returns an access_token and the long-lived grant is lost.

### Scopes
- `https://www.googleapis.com/auth/gmail.modify` — read + label + trash (covers all three phases)
- `https://www.googleapis.com/auth/userinfo.email` — used as a smoke test to confirm the authenticated identity
- `openid`

### Classifier
- Anthropic SDK (`anthropic` package). Default to a current Claude model balancing cost and quality (decide at implementation time, not pinned here).
- Prompt is deterministic-ish: low temperature, fixed category list, JSON output.
- **Sender-level cache**: classify the sender, not each individual message. Cache lives on disk (flat JSON or SQLite — decide when implementing). Re-classify only if the cached label is older than N days or the user explicitly invalidates.
- Never log full message bodies. Sender + subject + first ~200 chars of snippet is the working unit.

### Mutation Bookkeeping
Every label add/remove and archive writes a record (message ID, action, prior state, timestamp) to the `audit_log` table in `data/db/state.db` so manual undo is possible without scrolling Gmail.

## Environment Variables

Required in the shell environment (no `.env` loader — keep it simple, single-user CLI):
- `ANTHROPIC_API_KEY` — for the classifier

Files:
- `credentials.json` — OAuth client secret from Google Cloud Console (Desktop app type). Lives at repo root (where Google's download lands). Gitignored. Setup steps in `docs/devlog/2026-04-28-manual-items-todo.md`.
- `data/token.json` — generated on first auth run; refresh token persists here. **Inside the repo, gitignored via `data/`.**
- `data/db/state.db` — SQLite (sender classification cache, runs, audit log). **Inside the repo, gitignored via `data/`.**

## Confirmed Decisions (2026-04-28)

- **Scope**: lean fun project, easy to extend later. No rules engine, no calendar integration, no scheduling in Phases 1-3. Revisit after Phase 3 ships and you've used it for a few weeks.
- **Storage**: SQLite from day one at `data/db/state.db` (inside the repo, gitignored). stdlib `sqlite3`, no ORM.
- **Schedule**: interactive only. The 7-day OAuth Testing-mode token expiry makes unattended runs fragile; revisit after Phase 3 stabilizes.
