# gmailwiz

AI-assisted Gmail inbox triage. Classifies senders with Claude, then labels and archives in bulk, reversibly. Built as a single-user tool — runs locally against your own Gmail, free, no SaaS.

Self-use project: scope, defaults, and docs assume one user (Gaylon). The pieces are reusable but nothing's been hardened for general distribution.

## What it does

You sit down on a Sunday with 600 unread messages. You run one command and see:

```
SENDER                        UNREAD  CATEGORY        NEWEST SUBJECT
news@nytimes.com                 47   newsletter      The Morning: ...
do-not-reply@chase.com           23   transactional   Your statement is ready
deals@homedepot.com              19   promotional     Spring savings up to 40%
linkedin@e.linkedin.com          14   promotional     People you may know
mom@example.net                   3   personal        Re: Easter plans
calendly@calendly.com             2   transactional   New event: ...
```

Now you can tell at a glance that 80% of your unread mail is newsletters and promos you don't care about, and three are actually personal. From there you can label and archive in bulk — manually approving each phase, or in one shot if you've already pre-triaged what matters.

## Quick start

```bash
git clone https://github.com/gvorwaller/gmailwiz.git
cd gmailwiz
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...           # also add to your shell rc
# Place credentials.json (OAuth client secret from Google Cloud Console
# Desktop app) at the repo root. Setup steps:
#   docs/devlog/2026-04-28-manual-items-todo.md
python -m gmailwiz                            # interactive menu
```

First run opens a browser for OAuth consent and persists `data/token.json`. Subsequent runs are non-interactive until the token expires (~7 days; see Token rotation below).

## Interactive menu

`python -m gmailwiz` (or the `scripts/gmailwiz` wrapper) opens a numbered menu:

```
1. Show unread report                — Group unread mail by sender, classified by Claude.
2. Preview a labeling run            — See what would be labeled.
3. Apply a previewed labeling run    — Pick by date and apply.
4. Undo a previous run               — Reverse a label or archive run.
5. Re-authenticate with Google       — Refresh the OAuth token (weekly).
6. Preview an archive run            — From a previous label run.
7. Apply a previewed archive run     — Pick by date and apply.
8. Run full cycle                    — Report → label all 4 → archive all 4. One shot.
q. Quit
```

Menu options are the day-to-day UX. Pick a number, follow the prompts. No flags or run-IDs to memorize. Phases 1–3 (options 1/2/3/6/7) are step-by-step; option 8 chains everything for when you've already pre-triaged manually and want a clean sweep.

## Subcommands

Each menu option has a CLI counterpart for scripting. All accept `--json` for machine-readable output.

```bash
python -m gmailwiz auth                              # OAuth + print authenticated email
python -m gmailwiz report --limit 200                # read-only sender report
python -m gmailwiz label --category promotional      # preview a label plan
python -m gmailwiz label --category promotional --commit --run-id <id>
python -m gmailwiz archive --source-run-id <label-run-id>          # preview archive
python -m gmailwiz archive --commit --run-id <archive-run-id>
python -m gmailwiz undo --run-id <id>                # reverse any label or archive run
python -m gmailwiz run --limit 1000                  # full cycle, non-interactive
```

### `run` — the unattended full-cycle command

`python -m gmailwiz run` is the non-interactive equivalent of menu option 8. Same underlying code path as the M2 trigger service below.

```bash
python -m gmailwiz run --limit 1000 --json
```

Captures the inbox once (a single message-ID snapshot), then for each of the four categories: classify → label → archive, all driven from that fixed snapshot. The four phases cannot drift against each other mid-run.

Flags:
- `--limit N` — max unread messages to scan (default 1000)
- `--no-archive` — label all four categories but skip the archive phase
- `--headless` — fail with `auth_required` instead of opening the browser if the token is expired (used by the M2 trigger)
- `--json` — emit a structured result instead of the human summary

Exit codes: `0` success, `2` bad input / missing API key / auth_required, `4` partial failure, `5` total failure.

## Unattended runs (M2 trigger service)

For triggering one-pass runs from anywhere (e.g. Drafts on iOS), gmailwiz includes a FastAPI service that runs on a second machine. Drafts → Cloudflare Tunnel → that machine → `oneshot.run_one_pass` → Telegram notification.

Endpoints:

```
GET  /health              { "status": "ok" }                          — pure liveness
GET  /ready               check token / api key / headless auth        — preflight
POST /run                 Bearer auth; 202 + job_id; 409 if in flight  — trigger one-pass
GET  /jobs/{job_id}       full job state + parsed OneShotResult        — status / forensics
```

The service runs under launchd, sources `~/gmailwiz/.env` for secrets (`GMAILWIZ_TRIGGER_TOKEN`, `ANTHROPIC_API_KEY`, optional Telegram tokens), and listens on `127.0.0.1:8788`. A Cloudflare Tunnel exposes it publicly as `https://gmailwiz.gaylon.photos/`.

The Drafts JS action at `scripts/drafts_trigger_gmailwiz.js` reads a Bearer credential from the Drafts keychain and POSTs `/run`. An empty draft → default `limit=1000`. Type a number → that limit. Type `no-archive` → label-only.

Full setup walkthrough: [docs/deploy-m2.md](docs/deploy-m2.md).

## Deploy workflow

`scripts/deploy.sh` (run from M4) ships local commits to M2:

```bash
git push origin main                          # required; deploy refuses unpushed HEADs
scripts/deploy.sh
```

The script refuses on a dirty working tree or when local HEAD ≠ origin/main exactly, then SSHes to M2, runs `git pull --ff-only`, installs dep changes if `requirements.txt` moved, kicks the launchd unit, and verifies `/health` + `/ready` over the public hostname before exiting.

## Safety

- Every command shows what it would do **before** doing it. Mutations need an explicit `--commit` (or a `yes` at the prompt).
- Every batch is reversible. `undo --run-id <id>` reverses exactly that batch — not everything since Tuesday.
- Nothing happens silently. Each label add and archive writes an `audit_log` row recording the message id, action, prior label set, and timestamp.
- One-pass mode demotes to `partial_failure` (not `success`) when the snapshot phase lost any messages to fetch errors, unparseable senders, or UNKNOWN classifications. Unattended runs never claim success while having silently dropped mail.

## Architecture (the load-bearing bits)

- **Snapshot once, mutate from snapshot.** `oneshot.run_one_pass` calls `list_unread_message_ids` + `get_message_metadata` exactly once per run, then feeds the same message set to all four category phases. Pre-snapshot designs would have category #2 see an inbox already mutated by category #1's archive — that drift is what made unattended one-pass unsafe.
- **Sender-level classification cache.** Claude classifies the sender, not each message. Cached in `senders` table for 30 days; re-classified only when the cache entry is older or the prompt version changes.
- **Headless auth boundary.** `get_credentials(interactive=False)` raises `AuthRequired` rather than trying to launch the OAuth browser flow on a headless box. The trigger service catches this and surfaces `auth_required` via Telegram instead of hanging.
- **Thread-affine SQLite, owned by worker.** The FastAPI handler never shares its `sqlite3.Connection` with the worker thread; the worker opens its own connections to both `state.db` and `trigger_jobs.db`.
- **Audit log is the source of truth.** Run-row status is derivable from audit_log entries; an `heal_run_status_from_audit_log` helper repairs runs whose `update_run_status` write was interrupted mid-Gmail-mutation.

## State

All state lives inside the repo at `data/` (gitignored):

- `data/token.json` — OAuth refresh token (regenerated weekly; see Token rotation)
- `data/db/state.db` — sender classifications, run history, audit log (every label add / archive recorded for undo)
- `data/db/trigger_jobs.db` — only on the M2 box; one row per `POST /run`

## Token rotation

Google's "Testing"-mode OAuth refresh tokens expire after 7 days. Weekly:

```bash
python -m gmailwiz           # → menu option 5 → confirm browser
scp data/token.json Mprd:~/gmailwiz/data/token.json    # if running the M2 trigger
```

The trigger service's `/ready` endpoint returns 503 with `headless_auth.ok=false` when the token is expired/non-refreshable, and any Drafts-triggered run will land as `auth_required` in `/jobs/{id}` with a Telegram nudge.

## Testing

```bash
.venv/bin/python -m pytest tests/                     # 256 tests
.venv/bin/python -m pytest tests/test_oneshot.py -v   # snapshot + auth invariants
.venv/bin/python -m pytest tests/test_serve.py -v     # FastAPI contract
```

Core invariants verified by tests: snapshot fetched at most once per run; expired token in headless mode returns `auth_required` without invoking the browser flow; worker thread opens its own DB connections; `?token=` query is rejected even with a valid Bearer; report-phase shortfall (fetch failures / unparseable senders / classification UNKNOWNs) demotes the rollup to `partial_failure`.

## Repo layout

```
gmailwiz/
├── auth.py              # OAuth flow + AuthRequired (headless boundary)
├── categories.py        # Category enum + Gmail label name mapping
├── classifier.py        # Anthropic sender classifier + cache
├── cli.py               # click entry; menu + subcommands
├── db.py                # SQLite schema + accessors (state.db)
├── gmail_client.py      # thin googleapiclient wrapper
├── menu_text.py         # menu copy
├── oneshot.py           # run_one_pass orchestrator (single snapshot)
├── planning.py          # build_*_plan + apply_*_plan + heal_run_status
├── serve.py             # FastAPI trigger service (M2 unattended trigger)
├── serve_state.py       # jobs table (trigger_jobs.db)
└── telegram.py          # best-effort completion notifications

scripts/
├── gmailwiz                          # top-level wrapper (use this, not `python -m`)
├── deploy.sh                         # M4 → M2 deploy + verify
├── serve.sh                          # uvicorn wrapper for launchd
├── backup-sqlite.sh                  # CCC preflight snapshot
├── drafts_trigger_gmailwiz.js        # Drafts action
└── com.gmailwiz.trigger.plist.template

tests/                                # 256 tests, pytest
docs/                                 # devlog + deploy-m2.md
```

## What it does NOT do

- Not a daemon, cron job, or background process (the M2 trigger service is event-driven via Drafts).
- Never sends mail. Never auto-replies.
- Doesn't read full message bodies into Claude — sends sender email + subject + ~200-char snippet per sample, capped at 3 samples per sender.
- Doesn't delete anything. "Archive" in Gmail just removes the `INBOX` label; the message stays in All Mail and is fully searchable, reversible via `undo`.
- Doesn't share, sell, or export your mail anywhere. Single-user, runs on your machine(s) only.
