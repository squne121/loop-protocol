"""
.claude/skills/issue-refinement-loop/scripts/tests/test_skill_runtime_exec_env_allowlist.py

Hermetic test proving that `scripts/agent-guards/skill_runtime_exec.py`'s
REAL `_sanitize_env()` function actually carries `LOOP_SPARK_MODE` /
`LOOP_SPARK_FALLBACK` / `LOOP_PLANNED_OPERATIONS_JSON` through to the child
process environment for the bare `preflight.run` command id, and strips
them for every other command id (Issue #2311 fix_delta / PR #2320 review
P0-1).

This calls the real, unmodified `_sanitize_env()` function directly (not a
reimplementation, not a fake) with a crafted `os.environ`, which is the
exact function the canonical executor's dispatch path invokes at
`env=_sanitize_env(project_root, args.command_id)` before spawning the
child `workflow_start_entry.py` process (see `_dispatch`/`main` in
`skill_runtime_exec.py`). A full end-to-end `uv run ... skill_runtime_exec.py
--command-id preflight.run ...` subprocess additionally requires canonical
main root / default branch / trusted repo binding preconditions that this
worktree's own checkout does not satisfy (this file itself is being edited
from inside a linked issue worktree, not canonical main root) -- exercising
those preconditions is out of scope for this hermetic unit boundary and is
already covered by this Issue's existing `command_registry`/
`skill_runtime_command_policy` migration-parity fixture tests. What matters
here -- the actual env-var carry-through decision -- is proven directly and
deterministically against the real function object.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[5]
_GUARDS_DIR = _REPO_ROOT / "scripts" / "agent-guards"
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import skill_runtime_exec as sre  # noqa: E402

_CAPABILITY_ENV_NAMES = (
    "LOOP_SPARK_MODE",
    "LOOP_SPARK_FALLBACK",
    "LOOP_PLANNED_OPERATIONS_JSON",
)


def test_sanitize_env_carries_capability_request_for_bare_preflight_run(monkeypatch):
    monkeypatch.setenv("LOOP_SPARK_MODE", "required")
    monkeypatch.setenv("LOOP_SPARK_FALLBACK", "forbidden")
    monkeypatch.setenv(
        "LOOP_PLANNED_OPERATIONS_JSON",
        '[{"phase": "p", "actor_role": "r", "operation": "issue_comment", "requires_mutation": true}]',
    )

    env = sre._sanitize_env("/fake/project/root", command_id="preflight.run")

    assert env["LOOP_SPARK_MODE"] == "required"
    assert env["LOOP_SPARK_FALLBACK"] == "forbidden"
    assert env["LOOP_PLANNED_OPERATIONS_JSON"] == (
        '[{"phase": "p", "actor_role": "r", "operation": "issue_comment", "requires_mutation": true}]'
    )


def test_sanitize_env_strips_capability_request_for_sibling_commands(monkeypatch):
    """Sibling anchor-comment-driven profiles first-hop into
    `run_refinement_preflight.py` directly and never consume this env-based
    capability request -- the allowlist addition must be scoped exactly to
    the bare `preflight.run` command id and not silently widen to its
    siblings."""
    monkeypatch.setenv("LOOP_SPARK_MODE", "required")
    monkeypatch.setenv("LOOP_SPARK_FALLBACK", "forbidden")
    monkeypatch.setenv("LOOP_PLANNED_OPERATIONS_JSON", "[]")

    for command_id in (
        "preflight.run.with_anchor",
        "preflight.run.with_human_context",
        "preflight.run.with_agent_report",
        "",
    ):
        env = sre._sanitize_env("/fake/project/root", command_id=command_id)
        for env_name in _CAPABILITY_ENV_NAMES:
            assert env_name not in env, f"{env_name} leaked into command_id={command_id!r}"


def test_sanitize_env_omits_capability_env_when_unset_for_bare_preflight_run(monkeypatch):
    """When the caller never set these three env vars at all, they must
    simply be absent from the sanitized env (not present as empty strings)
    -- `workflow_start_entry.py`'s `os.environ.get(...)` fallback then
    correctly resolves to `None`, which its fail-closed `environment_failure`
    path (Issue #2311 AC5 / PR #2320 review P0-1 item 2) depends on."""
    for env_name in _CAPABILITY_ENV_NAMES:
        monkeypatch.delenv(env_name, raising=False)

    env = sre._sanitize_env("/fake/project/root", command_id="preflight.run")

    for env_name in _CAPABILITY_ENV_NAMES:
        assert env_name not in env
