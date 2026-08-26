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
