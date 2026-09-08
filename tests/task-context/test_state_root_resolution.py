"""LOOP_TASK_CONTEXT_STATE_ROOT / XDG_STATE_HOME resolution (Issue #2563
In Scope: state-root resolution rules)."""

from __future__ import annotations

import pathlib

import pytest

import task_context_config as config


def test_given_relative_override_when_resolving_state_root_then_rejected(monkeypatch):
    monkeypatch.setenv(config.STATE_ROOT_ENV_VAR, "relative/path")
    with pytest.raises(ValueError):
        config.resolve_state_root()


def test_given_absolute_override_when_resolving_state_root_then_used_verbatim(monkeypatch, tmp_path):
    override = tmp_path / "explicit-root"
    monkeypatch.setenv(config.STATE_ROOT_ENV_VAR, str(override))
    assert config.resolve_state_root() == override


def test_given_no_override_and_no_xdg_state_home_when_resolving_then_default_is_home_local_state(
    monkeypatch, tmp_path
):
    monkeypatch.delenv(config.STATE_ROOT_ENV_VAR, raising=False)
    monkeypatch.delenv(config.XDG_STATE_HOME_ENV_VAR, raising=False)
    fake_home = tmp_path / "home"
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: fake_home))
    root = config.resolve_state_root(cwd=str(pathlib.Path(__file__).resolve().parents[2]))
    assert str(root).startswith(str(fake_home / ".local" / "state"))
    assert "loop-protocol" in str(root)
    assert "task-context" in str(root)
    assert "v1" in root.parts


def test_given_relative_xdg_state_home_when_resolving_then_treated_as_unset(monkeypatch, tmp_path):
    monkeypatch.delenv(config.STATE_ROOT_ENV_VAR, raising=False)
    monkeypatch.setenv(config.XDG_STATE_HOME_ENV_VAR, "relative/xdg")
    fake_home = tmp_path / "home2"
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: fake_home))
    root = config.resolve_state_root(cwd=str(pathlib.Path(__file__).resolve().parents[2]))
    assert str(root).startswith(str(fake_home / ".local" / "state"))


def test_given_absolute_xdg_state_home_when_resolving_then_used(monkeypatch, tmp_path):
    monkeypatch.delenv(config.STATE_ROOT_ENV_VAR, raising=False)
    xdg = tmp_path / "custom-xdg-state"
    monkeypatch.setenv(config.XDG_STATE_HOME_ENV_VAR, str(xdg))
    root = config.resolve_state_root(cwd=str(pathlib.Path(__file__).resolve().parents[2]))
    assert str(root).startswith(str(xdg))


def test_given_state_root_when_db_path_computed_then_it_ends_with_canonical_filename(state_root):
    path = config.db_path()
    assert path.name == "task-context.sqlite3"
    assert path.parent == state_root
