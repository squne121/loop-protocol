"""scripts/claude-gpt/tests/test_actor_scoped_capability_probe.py

Issue #2340 AC2: actor/execution-substrate-scoped capability results.

Covers the specific failure mode this Issue exists to catch: root's own
`gh auth status` / `gh repo view` probe (`root_github_read`) passes, but the
consumer-equivalent probe that reuses the controlled executor's sanitized
env + trusted-host pin (`controlled_github_read`) fails -- and the workflow
must be routed to a typed `controlled_github_unavailable` BEFORE any
issue-editor / contract-update actor starts (rather than only discovering
this later as that actor's own `gh_issue_fetch_failed_rc_4`).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent.parent
_GUARDS_DIR = _REPO_ROOT / "scripts" / "agent-guards"

for _p in (_SCRIPTS_DIR, _GUARDS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import workflow_capability_preflight as wcp  # noqa: E402

_DEFAULT_REPO = "squne121/loop-protocol"


def _fake_completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _assess(monkeypatch, *, controlled_read_rc: int, github_auth: bool = True, github_repo_read: bool = True):
    monkeypatch.setattr(wcp, "_github_auth_ok", lambda: github_auth)
    monkeypatch.setattr(wcp, "_github_repo_read_ok", lambda repo: github_repo_read)
    monkeypatch.setattr(wcp.trusted_uv_mod, "check_trusted_uv", lambda project_root: {
        "status": wcp.trusted_uv_mod.STATUS_OK, "reason": "resolved", "resolved_path": "/fake/uv"
    })
    monkeypatch.setattr(wcp, "_run_env_only_preflight", lambda: {})

    captured_calls = []

    def _fake_run(argv, **kwargs):
        captured_calls.append((argv, kwargs))
        return _fake_completed(controlled_read_rc)

    monkeypatch.setattr(wcp.subprocess, "run", _fake_run)

    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_DEFAULT_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations=[],
    )
    return result, captured_calls


# =============================================================================
# GIVEN root github read PASSES but the controlled-equivalent probe FAILS,
# WHEN assess() runs, THEN it must route to typed controlled_github_unavailable
# BEFORE any downstream actor starts (AC2).
# =============================================================================


def test_root_read_pass_controlled_read_fail_blocks_before_downstream_actor(monkeypatch):
    result, calls = _assess(monkeypatch, controlled_read_rc=1, github_auth=True, github_repo_read=True)

    actor_caps = result["actor_capabilities"]
    assert actor_caps["root_github_read"]["status"] == "ready"
    assert actor_caps["controlled_github_read"]["status"] == "unavailable"
    assert actor_caps["controlled_github_read"]["reason_code"] == "controlled_github_unavailable"
    assert result["decision"] == "blocked"
    assert any("controlled_github_unavailable" in r for r in result["reasons"])
    # The probe itself must have actually been invoked (not skipped).
    assert len(calls) == 1


def test_controlled_read_probe_uses_hostname_pinned_sanitized_env(monkeypatch):
    """Issue #2340 fix_delta P0-1 (PR #2357 review, 2026-08-27): the
    controlled_github_read probe strips execution/log-hygiene noise
    (GH_HOST / GH_DEBUG / etc.) but PRESERVES the launcher-shared GitHub
    credential carrier (GH_TOKEN / GH_CONFIG_DIR) -- this probe must
    observe the SAME credential availability the downstream controlled
    executor write helpers do post-fix, or a `ready` verdict here would not
    actually predict whether the write can authenticate."""
    monkeypatch.setenv("GH_TOKEN", "ambient-shared-launcher-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/fake/native/gh/config")
    monkeypatch.setenv("GH_HOST", "evil.example.com")
    _result, calls = _assess(monkeypatch, controlled_read_rc=0)

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert "--hostname" in argv
    assert "github.com" in argv
    env = kwargs.get("env")
    assert env is not None
    for key in wcp._ENV_SANITIZE_KEYS:
        assert key not in env
    assert env.get("GH_HOST") != "evil.example.com"
    assert env.get("GH_TOKEN") == "ambient-shared-launcher-token"
    assert env.get("GH_CONFIG_DIR") == "/fake/native/gh/config"


def test_both_reads_ready_yields_ready_decision(monkeypatch):
    result, _calls = _assess(monkeypatch, controlled_read_rc=0)
    actor_caps = result["actor_capabilities"]
    assert actor_caps["root_github_read"]["status"] == "ready"
    assert actor_caps["controlled_github_read"]["status"] == "ready"
    assert result["decision"] == "ready"


def test_root_read_unavailable_also_blocks(monkeypatch):
    result, _calls = _assess(monkeypatch, controlled_read_rc=0, github_auth=False, github_repo_read=False)
    assert result["actor_capabilities"]["root_github_read"]["status"] == "unavailable"
    assert result["actor_capabilities"]["root_github_read"]["reason_code"] == "root_github_auth_unavailable"
    assert result["decision"] == "blocked"


# =============================================================================
# delegated_research_agy (advisory, degrades to a non-AGY fallback route
# rather than blocking outright -- AC3 consumes this as its input).
# =============================================================================


def test_delegated_research_agy_ready_when_binary_present(monkeypatch):
    monkeypatch.setattr(wcp.shutil, "which", lambda name: "/usr/bin/agy" if name == "agy" else None)
    result, _calls = _assess(monkeypatch, controlled_read_rc=0)
    assert result["actor_capabilities"]["delegated_research_agy"]["status"] == "ready"


def test_delegated_research_agy_degrades_with_fallback_route_when_absent(monkeypatch):
    monkeypatch.setattr(wcp.shutil, "which", lambda name: None)
    result, _calls = _assess(monkeypatch, controlled_read_rc=0)
    entry = result["actor_capabilities"]["delegated_research_agy"]
    assert entry["status"] == "degraded"
    # Existing gemini-cli-headless-delegation taxonomy value reused verbatim
    # (no new reason code invented -- In Scope item 4 / AC3).
    assert entry["reason_code"] == "agy_not_found"
    assert entry["fallback_route"] == "codebase_investigator_non_agy"


# =============================================================================
# spark_delegation mirrors the existing _spark_capability() verdict.
# =============================================================================


def test_spark_delegation_maps_not_required_to_ready(monkeypatch):
    result, _calls = _assess(monkeypatch, controlled_read_rc=0)
    assert result["actor_capabilities"]["spark_delegation"]["status"] == "ready"


def test_spark_delegation_maps_fallback_only_to_degraded_with_route():
    entry = wcp._spark_delegation_capability(wcp.SPARK_FALLBACK_ONLY)
    assert entry["status"] == "degraded"
    assert entry["fallback_route"] == "non_spark_agent"


def test_spark_delegation_maps_unavailable_to_unavailable():
    entry = wcp._spark_delegation_capability(wcp.SPARK_UNAVAILABLE)
    assert entry["status"] == "unavailable"


def test_spark_delegation_maps_eligible_to_ready():
    entry = wcp._spark_delegation_capability(wcp.SPARK_ELIGIBLE)
    assert entry["status"] == "ready"


# =============================================================================
# AC5: no credential/token content leaks into any actor_capabilities entry.
# =============================================================================


def test_actor_capabilities_never_carry_secret_like_values(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "super-secret-token-value")
    result, _calls = _assess(monkeypatch, controlled_read_rc=1)
    import json as _json

    serialized = _json.dumps(result["actor_capabilities"])
    assert "super-secret-token-value" not in serialized


def test_controlled_github_read_probe_exception_reports_unavailable(monkeypatch):
    monkeypatch.setattr(wcp, "_github_auth_ok", lambda: True)
    monkeypatch.setattr(wcp, "_github_repo_read_ok", lambda repo: True)
    monkeypatch.setattr(wcp.trusted_uv_mod, "check_trusted_uv", lambda project_root: {
        "status": wcp.trusted_uv_mod.STATUS_OK, "reason": "resolved", "resolved_path": "/fake/uv"
    })
    monkeypatch.setattr(wcp, "_run_env_only_preflight", lambda: {})

    def _raise(*_a, **_k):
        raise OSError("gh binary not found")

    monkeypatch.setattr(wcp.subprocess, "run", _raise)

    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_DEFAULT_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations=[],
    )
    assert result["actor_capabilities"]["controlled_github_read"]["status"] == "unavailable"
    assert result["decision"] == "blocked"
