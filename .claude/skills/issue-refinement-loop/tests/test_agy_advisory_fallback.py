"""Issue #2340 AC3: AGY advisory fallback routing.

`root_entry_router.resolve_agy_advisory_route()` decides whether an AGY
provider failure observed during an ADVISORY (non-required) repository
investigation escalates to terminal human judgment, or falls back to the
non-AGY `codebase-investigator` route -- without inventing any new
`failure_class` reason-code names (consumes the existing
`.claude/skills/gemini-cli-headless-delegation` taxonomy verbatim).
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import root_entry_router as router  # noqa: E402


def test_no_failure_stays_on_agy_route():
    """GIVEN no AGY failure observed, WHEN resolving the route, THEN it stays
    on the AGY route (nothing to fall back from)."""
    result = router.resolve_agy_advisory_route(
        failure_class=None, agy_required=False, fallback_allowed=True
    )
    assert result == {"route": "agy", "status": "ready", "reason_code": None}


def test_agy_timeout_advisory_fallback_allowed_degrades_instead_of_escalating():
    """GIVEN an AGY OAuth/subprocess timeout (existing `agy_timeout` taxonomy
    value) during an advisory investigation with fallback allowed, WHEN
    resolving the route, THEN it degrades to the non-AGY
    codebase-investigator route instead of terminal human escalation."""
    result = router.resolve_agy_advisory_route(
        failure_class="agy_timeout", agy_required=False, fallback_allowed=True
    )
    assert result["route"] == "codebase_investigator_non_agy"
    assert result["status"] == "degraded"
    assert result["reason_code"] == "agy_timeout"


def test_agy_auth_required_advisory_fallback_allowed_degrades():
    """Same as above for the `agy_auth_required` taxonomy value (auth
    unavailable, distinct from a subprocess timeout)."""
    result = router.resolve_agy_advisory_route(
        failure_class="agy_auth_required", agy_required=False, fallback_allowed=True
    )
    assert result["route"] == "codebase_investigator_non_agy"
    assert result["status"] == "degraded"
    assert result["reason_code"] == "agy_auth_required"


def test_agy_required_task_fails_closed_even_with_fallback_allowed():
    """GIVEN AGY is REQUIRED for this task (not advisory), WHEN it fails,
    THEN the route fails closed regardless of fallback_allowed."""
    result = router.resolve_agy_advisory_route(
        failure_class="agy_timeout", agy_required=True, fallback_allowed=True
    )
    assert result["route"] == "blocked"
    assert result["status"] == "unavailable"
    assert result["reason_code"] == "agy_timeout"


def test_advisory_but_fallback_forbidden_fails_closed():
    """GIVEN an advisory task but the invocation forbids fallback, WHEN AGY
    fails, THEN the route fails closed (no silent fallback substitution)."""
    result = router.resolve_agy_advisory_route(
        failure_class="agy_timeout", agy_required=False, fallback_allowed=False
    )
    assert result["route"] == "blocked"
    assert result["status"] == "unavailable"


def test_unrecognized_failure_class_fails_closed_never_invents_reason_code():
    """GIVEN a failure_class not present in the existing taxonomy, WHEN
    resolving the route, THEN it fails closed and the reason_code carries the
    ORIGINAL unrecognized value verbatim (never silently normalized/dropped,
    never treated as an implicit new canonical name)."""
    result = router.resolve_agy_advisory_route(
        failure_class="totally_made_up_failure_class", agy_required=False, fallback_allowed=True
    )
    assert result["route"] == "blocked"
    assert result["status"] == "unavailable"
    assert result["reason_code"] == "unrecognized_failure_class:totally_made_up_failure_class"


def test_all_existing_taxonomy_agy_failure_classes_are_recognized():
    """Every AGY failure_class documented in
    `.claude/skills/gemini-cli-headless-delegation/references/
    failure-class-taxonomy.md` must be an explicit member of this module's
    allowlist, so this decision function never treats a real existing
    taxonomy value as unrecognized."""
    taxonomy_path = (
        Path(__file__).resolve().parents[3]
        / "skills"
        / "gemini-cli-headless-delegation"
        / "references"
        / "failure-class-taxonomy.md"
    )
    text = taxonomy_path.read_text(encoding="utf-8")
    for failure_class in router._AGY_ADVISORY_FALLBACK_FAILURE_CLASSES:
        assert f"`{failure_class}`" in text, f"{failure_class} not documented in taxonomy.md"


def test_route_never_carries_secret_like_content():
    """AC5-adjacent: the route decision is a small closed-vocabulary dict --
    never carries token/credential-shaped free text."""
    result = router.resolve_agy_advisory_route(
        failure_class="agy_auth_required", agy_required=False, fallback_allowed=True
    )
    assert set(result.keys()) == {"route", "status", "reason_code"}


def test_permission_boundary_failure_classes_are_not_in_fallback_allowlist():
    """Issue #2340 fix_delta P1-1: `agy_permission_boundary_unavailable` /
    `agy_permission_boundary_inconclusive` must never be members of the
    provider-fallback allowlist -- `failure-class-taxonomy.md`'s "AGY
    permission-boundary runner failure classes" section explicitly states
    these two are NOT provider-fallback input (a dedicated runner never
    starts a fallback provider for them)."""
    assert "agy_permission_boundary_unavailable" not in router._AGY_ADVISORY_FALLBACK_FAILURE_CLASSES
    assert "agy_permission_boundary_inconclusive" not in router._AGY_ADVISORY_FALLBACK_FAILURE_CLASSES


def test_permission_boundary_failure_class_fails_closed_not_fallback():
    """Corollary of the above at the routing-decision level: even though
    these two remain valid taxonomy values (still documented in
    taxonomy.md), resolving a route for them must fail closed
    (`unrecognized_failure_class:...`), never silently degrade to the
    non-AGY fallback route."""
    for failure_class in (
        "agy_permission_boundary_unavailable",
        "agy_permission_boundary_inconclusive",
    ):
        result = router.resolve_agy_advisory_route(
            failure_class=failure_class, agy_required=False, fallback_allowed=True
        )
        assert result["route"] == "blocked"
        assert result["reason_code"] == f"unrecognized_failure_class:{failure_class}"


# =============================================================================
# Issue #2340 fix_delta P0-2 (PR #2357 review, 2026-08-27): route-level
# regression test proving `resolve_agy_advisory_route()` is actually wired
# into `capability_preflight_result()` -- the SAME production function
# `workflow_start_entry.py` calls at every Claude-GPT workflow start --
# rather than being reachable only from the pure-function tests above.
# =============================================================================


def _fake_completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    from subprocess import CompletedProcess

    return CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _stub_producer_result(monkeypatch, *, decision: str = "ready"):
    """Stub the `workflow_capability_preflight.py` subprocess invocation
    inside `capability_preflight_result()` so this test exercises real
    production code (the subprocess-boundary caller + the override
    function) without shelling out to `gh`/network."""
    import json as _json

    producer_stdout = _json.dumps(
        {
            "decision": decision,
            "checks": {},
            "actor_capabilities": {
                # The static producer probe would report `ready` here
                # (agy binary present) even though a REAL delegation
                # attempt this run already timed out -- that gap is
                # exactly what agy_observed_failure_class overrides.
                "delegated_research_agy": {
                    "status": "ready",
                    "reason_code": None,
                    "fallback_route": None,
                    "probe_execution_class": "agy_binary_which",
                }
            },
            "reasons": [],
        }
    )
    monkeypatch.setattr(
        router.subprocess,
        "run",
        lambda *a, **k: _fake_completed(0, stdout=producer_stdout),
    )


def test_capability_preflight_result_wires_agy_timeout_to_fallback_route(monkeypatch):
    """GIVEN a real observed `agy_timeout` this workflow run, WHEN
    `capability_preflight_result()` (the actual function `workflow_start_
    entry.py` calls in production) is invoked with
    `agy_observed_failure_class="agy_timeout"`, THEN the resolved
    `delegated_research_agy` entry reflects the non-AGY fallback route --
    proving the resolver drives a real caller's output, not just its own
    unit tests."""
    _stub_producer_result(monkeypatch)

    result = router.capability_preflight_result(
        repo="squne121/loop-protocol",
        agy_observed_failure_class="agy_timeout",
        agy_required=False,
        agy_fallback_allowed=True,
    )

    entry = result["actor_capabilities"]["delegated_research_agy"]
    assert entry["status"] == "degraded"
    assert entry["reason_code"] == "agy_timeout"
    assert entry["fallback_route"] == "codebase_investigator_non_agy"


def test_capability_preflight_result_agy_required_blocks_on_observed_timeout(monkeypatch):
    """GIVEN AGY is REQUIRED (not advisory) for this call, WHEN a real
    `agy_timeout` is observed, THEN the wired override fails closed
    (`unavailable`, no fallback route) rather than silently degrading."""
    _stub_producer_result(monkeypatch)

    result = router.capability_preflight_result(
        repo="squne121/loop-protocol",
        agy_observed_failure_class="agy_timeout",
        agy_required=True,
        agy_fallback_allowed=True,
    )

    entry = result["actor_capabilities"]["delegated_research_agy"]
    assert entry["status"] == "unavailable"
    assert entry["fallback_route"] is None


def test_capability_preflight_result_no_observed_failure_is_noop():
    """GIVEN no observed AGY failure (the default / common case), WHEN
    `capability_preflight_result()` runs, THEN the producer's own
    `delegated_research_agy` entry passes through unmodified (no spurious
    override when there is nothing to override)."""

    def _fake_run(*_a, **_k):
        import json as _json

        return _fake_completed(
            0,
            stdout=_json.dumps(
                {
                    "decision": "ready",
                    "checks": {},
                    "actor_capabilities": {
                        "delegated_research_agy": {
                            "status": "ready",
                            "reason_code": None,
                            "fallback_route": None,
                            "probe_execution_class": "agy_binary_which",
                        }
                    },
                    "reasons": [],
                }
            ),
        )

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(router.subprocess, "run", _fake_run)
        result = router.capability_preflight_result(repo="squne121/loop-protocol")

    entry = result["actor_capabilities"]["delegated_research_agy"]
    assert entry == {
        "status": "ready",
        "reason_code": None,
        "fallback_route": None,
        "probe_execution_class": "agy_binary_which",
    }
