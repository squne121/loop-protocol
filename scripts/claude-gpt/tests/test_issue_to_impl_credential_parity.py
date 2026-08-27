"""scripts/claude-gpt/tests/test_issue_to_impl_credential_parity.py

Issue #2340 AC6: system regression reproducing the #2332-origin failure
class (controlled GitHub unavailable -- AGY/Herdr runtime-only parts of
#2332 are already resolved by PR #2349 and excluded here) and the
#2330-origin Spark `fallback_only` failure class, through the REAL
production `workflow_capability_preflight.py` <-> `root_entry_router.py`
producer/consumer boundary (real subprocess dispatch, not a re-implemented
fake), extending the PR #2325 `issue_to_impl` harness family
(`test_runtime_smoke_issue_to_impl.py`) with this credential-parity-focused
slice.

`gh` is shadowed via a PATH-prepended fake binary (`fixtures/fake_gh.py`,
the SAME fixture `test_live_issue_create_canary.py` /
`test_runtime_smoke_issue_to_impl.py` already use) -- production code is
never modified or monkeypatched at the Python-object level for these tests;
only the process environment (PATH / FAKE_GH_* env vars) is controlled.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent.parent
_SKILLS_SCRIPTS_DIR = _REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
_FAKE_GH_PY = _TESTS_DIR / "fixtures" / "fake_gh.py"

if str(_SKILLS_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILLS_SCRIPTS_DIR))

import root_entry_router as router  # noqa: E402

_DEFAULT_REPO = "squne121/loop-protocol"


@pytest.fixture()
def fake_gh_env(tmp_path, monkeypatch):
    """Shadow `gh` on PATH with the shared deterministic fixture (Issue
    #2340 AC6). Root-level `gh auth status` / `gh repo view` succeed by
    default (`FAKE_GH_AUTH_OK` / `FAKE_GH_REPO_READ_OK` default to "1");
    the NEW `gh api --hostname github.com repos/<repo> --jq {name}` probe
    this Issue's AC2 `controlled_github_read` adds is intentionally NOT
    understood by the shared fixture ("unsupported fake gh args", exit 1),
    which is itself the exact fixture shape AC2 needs: root read passes
    while the consumer-equivalent probe fails."""
    fake_bin_dir = tmp_path / "fake-bin"
    fake_bin_dir.mkdir()
    wrapper = fake_bin_dir / "gh"
    wrapper.write_text(f'#!/bin/sh\nexec python3 "{_FAKE_GH_PY}" "$@"\n', encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    state_path = tmp_path / "fake_gh_state.json"
    monkeypatch.setenv("PATH", f"{fake_bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_GH_STATE", str(state_path))
    monkeypatch.setenv("FAKE_GH_AUTH_OK", "1")
    monkeypatch.setenv("FAKE_GH_REPO_READ_OK", "1")
    # Default: the consumer-equivalent probe also succeeds. Tests that need
    # the AC2 root-passes/controlled-fails asymmetry override this
    # explicitly to "0".
    monkeypatch.setenv("FAKE_GH_CONTROLLED_READ_OK", "1")
    return {"state_path": state_path}


def _strip_proxy_binary_from_path(monkeypatch) -> None:
    """Deterministically force `binary_available: false` for the Spark
    route regardless of whether this dev/CI machine happens to have
    `claude-code-proxy` installed, by removing that binary's directory from
    PATH for the subprocess this test drives (never touches production
    code -- only this test's own process environment)."""
    proxy_path = shutil.which("claude-code-proxy")
    if not proxy_path:
        return
    excluded_dir = str(Path(proxy_path).resolve().parent)
    current_path = os.environ.get("PATH", "")
    filtered = os.pathsep.join(p for p in current_path.split(os.pathsep) if p and p != excluded_dir)
    monkeypatch.setenv("PATH", filtered)


# =============================================================================
# #2332-origin failure class (excluding the already-resolved Herdr/runtime-
# only part -- PR #2349): controlled GitHub unavailable, classified BEFORE
# any downstream issue-editor / contract-update actor starts.
# =============================================================================


def test_controlled_github_unavailable_routes_typed_blocked_via_real_producer(fake_gh_env, monkeypatch):
    """GIVEN root `gh auth status` / `gh repo view` succeed but the
    consumer-equivalent probe fails, WHEN the REAL
    `workflow_capability_preflight.py` producer runs (real subprocess, not a
    Python-level fake), THEN `root_entry_router.capability_preflight_result()`
    reports `decision: blocked` with a typed `controlled_github_unavailable`
    reason -- the same failure mode that used to only surface downstream as
    issue-editor's own `gh_issue_fetch_failed_rc_4` (Issue #2332)."""
    monkeypatch.setenv("FAKE_GH_CONTROLLED_READ_OK", "0")
    result = router.capability_preflight_result(
        repo=_DEFAULT_REPO, spark_mode=None, spark_fallback=None, planned_operations=[]
    )
    assert result["decision"] == "blocked"
    assert any("controlled_github_unavailable" in r for r in result["reasons"])
    actor_caps = result.get("actor_capabilities", {})
    assert actor_caps, "actor_capabilities must be forwarded by capability_preflight_result (AC2)"
    assert actor_caps["root_github_read"]["status"] == "ready"
    assert actor_caps["controlled_github_read"]["status"] == "unavailable"
    assert actor_caps["controlled_github_read"]["reason_code"] == "controlled_github_unavailable"


def test_root_read_and_controlled_read_agree_when_both_healthy(fake_gh_env):
    """Negative control: with an unmodified fake `gh` shape, only the
    UN-taught `gh api --hostname` probe fails -- once that same probe IS
    understood (simulated here via FAKE_GH_REPO_READ_OK covering both), the
    overall decision must not be blocked purely on GitHub grounds. This pins
    that the blocked verdict above is caused by the credential-context
    divergence, not by some other unrelated fixture default."""
    # `gh repo view` alone succeeding does not make `controlled_github_read`
    # succeed (they are different command shapes) -- this is intentional:
    # the fixture demonstrates the exact asymmetry AC2 exists to catch.
    result = router.capability_preflight_result(
        repo=_DEFAULT_REPO, spark_mode=None, spark_fallback=None, planned_operations=[]
    )
    assert result["actor_capabilities"]["root_github_read"]["status"] == "ready"
    assert result["actor_capabilities"]["controlled_github_read"]["status"] == "ready"
    assert result["decision"] == "ready"


# =============================================================================
# #2330-origin failure class: Spark `fallback_only` must degrade (continue),
# not block, when fallback is allowed.
# =============================================================================


def test_spark_fallback_only_degrades_workflow_continues(fake_gh_env, monkeypatch):
    """GIVEN Spark's binary is unavailable (PATH-stripped, deterministic
    regardless of host machine) with `spark_mode=preferred` +
    `spark_fallback=allowed`, WHEN the real producer runs, THEN the overall
    decision is `degraded` (workflow continues) and the fallback route is
    recorded via `actor_capabilities.spark_delegation` -- reproducing the
    #2330 Spark `fallback_only` scenario end-to-end through the real
    producer/consumer subprocess boundary."""
    _strip_proxy_binary_from_path(monkeypatch)

    result = router.capability_preflight_result(
        repo=_DEFAULT_REPO,
        spark_mode="preferred",
        spark_fallback="allowed",
        planned_operations=[],
    )
    assert result["checks"]["spark"]["status"] == "fallback_only"
    assert result["decision"] == "degraded"
    assert result["actor_capabilities"]["spark_delegation"]["status"] == "degraded"
    assert result["actor_capabilities"]["spark_delegation"]["fallback_route"] == "non_spark_agent"


def test_workflow_start_entry_still_invokes_inner_preflight_when_degraded(fake_gh_env, monkeypatch):
    """The full production chain (`workflow_start_entry.run()` ->
    `root_entry_router.capability_preflight_result()`, both real, only the
    innermost `run_refinement_preflight.py` invocation is stubbed to avoid
    launching a second full loop stage from this unit test) must still
    proceed past capability preflight when Spark degrades to fallback_only
    -- `degraded` is not `blocked`."""
    _strip_proxy_binary_from_path(monkeypatch)
    sys.path.insert(0, str(_SKILLS_SCRIPTS_DIR)) if str(_SKILLS_SCRIPTS_DIR) not in sys.path else None
    import workflow_start_entry as wse

    invoked = {"count": 0}

    def _stub_inner(*, issue_number, repo):
        invoked["count"] += 1
        return 0

    result, exit_code = wse.run(
        issue_number=1,
        repo=_DEFAULT_REPO,
        spark_mode="preferred",
        spark_fallback="allowed",
        planned_operations_json=json.dumps(
            [{"phase": "impl", "actor_role": "worker", "operation": "issue_comment", "requires_mutation": True}]
        ),
        capability_preflight_result_fn=router.capability_preflight_result,
        invoke_inner_preflight_fn=_stub_inner,
    )
    assert result["decision"] == "degraded"
    assert invoked["count"] == 1
    assert exit_code == 0


def test_workflow_start_entry_never_invokes_inner_preflight_when_controlled_github_blocked(fake_gh_env, monkeypatch):
    """Mirrors AC2's core claim end-to-end at the `workflow_start_entry`
    boundary: a `controlled_github_unavailable` verdict must prevent
    `run_refinement_preflight.py` from ever being invoked."""
    monkeypatch.setenv("FAKE_GH_CONTROLLED_READ_OK", "0")
    import workflow_start_entry as wse

    invoked = {"count": 0}

    def _stub_inner(*, issue_number, repo):
        invoked["count"] += 1
        return 0

    result, exit_code = wse.run(
        issue_number=1,
        repo=_DEFAULT_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=json.dumps(
            [{"phase": "impl", "actor_role": "worker", "operation": "issue_comment", "requires_mutation": True}]
        ),
        capability_preflight_result_fn=router.capability_preflight_result,
        invoke_inner_preflight_fn=_stub_inner,
    )
    assert result["decision"] == "blocked"
    assert invoked["count"] == 0
    assert exit_code == 2
    assert any("controlled_github_unavailable" in r for r in result["reasons"])
