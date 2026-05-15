#!/bin/bash
# Produces a consistent snapshot of the gmailwiz SQLite database using
# SQLite's online backup API. Safe to run while a `gmailwiz` invocation
# is in flight — the .backup command acquires a read transaction and
# copies pages without blocking writers.
#
# Intended use: Carbon Copy Cloner pre-flight script, before CCC copies
# the data/backup/ directory to the Synology NAS. Run independently any
# time you want a clean on-disk snapshot.
#
# Outputs:
#   <project>/data/backup/state.db     — local snapshot
#   <project>/data/backup/preflight.log — append-only log
#
# This is a single-mode script (no prod/remote pull) — gmailwiz is a
# laptop-only CLI with no deployed instance. Mirrors the giftlist
# preflight template minus the SSH/scp section.

set -euo pipefail

# CCC may invoke us with a stripped PATH. Set a known-good one so /usr/bin
# tools resolve without depending on caller's environment.
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Resolve the project root relative to this script so CCC can invoke it via
# any working directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SRC_DB="${PROJECT_ROOT}/data/db/state.db"
BACKUP_DIR="${PROJECT_ROOT}/data/backup"
DEST_DB="${BACKUP_DIR}/state.db"

# Tee everything to a log file so CCC failures can be diagnosed after the
# fact — CCC's "Errors" tab only surfaces the wrapper message, not actual
# script stderr. Log path is fixed so it's easy to find.
LOG_FILE="${BACKUP_DIR}/preflight.log"
mkdir -p "${BACKUP_DIR}"
{
  echo "===== $(/bin/date -u +%Y-%m-%dT%H:%M:%SZ) preflight start ====="
  echo "uid=$EUID user=$(/usr/bin/id -un 2>/dev/null || echo ?) home=${HOME:-?} pwd=$(pwd)"
  echo "path=${PATH:-?}"
  echo "args=$*"
} >> "${LOG_FILE}"
exec > >(/usr/bin/tee -a "${LOG_FILE}") 2> >(/usr/bin/tee -a "${LOG_FILE}" >&2)

# CCC runs preflight as root; outputs would end up root-owned and block the
# next interactive run by the user. Reassert ownership on every exit path so
# the backup dir stays writable to gaylonvorwaller regardless of caller.
OWNER_USER="gaylonvorwaller"
OWNER_GROUP="staff"
restore_ownership() {
  if [[ $EUID -eq 0 ]]; then
    /usr/sbin/chown -R "${OWNER_USER}:${OWNER_GROUP}" "${BACKUP_DIR}" 2>/dev/null || true
  fi
}
trap restore_ownership EXIT

# CCC invokes preflight scripts with positional args ($1 source, $2 dest, $3
# exit code from prior task). We accept and ignore them; -h/--help is the
# only flag we honor.
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      echo "Usage: $0"
      echo "Snapshots gmailwiz SQLite DB to ${DEST_DB} for CCC to back up."
      exit 0
      ;;
    --*|-?)
      echo "Unknown flag: $arg" >&2
      exit 2
      ;;
    *) ;; # ignore positional args (CCC passes source/dest paths)
  esac
done

# --- local snapshot ---------------------------------------------------------
if [[ ! -f "${SRC_DB}" ]]; then
  echo "[backup-sqlite] source DB not found: ${SRC_DB}" >&2
  exit 1
fi

mkdir -p "${BACKUP_DIR}"

# SQLite's .backup command is transactional and handles WAL checkpointing
# internally — the resulting file has no companion .db-wal / .db-shm.
# -bail causes any error (lock contention, I/O, etc.) to propagate.
/usr/bin/sqlite3 -bail "${SRC_DB}" ".backup '${DEST_DB}'"

INTEGRITY="$(/usr/bin/sqlite3 -bail "${DEST_DB}" 'PRAGMA integrity_check;')"
if [[ "${INTEGRITY}" != "ok" ]]; then
  echo "[backup-sqlite] integrity check failed: ${INTEGRITY}" >&2
  exit 2
fi

SIZE="$(/usr/bin/stat -f%z "${DEST_DB}" 2>/dev/null || /usr/bin/wc -c < "${DEST_DB}")"
echo "[backup-sqlite] snapshot ok: ${DEST_DB} (${SIZE} bytes)"
