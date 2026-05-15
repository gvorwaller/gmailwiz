# gmailwiz on M2 — operational guide

> Audience: future-Gaylon (or future-Claude session) when something on the
> M2 trigger service breaks at 11pm on a Sunday. Steps are intentionally
> verbose; copy-paste over guessing.

The gmailwiz trigger service runs on the M2 Mac. Drafts → Cloudflare
Tunnel → `127.0.0.1:8788` → FastAPI → background thread →
`oneshot.run_one_pass` against the M2's `data/token.json`. Telegram fires
on completion.

## One-time M2 setup

1. **Clone the repo** (M2 prompt):
   ```bash
   cd ~ && git clone https://github.com/gvorwaller/gmailwiz.git
   cd ~/gmailwiz
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

2. **Copy credentials.json from M4** (one-time; same OAuth client across
   machines is intentional):
   ```bash
   scp ~/gmailwiz/credentials.json Mprd:~/gmailwiz/credentials.json
   ```

3. **Copy the token from M4** (must do this every ~6 days; see Token
   rotation below):
   ```bash
   scp ~/gmailwiz/data/token.json Mprd:~/gmailwiz/data/token.json
   ```

4. **Create `~/gmailwiz/.env` on M2** (NOT committed; not in git):
   ```bash
   ssh Mprd
   cd ~/gmailwiz
   cat > .env <<'EOF'
   GMAILWIZ_TRIGGER_TOKEN=<generate-32-bytes:  openssl rand -hex 32>
   ANTHROPIC_API_KEY=<your-key>
   TELEGRAM_BOT_TOKEN=<from-botfather>
   TELEGRAM_CHAT_ID=<your-chat-id-with-the-bot>
   EOF
   chmod 600 .env
   ```

   **Finding `TELEGRAM_CHAT_ID`**: open Telegram, send the bot any message
   (e.g. `/start`), then on any machine with curl:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
   ```
   The chat id is in `result[].message.chat.id` (your personal chat with
   the bot is typically a positive integer).

5. **Cloudflare Tunnel hostname**. The existing `m2.gaylon.photos` TCP
   tunnel stays as-is for VNC. Add a new HTTP public hostname:
   - Zero Trust → Networks → Tunnels → open the M2 tunnel.
   - Public Hostnames → Add:
     - Subdomain: `gmailwiz`
     - Domain: `gaylon.photos`
     - Service: `http://localhost:8788`
   - Save. Cloudflare propagates in ~30s.

6. **launchd unit** (so the service runs at boot and auto-restarts on
   crash). Copy the template:
   ```bash
   # On M2:
   cp ~/gmailwiz/scripts/com.gmailwiz.trigger.plist.template \
      ~/Library/LaunchAgents/com.gmailwiz.trigger.plist
   # Edit the paths inside if your $HOME isn't /Users/gaylonvorwaller.
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.gmailwiz.trigger.plist
   launchctl print gui/$(id -u)/com.gmailwiz.trigger | head -30
   ```

7. **Smoke test** from any machine:
   ```bash
   curl -fsS https://gmailwiz.gaylon.photos/health
   # → {"status":"ok"}

   curl -fsS https://gmailwiz.gaylon.photos/ready | python3 -m json.tool
   # → "ready": true   (if not, the JSON points at the failing check)

   # POST a real run (will actually mutate Gmail!):
   curl -fsS -X POST https://gmailwiz.gaylon.photos/run \
     -H "Authorization: Bearer $GMAILWIZ_TRIGGER_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"limit": 10}'
   # → {"job_id":"...","status":"queued"}
   ```

8. **Drafts action**. In Drafts, create a new JavaScript action and paste
   `scripts/drafts_trigger_gmailwiz.js`. Then create a Credential named
   `gmailwiz-trigger` with field `token` = `$GMAILWIZ_TRIGGER_TOKEN`.

## Each deploy

From M4 with the repo at the desired commit:

```bash
git push origin main          # deploy.sh refuses unpushed HEADs
scripts/deploy.sh
```

`deploy.sh` will:
- Refuse a dirty working tree.
- Refuse if local HEAD ≠ origin/main exactly.
- SSH to Mprd, `git pull --ff-only`.
- Cross-check the remote SHA matches local.
- `pip install -r requirements.txt` if requirements changed.
- Smoke `python -m gmailwiz --help` on M2.

After the script returns success, kick the service to pick up the new
code:

```bash
ssh Mprd launchctl kickstart -k gui/$(ssh Mprd id -u)/com.gmailwiz.trigger
# Verify the new code is running:
curl -fsS https://gmailwiz.gaylon.photos/health
```

> Note: `deploy.sh` does not kick the service automatically yet. Adding
> that is a TODO once we've watched the service run for a few weeks
> and trust the "no surprise downtime mid-deploy" assumption.

## Token rotation (~weekly)

Google's Testing-mode refresh tokens expire after 7 days. Workflow:

```bash
# On M4 (the only machine with a browser session):
python -m gmailwiz           # menu → 5 (Re-authenticate)
scp ~/gmailwiz/data/token.json Mprd:~/gmailwiz/data/token.json
# Confirm /ready goes green again:
curl -fsS https://gmailwiz.gaylon.photos/ready | python3 -m json.tool
```

If a Drafts trigger fires with a stale token, the worker thread will
mark the job `auth_required` and Telegram fires with the message
"auth_required on M4 (menu 5)". The run does nothing else.

## Inspecting state

Logs:

```bash
# launchd captures stdout/stderr to:
ssh Mprd 'tail -f ~/logs/gmailwiz-trigger/out.log ~/logs/gmailwiz-trigger/err.log'
```

Jobs table:

```bash
ssh Mprd sqlite3 ~/gmailwiz/data/db/trigger_jobs.db \
  "SELECT id, state, created_at, finished_at FROM jobs ORDER BY created_at DESC LIMIT 10"
```

A specific job:

```bash
curl -fsS https://gmailwiz.gaylon.photos/jobs/<job-id> | python3 -m json.tool
```

The gmailwiz audit log (per-message before/after label states):

```bash
ssh Mprd sqlite3 ~/gmailwiz/data/db/state.db \
  "SELECT * FROM audit_log WHERE run_id='<run-id>' LIMIT 10"
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `/health` returns 200 but `/ready` is 503 with `headless_auth.ok=false` | Token expired. Re-auth on M4 + scp token. |
| `/health` returns 200 but `/ready` is 503 with `anthropic_api_key.ok=false` | `.env` missing the key, or launchd didn't reload after edit. `launchctl kickstart -k ...`. |
| Drafts says "Bearer token rejected" | `gmailwiz-trigger` credential's `token` field doesn't match `GMAILWIZ_TRIGGER_TOKEN` on M2. |
| Drafts says "gmailwiz busy (job ...)" | Previous run is still in flight. Wait ~1 min and re-tap. |
| Telegram message never arrives but job is `done` | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` missing or wrong. Check `err.log` for `[telegram] skipped` / `[telegram] HTTP 4xx`. |
| Public hostname returns 502 | M2 cloudflared can't reach `localhost:8788`. Verify `launchctl print` shows the service running and `lsof -iTCP:8788 -sTCP:LISTEN` returns a row. |
| Service won't start after deploy | `requirements.txt` may have changed but `.venv` wasn't updated. SSH in, `.venv/bin/pip install -r requirements.txt`, `launchctl kickstart -k ...`. |

## Security notes

- `GMAILWIZ_TRIGGER_TOKEN` is a single Bearer. If exposed, an attacker
  with the token could trigger one-pass runs against your inbox — they
  cannot read your mail (the worker only writes labels and removes
  INBOX), but spurious archives are annoying. Rotate by setting a new
  token in `.env`, kickstarting the service, and updating the
  `gmailwiz-trigger` credential in Drafts.
- `~/gmailwiz/.env`, `data/token.json`, and `credentials.json` should be
  mode `0600` and owned by `$USER`. The repo is `0700`. Verify with
  `ls -la ~/gmailwiz`.
- The Cloudflare Tunnel public hostname is unauthenticated at the edge
  by default — Bearer at the app layer is the only gate. If/when this
  becomes a worry, gate `gmailwiz.gaylon.photos` behind Cloudflare
  Access (email OTP).
