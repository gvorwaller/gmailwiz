# Manual setup items — 2026-04-28

> **STATUS: ✅ ALL ITEMS COMPLETE (2026-04-28).** Coding agents do not need to perform any of the steps below. They are recorded for historical context only. See "Verification one-liners" at the bottom to confirm current state.

Items that require human action outside the repo. Claude/td cannot complete these.

## Google Cloud OAuth client (blocks `td-650d79` auth bootstrap) — ✅ done 2026-04-28

Required before any Gmail API call can succeed. Stays in **Testing** mode — no app verification needed as long as Gaylon is the sole test user. (See "Why Testing mode" below for why we accepted the 7-day refresh-token expiry.)

**Plan deviation: re-used the existing `gaylonphotos` GCP project instead of creating a new `gmailwiz` project.** That project already had Gmail API enabled, OAuth consent screen configured (External / Testing / `gaylon@vorwaller.net` as test user), and one OAuth client (`Giftlist`, web app) for the giftlist repo. Adding a second OAuth client to the same project is the supported pattern; one consent screen serves multiple clients. Steps 1–3 below were skipped as already-satisfied.

1. ~~**Create project** in Google Cloud Console (e.g. name it `gmailwiz`).~~ — skipped, used existing `gaylonphotos`.
2. ~~**Enable Gmail API**: APIs & Services → Library → Gmail API → Enable.~~ — already enabled in `gaylonphotos`.
3. ~~**Configure OAuth consent screen**~~ — already configured (External, Testing, test user `gaylon@vorwaller.net`). Scopes left empty; `InstalledAppFlow` requests them at runtime.
4. **Create OAuth client credentials** — done:
   - APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Application type: **Desktop app**
   - Name: `gmailwiz CLI`
   - Downloaded JSON → moved to `~/gmailwiz/credentials.json` (gitignored, verified).
   - Verified JSON shape: top-level key `installed`, `redirect_uris: ['http://localhost']`, `project_id: gaylonphotos`.

### Why Testing mode (and not Production)

Considered upgrading to permanent auth to dodge the 7-day refresh-token expiry. Three options exist, only one is realistic:

- **Publish to Production + verify**: `gmail.modify` is a *restricted* scope, requiring third-party CASA security audit (~$15K–$75K, multi-week). Prohibitive for self-use.
- **Google Workspace + Internal app**: would require putting `vorwaller.net` (or another domain Gaylon controls) on Google Workspace at ~$7/mo. Not justified to avoid a weekly re-auth click.
- **Stay in Testing**: weekly re-auth (30-second redo). Accepted.

Giftlist is in the same boat for the same reason — see `giftlist/docs/2026-04-15_implementation-plan.md`.

## ANTHROPIC_API_KEY — ✅ done 2026-04-28

Exported from `~/.zshrc` (copied from `~/gaylonphotos/.env`). Verified persistent across new shells. No `.env` loader in gmailwiz — the project reads `os.environ['ANTHROPIC_API_KEY']` directly. See `docs/2026-04-28_implementation-plan-Codex.md` § "State And Config Layout" for the resolved decision.

## Notes

- Scopes that will be requested at runtime (from `td-650d79` OAuth reference comment):
  - `https://www.googleapis.com/auth/gmail.modify`
  - `https://www.googleapis.com/auth/userinfo.email`
  - `openid`
- Refresh tokens in Testing mode expire after **7 days**. Re-running the auth flow regenerates them; tolerable for self-use.
- `credentials.json` and `token.json` are both gitignored — never commit either.

## Verification one-liners

If you (or a coding agent) want to confirm the manual setup is still in place before starting dev work:

```bash
ls -l ~/gmailwiz/credentials.json                              # exists, ~401 bytes
git -C ~/gmailwiz check-ignore -v credentials.json             # confirms gitignored
zsh -ic 'echo "len=${#ANTHROPIC_API_KEY}"'                     # length=108
python3 -c "import json; d=json.load(open('credentials.json')); print(list(d)[0], d['installed']['project_id'])"
# Expected: installed gaylonphotos
```
