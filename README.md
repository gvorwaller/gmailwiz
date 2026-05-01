# gmailwiz

A terminal command for AI-assisted Gmail inbox triage. Runs on your laptop, against your own Gmail, free. Like SaneBox if SaneBox were a single Python script you owned.

Self-use only — built for a single user (Gaylon), no multi-account support, no SaaS, no daemon.

## What it does

You sit down on a Sunday with 600 unread messages. You run one command and see:

```
SENDER                        UNREAD  CATEGORY        NEWEST SUBJECT
news@nytimes.com                 47   newsletter      The Morning: ...
do-not-reply@chase.com           23   transactional   Your statement is ready
deals@homedepot.com              19   promotional     Spring savings up to 40%
linkedin@e.linkedin.com          14   promotional     People you may know
mom@vorwaller.net                 3   personal        Re: Easter plans
calendly@calendly.com             2   transactional   New event: ...
```

Now you can tell at a glance that 80% of your unread mail is newsletters and promos you don't care about, and three are actually personal. From there you can label and archive in bulk.

## How you'll use it

**The primary interface is an interactive menu.** Just run:

```
python -m gmailwiz
```

You get a numbered menu with plain-English descriptions of each action. Pick a number, follow the prompts. No flags or run-IDs to memorize. Subcommands below still work directly for scripting.

**Phase 1 — read-only (the report above).** Look, decide, manually handle the personal ones in Gmail.

```
python -m gmailwiz report
```

**Phase 2 — bulk label.** Apply Gmail labels (e.g. `gmailwiz/promotional`) in batches:

```
python -m gmailwiz label --category promotional
```

This *previews* what it would do — no changes yet:

```
WOULD label 47 messages from news@nytimes.com  -> gmailwiz/newsletter
WOULD label 19 messages from deals@homedepot.com -> gmailwiz/promotional
... 73 messages total. Re-run with --commit to apply.
```

If it looks right, re-run with `--commit`. Made a mistake? `python -m gmailwiz undo --run-id <id>` reverses exactly that batch — not everything-since-Tuesday, just what you just applied.

**Phase 3 — bulk archive.** Same pattern, but removes messages from `INBOX`. Messages stay in Gmail, just out of view. Reversible.

## Safety, in one line

Every command shows what it would do **before** doing it. Changes need `--commit`. Anything that mutates can be `undo`'d. Nothing happens silently.

## What it does NOT do

- Not a daemon, cron job, or background process — does nothing unless you run it.
- Never sends mail. Never auto-replies.
- Doesn't read full message bodies into Claude — sends sender + subject + a short snippet, no more.
- Doesn't delete anything. "Archive" in Gmail just means "out of inbox", recoverable forever.

## Setup

See `docs/devlog/2026-04-28-manual-items-todo.md` (manual prerequisites — already complete) and `docs/2026-04-28_implementation-plan-Codex.md` (the prescriptive coding plan).

In short: the project shares Google's `gaylonphotos` GCP OAuth project with the `giftlist` repo (re-used to skip redundant setup), reads `~/gmailwiz/credentials.json` for the OAuth client secret, and expects `ANTHROPIC_API_KEY` in the shell environment.

## State

Stored inside the repo at `data/` (gitignored — never committed):
- `data/token.json` — OAuth token cache (refreshed automatically; re-auth required every 7 days because the OAuth project stays in Google's "Testing" mode)
- `data/db/state.db` — SQLite for sender classification cache, run history, and audit log (so `undo` can target a specific batch)
