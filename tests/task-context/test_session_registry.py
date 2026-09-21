"""Issue #2566 fix_delta P1-B iteration 2 -- pure/read-only unit tests for
`task_context_session_registry.py` (Claude Code's own on-disk session
registry, read-only consumption). No DB, no `task_context_hook_flows`
integration -- see `test_pre_tool_use_guard.py` for the guard-level
integration tests exercising this module through
`on_pre_tool_use`/`SendMessage`."""

from __future__ import annotations

import json

import task_context_session_registry as session_registry


def _write_record(directory, *, pid: int, session_id, name) -> None:
    record = {"pid": pid, "sessionId": session_id, "name": name}
    (directory / f"{pid}.json").write_text(json.dumps(record), encoding="utf-8")


def test_given_unique_name_match_when_resolving_then_returns_session_id(tmp_path):
    _write_record(tmp_path, pid=1, session_id="sess-abc", name="alpha")

    assert session_registry.resolve_session_name_to_claude_session_id("alpha", registry_dir=tmp_path) == "sess-abc"


def test_given_no_match_when_resolving_then_returns_none(tmp_path):
    _write_record(tmp_path, pid=1, session_id="sess-abc", name="alpha")

    assert session_registry.resolve_session_name_to_claude_session_id("beta", registry_dir=tmp_path) is None


def test_given_colliding_names_when_resolving_then_returns_none(tmp_path):
    _write_record(tmp_path, pid=1, session_id="sess-abc", name="dup")
    _write_record(tmp_path, pid=2, session_id="sess-def", name="dup")

    assert session_registry.resolve_session_name_to_claude_session_id("dup", registry_dir=tmp_path) is None


def test_given_missing_registry_dir_when_resolving_then_returns_none_fail_closed(tmp_path):
    missing_dir = tmp_path / "does-not-exist"

    assert session_registry.resolve_session_name_to_claude_session_id("alpha", registry_dir=missing_dir) is None


def test_given_corrupt_json_file_when_resolving_then_skips_it_without_raising(tmp_path):
    (tmp_path / "1.json").write_text("{not valid json", encoding="utf-8")
    _write_record(tmp_path, pid=2, session_id="sess-def", name="beta")

    assert session_registry.resolve_session_name_to_claude_session_id("beta", registry_dir=tmp_path) == "sess-def"


def test_given_non_dict_json_file_when_resolving_then_skips_it_without_raising(tmp_path):
    (tmp_path / "1.json").write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    _write_record(tmp_path, pid=2, session_id="sess-def", name="beta")

    assert session_registry.resolve_session_name_to_claude_session_id("beta", registry_dir=tmp_path) == "sess-def"


def test_given_empty_to_when_resolving_then_returns_none(tmp_path):
    _write_record(tmp_path, pid=1, session_id="sess-abc", name="")

    assert session_registry.resolve_session_name_to_claude_session_id("", registry_dir=tmp_path) is None
    assert session_registry.resolve_session_name_to_claude_session_id(None, registry_dir=tmp_path) is None


def test_given_record_missing_session_id_when_resolving_then_returns_none(tmp_path):
    (tmp_path / "1.json").write_text(json.dumps({"pid": 1, "name": "alpha"}), encoding="utf-8")

    assert session_registry.resolve_session_name_to_claude_session_id("alpha", registry_dir=tmp_path) is None


def test_given_no_explicit_registry_dir_when_resolving_then_uses_env_var_override(tmp_path, monkeypatch):
    monkeypatch.setenv(session_registry.SESSION_REGISTRY_DIR_ENV_VAR, str(tmp_path))
    _write_record(tmp_path, pid=1, session_id="sess-abc", name="alpha")

    assert session_registry.resolve_session_name_to_claude_session_id("alpha") == "sess-abc"


# ---------------------------------------------------------------------------
# Issue #2567 AC5: CLAUDE_CONFIG_DIR precedence for the default registry dir
# (Claude-GPT's isolated CLAUDE_CONFIG_DIR relocates Claude Code's own
# on-disk session registry out from under ~/.claude/sessions).
# ---------------------------------------------------------------------------


def test_given_registry_override_and_claude_config_dir_both_set_when_resolving_default_dir_then_override_wins(
    tmp_path, monkeypatch
):
    override_dir = tmp_path / "override"
    claude_config_dir = tmp_path / "claude-config"
    monkeypatch.setenv(session_registry.SESSION_REGISTRY_DIR_ENV_VAR, str(override_dir))
    monkeypatch.setenv(session_registry.CLAUDE_CONFIG_DIR_ENV_VAR, str(claude_config_dir))

    assert session_registry.resolve_session_registry_dir() == override_dir


def test_given_no_override_and_claude_config_dir_set_when_resolving_default_dir_then_uses_config_dir_sessions(
    tmp_path, monkeypatch
):
    claude_config_dir = tmp_path / "claude-gpt-isolated-config"
    monkeypatch.delenv(session_registry.SESSION_REGISTRY_DIR_ENV_VAR, raising=False)
    monkeypatch.setenv(session_registry.CLAUDE_CONFIG_DIR_ENV_VAR, str(claude_config_dir))

    assert session_registry.resolve_session_registry_dir() == claude_config_dir / "sessions"


def test_given_no_override_and_no_claude_config_dir_when_resolving_default_dir_then_uses_home_claude_sessions(
    tmp_path, monkeypatch
):
    fake_home = tmp_path / "home"
    monkeypatch.delenv(session_registry.SESSION_REGISTRY_DIR_ENV_VAR, raising=False)
    monkeypatch.delenv(session_registry.CLAUDE_CONFIG_DIR_ENV_VAR, raising=False)
    monkeypatch.setattr(
        session_registry.pathlib.Path, "home", classmethod(lambda cls: fake_home)
    )

    assert session_registry.resolve_session_registry_dir() == fake_home / ".claude" / "sessions"


def test_given_claude_config_dir_session_registry_when_resolving_name_then_resolves_to_session_id(
    tmp_path, monkeypatch
):
    """End-to-end AC5 regression: a Claude-GPT-shaped ``CLAUDE_CONFIG_DIR``
    (isolated config root) with a real on-disk ``sessions/`` registry under
    it must resolve a peer name -> sessionId the same way Native Claude's
    ``~/.claude/sessions`` does."""
    claude_config_dir = tmp_path / "claude-gpt-isolated-config"
    registry_dir = claude_config_dir / "sessions"
    registry_dir.mkdir(parents=True)
    monkeypatch.delenv(session_registry.SESSION_REGISTRY_DIR_ENV_VAR, raising=False)
    monkeypatch.setenv(session_registry.CLAUDE_CONFIG_DIR_ENV_VAR, str(claude_config_dir))
    _write_record(registry_dir, pid=1, session_id="sess-gpt-1", name="peer-gpt")

    assert session_registry.resolve_session_name_to_claude_session_id("peer-gpt") == "sess-gpt-1"
