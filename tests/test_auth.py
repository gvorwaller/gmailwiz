"""Tests for `gmailwiz.auth` — corrupt token handling, path defaults.

The actual OAuth flow can't be tested headlessly (requires browser interaction),
so these focus on the deterministic helper paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gmailwiz import auth as gw_auth


def test_load_token_returns_none_when_file_missing(tmp_path):
    missing = tmp_path / "nonexistent" / "token.json"
    assert gw_auth._load_token(missing) is None


def test_load_token_returns_none_when_file_corrupt(tmp_path):
    """Corrupt JSON should be treated as 'no token' so the next call re-auths."""
    bad = tmp_path / "token.json"
    bad.write_text("{this is not valid json")
    assert gw_auth._load_token(bad) is None


def test_load_token_returns_none_when_json_missing_required_fields(tmp_path):
    """Valid JSON but not a credentials shape — also treated as missing."""
    bad = tmp_path / "token.json"
    bad.write_text('{"unrelated": "data"}')
    assert gw_auth._load_token(bad) is None


def test_default_credentials_path_anchors_to_repo_root():
    """credentials.json must resolve from the repo root, not from cwd.

    Pre-fix, this was `Path("credentials.json")` (relative). Running
    `python -m gmailwiz` from any directory other than the repo root would
    FileNotFoundError. The fix anchors via `Path(__file__).resolve().parent.parent`.
    """
    expected_dir = Path(__file__).resolve().parent.parent
    assert gw_auth.DEFAULT_CREDENTIALS_PATH.parent == expected_dir
    assert gw_auth.DEFAULT_CREDENTIALS_PATH.name == "credentials.json"
    assert gw_auth.DEFAULT_CREDENTIALS_PATH.is_absolute()


def test_oauthlib_relax_token_scope_is_set():
    """Importing auth must set OAUTHLIB_RELAX_TOKEN_SCOPE before oauthlib loads.

    Without this, `include_granted_scopes=True` causes oauthlib to raise on
    the legitimate scope-merge that Google performs. See auth.py top-of-module
    comment.
    """
    import os
    assert os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE") == "1"


# ---------------------------------------------------------------------------
# _save_token atomic write + permissions
# ---------------------------------------------------------------------------


import json
import os
import stat
from unittest.mock import MagicMock


def _fake_creds(payload: dict | None = None):
    """Build a Credentials-shaped stand-in whose `to_json` returns a fixed payload."""
    payload = payload or {"refresh_token": "rt", "client_id": "cid"}
    creds = MagicMock()
    creds.to_json.return_value = json.dumps(payload)
    return creds


def test_save_token_writes_file_with_payload(tmp_path):
    target = tmp_path / "token.json"
    creds = _fake_creds({"refresh_token": "abc", "client_id": "xyz"})
    gw_auth._save_token(creds, target)
    assert target.exists()
    assert json.loads(target.read_text()) == {"refresh_token": "abc", "client_id": "xyz"}


def test_save_token_sets_0600_permissions(tmp_path):
    target = tmp_path / "token.json"
    gw_auth._save_token(_fake_creds(), target)
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_save_token_creates_parent_dir(tmp_path):
    target = tmp_path / "nested" / "deep" / "token.json"
    gw_auth._save_token(_fake_creds(), target)
    assert target.exists()
    # Parent dir should be 0o700.
    parent_mode = stat.S_IMODE(target.parent.stat().st_mode)
    assert parent_mode == 0o700


def test_save_token_no_leftover_tmp_file(tmp_path):
    target = tmp_path / "token.json"
    gw_auth._save_token(_fake_creds(), target)
    leftovers = list(tmp_path.glob("token.json.*.tmp"))
    assert leftovers == [], f"unexpected leftover tmp files: {leftovers}"


def test_save_token_overwrites_existing(tmp_path):
    target = tmp_path / "token.json"
    target.write_text('{"old": "data"}')
    gw_auth._save_token(_fake_creds({"new": "data"}), target)
    assert json.loads(target.read_text()) == {"new": "data"}


def test_save_token_uses_pid_and_uuid_in_tmp_name(tmp_path, monkeypatch):
    """Tmp file must include the PID + uuid so two CLIs (or a recycled PID
    after a crash) can never collide on the O_EXCL open."""
    import re

    target = tmp_path / "token.json"
    captured = {}

    real_open = os.open
    def spy_open(path, flags, mode=0o777):
        if "token.json." in str(path) and str(path).endswith(".tmp"):
            captured["tmp_path"] = path
        return real_open(path, flags, mode)

    monkeypatch.setattr(gw_auth.os, "open", spy_open)
    gw_auth._save_token(_fake_creds(), target)
    assert "tmp_path" in captured
    # Format: token.json.<pid>.<8-hex-uuid>.tmp
    pattern = rf"token\.json\.{os.getpid()}\.[0-9a-f]{{8}}\.tmp$"
    assert re.search(pattern, str(captured["tmp_path"])), (
        f"unexpected tmp name: {captured['tmp_path']}"
    )


def test_get_credentials_headless_raises_when_token_missing(tmp_path, monkeypatch):
    """Headless mode: missing token.json must raise AuthRequired and never invoke the flow."""
    monkeypatch.setattr(gw_auth, "DEFAULT_TOKEN_PATH", tmp_path / "absent" / "token.json")

    def _flow_should_not_run(_path):
        raise AssertionError("interactive flow must not run when interactive=False")

    monkeypatch.setattr(gw_auth, "_run_flow", _flow_should_not_run)
    with pytest.raises(gw_auth.AuthRequired) as exc_info:
        gw_auth.get_credentials(interactive=False)
    assert "no token" in exc_info.value.reason


def test_get_credentials_headless_raises_on_refresh_failure(tmp_path, monkeypatch):
    """Expired token + refresh failure in headless mode → AuthRequired, no flow."""
    token_path = tmp_path / "token.json"

    fake_creds = MagicMock()
    fake_creds.valid = False
    fake_creds.expired = True
    fake_creds.refresh_token = "rt"

    def _boom(_request):
        raise RuntimeError("refresh token expired")

    fake_creds.refresh.side_effect = _boom

    monkeypatch.setattr(gw_auth, "_load_token", lambda _p: fake_creds)
    monkeypatch.setattr(gw_auth, "_save_token", lambda _c, _p: None)
    monkeypatch.setattr(
        gw_auth,
        "_run_flow",
        lambda _p: (_ for _ in ()).throw(AssertionError("flow must not run")),
    )

    with pytest.raises(gw_auth.AuthRequired) as exc_info:
        gw_auth.get_credentials(token_path=token_path, interactive=False)
    assert "refresh failed" in exc_info.value.reason


def test_get_credentials_headless_raises_when_no_refresh_token(tmp_path, monkeypatch):
    """A token without a refresh_token can't be refreshed — must raise in headless mode."""
    token_path = tmp_path / "token.json"

    fake_creds = MagicMock()
    fake_creds.valid = False
    fake_creds.expired = True
    fake_creds.refresh_token = None  # no refresh_token

    monkeypatch.setattr(gw_auth, "_load_token", lambda _p: fake_creds)
    monkeypatch.setattr(
        gw_auth,
        "_run_flow",
        lambda _p: (_ for _ in ()).throw(AssertionError("flow must not run")),
    )

    with pytest.raises(gw_auth.AuthRequired) as exc_info:
        gw_auth.get_credentials(token_path=token_path, interactive=False)
    assert "refresh_token" in exc_info.value.reason


def test_get_credentials_headless_returns_valid_creds(tmp_path, monkeypatch):
    """Sanity: a valid stored token returns successfully even in headless mode."""
    fake_creds = MagicMock()
    fake_creds.valid = True
    fake_creds.expired = False

    monkeypatch.setattr(gw_auth, "_load_token", lambda _p: fake_creds)
    monkeypatch.setattr(
        gw_auth,
        "_run_flow",
        lambda _p: (_ for _ in ()).throw(AssertionError("flow must not run")),
    )

    got = gw_auth.get_credentials(token_path=tmp_path / "t.json", interactive=False)
    assert got is fake_creds


def test_get_credentials_headless_refreshes_expired_token(tmp_path, monkeypatch):
    """Expired + refresh succeeds in headless mode: returns the refreshed creds."""
    fake_creds = MagicMock()
    fake_creds.valid = False
    fake_creds.expired = True
    fake_creds.refresh_token = "rt"
    fake_creds.refresh = MagicMock(return_value=None)

    monkeypatch.setattr(gw_auth, "_load_token", lambda _p: fake_creds)
    saved = {}
    monkeypatch.setattr(gw_auth, "_save_token", lambda c, p: saved.setdefault("ok", True))
    monkeypatch.setattr(
        gw_auth,
        "_run_flow",
        lambda _p: (_ for _ in ()).throw(AssertionError("flow must not run")),
    )

    got = gw_auth.get_credentials(token_path=tmp_path / "t.json", interactive=False)
    assert got is fake_creds
    assert fake_creds.refresh.called
    assert saved == {"ok": True}


def test_get_credentials_force_reauth_headless_is_rejected(tmp_path):
    """force_reauth=True + interactive=False is a contradiction — must raise immediately."""
    with pytest.raises(gw_auth.AuthRequired):
        gw_auth.get_credentials(
            token_path=tmp_path / "t.json",
            force_reauth=True,
            interactive=False,
        )


def test_save_token_succeeds_with_pre_fix_pid_only_orphan(tmp_path):
    """Regression guard for the uuid-suffix fix. Plants a tmp file at the
    EXACT path the pre-fix (PID-only) code would have created. Pre-fix,
    a save would have hit O_EXCL FileExistsError on this orphan; post-fix,
    the uuid randomization picks a different filename and the save succeeds.

    This test would FAIL on a PID-only implementation, proving the fix.
    """
    target = tmp_path / "token.json"
    # Pre-fix filename format: token.json.<pid>.tmp
    pre_fix_orphan = tmp_path / f"token.json.{os.getpid()}.tmp"
    pre_fix_orphan.write_text("orphan from previous PID-only run")

    gw_auth._save_token(_fake_creds({"v": 1}), target)
    assert json.loads(target.read_text()) == {"v": 1}
    # The pre-fix orphan is left untouched — different filename means
    # we never opened or replaced it.
    assert pre_fix_orphan.exists()
    assert pre_fix_orphan.read_text() == "orphan from previous PID-only run"
