"""Regression test for Issue #1886 AC7 fix-delta (iteration 7): the Codex
CLI child spawn session id must be derivable from the on-disk rollout log's
own *content* linkage (``payload.parent_thread_id`` / ``payload.session_id``
recorded in the child's own ``session_meta`` record), not solely from a
filename-substring match against the parent's ``thread_id``.

This runner always invokes ``codex exec --ephemeral`` (see
``run_structured_codex``) -- analogous to Claude Code's
``--no-session-persistence`` -- which suppresses persistence of the
*parent* thread's own rollout log. No file's filename will ever contain the
parent's ``thread_id`` in that case, so the prior filename-substring-only
lookup in ``extract_codex_child_session_id`` was structurally unable to
ever return a value for this runner's own invocation shape, making
``native_spawn_event_observed`` permanently ``False`` for the ``codex_cli``
runtime regardless of whether a real spawn happened.

This is a dedicated new test file (not appended to the pre-existing general
smoke suite), per Issue #1960's Current Validated Scope / Issue #1285 /
PR #1305 VC contract convention (also followed by
``test_run_worktree_agent_runtime_smoke_stream_child_session_id.py`` for the
analogous Claude Code fix in iteration 6).

This test is fully hermetic: it writes synthetic rollout log files (shaped
exactly like real, live-observed Codex CLI 0.146.0 rollout logs for a
spawned ``codebase-investigator`` sub-agent thread) under a temporary
``~/.codex/sessions`` directory (via monkeypatching ``Path.home``) -- no
real ``codex`` binary or network access involved.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_worktree_agent_runtime_smoke_codex_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_child_rollout_log(sessions_dir: Path, *, own_id: str, parent_thread_id: str) -> Path:
    """Shaped after a real, live-observed rollout log written for a spawned
    Codex CLI sub-agent thread: the file's own id is embedded both in its
    filename and in the first ``session_meta`` record's ``payload.id``; the
    spawning parent's own ``thread_id`` is recorded as
    ``payload.parent_thread_id`` (and duplicated as ``payload.session_id``)
    -- never in the filename."""
    day_dir = sessions_dir / "2026" / "08" / "06"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-2026-08-06T21-51-04-{own_id}.jsonl"
    lines = [
        {
            "timestamp": "2026-08-06T12:51:04.552Z",
            "type": "session_meta",
            "payload": {
                "session_id": parent_thread_id,
                "id": own_id,
                "parent_thread_id": parent_thread_id,
                "cwd": "/home/example/worktree",
                "thread_source": "subagent",
                "agent_role": "codebase-investigator",
            },
        },
        {"timestamp": "2026-08-06T12:51:05.000Z", "type": "turn_context", "payload": {}},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


def test_extract_codex_child_session_id_via_content_linked_rollout_log_under_ephemeral(
    fake_home: Path, monkeypatch
):
    """Fallback path (iteration 7 fix): under ``--ephemeral``, no rollout
    log's *filename* ever contains the parent's own ``thread_id`` -- the
    child's own rollout log must instead be located by its content-level
    ``parent_thread_id`` linkage, and the child's own thread id (its
    ``payload.id``) returned as evidence of a distinct native spawn."""
    module = _load_module()
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: fake_home))

    parent_thread_id = "019fd720-2814-7362-b530-cb659cec97f8"
    child_own_id = "019fd720-9458-7703-b3f0-07aac6e6b350"
    _write_child_rollout_log(
        fake_home / ".codex" / "sessions", own_id=child_own_id, parent_thread_id=parent_thread_id
    )

    child_session_id = module.extract_codex_child_session_id(parent_thread_id)
    assert child_session_id == child_own_id
    assert child_session_id != parent_thread_id


def test_extract_codex_child_session_id_prefers_filename_match_when_present(
    fake_home: Path, monkeypatch
):
    """Primary path (unchanged): if a rollout log's filename directly
    contains the parent's ``thread_id`` (the parent's own transcript was
    persisted, e.g. a future non-``--ephemeral`` caller), the existing
    ``spawn_agent`` function_call/function_call_output parsing must still
    take precedence over the content-linked fallback."""
    module = _load_module()
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: fake_home))

    parent_thread_id = "parent-thread-zzzz"
    sessions_dir = fake_home / ".codex" / "sessions" / "2026" / "08" / "06"
    sessions_dir.mkdir(parents=True)
    parent_log = sessions_dir / f"rollout-2026-08-06T00-00-00-{parent_thread_id}.jsonl"
    lines = [
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "spawn_agent", "call_id": "call_1"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": json.dumps({"agent_id": "spawned-agent-id-from-tool-output"}),
            },
        },
    ]
    parent_log.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

    child_session_id = module.extract_codex_child_session_id(parent_thread_id)
    assert child_session_id == "spawned-agent-id-from-tool-output"


def test_extract_codex_child_session_id_returns_none_without_any_linkage(
    fake_home: Path, monkeypatch
):
    """Fail-closed: no filename match and no rollout log content links back
    to the given parent id -> ``None``, never a guess."""
    module = _load_module()
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: fake_home))

    _write_child_rollout_log(
        fake_home / ".codex" / "sessions",
        own_id="unrelated-child-id",
        parent_thread_id="some-other-parent-thread-id",
    )

    assert module.extract_codex_child_session_id("this-parent-id-has-no-match") is None


def test_extract_codex_child_session_id_returns_none_for_empty_parent_id(
    fake_home: Path, monkeypatch
):
    module = _load_module()
    monkeypatch.setattr(module.Path, "home", classmethod(lambda cls: fake_home))
    assert module.extract_codex_child_session_id(None) is None
    assert module.extract_codex_child_session_id("") is None
