#!/usr/bin/env bash
# deploy.sh — push the current local HEAD to M2 (~/gmailwiz) via git pull.
#
# Strict-by-design: refuses to deploy a dirty working tree and refuses to
# deploy a HEAD that isn't already on origin/main. M2 runs `git pull
# --ff-only`, then we cross-check that the remote SHA matches our local
# SHA, install dep changes if requirements.txt moved, and smoke `python
# -m gmailwiz --help`.
#
# Phase 1 ship: code sync + smoke only. Phase 2 will extend this to kick
# the launchd unit and curl `/ready` on the trigger service.

set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-Mprd}"
REMOTE_PATH="${REMOTE_PATH:-\$HOME/gmailwiz}"
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
  fail "python -m gmailwiz --help failed on ${REMOTE_HOST}. Check the venv + dependencies."
fi
log "  --help smoke OK ✓"

log ""
log "Deploy complete."
log "  ${REMOTE_HOST}:${REMOTE_PATH} now at ${LOCAL_SHA:0:12}"
log ""
log "Suggested follow-up:"
log "  ssh ${REMOTE_HOST} '.venv/bin/python -m gmailwiz run --limit 10 --json --headless' | jq"
