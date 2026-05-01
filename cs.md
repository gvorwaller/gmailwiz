# AI Assistant Session Guide

## Session Startup (Required)
1. Read `cs.md` (this file) — hard rules that override defaults
2. Read `CLAUDE.md` — project overview and patterns
3. Check recent devlog entries in `docs/devlog/`
4. Run `td usage --new-session` to see current tasks

---

## Core Principles

### No Assumptions
- **Never guess** when you can verify — read source code, check config files, test directly
- **Never assume API behavior** — read Gmail API / Anthropic SDK docs or test against the live API in dry-run mode
- **State uncertainty explicitly** — if you must hypothesize, say so and ask for confirmation
- **Ask when uncertain** — one question is cheaper than one wrong assumption

### No Quick Fixes
- Find root causes, not band-aids
- Implement maintainable solutions
- If a fix requires multiple rounds, slow down and trace the data flow

### Evidence-Based Debugging (MANDATORY)
When diagnosing errors, follow this methodology instead of guessing:

1. **Read the relevant source code** before forming any hypothesis
2. **Trace the data flow** — CLI args -> auth -> Gmail API call -> Anthropic classifier -> output
3. **Test each layer independently** — auth alone, list alone, classify alone
4. **Compare expected vs actual** at each boundary
5. **Never assume a cause** — verify with evidence first, then propose a fix

> "No guesses, only solid evidence, tracing the code carefully."

---

## Runtime Context

### Local CLI, Self-Use Only
- Runs on Gaylon's Mac against his personal Gmail account. No deployed instances, no other users.
- Google Cloud project stays in **OAuth Testing mode** — no app verification needed. Refresh tokens in Testing mode expire after **7 days**; re-running the auth flow regenerates them. Tolerable for self-use.
- No CI, no servers, no PM2, no Nginx, no Cloudflare. A `python -m gmailwiz` invocation on a laptop is the entire runtime.

### OAuth Credentials Are Never Committed
- `credentials.json` (OAuth client secret) and `token.json` (refresh token) are gitignored. **Never commit either.** If either ends up staged, stop and remove it before continuing.
- Scopes used: `gmail.modify`, `userinfo.email`, `openid`. Full reference on `td-650d79` comment.

---

## Project-Specific Rules

### Reversibility Ladder
gmailwiz mutates a real Gmail account. Each phase escalates blast radius — earlier phases must keep working as the read-only baseline.

1. **Phase 1 — read-only**: list + group + classify. No mutations.
2. **Phase 2 — `--label`**: apply Gmail labels. Reversible (labels can be removed). Mutations only run when explicitly requested.
3. **Phase 3 — `--archive`**: remove `INBOX` label. Reversible but tedious. Requires explicit flag, never default.

Never skip ahead. Phase 1 must be solid before Phase 2 ships, and so on.

### Dry-Run Is Default
All mutating commands default to dry-run. Only commit changes when `--commit` (or equivalent flag) is passed explicitly. Dry-run output must show the exact API calls that would have been made — message IDs, label IDs, action — not a summary.

### Data Integrity
**NEVER:**
- Fabricate or stub Gmail messages, senders, or classifications to make a demo work
- Use fallback data to mask broken code or quota errors
- Mutate the live mailbox without explicit user confirmation
- Skip the dry-run preview when adding a new mutating action

**ALWAYS:**
- Surface raw Gmail API errors verbatim — do not swallow them into generic messages
- Log every mutation (label add/remove, archive) with the message ID and prior state so manual undo is possible
- Validate inputs at the CLI boundary; trust internal code thereafter

### Anthropic API Usage
- Classifier prompts must be deterministic where possible (low temperature, fixed category list, JSON output)
- **Cache classifications by sender, not by message** — don't re-classify every message from the same address
- Batch requests where the SDK supports it; respect rate limits
- Never log full message bodies. Sender + subject + first ~200 chars of snippet is the working unit

---

## Development Workflow
- Activate the venv before any `python` or `pip` command: `source .venv/bin/activate`
- **Always `cd` back** to project root after operations
- **Use absolute paths** when possible to avoid directory confusion
- **Commits**: only commit when explicitly asked

### Verification Commands
Test/lint setup is not yet established. When code lands, the expected baseline is:
- `python -m gmailwiz --help` exits clean
- `pytest` (once added) passes with 0 warnings

Re-freeze deps after any manual install: `pip freeze > requirements.txt`

---

## State Tracking Tools
- `td` — task management CLI (focused issue lives in this repo's `.todos/`)
- `td-650d79` — build Gmail triage CLI (P1, currently focused)
- `td-da165c` — Google Cloud OAuth client setup (P1, manual prereq; blocks `td-650d79`). See `docs/devlog/2026-04-28-manual-items-todo.md`.

---

## Historical Failures (Learn From These)
*(Empty — repo initialized 2026-04-28. Add entries here as real failures happen so future sessions don't repeat them.)*

### Key Principle
> Assumptions are the enemy. Read the code. Read the API docs. Test the layer. Only then diagnose.
