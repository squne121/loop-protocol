"""Shared fixtures for Task Context v1 deterministic tests (Issue #2563).

``scripts/task-context`` is a hyphenated directory name and therefore
cannot be imported as a normal Python package (``import
scripts.task_context...``). Instead we add the directory (and its
``migrations`` subdirectory) to ``sys.path`` once here and import the
uniquely-prefixed ``task_context_*`` modules by bare name. Test modules in
this directory rely on this conftest having already run (pytest imports
``conftest.py`` before sibling test modules in the same directory).
"""

from __future__ import annotations

import pathlib
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts" / "task-context"
_MIGRATIONS_DIR = _SCRIPTS_DIR / "migrations"

for _dir in (str(_SCRIPTS_DIR), str(_MIGRATIONS_DIR)):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import task_context_config as config  # noqa: E402
import task_context_db as db  # noqa: E402
import task_context_migration_runner as migration_runner  # noqa: E402

REPO_ROOT = _REPO_ROOT
SCRIPTS_DIR = _SCRIPTS_DIR
MIGRATIONS_DIR = _MIGRATIONS_DIR


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    """Point LOOP_TASK_CONTEXT_STATE_ROOT at an isolated tmp directory so
    tests never touch a real developer machine's Task Context DB."""
    root = tmp_path / "task-context-state-root"
    monkeypatch.setenv(config.STATE_ROOT_ENV_VAR, str(root))
    return root


@pytest.fixture
def db_file(state_root):
    return config.db_path()


@pytest.fixture
def conn(db_file):
    connection = db.connect(db_file)
    migration_runner.migrate(connection)
    yield connection
    connection.close()
