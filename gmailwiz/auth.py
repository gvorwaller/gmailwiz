"""OAuth bootstrap for Gmail API access.

Uses ``google-auth-oauthlib``'s ``InstalledAppFlow`` to drive a desktop OAuth
flow against the ``credentials.json`` file at the repo root. Tokens persist in
``data/token.json`` (inside the repo, gitignored) so subsequent runs don't
require browser interaction (until the refresh token expires — 7 days, since
the OAuth project stays in Google's "Testing" mode).

Mirrors the giftlist TS implementation's flag set: ``access_type=offline``,
``prompt=consent``, ``include_granted_scopes=True``.
"""

from __future__ import annotations

import os

# Must be set BEFORE importing oauthlib (transitively pulled in by
# google_auth_oauthlib). When `include_granted_scopes=True` is used, Google
# returns the union of scopes previously granted for this Google account
# across all OAuth clients (e.g. giftlist's `gmail.readonly` and
# `contacts.readonly` get merged in). oauthlib's default is to raise on any
# scope mismatch between request and response. Relaxing it tells oauthlib to
# treat the merge as informational, which is the documented intended behavior
# of `include_granted_scopes`. See:
# https://github.com/googleapis/google-auth-library-python-oauthlib/issues/24
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

import json
import sys
import uuid
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# gmail.modify covers the read + label + archive scopes used across all phases.
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
)

# Default file locations. Tests inject custom paths via the helper functions
# below; the CLI uses these defaults.
#
# Both anchor to the repo root (one directory above this module) so they
# resolve correctly regardless of cwd, AND so all project state stays
# co-located with the source — visible in Finder, easy to find. `data/` is
# gitignored — token.json is never committed.
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CREDENTIALS_PATH = _REPO_ROOT / "credentials.json"
DEFAULT_TOKEN_PATH = _REPO_ROOT / "data" / "token.json"


def _ensure_parent_dir(path: Path) -> None:
    """Create the parent dir with restrictive permissions.

    `data/` (and `data/db/`) hold the OAuth token and the SQLite state DB.
    0o700 is the POLA mode for credential-adjacent dirs; chmod is
    best-effort because some filesystems (e.g. exFAT) ignore mode bits.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass


def _load_token(token_path: Path) -> Optional[Credentials]:
    """Load saved credentials from disk, if any.

    Returns ``None`` only for the corrupt-file or missing-file case (the only
    safe-to-recover scenarios — re-authing fixes them). Permission/IO errors
    re-raise per cs.md "surface raw errors verbatim".
    """
    if not token_path.exists():
        return None
    try:
        return Credentials.from_authorized_user_file(str(token_path), list(SCOPES))
    except (json.JSONDecodeError, ValueError):
        # Corrupt or unparseable token file — treat as missing so the next call re-auths.
        return None


def _save_token(creds: Credentials, token_path: Path) -> None:
    """Persist credentials to disk in JSON form.

    Writes via a `.tmp` sibling + `os.replace` so a process kill mid-write
    can't leave a half-written `token.json` (which the next run would treat
    as corrupt and force re-auth, throwing away an otherwise-valid grant).

    The file is created with mode 0o600 — it contains a long-lived
    refresh_token plus client_id/client_secret, so even on a single-user Mac
    we shouldn't leave it world-readable.
    """
    _ensure_parent_dir(token_path)
    payload = creds.to_json()
    # Per-PID + random uuid suffix so two concurrent CLIs (or a future
    # process that happens to get the same PID after a crash that left an
    # orphan tmp file) can never collide on the O_EXCL open. Orphan tmp
    # files are cosmetic clutter, recoverable manually with `rm
    # data/token.json.*.tmp`.
    tmp = token_path.with_suffix(
        f"{token_path.suffix}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    )
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        f = os.fdopen(fd, "w")
    except BaseException:
        # `fdopen` failed before taking ownership of the fd — close it
        # ourselves and bail. (Without this, ENOMEM-class failures during
        # `fdopen` would leak the descriptor.)
        os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise

    try:
        with f:
            f.write(payload)
            f.flush()
            # fsync before replace ensures the bytes are durable; without
            # this, a power loss between write and replace can leave an
            # empty file post-replace on some filesystems.
            os.fsync(f.fileno())
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    os.replace(str(tmp), str(token_path))
    # os.replace preserves the source file's mode (0o600 above).
    # fsync the containing directory so the rename is durable across a
    # power loss / OS crash. Without this, the file contents are durable
    # (we fsync'd the fd above) but the directory entry pointing to them
    # might not be flushed to stable storage. Best-effort: directory
    # fsync isn't supported on every filesystem (e.g. some Windows FS).
    try:
        dir_fd = os.open(str(token_path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _run_flow(credentials_path: Path) -> Credentials:
    """Run the localhost-callback OAuth flow and return fresh credentials.

    Uses the same flag set as the giftlist sister project:
      * ``access_type='offline'``  — required for a refresh_token
      * ``prompt='consent'``       — forces the consent screen so a refresh_token
                                     is returned every time (without it, the
                                     second connect only returns access_token
                                     and the long-lived grant is lost)
      * ``include_granted_scopes='true'``
    """
    if not credentials_path.exists():
        raise FileNotFoundError(
            f"OAuth client secrets not found at {credentials_path}. "
            "See docs/devlog/2026-04-28-manual-items-todo.md for setup."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), list(SCOPES))
    return flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )


def get_credentials(
    *,
    credentials_path: Optional[Path] = None,
    token_path: Optional[Path] = None,
    force_reauth: bool = False,
) -> Credentials:
    """Return valid Google credentials, running the OAuth flow if needed.

    Resolves ``DEFAULT_CREDENTIALS_PATH`` / ``DEFAULT_TOKEN_PATH`` inside the
    body (rather than via parameter defaults) so test fixtures and other
    callers can override the module-level constants via ``monkeypatch.setattr``
    and have the override actually take effect — Python binds default
    parameter values at function-definition time, which would otherwise pin
    the path to whatever the constant was at import.

    Behavior:
      * ``force_reauth=True`` — always run the OAuth flow fresh, overwriting
        any existing ``token.json``. This is the path the menu's
        "Re-authenticate" option uses.
      * Otherwise: load ``token.json`` if present; if expired and refreshable,
        refresh it; otherwise run the OAuth flow.
    """
    if credentials_path is None:
        credentials_path = DEFAULT_CREDENTIALS_PATH
    if token_path is None:
        token_path = DEFAULT_TOKEN_PATH

    if force_reauth:
        creds = _run_flow(credentials_path)
        _save_token(creds, token_path)
        return creds

    creds = _load_token(token_path)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds, token_path)
            return creds
        except Exception as exc:
            # Refresh can fail for several reasons: 7-day Testing-mode TTL,
            # revoked grant, network outage, clock skew. Fall through to the
            # interactive flow rather than masking the error — but log to
            # stderr so the user can tell why we're prompting again.
            sys.stderr.write(
                f"Token refresh failed ({type(exc).__name__}: {exc}); "
                "starting interactive auth.\n"
            )

    creds = _run_flow(credentials_path)
    _save_token(creds, token_path)
    return creds


def get_authenticated_email(creds: Credentials) -> Optional[str]:
    """Smoke-test the auth: ask Google's userinfo endpoint who we are.

    Raises on transport errors (network, scope revoked, HTTP failure) per
    cs.md — callers should surface those so the user knows why we couldn't
    confirm identity, rather than seeing a generic "could not fetch the
    email" message.

    Returns the email string. Returns ``None`` only in the well-formed-but-
    empty case where userinfo responded successfully but did not include an
    ``email`` field (e.g., the scope was technically granted but the IdP
    declined to share). The caller distinguishes these two outcomes by
    whether an exception was raised.
    """
    oauth2 = build("oauth2", "v2", credentials=creds, cache_discovery=False)
    info = oauth2.userinfo().get().execute()
    email = info.get("email")
    # Treat empty strings the same as missing — `_run_auth` displays the
    # value with `if email:`, and an empty-string round-trip would silently
    # log "Authenticated, but..." while looking like a valid string.
    return email if isinstance(email, str) and email else None
