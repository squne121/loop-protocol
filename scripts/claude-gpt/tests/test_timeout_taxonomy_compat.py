"""scripts/claude-gpt/tests/test_timeout_taxonomy_compat.py

Issue #2340 AC8: this Issue does not reimplement nested-timeout /
shared-deadline ownership (Issue #2322's scope). It only fixes exception
handling correctness (a `gh` subprocess hang must fail closed into this
module's EXISTING `unavailable`/`False` contract instead of raising an
uncaught `subprocess.TimeoutExpired`), and consumes the existing timeout
reason taxonomy verbatim wherever this Issue's new code touches it
(`agy_timeout` from `.claude/skills/gemini-cli-headless-delegation`,
`transport_error` from `.claude/skills/implement-issue/scripts/
update_branch.py`'s `UPDATE_BRANCH_RESULT_V1` contract).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent.parent
_GUARDS_DIR = _REPO_ROOT / "scripts" / "agent-guards"
_ISSUE_REFINEMENT_LOOP_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
_UPDATE_BRANCH_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "implement-issue" / "scripts"

for _p in (_SCRIPTS_DIR, _GUARDS_DIR, _ISSUE_REFINEMENT_LOOP_SCRIPTS, _UPDATE_BRANCH_SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import workflow_capability_preflight as wcp  # noqa: E402
import root_entry_router as router  # noqa: E402
import update_branch  # noqa: E402


def _timeout_expired():
    return subprocess.TimeoutExpired(cmd=["gh"], timeout=15)


# =============================================================================
# GIVEN a `gh` subprocess hangs (TimeoutExpired), WHEN this Issue's new/fixed
# probes run, THEN they fail closed into the EXISTING return contract instead
# of propagating an uncaught exception (Issue #2340 AC8).
# =============================================================================


def test_controlled_github_read_capability_fails_closed_on_timeout(monkeypatch):
    def _raise(*_a, **_k):
        raise _timeout_expired()

    monkeypatch.setattr(wcp.subprocess, "run", _raise)
    result = wcp._controlled_github_read_capability("squne121/loop-protocol")
    assert result["status"] == "unavailable"
    assert result["reason_code"] == "controlled_github_unavailable"


def test_github_auth_ok_fails_closed_on_timeout_not_uncaught_exception(monkeypatch):
    def _raise(*_a, **_k):
        raise _timeout_expired()

    monkeypatch.setattr(wcp.subprocess, "run", _raise)
    assert wcp._github_auth_ok() is False


def test_github_repo_read_ok_fails_closed_on_timeout_not_uncaught_exception(monkeypatch):
    def _raise(*_a, **_k):
        raise _timeout_expired()

    monkeypatch.setattr(wcp.subprocess, "run", _raise)
    assert wcp._github_repo_read_ok("squne121/loop-protocol") is False


def test_assess_never_produces_a_decision_value_outside_the_fixed_enum_under_timeout(monkeypatch):
    """A subprocess timeout anywhere in assess()'s probes must still resolve
    to one of the existing `ready`/`degraded`/`blocked` decision values --
    this Issue introduces no fourth `timeout` decision state (no parallel
    timeout-ownership state machine)."""
    def _raise(*_a, **_k):
        raise _timeout_expired()

    monkeypatch.setattr(wcp, "_github_auth_ok", lambda: True)
    monkeypatch.setattr(wcp, "_github_repo_read_ok", lambda repo: True)
    monkeypatch.setattr(
        wcp.trusted_uv_mod,
        "check_trusted_uv",
        lambda project_root: {
            "status": wcp.trusted_uv_mod.STATUS_OK, "reason": "resolved", "resolved_path": "/fake/uv",
        },
    )
    monkeypatch.setattr(wcp, "_run_env_only_preflight", lambda: {})
    monkeypatch.setattr(wcp.subprocess, "run", _raise)

    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo="squne121/loop-protocol",
        spark_mode=None,
        spark_fallback=None,
        planned_operations=[],
    )
    assert result["decision"] in ("ready", "degraded", "blocked")
    assert result["decision"] == "blocked"  # controlled_github_read failed closed


# =============================================================================
# Existing taxonomy values this Issue consumes (not reimplements) stay
# available and unrenamed.
# =============================================================================


def test_agy_timeout_taxonomy_value_still_consumed_by_advisory_route():
    result = router.resolve_agy_advisory_route(
        failure_class="agy_timeout", agy_required=False, fallback_allowed=True
    )
    assert result["reason_code"] == "agy_timeout"


def test_update_branch_transport_error_reason_code_unrenamed():
    """`update_branch.py` (Issue #1429's `UPDATE_BRANCH_RESULT_V1` contract,
    referenced by `implement-issue/SKILL.md`'s `## update_branch Contract`)
    owns its own timeout/transport-error classification -- Issue #2340 does
    not touch or reimplement it. This pins the exact constant value so a
    future accidental rename in that module is caught here too."""
    assert update_branch.REASON_TRANSPORT_ERROR == "transport_error"


def test_no_new_competing_timeout_ownership_module_introduced():
    """Issue #2322 (not yet merged as of this Issue) owns shared-deadline
    unification. This Issue must not introduce a competing/duplicate
    deadline-management module of its own."""
    forbidden_names = ("shared_deadline", "nested_timeout_manager", "timeout_ownership")
    for name in forbidden_names:
        assert not (_SCRIPTS_DIR / f"{name}.py").exists()
        assert not (_GUARDS_DIR / f"{name}.py").exists()
