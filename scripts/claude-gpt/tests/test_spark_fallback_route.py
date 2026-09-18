"""scripts/claude-gpt/tests/test_spark_fallback_route.py

Issue #2651: GPT-5.3-Codex-Spark delegation is retired repository-wide.
This file used to verify Issue #2340 AC4's `preferred + fallback allowed +
fallback_only` degraded-continue semantics -- a live binary/auth-based
Spark eligibility judgment that no longer exists. It is replaced (file path
kept, per Issue #2651 Allowed Paths -- no file deletion) with a negative
regression suite: no `spark_mode` directive can ever reach `eligible` /
`fallback_only` / `degraded` any more. Any non-None `spark_mode`
(`required` or `preferred`, regardless of `spark_fallback` or observed
binary/auth availability) now deterministically yields a retired
`blocked` decision -- never a silent fallback to a different model/agent,
and never a live-observed eligibility promotion. `spark_mode=None`
(ordinary callers) is unaffected.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent.parent
_GUARDS_DIR = _REPO_ROOT / "scripts" / "agent-guards"

for _p in (_SCRIPTS_DIR, _GUARDS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import workflow_capability_preflight as wcp  # noqa: E402

_DEFAULT_REPO = "squne121/loop-protocol"


def _assess_with_spark(monkeypatch, *, spark_mode, spark_fallback):
    """No `_run_env_only_preflight` monkeypatch here (unlike the retired
    positive-contract version of this file): `assess()`'s retired Spark
    decision (`_spark_status()`) never calls it any more, so a test that
    still wanted to prove "even if the binary/auth WERE observed available,
    the directive is still retired" would need to patch it -- the tests
    below intentionally omit that to prove the retired branch is reached
    unconditionally, with no probe spawned at all."""
    monkeypatch.setattr(wcp, "_github_auth_probe", lambda deadline_ns: wcp.ProbeOutcome(wcp.PROBE_COMPLETED))
    monkeypatch.setattr(wcp, "_github_repo_read_probe", lambda repo, deadline_ns: wcp.ProbeOutcome(wcp.PROBE_COMPLETED))
    monkeypatch.setattr(wcp.trusted_uv_mod, "check_trusted_uv", lambda project_root: {
        "status": wcp.trusted_uv_mod.STATUS_OK, "reason": "resolved", "resolved_path": "/fake/uv"
    })

    def _fail_if_called(*args, **kwargs):
        raise AssertionError(
            "assess() must not spawn the retired Spark env-only probe any more"
        )

    monkeypatch.setattr(wcp, "_run_env_only_preflight", _fail_if_called)
    monkeypatch.setattr(wcp.subprocess, "run", lambda *a, **k: __import__("subprocess").CompletedProcess([], 0))
    monkeypatch.setattr(wcp.shutil, "which", lambda name: None)

    return wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_DEFAULT_REPO,
        spark_mode=spark_mode,
        spark_fallback=spark_fallback,
        planned_operations=[],
    )


@pytest.mark.parametrize("spark_fallback", ["allowed", "forbidden", None])
def test_preferred_directive_is_always_retired_regardless_of_fallback(monkeypatch, spark_fallback):
    """GIVEN spark_mode=preferred (any spark_fallback value), WHEN assess()
    runs, THEN the route is retired and the overall decision is `blocked`
    -- never `degraded`/`ready`, and no live probe is spawned to judge
    eligibility."""
    result = _assess_with_spark(monkeypatch, spark_mode="preferred", spark_fallback=spark_fallback)
    assert result["checks"]["spark"]["status"] == "retired"
    assert result["decision"] == "blocked"
    spark_entry = result["actor_capabilities"]["spark_delegation"]
    assert spark_entry["status"] == "unavailable"
    assert spark_entry["reason_code"] == "spark_delegation_retired"
    assert spark_entry["fallback_route"] is None
    assert any("spark:retired" in r for r in result["reasons"])


@pytest.mark.parametrize("spark_fallback", ["allowed", "forbidden", None])
def test_required_directive_is_always_retired_regardless_of_fallback(monkeypatch, spark_fallback):
    """GIVEN spark_mode=required (any spark_fallback value), WHEN assess()
    runs, THEN the route is retired and the overall decision is `blocked`.
    `required` and `preferred` share identical retired semantics -- there
    is no more mode-specific branching once Spark is retired."""
    required_result = _assess_with_spark(monkeypatch, spark_mode="required", spark_fallback=spark_fallback)
    preferred_result = _assess_with_spark(monkeypatch, spark_mode="preferred", spark_fallback=spark_fallback)
    assert required_result["checks"]["spark"]["status"] == preferred_result["checks"]["spark"]["status"] == "retired"
    assert required_result["decision"] == preferred_result["decision"] == "blocked"


def test_spark_mode_none_is_unaffected_ordinary_caller(monkeypatch):
    """GIVEN spark_mode=None (an ordinary caller that never requested
    Spark), WHEN assess() runs, THEN the workflow proceeds `ready` and
    `actor_capabilities.spark_delegation` reports ready -- no regression
    for callers that never touch the retired route."""
    result = _assess_with_spark(monkeypatch, spark_mode=None, spark_fallback=None)
    assert result["checks"]["spark"]["status"] == "not_required"
    assert result["decision"] == "ready"
    assert result["actor_capabilities"]["spark_delegation"]["status"] == "ready"
    assert not any(r.startswith("spark:") for r in result["reasons"])
