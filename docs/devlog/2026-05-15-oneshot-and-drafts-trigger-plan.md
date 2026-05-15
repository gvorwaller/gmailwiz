# gmailwiz one-pass + Drafts trigger on M2

## Context

`gmailwiz` is the menu-based Gmail triage CLI shipped through Phases 1+2+3 (commits `1cff7d5`, `c38061a`, `996b5ea`; 217 tests passing). Real-world use validated: ~1,045 messages classified, labeled, and archived across 4 categories on 2026-05-02–03.

Two existing td notes capture the next ambition:
- `td-816bae` (P2): non-interactive one-pass mode — classify → label → archive in a single command for unattended invocation.
- `td-474b23` (P2): Drafts → endpoint trigger that invokes one-pass on M2 via existing Cloudflare Tunnel, with Telegram completion notification. Depends on `td-816bae`.

**Workflow context**: the user pre-triages via Claude iOS (read + summarize, flag anything that needs reply) and a quick manual inbox scan, THEN triggers Drafts → gmailwiz to clean-sweep everything else. Because the user has already seen anything important, aggressive auto-label + auto-archive across **all 4 categories** is the correct default for one-pass.

**Decisions taken** (2026-05-15 planning session):
- **Architecture**: M2-only via existing Cloudflare Tunnel. Drafts → `gmailwiz.gaylon.photos` (or chosen subdomain) → M2 → `127.0.0.1:<port>`. No DO involvement for gmailwiz. Smaller blast radius (OAuth token never leaves M2) and a simpler topology than the DO-fronted td-sync pattern.
- **Scope of one-pass**: label all 4 classifiable categories, archive all 4. The user's pre-triage workflow makes archiving personal acceptable.

**Plan revisions (2026-05-15, post-CODEX review)**:
- Auth needs an explicit non-interactive mode at the `auth.py` boundary; catching refresh failure inside `oneshot` is too late because `get_credentials` currently falls through to `InstalledAppFlow.run_local_server` (`gmailwiz/auth.py:233`) which would hang on M2.
- One-pass must operate on a single message-id snapshot captured up front. `build_label_plan` / `build_archive_plan` each refetch Gmail state (`gmailwiz/planning.py:271`, `:750`), so naive sequencing would let later categories see an inbox already mutated by earlier ones. Planning helpers must accept a provided snapshot.
- API auth is header-only (`Authorization: Bearer`); no query-string token.
- DB connections are thread-affine (`sqlite3.connect`, `gmailwiz/db.py:124-140`). The HTTP request handler must not share its connection with the worker thread — the worker opens its own.
- The service-account escape hatch is removed from the risks list; it is not a viable substitute for personal-Gmail OAuth without Workspace domain-wide delegation.

Development on M4; production on M2.

---

## Phase 1 — `td-816bae`: one-pass mode in gmailwiz

### What

A new subcommand `python -m gmailwiz run` that chains `report` → `label` (all 4 categories) → `archive` (all 4 categories) end-to-end with no prompts, structured JSON output, and a non-zero exit on auth-failure / partial-failure / total-failure.

### Critical files

- **NEW** `gmailwiz/oneshot.py` — orchestrator: `run_one_pass(*, creds, db_path, limit, archive=True, on_progress) -> OneShotResult`. Opens its own `sqlite3.Connection` internally (it may be called from a worker thread; see Phase 2 threading note). Returns a dataclass with `report_run_id`, per-category `label_run_id`/`labels_applied`/`labels_failed`, per-category `archive_run_id`/`archive_applied`/`archive_failed`, top-level status, wall time, `error_code`, plus the captured `snapshot_message_ids` for traceability.
- **MODIFY** `gmailwiz/auth.py` — add non-interactive auth surface. Either a new `get_credentials(interactive: bool = True)` parameter or a sibling `get_credentials_headless()`. In non-interactive mode: missing/corrupt token → raise `AuthRequired`; expired token + refresh failure → raise `AuthRequired`; never call `_run_flow` / `InstalledAppFlow.run_local_server`. `oneshot.run_one_pass` translates `AuthRequired` into `error_code="auth_required"`.
- **MODIFY** `gmailwiz/planning.py` — refactor `build_label_plan` and `build_archive_plan` to accept an optional caller-provided snapshot (e.g. `messages: Optional[Sequence[GmailMessage]] = None`, or a sibling `message_ids` arg + skip the `list_unread_message_ids` / refetch path when provided). Existing call sites pass `None` and behavior is unchanged. `oneshot` captures the snapshot once and reuses it across all four categories.
- **MODIFY** `gmailwiz/cli.py:_run_report` — extract its classification path into a reusable `_classify_inbox(creds, conn, *, limit, on_progress) -> ClassifyResult` helper (which itself takes/returns the snapshot) so `oneshot.py` reuses the exact same Gmail-read + Claude-classify logic instead of duplicating it.
- **MODIFY** `gmailwiz/cli.py` — register `@main.command("run")` with `--limit`, `--json`, `--no-archive` flags. The CLI subcommand is a thin wrapper around `oneshot.run_one_pass` + JSON serialization.
- **MODIFY** `gmailwiz/menu_text.py` — add menu option **#8 "Run full cycle"**: report → label-all → archive-all in one go. Prompts only for `--limit` (default 1000), no per-category prompts. Internally calls `oneshot.run_one_pass` (same path as the `run` subcommand and the future trigger service).
- **MODIFY** `gmailwiz/cli.py` — add `_menu_run_one_pass()` handler wired to menu key `8`.
- **NEW** `tests/test_oneshot.py` — full happy path, expired-token headless path (must return `auth_required` without invoking any flow), snapshot stability across all four categories (inbox mutated between phases ≠ snapshot mutated), partial-failure path, JSON output schema.

### Deploy script (M4 → M2)

- **NEW** `scripts/deploy.sh` — operator-run from M4. Behavior:
  1. Refuse if `git status --porcelain` is non-empty (dirty working tree).
  2. Refuse if `HEAD` SHA is not present on `origin/main` (push first).
  3. SSH to `Mprd` (existing alias), `cd ~/gmailwiz`, `git fetch origin && git checkout main && git pull --ff-only origin main`.
  4. Verify the remote HEAD SHA now equals the local HEAD SHA (defensive — catches divergence).
  5. If `requirements.txt` changed in the deployed diff, `.venv/bin/pip install -r requirements.txt` on M2.
  6. Remote smoke: `.venv/bin/python -m gmailwiz --help` succeeds (basic import + click registration).
  7. Print a clear ✓/✗ summary, exit non-zero on any step's failure.
  - Phase 2 will extend this script to also kick the launchd unit and curl `/ready`. For Phase 1 ship, `deploy.sh` only handles code sync + smoke.

### Snapshot model

`run_one_pass` captures the inbox once:

```
ids = list_unread_message_ids(creds, max_results=limit, query="is:unread in:inbox")
messages = get_message_metadata(creds, ids)  # one fetch
```

Then for each phase:
1. **Report/classify**: classify senders derived from `messages`.
2. **Label all 4**: for each `category in CLASSIFIABLE_CATEGORIES`, call the refactored `build_label_plan(..., messages=messages)`. Apply.
3. **Archive all 4**: same pattern with `build_archive_plan(..., message_ids=[m.id for m in messages])`. Archive planners that need current `label_ids` post-label should be passed the locally projected after-state from the label step (the audit_log already captures this), not refetched from Gmail.

This is the design-level change: planning helpers stop being the source of "what is in my inbox right now" when called by `oneshot`. The CLI subcommands keep their existing live-fetch behavior by passing `messages=None`.

### Reuses (no changes needed)

- `gmailwiz/planning.py`: `apply_label_plan`, `apply_archive_plan`, `heal_run_status_from_audit_log` — directly composable.
- `gmailwiz/db.py:open_db`, `gmailwiz/classifier.py:classify_senders`, `gmailwiz/gmail_client.py:*` — direct reuse.
- The `--json` shapes already documented in `cli.py` (`_print_label_plan_preview`, `_run_label_commit`, `_print_archive_plan_preview`, etc.).

### Default behavior

```
python -m gmailwiz run --limit 1000 --json
```

→ classify inbox → label each of `{promotional, transactional, newsletter, personal}` → archive each of those label runs → emit one JSON blob with everything. Exits 0 on full success, 4 on any partial failure, 5 on total failure, 2 on auth_required.

---

## Phase 2 — `td-474b23`: Drafts → trigger service on M2

### What

A FastAPI service co-located in the gmailwiz repo at `gmailwiz/serve.py`, exposing a tiny HTTP API behind the existing M2 Cloudflare Tunnel. Drafts hits `POST https://gmailwiz.gaylon.photos/run?token=<secret>`, the service runs `oneshot.run_one_pass` in a background thread, returns a `job_id` immediately, and sends a Telegram message with the summary on completion.

### Critical files

- **NEW** `gmailwiz/serve.py` — FastAPI app. Endpoints:
  - `POST /run` — auth via `Authorization: Bearer <token>` header **only** (no query-string token; query params leak to Cloudflare logs, request logs, error traces, and any future ASGI middleware). Optional `{"limit": int}` body override. Spawns one-pass in a background thread, returns `{"job_id": "<uuid>"}` immediately (202 Accepted).
  - `GET /jobs/{job_id}` — query status (`queued`/`running`/`done`/`failed`/`auth_required`). Useful for debugging; not required for happy path.
  - `GET /health` — liveness only; returns 200 if the process is up. Never depends on `data/token.json` or `ANTHROPIC_API_KEY` — used by Cloudflare Tunnel and monitoring, which should not flap when a real run would.
  - `GET /ready` — preflight readiness: checks `credentials.json` exists, `data/token.json` exists and parses, `data/token.json` is currently valid OR refreshable without a flow, `ANTHROPIC_API_KEY` is set, no run is in flight. Returns 200 with a JSON breakdown or 503 with the failing checks. This is what surfaces "you need to re-auth on M4" before triggering a run.
  - Concurrency lock: at most one one-pass in flight at a time. Concurrent `POST /run` returns **`409 Conflict`** (not 429 — the request is rejected because of conflicting state, not because the caller is rate-limited) with the in-flight `job_id`.
  - **Threading**: the request handler validates input, persists a `queued` jobs-table row, and submits the work. The worker thread (a) opens its own `gmailwiz` `state.db` connection via `db.open_db`, (b) opens its own `trigger_jobs.db` connection, (c) calls `oneshot.run_one_pass(creds=..., db_path=..., ...)`, (d) closes both connections. No `sqlite3.Connection` ever crosses the handler/worker boundary — `sqlite3` connections are thread-affine (`gmailwiz/db.py:124-140`) and sharing one would fail at runtime.
- **NEW** `gmailwiz/telegram.py` — minimal `send_message(text)` via `httpx` POST to `https://api.telegram.org/bot<token>/sendMessage`. Reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from env. Best-effort: log failures but don't fail the job.
- **NEW** `gmailwiz/serve_state.py` (or co-locate in serve.py) — jobs table in a separate SQLite file at `data/db/trigger_jobs.db` to keep gmailwiz's `state.db` focused on triage state. Schema: `id TEXT PK, started_at, finished_at, status, summary_json, error`.
- **MODIFY** `requirements.txt` — add `fastapi`, `uvicorn`, `httpx`.
- **NEW** `tests/test_serve.py` — FastAPI `TestClient` tests for auth gating, 429 concurrency lock, JSON shapes, job lifecycle.
- **NEW** `scripts/serve.sh` — wrapper to launch uvicorn (`uvicorn gmailwiz.serve:app --host 127.0.0.1 --port 8788`). Mirrors `scripts/gmailwiz` pattern (project-root resolution via `BASH_SOURCE`).
- **NEW** `scripts/drafts_trigger_gmailwiz.js` — Drafts action. Mirrors `/Users/gaylonvorwaller/td-sync/drafts_send_to_td.js`. Constructs URL with token, POSTs, shows success/error toast in Drafts.
- **NEW** `~/Library/LaunchAgents/com.gmailwiz.trigger.plist` (M2 only, not committed) — launchd unit that runs `scripts/serve.sh` and keeps it up. Mirrors `com.td.do-puller.plist` pattern.
- **NEW** `docs/deploy-m2.md` — operational doc: how to deploy, how to restart, how to read logs, how to rotate the trigger token, how to re-auth (M4 → scp token to M2).

### Reuses

- `gmailwiz/oneshot.py:run_one_pass` (Phase 1 deliverable) — the entire request handler is essentially "validate token → spawn thread → return job_id → background thread calls `run_one_pass` → send Telegram".
- `gmailwiz/db.py:open_db` — for the jobs table (point at a different path).
- `~/td-sync/do-server/server.py` — reference for auth pattern (query-string token vs Bearer header).
- `~/td-sync/drafts_send_to_td.js` — reference for the Drafts script structure.

### Configuration (env vars, `.env` on M2, never committed)

- `GMAILWIZ_TRIGGER_TOKEN` — shared secret for `Authorization: Bearer`.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — for completion notifications. **Decision deferred**: new bot vs. reuse giftlist's. Recommend new bot for clean separation; cheap to set up.
- `ANTHROPIC_API_KEY` — same as today; M2 needs it.
- The OAuth token at `data/token.json` and `credentials.json` at repo root — pre-positioned via `scp` from M4.

---

## Phase 3 — dev/deploy workflow

- **M4** (dev): all code lives here, all tests run here, OAuth re-auth runs here (only machine with a browser session for the flow).
- **M2** (prod): receives deploys, runs `uvicorn` under launchd, holds the live token and DB.

### Deploy pattern (mirrors td-sync)

1. M4: write code, `pytest`, git commit.
2. M4: `scp -r gmailwiz/ tests/ requirements.txt scripts/ M2:~/gmailwiz/` (or `git pull` on M2 if a remote is configured).
3. M2: `cd ~/gmailwiz && .venv/bin/pip install -r requirements.txt` if deps changed.
4. M2: `launchctl kickstart -k gui/$(id -u)/com.gmailwiz.trigger` to restart service.
5. M2: `curl -fsS https://gmailwiz.gaylon.photos/health` smoke test.
6. M4 or phone: trigger from Drafts; verify Telegram arrives.

### Token lifecycle (the headless-OAuth problem)

Testing-mode refresh tokens expire after 7 days. M2 has no display for an interactive re-auth. Mitigation:

1. **Weekly cadence**: on M4, run `gmailwiz` menu option 5 (re-auth) every ~6 days to regenerate the token. Then `scp ~/gmailwiz/data/token.json M2:~/gmailwiz/data/token.json`. Single command if scripted.
2. **Pre-emptive alert** (P3 follow-up): trigger service checks token `expiry` on each `/run`. If within 24h of expiry, send Telegram alert "Re-auth on M4 soon" before kicking off one-pass. Doesn't block the run; just nudges.
3. **Hard failure**: if token is expired/revoked at run time, `oneshot.run_one_pass` returns `error_code="auth_required"`. Service sends Telegram alert; the run does nothing. User re-auths on M4 and re-triggers from Drafts.

---

## Files to be modified / created (consolidated)

**Phase 1 (one-pass)**
- NEW `gmailwiz/oneshot.py`
- NEW `tests/test_oneshot.py`
- NEW `scripts/deploy.sh`
- MODIFY `gmailwiz/auth.py` (`AuthRequired` + `interactive=False` mode)
- MODIFY `gmailwiz/planning.py` (`build_label_plan`/`build_archive_plan` accept optional snapshot)
- MODIFY `gmailwiz/cli.py` (extract `_classify_inbox`, register `run` subcommand, menu #8 handler)
- MODIFY `gmailwiz/menu_text.py` (menu option #8 "Run full cycle")

**Phase 2 (trigger)**
- NEW `gmailwiz/serve.py`
- NEW `gmailwiz/telegram.py`
- NEW `tests/test_serve.py`
- NEW `scripts/serve.sh`
- NEW `scripts/drafts_trigger_gmailwiz.js`
- NEW `docs/deploy-m2.md`
- MODIFY `requirements.txt` (add `fastapi`, `uvicorn`, `httpx`)
- NEW (on M2, not committed) `~/Library/LaunchAgents/com.gmailwiz.trigger.plist`
- NEW (on M2, not committed) `~/gmailwiz/.env`

---

## Verification

### Phase 1 (run on M4)
1. `pytest tests/test_oneshot.py` — all green. Must include:
   - happy path: snapshot captured once, 4 label runs + 4 archive runs derived from it
   - expired-token headless: `oneshot.run_one_pass` returns `error_code="auth_required"` and no `_run_flow` / `InstalledAppFlow.run_local_server` call was made (assert via patch/mock that the flow function is never invoked)
   - snapshot stability: mutate the Gmail mock between phases (drop/relabel messages) and assert subsequent label/archive plans still operate on the original snapshot, not the mutated state
   - partial failure: one category's `apply_label_plan` raises mid-run; status reflects partial; other categories still run; final `error_code` set
2. `pytest tests/test_planning.py` — existing helpers with `messages=None` produce the same results as before the refactor (regression guard).
3. `pytest tests/` — full suite still 217+ passing.
4. `python -m gmailwiz run --limit 10 --json | jq` — smoke against real Gmail (small limit for safety). Confirm JSON parses, run_ids match new audit_log rows, labels visible in Gmail UI.
5. `python -m gmailwiz run --no-archive --limit 10 --json` — verify the archive opt-out flag.
6. Inspect SQLite: confirm one `report` run + four `label` runs + four `archive` runs (or four `label` + zero `archive` with `--no-archive`).

### Phase 2 (run on M4 first, then M2)
1. `pytest tests/test_serve.py` — TestClient covers:
   - auth gate: header-only Bearer accepted; missing/wrong header → 401; query-string `?token=` → 401 (never accepted)
   - 409 lock: second `POST /run` while one is in flight returns 409 with the in-flight `job_id`
   - `/ready` failure modes: missing token, missing credentials.json, missing `ANTHROPIC_API_KEY`, expired non-refreshable token each return 503 with the right failing check named
   - `/health` returns 200 even when `/ready` would be 503 (decoupled)
   - threaded DB access: worker opens its own connection (no thread-shared `sqlite3.Connection`)
   - job lifecycle: queued → running → done; failed jobs include `error_code`
2. **Local on M4**: `uvicorn gmailwiz.serve:app --host 127.0.0.1 --port 8788`. `curl http://127.0.0.1:8788/health` → 200. `curl http://127.0.0.1:8788/ready` → 200. `curl -X POST -H "Authorization: Bearer <test-token>" http://127.0.0.1:8788/run` → 202 with `job_id`. Wait, verify Telegram message arrives.
3. **Deploy to M2**: scp, restart launchd, `curl https://gmailwiz.gaylon.photos/health` from any machine.
4. **End-to-end from Drafts**: install `drafts_trigger_gmailwiz.js` action, trigger it from phone, watch Telegram for completion.
5. **Failure mode**: temporarily corrupt `data/token.json` on M2, trigger, verify Telegram says "auth_required" with the right error text.

---

## Risks / known issues / deferred items

- **Token expiry pain**: weekly manual re-auth + scp is the floor. The escape hatch is moving out of Google's Testing mode (CASA audit cost is the blocker — same wall as Phase 1 ship). A service account is **not** a viable substitute for personal-Gmail OAuth: service accounts impersonate users only via Workspace domain-wide delegation, which doesn't apply to a consumer Gmail account.
- **Concurrent one-pass**: in-flight lock in `serve.py` returns 429. If the user triggers from Drafts while a slow run is mid-flight, they get the existing `job_id` back and Telegram fires once.
- **Telegram bot setup**: new bot via BotFather; ~5 min one-time. If user wants to reuse giftlist's bot, swap the env vars.
- **Hostname not yet picked**: defaulting to `gmailwiz.gaylon.photos`; trivial to change to `gw.gaylon.photos` or similar in cloudflared config.
- **Logs**: M2 launchd unit writes to `~/logs/gmailwiz-trigger/{out,err}.log` (mirroring td-sync convention).
- **No CI**: deploy is manual scp + launchctl. Same model as td-sync.
- **One-pass invocation from menu (P3)**: not strictly required; the trigger service will be the primary path. Adding a menu option is one helper + one `MENU_OPTIONS` entry — defer until needed.
- **Existing td-sync project whitelist already includes `gmailwiz`**: `/Users/gaylonvorwaller/td-sync/td_do_puller.py:23-28`. Capturing this for reference — td-sync remains the path for *creating td tasks* about gmailwiz from Drafts; the new gmailwiz trigger service is a separate path for *triggering one-pass runs*.
