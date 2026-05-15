#!/usr/bin/env bash
# serve.sh — launches the gmailwiz FastAPI trigger service under uvicorn.
#
# Run from anywhere; cwd is set to the project root so credentials.json
# and data/db/state.db resolve. Reads env vars from the shell or from
# `<project>/.env` (sourced if present) so launchd can pin them without
# inlining secrets into the plist.
#
# Required env vars:
#   GMAILWIZ_TRIGGER_TOKEN  — Bearer shared secret for POST /run
#   ANTHROPIC_API_KEY       — classifier
# Optional:
#   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  — completion notifications
#   GMAILWIZ_SERVE_PORT     — defaults to 8788
#   GMAILWIZ_SERVE_HOST     — defaults to 127.0.0.1 (loopback only;
#                             Cloudflare Tunnel does the public exposure)

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="${PROJECT_ROOT}/.venv/bin/python"
UVICORN="${PROJECT_ROOT}/.venv/bin/uvicorn"

if [[ ! -x "${UVICORN}" ]]; then
  echo "serve.sh: uvicorn not found at ${UVICORN}" >&2
  echo "Did the .venv get nuked? Re-create with:" >&2
  echo "  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt" >&2
  exit 1
fi

cd "${PROJECT_ROOT}"

# Source .env if present so launchd can keep secrets out of the plist.
# `set -a` exports every variable assigned while it's active.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -z "${GMAILWIZ_TRIGGER_TOKEN:-}" ]]; then
  echo "serve.sh: GMAILWIZ_TRIGGER_TOKEN is not set — refusing to start." >&2
  echo "Set it in the shell or in ${PROJECT_ROOT}/.env" >&2
  exit 2
fi

HOST="${GMAILWIZ_SERVE_HOST:-127.0.0.1}"
PORT="${GMAILWIZ_SERVE_PORT:-8788}"

echo "serve.sh: starting uvicorn on ${HOST}:${PORT}"
exec "${UVICORN}" gmailwiz.serve:app \
  --host "${HOST}" \
  --port "${PORT}" \
  --log-level info \
  --no-access-log
