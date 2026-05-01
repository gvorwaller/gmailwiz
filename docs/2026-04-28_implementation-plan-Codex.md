# gmailwiz — Implementation Plan (Codex Revision)

## Context

Gaylon's personal Gmail inbox is the problem. Years of newsletters, transactional mail, and sporadic personal threads have piled up to the point where SaneBox-style triage is appealing but the SaaS pricing isn't justified for a single user. gmailwiz is a Python CLI that reads the inbox via the Gmail API, asks Claude to categorize senders, and eventually labels and archives in batches, all from the laptop, all reversible, all dry-run by default.

The repo currently contains documentation, a `.venv` with key dependencies installed, and manual setup notes for Google Cloud OAuth. No application code exists yet. This plan covers the first three phases and tightens the mutation model so Claude Code has a safer implementation target.

This document is intentionally more prescriptive than the original plan where reversibility and state integrity are concerned. The goal is to reduce ambiguity before implementation begins.

---

## Prerequisites Status — ✅ Complete (2026-04-28)

**Coding agents: do not redo any of this. Pick up at "Phase 1 — Read-Only Report".**

| Prerequisite | Status | Where it lives |
|---|---|---|
| GCP project + Gmail API enabled | ✅ | Existing project `gaylonphotos` (re-used; not a new `gmailwiz` project) |
| OAuth consent screen | ✅ | External / Testing / test user `gaylon@vorwaller.net` |
| OAuth Desktop client | ✅ | Client name `gmailwiz CLI` inside `gaylonphotos` |
| `credentials.json` on disk | ✅ | `~/gmailwiz/credentials.json` (gitignored) — `installed`-shape, `redirect_uris=['http://localhost']`, `project_id=gaylonphotos` |
| `ANTHROPIC_API_KEY` env var | ✅ | Exported from `~/.zshrc`, length 108, prefix `sk-ant-api03...` |

Full details and rationale (including why we stay in OAuth **Testing** mode and accept the 7-day refresh-token expiry rather than going through Google's CASA verification): `docs/devlog/2026-04-28-manual-items-todo.md` and `docs/devlog/2026-04-28.md`.

`auth.py` should call `InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)` against the file at the repo root. No project ID needs to be hardcoded — it is in `credentials.json`.

---

## Approach

- **Language / runtime**: Python 3.14, single-process CLI, run from terminal
- **CLI framework**: `click`
- **Storage**: SQLite from day one via stdlib `sqlite3`
- **Auth**: `google-auth-oauthlib` `InstalledAppFlow`, scopes `gmail.modify` + `userinfo.email` + `openid`
- **Classifier**: Anthropic SDK (`anthropic`), deterministic prompt, fixed category list, strict JSON output
- **Execution model**: all mutating commands create an explicit run plan before any write occurs
- **Reversibility ladder enforced**: Phase 1 read-only -> Phase 2 label -> Phase 3 archive
- **Primary user interface is an interactive menu**, not raw subcommands (see "User-Facing Entry Point" below)

---

## User-Facing Entry Point — Interactive Menu (REQUIRED)

**This is the primary way the user invokes gmailwiz.** Subcommands like `report`, `label`, `archive`, `undo` exist and remain callable directly, but they are not what the user types day-to-day. The user does not want to memorize flags or run-IDs.

### Behavior

Running `python -m gmailwiz` with **no arguments** drops into an interactive menu loop:

```
gmailwiz — Gmail inbox triage

What do you want to do?

  1. Show unread report             — Group unread mail by sender, classified by Claude. No changes to Gmail.
  2. Preview a labeling run         — See what would be labeled. Choose a category. Nothing is applied yet.
  3. Apply a previewed labeling run — Pick a previously-previewed run by date and apply it.
  4. Undo a previous run            — Reverse a labeling or archive run by date.
  5. Re-authenticate with Google    — Refresh the OAuth token (needed every 7 days).
  q. Quit

>
```

Each numbered item is gated on the phase it belongs to. Phase 1 ships with options 1, 5, q only. Phase 2 unlocks 2, 3, 4 (label/undo). Phase 3 unlocks archive equivalents. Items not yet available are not shown.

### Selection flow

When the user picks an option:

1. **Print a one-paragraph plain-English description of what is about to happen**, including which inputs (e.g. category filter) will be needed. This is the same text shown in the menu, expanded — not a wall of help.
2. **Prompt for any required inputs** (e.g. category, limit). Defaults shown in brackets.
3. For **mutating actions** (commit, undo) — show the proposed operations and require an explicit `yes` typed at a confirmation prompt before executing. `--commit` semantics from the underlying subcommand still apply; the menu just routes to them.
4. Run the action.
5. Display results. If output is long, paginate or pipe through `less` (cross-platform fallback: print all and let terminal scroll).
6. Return to the menu.

### Selecting prior runs without remembering IDs

Options that need a `run-id` (apply previewed run, undo) must **list recent runs from the `runs` table** in human-readable form and let the user pick by number:

```
Which previewed labeling run do you want to apply?
  1. 2026-04-30 14:57  — promotional, 73 messages, planned
  2. 2026-04-30 12:10  — newsletter, 41 messages, planned
  q. Cancel

>
```

The user never types or sees a raw `run_id` string.

### Implementation

- Default click subcommand: invoking the click group with no args calls a new `cli.menu()` function. (In click: set `invoke_without_command=True` on the group and check `ctx.invoked_subcommand is None`.)
- Use stdlib `input()` for prompts; do **not** add a TUI dependency (`questionary`, `prompt_toolkit`, etc.) in Phase 1. If readability becomes an issue later, revisit.
- Each menu option dispatches to the same Python functions the subcommands call — do not shell out to `python -m gmailwiz <sub>`. The subcommand entry points and the menu both call shared helpers.
- Every prompt must accept `q` / `quit` / Ctrl-C cleanly without orphaning a partially-built run.
- Helpful descriptions (the text in the menu and the expanded text shown after selection) live in `gmailwiz/menu_text.py` (or as constants near the menu function) — not buried in click `help=` strings — so they're easy to revise without touching argument parsing.

### Phase 1 menu deliverable

The Phase 1 ship criteria below (see "Phase 1 Ship Criteria") include: running `python -m gmailwiz` with no args opens this menu, option 1 produces a report, option 5 re-authenticates, q quits. That is the day-one user experience.

---

## Decisions Status

**Confirmed by user 2026-04-28:**

1. **Scope**: lean fun project, easy to extend later
2. **Storage**: SQLite from day one at `data/db/state.db` (inside the repo, gitignored)
3. **Schedule**: interactive only for now

These are now load-bearing for the rest of the plan. The coding agent should implement against them without re-asking.

---

## State And Config Layout

Keep operational state out of the repo root except for `credentials.json`, which is only there because Google's downloaded desktop-app credential file naturally starts there during setup.

- `data/db/state.db` — SQLite database (inside the repo, gitignored)
- `data/token.json` — OAuth token cache (inside the repo, gitignored)
- `credentials.json` — OAuth client secret at the repo root, supplied manually, gitignored

Environment configuration — **decision made (2026-04-28): shell env, no `.env` loader.**

- `ANTHROPIC_API_KEY` is read directly from `os.environ` (or `os.getenv`). No `python-dotenv`, no `.env` file in the repo.
- The key is already exported from `~/.zshrc` (verified). If a future deploy target (e.g. cron, systemd) needs it, that target is responsible for exporting it before invoking `python -m gmailwiz`.
- Implementations that find no key should `sys.exit` with a clear message naming the variable, not silently fall back.

---

## Module Layout

```text
gmailwiz/
├── __init__.py
├── __main__.py          # entry -> cli.main()
├── cli.py               # click command group and subcommands
├── auth.py              # OAuth flow, token persistence, identity check
├── gmail_client.py      # Gmail API wrapper
├── classifier.py        # Anthropic classification + cache integration
├── db.py                # sqlite schema + data access
├── categories.py        # Category enum + label mapping
└── planning.py          # build persisted execution plans for mutating commands
tests/
├── test_classifier.py
├── test_db.py
├── test_cli.py
└── test_planning.py
```

`pyproject.toml` is optional for Phase 1. It is not required just to make `python -m gmailwiz` work if `__main__.py` exists.

---

## Data Model

Use SQLite for both classifier cache and mutation bookkeeping.

### `senders`

- `email TEXT PRIMARY KEY`
- `display_name TEXT`
- `category TEXT NOT NULL`
- `classified_at TEXT NOT NULL`
- `prompt_version TEXT NOT NULL`
- `model TEXT NOT NULL`

### `runs`

One row per dry-run or commit plan.

- `id TEXT PRIMARY KEY`
- `phase TEXT NOT NULL` (`report`, `label`, `archive`, `undo`)
- `created_at TEXT NOT NULL`
- `query TEXT`
- `limit_count INTEGER`
- `category_filter TEXT`
- `dry_run INTEGER NOT NULL`
- `status TEXT NOT NULL` (`planned`, `committed`, `partially_failed`, `undone`, `failed`)

### `audit_log`

One row per attempted Gmail mutation.

- `id INTEGER PRIMARY KEY`
- `run_id TEXT NOT NULL`
- `ts TEXT NOT NULL`
- `action TEXT NOT NULL`
- `message_id TEXT NOT NULL`
- `thread_id TEXT`
- `sender_email TEXT`
- `before_label_ids TEXT NOT NULL`
- `after_label_ids TEXT NOT NULL`
- `status TEXT NOT NULL` (`planned`, `applied`, `failed`, `reverted`)
- `error TEXT`

### `app_state`

- `key TEXT PRIMARY KEY`
- `value TEXT NOT NULL`

Store label arrays as JSON strings. The important part is not the encoding choice but that both pre- and post-mutation state are recorded.

---

## Run Model

This is the main correction to the original plan.

Mutating commands must not compute one candidate set for dry-run and a different candidate set for commit. Instead:

1. Build an explicit run plan from the current mailbox state
2. Persist that plan with a generated `run_id`
3. Print the exact Gmail operations that would run
4. Only execute that exact plan when `--commit` is provided

This keeps dry-run and commit aligned and makes `undo` targetable.

Primary undo target should be `--run-id`, not `--since`. Time-based undo may exist as a convenience, but it should not be the primary safety mechanism.

---

## Phase 1 — Read-Only Report

### Deliverables

1. `auth.py`
   - `get_credentials()`: load `data/token.json` if present and valid
   - Otherwise run `InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)` with `access_type='offline'`, `prompt='consent'`, and `include_granted_scopes=True`
   - Refresh expired credentials
   - `get_authenticated_email()` for smoke-test identity output

2. `gmail_client.py`
   - `list_unread_message_ids(max_results)`
   - `get_message_metadata(ids)`
   - Return lightweight dicts with `id`, `thread_id`, `sender_email`, `sender_name`, `subject`, `snippet`, `internal_date`, `label_ids`

3. `db.py`
   - Initialize the schema above
   - Sender cache read/write helpers
   - Run creation and audit-log helpers

4. `classifier.py`
   - `classify_senders(senders: list[Sender]) -> dict[email, Category]`
   - Only classify uncached or stale senders
   - Input payload limited to sender identity plus a few representative subjects and short snippets
   - Strict JSON response with allowed categories only
   - Unknown or malformed outputs must be surfaced and handled explicitly, not silently coerced

5. `cli.py`
   - `auth` subcommand
   - `report` subcommand
   - `report --json`
   - Group by sender email, show category, unread count, newest subject

### Phase 1 Ship Criteria

- `python -m gmailwiz` (no args) opens the interactive menu with options 1 (report), 5 (re-auth), q (quit) only
- Menu option 1 produces a coherent grouped report and returns to the menu when done
- Menu option 5 re-runs the OAuth flow and prints the authenticated email
- Direct subcommands `python -m gmailwiz auth` and `python -m gmailwiz report` also work for scripting
- No Gmail mutations are possible from Phase 1 code paths
- Sender parsing and classifier failure cases are visible and test-covered

### Phase 1 Scope Control

Keep this phase minimal. Do not make Google batch request support or packaging metadata a prerequisite unless they solve a real measured problem during implementation.

---

## Phase 2 — Label

### Deliverables

1. `categories.py`
   - Map category enum values to Gmail label names such as `gmailwiz/promotional`

2. `gmail_client.py`
   - `ensure_labels_exist(label_names)`
   - `modify_labels(message_id, add_label_ids, remove_label_ids)`
   - Raw Gmail API errors should surface verbatim

3. `planning.py`
   - Build a label plan from current mailbox state
   - Persist `runs` row plus one `audit_log` row per proposed message mutation with `status='planned'`
   - Senders classified as `unknown` (or whose classification failed parsing) are excluded from any label or archive plan; they remain visible in `report` output but never produce mutations

4. `cli.py`
   - `label` subcommand
   - Default is dry-run
   - Dry-run output must show exact intended operations: message ID, sender, labels before, labels after
   - `--commit` executes the persisted plan for that run
   - `--category <name>` to scope the plan
   - `--run-id <id>` to commit an already-previewed plan if desired

5. `undo`
   - Primary path: `undo --run-id <id>`
   - Reverse the recorded pre/post label state, not a recomputed mailbox query
   - Also dry-run by default

### Phase 2 Ship Criteria

- Dry-run output matches a hand-verified sample
- Commit mutates exactly the previewed message set
- Partial failures are recorded clearly
- `undo --run-id` removes applied labels cleanly
- Interactive menu now exposes options 2 (preview labeling), 3 (apply previewed run), 4 (undo a run), with the prior-run picker described in "User-Facing Entry Point"

---

## Phase 3 — Archive

### Archive Candidate Rule

Do not define archive candidates as merely "messages that already have a gmailwiz category label." That is too broad and can catch stale or manually labeled mail.

Archive should target one of these precise scopes:

- Preferred: messages attached to a specific prior labeling `run_id`
- Acceptable fallback: messages currently in `INBOX` that match the active query and category filter and were included in the just-built archive plan

### Deliverables

1. `cli.py`
   - `archive` subcommand
   - Default is dry-run
   - `--commit` required for writes
   - Removes `INBOX` only for messages in the persisted archive plan
   - `--category <name>`
   - `--max <n>`
   - Prefer `--from-run-id <id>` or equivalent to archive from a reviewed labeling run

2. `planning.py`
   - Archive plan builder using explicit message IDs, never only a loose label query at commit time

3. `undo`
   - Re-add `INBOX` using the recorded `before_label_ids` and `after_label_ids`

### Phase 3 Ship Criteria

- Archive dry-run shows the exact expected message set
- Commit only archives the previewed set
- Undo restores `INBOX` for the affected messages
- Interactive menu now exposes archive equivalents (preview, apply, undo) using the same prior-run-picker pattern

---

## Verification Plan

### Manual smoke tests

- `source .venv/bin/activate`
- `python -m gmailwiz auth`
- `python -m gmailwiz report --limit 50`
- `python -m gmailwiz label --limit 50`
- `python -m gmailwiz label --commit --run-id <previewed-run-id>`
- `python -m gmailwiz undo --run-id <label-run-id>`
- `python -m gmailwiz archive --from-run-id <label-run-id>`
- `python -m gmailwiz archive --commit --run-id <archive-previewed-run-id>`
- `python -m gmailwiz undo --run-id <archive-run-id>`

### Automated tests

- `test_classifier.py`
  - prompt shape
  - valid JSON parsing
  - malformed JSON handling
  - unknown category handling

- `test_db.py`
  - schema creation
  - sender upsert
  - run creation
  - audit-log persistence
  - reverse application using stored before/after label state

- `test_planning.py`
  - dry-run plan and commit plan identity
  - archive scoping by run ID
  - no recomputation drift between preview and commit

- `test_cli.py`
  - click wiring
  - dry-run output contains exact operations
  - `undo --run-id` behavior

---

## Implementation Notes For Claude Code

- Do not silently resolve repo-level open decisions that docs say are still pending
- Do not recompute candidate message sets at commit time for mutating commands
- Do not make `undo --since` the primary rollback path
- Do not broaden archive scope beyond a reviewed plan
- Do not add packaging or batching complexity unless it is justified by actual Phase 1 implementation pain

The core architecture remains good: phased delivery, dry-run first, sender-level caching, and SQLite are all sensible. The main requirement is to encode mailbox mutation safety directly into the implementation plan so the coding agent has less room to improvise.
