"""scripts/claude-gpt/tests/test_spark_fallback_route.py

Issue #2340 AC4: `preferred + fallback allowed + fallback_only` must let the
workflow continue in `degraded` mode (not `blocked`) and must record the
fallback route via `actor_capabilities.spark_delegation` -- without this
preflight module itself fabricating a `resolvedModel` claim (that evidence
is Child A #2274's live-invocation responsibility, not a static preflight
judgment; Issue #2340 In Scope item 5 keeps the lazy-attempt design).
"""

from __future__ import annotations

import json
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


def _assess_with_spark(monkeypatch, *, spark_mode, spark_fallback, binary_available, auth_available):
    monkeypatch.setattr(wcp, "_github_auth_probe", lambda deadline_ns: wcp.ProbeOutcome(wcp.PROBE_COMPLETED))
    monkeypatch.setattr(wcp, "_github_repo_read_probe", lambda repo, deadline_ns: wcp.ProbeOutcome(wcp.PROBE_COMPLETED))
    monkeypatch.setattr(wcp.trusted_uv_mod, "check_trusted_uv", lambda project_root: {
        "status": wcp.trusted_uv_mod.STATUS_OK, "reason": "resolved", "resolved_path": "/fake/uv"
    })
    monkeypatch.setattr(
        wcp,
        "_run_env_only_preflight",
        lambda deadline_ns: wcp.ProbeOutcome(
            wcp.PROBE_COMPLETED,
            stdout=json.dumps(
                {
                    "binary_available": binary_available,
                    "chatgpt_auth": {"available": auth_available},
                }
            ),
        ),
    )
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


def test_preferred_fallback_allowed_fallback_only_degrades_and_continues(monkeypatch):
    """GIVEN spark_mode=preferred + spark_fallback=allowed but the Spark
    binary/auth is unavailable, WHEN assess() runs, THEN the overall decision
    is `degraded` (workflow continues) rather than `blocked`, and the
    fallback route is recorded in `actor_capabilities.spark_delegation`."""
    result = _assess_with_spark(
        monkeypatch,
        spark_mode="preferred",
        spark_fallback="allowed",
        binary_available=False,
        auth_available=False,
    )
    assert result["checks"]["spark"]["status"] == "fallback_only"
    assert result["decision"] == "degraded"
    spark_entry = result["actor_capabilities"]["spark_delegation"]
    assert spark_entry["status"] == "degraded"
    assert spark_entry["fallback_route"] == "non_spark_agent"
    assert any("spark:fallback_only" in r for r in result["reasons"])


def test_preferred_fallback_forbidden_unavailable_blocks(monkeypatch):
    """GIVEN spark_fallback=forbidden and Spark is unavailable, WHEN
    assess() runs, THEN the workflow is blocked (fail closed, no silent
    fallback)."""
    result = _assess_with_spark(
        monkeypatch,
        spark_mode="preferred",
        spark_fallback="forbidden",
        binary_available=False,
        auth_available=False,
    )
    assert result["checks"]["spark"]["status"] == "unavailable"
    assert result["decision"] == "blocked"
    assert result["actor_capabilities"]["spark_delegation"]["status"] == "unavailable"


def test_preferred_binary_and_auth_available_is_eligible_not_fallback(monkeypatch):
    """GIVEN Spark binary + auth ARE available, WHEN assess() runs, THEN the
    route is `eligible` (workflow proceeds with Spark, not degraded), and
    this preflight does NOT fabricate a `resolvedModel` claim -- that remains
    the live-invocation's own evidence responsibility (lazy-attempt design,
    In Scope item 5)."""
    result = _assess_with_spark(
        monkeypatch,
        spark_mode="preferred",
        spark_fallback="allowed",
        binary_available=True,
        auth_available=True,
    )
    assert result["checks"]["spark"]["status"] == "eligible"
    assert result["decision"] == "ready"
    assert result["actor_capabilities"]["spark_delegation"]["status"] == "ready"
    assert "resolvedModel" not in result["checks"]["spark"]
    assert set(result["checks"]["spark"].keys()) == {"status"}


def test_required_mode_with_fallback_allowed_is_same_semantics_as_preferred(monkeypatch):
    """`_spark_capability()` treats `spark_mode` truthiness uniformly --
    `required` and `preferred` share the same fallback_only/degraded
    semantics when `spark_fallback=allowed` (no separate global preflight
    added for `required`, per In Scope item 5)."""
    required_result = _assess_with_spark(
        monkeypatch, spark_mode="required", spark_fallback="allowed",
        binary_available=False, auth_available=False,
    )
    preferred_result = _assess_with_spark(
        monkeypatch, spark_mode="preferred", spark_fallback="allowed",
        binary_available=False, auth_available=False,
    )
    required_spark_status = required_result["checks"]["spark"]["status"]
    preferred_spark_status = preferred_result["checks"]["spark"]["status"]
    assert required_spark_status == preferred_spark_status == "fallback_only"
    assert required_result["decision"] == preferred_result["decision"] == "degraded"
