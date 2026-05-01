"""Shared pytest fixtures.

Adds the project root to `sys.path` so tests can ``import gmailwiz`` without
needing an installed package, and prevents tests from accidentally writing to
the project's real `data/` directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _isolate_gmailwiz_state(tmp_path, monkeypatch):
    """Redirect every default project-state path to a tmp dir.

    Belt-and-suspenders: even if a test imports `auth` or `db` and uses
    DEFAULT_TOKEN_PATH / DEFAULT_DB_PATH directly, it can't clobber the
    real `data/` files. Tests that want to exercise actual paths can
    override by passing explicit paths.
    """
    fake_data = tmp_path / "data"
    (fake_data / "db").mkdir(parents=True, exist_ok=True)

    from gmailwiz import auth as gw_auth
    from gmailwiz import db as gw_db

    monkeypatch.setattr(gw_auth, "DEFAULT_TOKEN_PATH", fake_data / "token.json")
    monkeypatch.setattr(gw_db, "DEFAULT_DB_PATH", fake_data / "db" / "state.db")
