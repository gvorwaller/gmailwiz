#!/usr/bin/env bash
# deploy.sh — push the current local HEAD to M2 (~/gmailwiz) via git pull,
# then restart the trigger service and verify it's healthy on the public
# hostname before exiting.
#
# Strict-by-design: refuses to deploy a dirty working tree and refuses to
# deploy a HEAD that isn't already on origin/main. M2 runs `git pull
# --ff-only`, then we cross-check that the remote SHA matches our local
# SHA, install dep changes if requirements.txt moved, smoke `python -m
# gmailwiz --help` on the remote, kick the launchd unit, and poll the
# public /health + /ready endpoints to confirm the new code is serving.
#
# Exit codes:
#   0  full success (service running, /health 200, /ready 200)
#   0  service running and /health 200, but /ready 503 (token expired or
#      similar operational issue — DEPLOY is not the cause; warning printed)
#   1  any pre-deploy guard fails (dirty tree, unpushed HEAD, etc.)
#   2  remote git/pip/smoke failed
#   3  service didn't come back up after kickstart (deploy DID land code,
#      but the daemon is unhealthy)

set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-Mprd}"
REMOTE_PATH="${REMOTE_PATH:-\$HOME/gmailwiz}"
PUBLIC_BASE="${PUBLIC_BASE:-https://gmailwiz.gaylon.photos}"
LAUNCHD_LABEL="${LAUNCHD_LABEL:-com.gmailwiz.trigger}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-30}"
LOCAL_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()   { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[deploy]\033[0m %s\n' "$*" >&2; }
err()   { printf '\033[1;31m[deploy]\033[0m %s\n' "$*" >&2; }
fail()  { err "$1"; exit 1; }

cd "${LOCAL_REPO_ROOT}"

# ---------------------------------------------------------------------------
# 1. Refuse if working tree is dirty.
# ---------------------------------------------------------------------------
log "Checking local working tree is clean..."
if [[ -n "$(git status --porcelain)" ]]; then
  err "Working tree is dirty — refusing to deploy."
  err "Commit or stash first, then push to origin/main:"
  git status --short >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 2. Resolve local HEAD and confirm it's on origin/main.
# ---------------------------------------------------------------------------
log "Resolving local HEAD..."
LOCAL_SHA="$(git rev-parse HEAD)"
LOCAL_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
log "  branch=${LOCAL_BRANCH}  sha=${LOCAL_SHA}"

log "Fetching origin..."
git fetch --quiet origin main

# Require local HEAD == origin/main EXACTLY. "Ancestor" would pass when
# local is behind origin (operator reviewed old code, deploy ships newer
# code), AND it would pass when local is ahead but not yet pushed.
# Both are footguns. Strict equality forces the operator to consciously
# resolve either case before any remote mutation.
ORIGIN_SHA="$(git rev-parse origin/main)"
if [[ "${LOCAL_SHA}" != "${ORIGIN_SHA}" ]]; then
  err "Local HEAD ${LOCAL_SHA:0:12} != origin/main ${ORIGIN_SHA:0:12}."
  if git merge-base --is-ancestor "${LOCAL_SHA}" "origin/main"; then
    err "  Local is BEHIND origin/main. You'd ship code you haven't reviewed."
    err "  Pull first:  git pull --ff-only origin ${LOCAL_BRANCH}"
  elif git merge-base --is-ancestor "${ORIGIN_SHA}" "${LOCAL_SHA}"; then
    err "  Local is AHEAD of origin/main. Your changes aren't pushed."
    err "  Push first:  git push origin ${LOCAL_BRANCH}"
  else
    err "  Local and origin/main have DIVERGED."
    err "  Reconcile manually (rebase or merge), push, then re-run deploy."
  fi
  exit 1
fi
log "  HEAD matches origin/main ✓"

# ---------------------------------------------------------------------------
# 3. Detect requirements.txt changes between deployed remote SHA and local.
# ---------------------------------------------------------------------------
log "Querying remote HEAD on ${REMOTE_HOST}..."
REMOTE_PRE_SHA="$(ssh "${REMOTE_HOST}" "cd ${REMOTE_PATH} && git rev-parse HEAD" 2>/dev/null || echo "")"
if [[ -z "${REMOTE_PRE_SHA}" ]]; then
  fail "Could not read remote git state at ${REMOTE_HOST}:${REMOTE_PATH}. Is the repo cloned there?"
fi
log "  remote pre-deploy sha=${REMOTE_PRE_SHA}"

REQS_CHANGED=0
if [[ "${REMOTE_PRE_SHA}" != "${LOCAL_SHA}" ]]; then
  if git diff --name-only "${REMOTE_PRE_SHA}" "${LOCAL_SHA}" 2>/dev/null | grep -qx 'requirements.txt'; then
    REQS_CHANGED=1
    log "  requirements.txt changed since remote HEAD — will run pip install."
  fi
else
  log "  remote already at this SHA — pull will be a no-op."
fi

# ---------------------------------------------------------------------------
# 4. Pull on the remote (ff-only).
# ---------------------------------------------------------------------------
log "Pulling on ${REMOTE_HOST}..."
ssh "${REMOTE_HOST}" bash -s <<EOF
set -euo pipefail
cd ${REMOTE_PATH}
git fetch origin main
git checkout main
git pull --ff-only origin main
EOF

# ---------------------------------------------------------------------------
# 5. Defensive cross-check: remote SHA now equals local.
# ---------------------------------------------------------------------------
REMOTE_POST_SHA="$(ssh "${REMOTE_HOST}" "cd ${REMOTE_PATH} && git rev-parse HEAD")"
if [[ "${REMOTE_POST_SHA}" != "${LOCAL_SHA}" ]]; then
  err "Remote SHA after pull (${REMOTE_POST_SHA:0:12}) doesn't match local HEAD (${LOCAL_SHA:0:12})."
  err "Investigate manually — divergence is unexpected when origin/main is the only ref involved."
  exit 1
fi
log "  remote post-deploy sha=${REMOTE_POST_SHA} ✓"

# ---------------------------------------------------------------------------
# 6. Install deps if requirements.txt changed.
# ---------------------------------------------------------------------------
if [[ "${REQS_CHANGED}" -eq 1 ]]; then
  log "Installing requirements.txt on remote..."
  ssh "${REMOTE_HOST}" "cd ${REMOTE_PATH} && .venv/bin/pip install --quiet -r requirements.txt"
  log "  pip install OK ✓"
fi

# ---------------------------------------------------------------------------
# 7. Smoke: python -m gmailwiz --help on the remote.
# ---------------------------------------------------------------------------
log "Smoke-testing remote import (python -m gmailwiz --help)..."
if ! ssh "${REMOTE_HOST}" "cd ${REMOTE_PATH} && .venv/bin/python -m gmailwiz --help >/dev/null"; then
  err "python -m gmailwiz --help failed on ${REMOTE_HOST}. Check the venv + dependencies."
  exit 2
fi
log "  --help smoke OK ✓"

# ---------------------------------------------------------------------------
# 8. Kick the launchd unit so the daemon picks up the new code.
#    `launchctl kickstart -k` stops the running process and re-launches it
#    using the existing service definition. The unit's KeepAlive=true means
#    it would restart itself eventually if it crashed; kickstart makes it
#    happen synchronously and right now.
# ---------------------------------------------------------------------------
log "Kicking launchd unit (${LAUNCHD_LABEL})..."
# `kickstart -k` succeeds silently with no output; on failure (unknown
# label etc.) it prints "Could not find service ..." and exits non-zero.
# The subsequent /health poll catches any silent failure regardless.
if ! ssh "${REMOTE_HOST}" "launchctl kickstart -k gui/\$(id -u)/${LAUNCHD_LABEL}"; then
  err "launchctl kickstart failed. Is ${LAUNCHD_LABEL} bootstrapped on ${REMOTE_HOST}?"
  err "  Check: ssh ${REMOTE_HOST} 'launchctl print gui/\$(id -u)/${LAUNCHD_LABEL}'"
  exit 3
fi
log "  kickstart issued ✓"

# ---------------------------------------------------------------------------
# 9. Verify /health on the public hostname.
#    Poll until 200 or HEALTH_TIMEOUT_SECONDS elapses. The unit takes a
#    second or two to start uvicorn after kickstart; CloudFlare may add
#    another beat. 30s default is plenty of headroom.
# ---------------------------------------------------------------------------
log "Verifying /health at ${PUBLIC_BASE} ..."
deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
last_code="?"
while [[ $(date +%s) -lt ${deadline} ]]; do
  code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 "${PUBLIC_BASE}/health" || echo "000")
  last_code="${code}"
  if [[ "${code}" == "200" ]]; then
    break
  fi
  sleep 1
done
if [[ "${last_code}" != "200" ]]; then
  err "/health did not return 200 within ${HEALTH_TIMEOUT_SECONDS}s (last=${last_code})."
  err "  Code is on disk at the right SHA, but the service is not serving."
  err "  Check on ${REMOTE_HOST}:  tail -50 ~/logs/gmailwiz-trigger/err.log"
  exit 3
fi
log "  /health 200 ✓"

# ---------------------------------------------------------------------------
# 10. Verify /ready. 200 == fully operational. 503 == service is up but
#     operationally degraded (e.g. token expired) — that's NOT a deploy
#     failure; we warn and exit 0 so the operator can re-auth at leisure.
# ---------------------------------------------------------------------------
log "Verifying /ready at ${PUBLIC_BASE} ..."
ready_body=$(mktemp)
ready_code=$(curl -sS -o "${ready_body}" -w "%{http_code}" --max-time 10 "${PUBLIC_BASE}/ready" || echo "000")
if [[ "${ready_code}" == "200" ]]; then
  log "  /ready 200 ✓ (all preflight checks pass)"
  rm -f "${ready_body}"
elif [[ "${ready_code}" == "503" ]]; then
  warn "/ready returned 503 — service is running but not fully ready."
  warn "  This is independent of the deploy. Failing checks:"
  if command -v jq >/dev/null 2>&1; then
    jq -r '.checks | to_entries[] | select(.value.ok == false) | "    - \(.key): \(.value.reason // "no reason given")"' < "${ready_body}" >&2 || cat "${ready_body}" >&2
  else
    cat "${ready_body}" >&2
  fi
  rm -f "${ready_body}"
else
  err "/ready returned HTTP ${ready_code} (expected 200 or 503)."
  cat "${ready_body}" >&2
  rm -f "${ready_body}"
  exit 3
fi

log ""
log "Deploy complete."
log "  ${REMOTE_HOST}:${REMOTE_PATH} now at ${LOCAL_SHA:0:12}"
log "  Trigger service kicked + verified at ${PUBLIC_BASE}"
