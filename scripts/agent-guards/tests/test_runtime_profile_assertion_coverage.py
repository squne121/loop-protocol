"""Tests for `extension_surface_policy_matcher.derive_required_runtime_assertions`
(Issue #2771 AC1/AC2).

Covers the hard-only required-set derivation and the profile-level
(not assertion-level) hard/advisory grouping rule: a profile's assertions
become required as soon as *any* matched rule for that profile is
`enforcement == "hard"`; an all-advisory set of matched rules for a profile
keeps that profile's assertions entirely out of the required set.

Also covers the policy-integrity failure modes (dangling profile reference,
duplicate assertion id within the same profile) that must raise
`PolicyLoadError` rather than silently mis-derive a required set (Issue
#2771 AC6).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

from extension_surface_policy_matcher import (  # noqa: E402
    PolicyLoadError,
    derive_required_runtime_assertions,
)


def _policy_with_profile(profile_id: str, assertion_ids: list[str]) -> dict:
    return {
        "verification_profiles": {
            profile_id: {
                "runner": "worktree-agent-runtime-smoke",
                "mode": "system_test",
                "assertions": [{"id": aid, "description": f"{aid} description"} for aid in assertion_ids],
            }
        }
    }


def _rule(profile_id: str, enforcement: str) -> dict:
    return {
        "rule_id": f"rule-{profile_id}-{enforcement}",
        "verification_profile": profile_id,
        "enforcement": enforcement,
    }


# --- AC1: hard-only derivation -------------------------------------------


def test_hard_only_rule_yields_full_profile_assertion_set():
    """GIVEN a single matched rule with enforcement: hard
    WHEN required assertions are derived
    THEN every assertion of the matched profile is required."""
    policy = _policy_with_profile("p1", ["a1", "a2"])
    required = derive_required_runtime_assertions([_rule("p1", "hard")], policy)
    assert required == {("p1", "a1"), ("p1", "a2")}


def test_advisory_only_rule_yields_empty_required_set():
    """GIVEN a single matched rule with enforcement: advisory only
    WHEN required assertions are derived
    THEN the required set is empty (advisory-only never blocks, PR #2370)."""
    policy = _policy_with_profile("p1", ["a1", "a2"])
    required = derive_required_runtime_assertions([_rule("p1", "advisory")], policy)
    assert required == set()


def test_no_matched_rules_yields_empty_required_set():
    """GIVEN no matched rules at all
    WHEN required assertions are derived
    THEN the required set is empty."""
    policy = _policy_with_profile("p1", ["a1"])
    required = derive_required_runtime_assertions([], policy)
    assert required == set()


def test_matched_rule_without_verification_profile_is_ignored():
    """GIVEN a matched rule that declares no verification_profile
    WHEN required assertions are derived
    THEN that rule contributes nothing to the required set."""
    policy = _policy_with_profile("p1", ["a1"])
    rule = {"rule_id": "no-profile-rule", "verification_profile": None, "enforcement": "hard"}
    required = derive_required_runtime_assertions([rule], policy)
    assert required == set()


# --- AC2: profile-level grouping across multiple matched_rules entries ---


def test_profile_required_if_any_matching_entry_is_hard():
    """GIVEN the same profile matched by two entries, one advisory and one hard
    WHEN required assertions are derived
    THEN the profile's full assertion set is required (profile-level
    decision, not a partial per-entry split)."""
    policy = _policy_with_profile("p1", ["a1", "a2"])
    required = derive_required_runtime_assertions(
        [_rule("p1", "advisory"), _rule("p1", "hard")], policy
    )
    assert required == {("p1", "a1"), ("p1", "a2")}


def test_profile_not_required_when_all_matching_entries_are_advisory():
    """GIVEN the same profile matched by two entries, both advisory
    WHEN required assertions are derived
    THEN the profile contributes nothing to the required set."""
    policy = _policy_with_profile("p1", ["a1", "a2"])
    required = derive_required_runtime_assertions(
        [_rule("p1", "advisory"), _rule("p1", "advisory")], policy
    )
    assert required == set()


def test_multiple_profiles_simultaneously_hard_and_advisory_mixed():
    """GIVEN two profiles matched at once, one hard and one advisory-only
    WHEN required assertions are derived
    THEN only the hard profile's assertions are required."""
    policy = {
        "verification_profiles": {
            "p1": {
                "runner": "worktree-agent-runtime-smoke",
                "mode": "system_test",
                "assertions": [{"id": "a1", "description": "a1"}],
            },
            "p2": {
                "runner": "worktree-agent-runtime-smoke",
                "mode": "system_test",
                "assertions": [{"id": "b1", "description": "b1"}],
            },
        }
    }
    required = derive_required_runtime_assertions(
        [_rule("p1", "hard"), _rule("p2", "advisory")], policy
    )
    assert required == {("p1", "a1")}


def test_multiple_profiles_simultaneously_both_hard():
    """GIVEN two profiles matched at once, both hard
    WHEN required assertions are derived
    THEN both profiles' assertions are required (Issue #2467 precedent:
    multiple verification profiles can be required simultaneously)."""
    policy = {
        "verification_profiles": {
            "p1": {"assertions": [{"id": "a1", "description": "a1"}]},
            "p2": {"assertions": [{"id": "b1", "description": "b1"}]},
        }
    }
    required = derive_required_runtime_assertions(
        [_rule("p1", "hard"), _rule("p2", "hard")], policy
    )
    assert required == {("p1", "a1"), ("p2", "b1")}


# --- AC6: policy integrity failures (distinguished from Issue defects) ---


def test_dangling_profile_reference_raises_policy_load_error():
    """GIVEN a matched rule referencing a verification_profile id that does
    not exist in the policy's verification_profiles mapping
    WHEN required assertions are derived
    THEN a PolicyLoadError is raised (policy integrity failure, not an
    Issue-side needs_fix, Issue #2771 AC6)."""
    policy = {"verification_profiles": {}}
    with pytest.raises(PolicyLoadError):
        derive_required_runtime_assertions([_rule("does-not-exist", "hard")], policy)


def test_duplicate_assertion_id_within_profile_raises_policy_load_error():
    """GIVEN a matched profile whose assertions[] declares the same id twice
    WHEN required assertions are derived
    THEN a PolicyLoadError is raised (policy integrity failure, Issue #2771
    AC6)."""
    policy = {
        "verification_profiles": {
            "p1": {
                "assertions": [
                    {"id": "dup", "description": "first"},
                    {"id": "dup", "description": "second"},
                ]
            }
        }
    }
    with pytest.raises(PolicyLoadError):
        derive_required_runtime_assertions([_rule("p1", "hard")], policy)


def test_missing_verification_profiles_mapping_raises_policy_load_error():
    """GIVEN a policy dict with no top-level verification_profiles mapping
    WHEN required assertions are derived from a hard matched rule
    THEN a PolicyLoadError is raised (fail closed, not silent empty set)."""
    with pytest.raises(PolicyLoadError):
        derive_required_runtime_assertions([_rule("p1", "hard")], {})


def test_advisory_only_match_against_dangling_profile_still_raises():
    """GIVEN an advisory-only matched rule referencing a nonexistent profile
    WHEN required assertions are derived
    THEN a PolicyLoadError is still raised -- a dangling profile reference is
    a policy defect regardless of the referencing rule's enforcement value
    (advisory-only references do not silently mask a malformed policy)."""
    policy = {"verification_profiles": {}}
    with pytest.raises(PolicyLoadError):
        derive_required_runtime_assertions([_rule("does-not-exist", "advisory")], policy)
