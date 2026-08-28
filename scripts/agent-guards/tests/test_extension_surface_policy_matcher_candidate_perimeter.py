"""Tests for the `unclassified_candidate` / `ordinary` / `matched_rule`
three-way classification and non-blocking advisory (Issue #2339).

Issue #2339 re-designs the original Issue #2290 `unknown_surface_policy`
contract (which OWNER review on Issue #2339 itself rejected as P0-broken:
`has_match: false` was being treated as a uniform `human_judgment` / `gate:
block` verdict, conflating "ordinary path" with "project-local unclassified
extension candidate"). This file covers AC1 / AC4-AC12 of Issue #2339
(AC2/AC3 regression coverage lives in the pre-existing
`test_extension_surface_policy_matcher.py`; AC7 consumer-parity coverage
lives in the `review-issue` / `issue-contract-review` skill test dirs; AC12
is a plain `rg` Verification Command with no dedicated test function here).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import extension_surface_policy_matcher  # noqa: E402
from extension_surface_policy_matcher import (  # noqa: E402
    PolicyLoadError,
    evaluate_allowed_paths,
    evaluate_issue_risk_trigger,
    load_policy,
)

_REPO_ROOT = _GUARDS_DIR.parents[1]


# ---------------------------------------------------------------------------
# AC1: docs/** only Allowed Paths -> `ordinary`, verdict: approve, no advisory.
# ---------------------------------------------------------------------------


def test_docs_only_path_is_ordinary_approve_no_advisory():
    result = evaluate_issue_risk_trigger(
        allowed_path_entries=["docs/dev/extension-surface-runtime-policy.yaml"],
        declared_decision="not_applicable",
        rva_section_text="decision: not_applicable",
    )
    assert result["verdict"] == "approve"
    assert result["advisories"] == []
    policy_evaluation = result["policy_evaluation"]
    assert policy_evaluation["has_match"] is False
    classifications = {c["entry"]: c["classification"] for c in policy_evaluation["path_classifications"]}
    assert classifications["docs/dev/extension-surface-runtime-policy.yaml"] == "ordinary"


# ---------------------------------------------------------------------------
# AC4: a project_candidate_path_globs hit with no known rule selector match
# -> `unclassified_candidate`.
# ---------------------------------------------------------------------------


def test_candidate_perimeter_non_matching_rule_is_unclassified_candidate():
    result = evaluate_allowed_paths([".claude/commands/some-new-command.md"])
    classifications = {c["entry"]: c["classification"] for c in result["path_classifications"]}
    assert classifications[".claude/commands/some-new-command.md"] == "unclassified_candidate"
    assert result["has_match"] is False


# ---------------------------------------------------------------------------
# AC5: `unclassified_candidate` entries never set `final_decision`.
# ---------------------------------------------------------------------------


def test_unclassified_candidate_final_decision_is_null():
    result = evaluate_allowed_paths([".claude/commands/some-new-command.md"])
    classifications = {c["entry"]: c["classification"] for c in result["path_classifications"]}
    assert classifications[".claude/commands/some-new-command.md"] == "unclassified_candidate"
    assert result["final_decision"] is None


# ---------------------------------------------------------------------------
# AC6: `unclassified_candidate` -> verdict: approve + advisory present, no
# `gate: block`-equivalent stop.
# ---------------------------------------------------------------------------


def test_unclassified_candidate_is_approve_with_advisory_not_block():
    result = evaluate_issue_risk_trigger(
        allowed_path_entries=[".claude/commands/some-new-command.md"],
        declared_decision="not_applicable",
        rva_section_text="decision: not_applicable",
    )
    assert result["verdict"] == "approve"
    assert result["reasons"] == []
    assert result["advisories"]
    assert any("some-new-command.md" in advisory for advisory in result["advisories"])


# ---------------------------------------------------------------------------
# AC8: `human_judgment` never appears as any `final_decision` value, across
# matched_rule / unclassified_candidate / ordinary classifications.
# ---------------------------------------------------------------------------


def test_human_judgment_never_appears_in_final_decision():
    fixtures = [
        [".claude/agents/implementation-worker.md"],  # matched_rule (hard)
        [".claude/commands/some-new-command.md"],  # unclassified_candidate
        [".claude/rules/project-constitution.md"],  # unclassified_candidate (real file)
        ["docs/dev/extension-surface-runtime-policy.yaml"],  # ordinary
    ]
    for entries in fixtures:
        result = evaluate_allowed_paths(entries)
        assert result["final_decision"] != "human_judgment"
        assert result["final_decision"] in (None, "not_applicable", "deferred", "immediate")


# ---------------------------------------------------------------------------
# AC9: a structurally malformed `unknown_surface_policy` (non-list
# `project_candidate_path_globs`) must fail closed -- PolicyLoadError, not a
# silent approve.
# ---------------------------------------------------------------------------


def test_malformed_candidate_policy_is_policy_unavailable_not_silent_approve():
    malformed_policy = {
        "resolution": {"multiple_matches": "evaluate_all", "final_decision": "most_restrictive"},
        "rules": [
            {
                "id": "rule-unrelated",
                "selectors": [
                    {"source_scope": "project", "path_globs": ["fixtures/**"]},
                ],
                "default_decision": "immediate",
                "verification_profile": "profile-unrelated",
            },
        ],
        "unknown_surface_policy": {
            "decision": "human_judgment",
            "gate": "advisory",
            "project_candidate_path_globs": "not-a-list",
        },
    }
    with pytest.raises(PolicyLoadError):
        evaluate_allowed_paths([".claude/commands/some-new-command.md"], policy=malformed_policy)


# ---------------------------------------------------------------------------
# AC10: a real, existing repository-relative path (`.claude/rules/
# project-constitution.md`, not a fictitious fixture) must be detected as
# `unclassified_candidate` (or a matching formal rule, if one existed) --
# using the real production policy YAML, not a synthetic fixture-only
# policy.
# ---------------------------------------------------------------------------


def test_claude_rules_project_constitution_is_detected():
    real_path = _REPO_ROOT / ".claude" / "rules" / "project-constitution.md"
    assert real_path.is_file(), "fixture depends on a real, existing repository file"

    policy = load_policy()
    result = evaluate_allowed_paths([".claude/rules/project-constitution.md"], policy=policy)
    classifications = {c["entry"]: c["classification"] for c in result["path_classifications"]}
    assert classifications[".claude/rules/project-constitution.md"] in (
        "unclassified_candidate",
        "matched_rule",
    )
    # As of Issue #2339, no formal rule selector covers `.claude/rules/**`
    # (Out of Scope: adding a new risk-trigger rule/selector), so this must
    # currently resolve specifically to `unclassified_candidate`.
    assert classifications[".claude/rules/project-constitution.md"] == "unclassified_candidate"


# ---------------------------------------------------------------------------
# AC11: user / plugin / session / cli source_scope surfaces are NOT handled
# by this Issue's candidate-perimeter classification -- documented here by
# test name/comment (Issue #2339 Out of Scope: Issue-time semantic proof for
# non-project source_scope surfaces).
# ---------------------------------------------------------------------------


def test_non_project_source_scope_surfaces_are_documented_out_of_scope():
    """`unknown_surface_policy.project_candidate_path_globs` is a flat,
    repository-relative glob list with no `source_scope` concept at all --
    unlike `rules[].selectors[]`, which explicitly enumerates `project` /
    `user` / `managed` / `plugin` / `session` / `cli` source_scope entries.
    The candidate-perimeter classification added by this Issue (`_matches_
    candidate_perimeter` / `unclassified_candidate`) therefore never
    attempts to resolve user/managed/plugin/session/cli-scoped surfaces --
    those remain out of scope for Issue-time (static, Allowed-Paths-based)
    detection, same as the existing `_project_path_globs()` rule-selector
    filtering (Issue #2290 In Scope, unchanged) and Issue #2339's own
    explicit Out of Scope list.
    """
    policy = load_policy()
    unknown_surface_policy = policy.get("unknown_surface_policy") or {}
    assert "project_candidate_path_globs" in unknown_surface_policy
    assert "source_scope" not in unknown_surface_policy
    assert "selectors" not in unknown_surface_policy

    # Sanity: the candidate-perimeter matcher itself takes a flat
    # `list[str]`, not a `selectors[]`-shaped structure.
    result = extension_surface_policy_matcher._matches_candidate_perimeter(
        ".claude/commands/foo.md", [".claude/commands/**"]
    )
    assert result == ".claude/commands/**"
