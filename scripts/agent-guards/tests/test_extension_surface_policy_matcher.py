"""Tests for `extension_surface_policy_matcher.py` (Issue #2290).

AC1-AC5 exercise the shared evaluator directly. AC7 is the cross-skill
parity test: given the same fixture body, `.claude/skills/review-issue`'s
`check_issue_contract.py` and `.claude/skills/issue-contract-review`'s
`contract_readiness_check.py` must return the same verdict because both
call the exact same `evaluate_issue_risk_trigger()` function in this
module (structural parity, not independently re-derived logic).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

from extension_surface_policy_matcher import (  # noqa: E402
    DECISION_RANK,
    evaluate_allowed_paths,
    evaluate_issue_risk_trigger,
    load_policy,
)

_REPO_ROOT = _GUARDS_DIR.parents[1]
_REVIEW_ISSUE_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "review-issue" / "scripts"
_CONTRACT_READINESS_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"


def _load_module_from_path(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ---------------------------------------------------------------------------
# AC1: exact risky Allowed Path + not_applicable -> needs_fix
# ---------------------------------------------------------------------------


def test_exact_path_risky_not_applicable():
    result = evaluate_issue_risk_trigger(
        allowed_path_entries=[".claude/agents/implementation-worker.md"],
        declared_decision="not_applicable",
        rva_section_text="decision: not_applicable",
    )
    assert result["verdict"] == "needs_fix"
    assert result["policy_evaluation"]["has_match"] is True
    assert any(
        m["match_kind"] == "exact" for m in result["policy_evaluation"]["matched_rules"][0]["matches"]
    )
    assert result["reasons"]


# ---------------------------------------------------------------------------
# AC2: wildcard Allowed Path with conservative (literal/static prefix)
# overlap -> needs_fix. This is a syntactic candidate-discovery match, not a
# semantic diff judgment -- the comment below documents that distinction.
# ---------------------------------------------------------------------------


def test_wildcard_conservative_overlap():
    # ".claude/skills/some-new-skill/**" shares the static prefix
    # [".claude", "skills"] with the policy rule selector
    # ".claude/skills/**/SKILL.md" -- this is a *conservative* candidate
    # match (literal/static prefix comparison), not proof that the two
    # path spaces actually intersect once the wildcard segments expand.
    result = evaluate_issue_risk_trigger(
        allowed_path_entries=[".claude/skills/some-new-skill/**"],
        declared_decision="not_applicable",
        rva_section_text="decision: not_applicable",
    )
    assert result["verdict"] == "needs_fix"
    matched_rule = result["policy_evaluation"]["matched_rules"][0]
    assert matched_rule["matches"][0]["match_kind"] == "conservative_wildcard_prefix"


# ---------------------------------------------------------------------------
# AC3: docs-only Allowed Paths + not_applicable -> approve (no candidate match)
# ---------------------------------------------------------------------------


def test_docs_only_not_applicable_approve():
    result = evaluate_issue_risk_trigger(
        allowed_path_entries=["docs/dev/extension-surface-runtime-policy.yaml", "docs/product/requirements.md"],
        declared_decision="not_applicable",
        rva_section_text="decision: not_applicable",
    )
    assert result["verdict"] == "approve"
    assert result["policy_evaluation"]["has_match"] is False
    assert result["reasons"] == []


# ---------------------------------------------------------------------------
# AC4: multiple matched rules -> single most_restrictive final_decision
# ---------------------------------------------------------------------------


_MULTI_RULE_POLICY = {
    "resolution": {"multiple_matches": "evaluate_all", "final_decision": "most_restrictive"},
    "rules": [
        {
            "id": "rule-deferred-scope",
            "selectors": [
                {"source_scope": "project", "path_globs": ["fixtures/deferred-surface/**"]},
            ],
            "default_decision": "deferred",
            "verification_profile": "profile-a",
        },
        {
            "id": "rule-immediate-scope",
            "selectors": [
                {"source_scope": "project", "path_globs": ["fixtures/**"]},
            ],
            "default_decision": "immediate",
            "verification_profile": "profile-b",
        },
    ],
}


def test_multiple_rule_most_restrictive():
    result = evaluate_allowed_paths(
        allowed_path_entries=["fixtures/deferred-surface/foo.py"],
        policy=_MULTI_RULE_POLICY,
    )
    assert len(result["matched_rules"]) == 2
    assert result["final_decision"] == "immediate"
    assert DECISION_RANK["immediate"] > DECISION_RANK["deferred"]


# ---------------------------------------------------------------------------
# AC5: verification_profiles union derived from matched rules, no Issue-body
# duplication required.
# ---------------------------------------------------------------------------


def test_verification_profile_union_derived():
    result = evaluate_allowed_paths(
        allowed_path_entries=["fixtures/deferred-surface/foo.py"],
        policy=_MULTI_RULE_POLICY,
    )
    assert result["verification_profiles"] == ["profile-a", "profile-b"]


# ---------------------------------------------------------------------------
# AC7: review-issue / issue-contract-review parity via the shared evaluator
# ---------------------------------------------------------------------------

_PARITY_FIXTURE_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "test"
change_kind: workflow
```

## Outcome

Concrete outcome sentence for parity fixture testing purposes only.

## Acceptance Criteria

- [ ] AC1: concrete AC

## Verification Commands

```bash
# AC1
$ rg -n "concrete" file.py
```

## Allowed Paths

- .claude/agents/implementation-worker.md

## Stop Conditions

- one
- two
- three
- four
- five
- six

## Runtime Verification Applicability

- decision: not_applicable
- reason: parity fixture, no runtime execution needed

## Required Skills

none
"""


def test_review_issue_and_contract_readiness_parity():
    check_issue_contract = _load_module_from_path(
        "check_issue_contract_for_ext_surface_parity_test",
        _REVIEW_ISSUE_SCRIPTS / "check_issue_contract.py",
    )
    contract_readiness_check = _load_module_from_path(
        "contract_readiness_check_for_ext_surface_parity_test",
        _CONTRACT_READINESS_SCRIPTS / "contract_readiness_check.py",
    )

    review_issue_status, review_issue_reasons = check_issue_contract.check_c14_extension_surface_risk_trigger(
        _PARITY_FIXTURE_BODY, "implementation"
    )
    readiness_errors = contract_readiness_check.check_extension_surface_risk_trigger(_PARITY_FIXTURE_BODY)

    review_issue_needs_fix = review_issue_status == check_issue_contract.CheckResult.FAIL
    readiness_needs_fix = bool(readiness_errors)

    assert review_issue_needs_fix == readiness_needs_fix
    assert review_issue_needs_fix is True
    assert bool(review_issue_reasons) == bool(readiness_errors)


def test_load_policy_reads_real_policy_yaml():
    # Sanity check that the real repository policy file loads and matches
    # against a known risky selector -- guards against the fixture-only
    # tests above silently diverging from the production YAML shape.
    policy = load_policy()
    assert policy.get("schema_version") == "v2"
    result = evaluate_allowed_paths([".claude/hooks/some_hook.py"], policy=policy)
    assert result["has_match"] is True
    assert result["final_decision"] == "immediate"
