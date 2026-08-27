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

import extension_surface_policy_matcher  # noqa: E402
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


# ---------------------------------------------------------------------------
# P0 (Issue #2290, PR #2335 OWNER review fix_delta -- highest priority):
# Issue #2290's own declared Allowed Paths must not self-violate the gate it
# introduces. `.claude/skills/**/scripts/**` matches are candidate-discovery
# only ("advisory") and must not force needs_fix, unlike
# `.claude/skills/**/SKILL.md` matches which remain a hard block.
# ---------------------------------------------------------------------------


def test_self_application_skill_scripts_only_is_advisory_not_hard_block():
    # Issue #2290's own Allowed Paths (as declared in the live Issue body):
    # only touches skill *script* files, not any SKILL.md, and declares
    # decision: not_applicable. This must resolve to `approve`, not
    # `needs_fix` -- otherwise the gate this Issue introduces would
    # self-violate on its own contract.
    result = evaluate_issue_risk_trigger(
        allowed_path_entries=[
            ".claude/skills/review-issue/scripts/check_issue_contract.py",
            ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py",
        ],
        declared_decision="not_applicable",
        rva_section_text="decision: not_applicable",
    )
    assert result["verdict"] == "approve"
    assert result["reasons"] == []
    policy_evaluation = result["policy_evaluation"]
    assert policy_evaluation["has_match"] is True
    assert policy_evaluation["final_decision"] is None
    matched_rule = policy_evaluation["matched_rules"][0]
    assert matched_rule["enforcement"] == "advisory"
    assert all(
        match["path_glob"] == ".claude/skills/**/scripts/**" for match in matched_rule["matches"]
    )


def test_skill_md_match_remains_hard_block():
    # Contrast case: a SKILL.md match (skill runtime semantics itself) must
    # keep forcing needs_fix as before -- only the scripts/** selector was
    # downgraded to advisory.
    result = evaluate_issue_risk_trigger(
        allowed_path_entries=[".claude/skills/some-skill/SKILL.md"],
        declared_decision="not_applicable",
        rva_section_text="decision: not_applicable",
    )
    assert result["verdict"] == "needs_fix"
    matched_rule = result["policy_evaluation"]["matched_rules"][0]
    assert matched_rule["enforcement"] == "hard"


def test_wildcard_entry_ambiguous_prefix_resolves_to_advisory_not_hard():
    # Root cause of the P0 re-fix (PR #2335, second fix_delta): a *wildcard*
    # Allowed Path entry whose conservative static prefix matches BOTH
    # ``.claude/skills/**/scripts/**`` (candidate-only/advisory) and
    # ``.claude/skills/**/SKILL.md`` (hard) within the same rule must not
    # be forced hard purely because ``SKILL.md`` happened to be iterated
    # first in the rule's ``path_globs`` list. This is exactly the shape
    # of Issue #2290's own wildcard Allowed Path entries (e.g.
    # ``.claude/skills/review-issue/tests/**``).
    result = evaluate_issue_risk_trigger(
        allowed_path_entries=[".claude/skills/review-issue/tests/**"],
        declared_decision="not_applicable",
        rva_section_text="decision: not_applicable",
    )
    assert result["verdict"] == "approve"
    assert result["reasons"] == []
    policy_evaluation = result["policy_evaluation"]
    assert policy_evaluation["has_match"] is True
    assert policy_evaluation["final_decision"] is None
    matched_rule = policy_evaluation["matched_rules"][0]
    assert matched_rule["enforcement"] == "advisory"
    assert all(
        match["path_glob"] == ".claude/skills/**/scripts/**" for match in matched_rule["matches"]
    )


def test_wildcard_entry_matching_only_skill_md_glob_still_hard():
    # Contrast case for the ambiguity fix: a wildcard entry whose static
    # prefix only reaches a rule glob that is NOT candidate-only (no
    # ambiguity at all, since the rule in this fixture policy only has one
    # glob) must remain a hard match, same as before the fix.
    single_glob_policy = {
        "resolution": {"multiple_matches": "evaluate_all", "final_decision": "most_restrictive"},
        "rules": [
            {
                "id": "rule-skill-md-only",
                "selectors": [
                    {"source_scope": "project", "path_globs": [".claude/skills/**/SKILL.md"]},
                ],
                "default_decision": "immediate",
                "verification_profile": "profile-skill-md",
            },
        ],
    }
    result = evaluate_allowed_paths(
        allowed_path_entries=[".claude/skills/some-skill/**"],
        policy=single_glob_policy,
    )
    matched_rule = result["matched_rules"][0]
    assert matched_rule["enforcement"] == "hard"
    assert result["final_decision"] == "immediate"


# ---------------------------------------------------------------------------
# P0 re-fix (Issue #2290, PR #2335 second OWNER review fix_delta): the full
# live Issue #2290 Allowed Paths set -- exact script paths AND all wildcard
# entries -- must not self-violate the gate this Issue introduces, when run
# through BOTH consumer functions (review-issue and issue-contract-review).
# The previous P0 fix's self-application test only exercised the two exact
# script paths and missed the wildcard entries that actually trigger the
# glob-iteration-order bug.
# ---------------------------------------------------------------------------

_ISSUE_2290_LIVE_ALLOWED_PATHS = [
    "scripts/agent-guards/extension_surface_policy_matcher.py",
    "scripts/agent-guards/tests/test_extension_surface_policy_matcher.py",
    ".claude/skills/review-issue/scripts/check_issue_contract.py",
    ".claude/skills/review-issue/fixtures/**",
    ".claude/skills/review-issue/schemas/**",
    ".claude/skills/review-issue/tests/**",
    ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py",
    ".claude/skills/issue-contract-review/tests/**",
    ".claude/skills/issue-contract-review/scripts/tests/test_baseline_vc_preflight_timeout_classification.py",
]

_ISSUE_2290_LIVE_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "test"
change_kind: workflow
```

## Outcome

Concrete outcome sentence for Issue #2290 self-application fixture.

## Acceptance Criteria

- [ ] AC1: concrete AC

## Verification Commands

```bash
# AC1
$ rg -n "concrete" file.py
```

## Allowed Paths

- scripts/agent-guards/extension_surface_policy_matcher.py
- scripts/agent-guards/tests/test_extension_surface_policy_matcher.py
- .claude/skills/review-issue/scripts/check_issue_contract.py
- .claude/skills/review-issue/fixtures/**
- .claude/skills/review-issue/schemas/**
- .claude/skills/review-issue/tests/**
- .claude/skills/issue-contract-review/scripts/contract_readiness_check.py
- .claude/skills/issue-contract-review/tests/**
- .claude/skills/issue-contract-review/scripts/tests/test_baseline_vc_preflight_timeout_classification.py

## Stop Conditions

- one
- two
- three
- four
- five
- six

## Runtime Verification Applicability

- decision: not_applicable
- reason: self-application fixture, no runtime execution needed

## Required Skills

none
"""


def test_shared_evaluator_self_application_full_allowed_paths_set_is_approve():
    # Direct shared-evaluator exercise of the FULL live Issue #2290 Allowed
    # Paths set (exact + all 4 wildcard entries), not just the 2 exact
    # script paths the earlier P0 fix's test covered.
    result = evaluate_issue_risk_trigger(
        allowed_path_entries=_ISSUE_2290_LIVE_ALLOWED_PATHS,
        declared_decision="not_applicable",
        rva_section_text="decision: not_applicable",
    )
    assert result["verdict"] == "approve"
    assert result["reasons"] == []
    matched_rule = result["policy_evaluation"]["matched_rules"][0]
    assert matched_rule["enforcement"] == "advisory"


def test_live_issue_2290_body_self_application_review_issue_and_contract_readiness_approve():
    # Fetches Issue #2290's OWN live body via `gh issue view` and runs it
    # through both consumer functions unmodified, so this test fails if the
    # live Issue body's Allowed Paths declaration ever drifts from the
    # fixture-embedded copy above.
    import shutil
    import subprocess

    check_issue_contract = _load_module_from_path(
        "check_issue_contract_for_live_2290_self_application_test",
        _REVIEW_ISSUE_SCRIPTS / "check_issue_contract.py",
    )
    contract_readiness_check = _load_module_from_path(
        "contract_readiness_check_for_live_2290_self_application_test",
        _CONTRACT_READINESS_SCRIPTS / "contract_readiness_check.py",
    )

    if shutil.which("gh") is None:
        import pytest

        pytest.skip("gh CLI not available in this environment")

    try:
        proc = subprocess.run(
            ["gh", "issue", "view", "2290", "--json", "body", "--jq", ".body"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        import pytest

        pytest.skip(f"gh issue view failed to execute: {exc}")

    if proc.returncode != 0 or not proc.stdout.strip():
        import pytest

        pytest.skip(f"gh issue view 2290 did not return a body (rc={proc.returncode}): {proc.stderr}")

    live_body = proc.stdout

    review_issue_status, review_issue_reasons = check_issue_contract.check_c14_extension_surface_risk_trigger(
        live_body, "implementation"
    )
    readiness_errors = contract_readiness_check.check_extension_surface_risk_trigger(live_body)

    review_issue_needs_fix = review_issue_status == check_issue_contract.CheckResult.FAIL
    readiness_needs_fix = bool(readiness_errors)

    assert review_issue_needs_fix is False, review_issue_reasons
    assert readiness_needs_fix is False, readiness_errors


def test_mixed_scripts_and_skill_md_match_is_hard():
    # A rule with at least one hard hit (SKILL.md) alongside an advisory hit
    # (scripts/**) must still be classified "hard" overall for that rule.
    result = evaluate_allowed_paths(
        allowed_path_entries=[
            ".claude/skills/foo/scripts/bar.py",
            ".claude/skills/foo/SKILL.md",
        ],
    )
    matched_rule = next(
        r for r in result["matched_rules"] if r["rule_id"] == "skill-invocation-procedure-or-contract-change"
    )
    assert matched_rule["enforcement"] == "hard"
    assert result["final_decision"] == "immediate"


# ---------------------------------------------------------------------------
# P1-2 (Issue #2290, PR #2335 OWNER review fix_delta): load_policy() must
# fail closed (raise PolicyLoadError) on structurally incompatible policy
# YAML instead of silently degrading to a normal "no candidate match".
# ---------------------------------------------------------------------------


def test_load_policy_rejects_unsupported_schema_version(tmp_path):
    from extension_surface_policy_matcher import PolicyLoadError

    bad_policy_path = tmp_path / "bad_schema_version.yaml"
    bad_policy_path.write_text(
        "schema_version: v1\n"
        "resolution:\n"
        "  multiple_matches: evaluate_all\n"
        "  final_decision: most_restrictive\n"
        "rules:\n"
        "  - id: r1\n"
        "    selectors:\n"
        "      - source_scope: project\n"
        "        path_globs: ['foo/**']\n"
        "    default_decision: immediate\n"
    )
    try:
        load_policy(bad_policy_path)
        assert False, "expected PolicyLoadError"
    except PolicyLoadError:
        pass


def test_load_policy_rejects_empty_rules(tmp_path):
    from extension_surface_policy_matcher import PolicyLoadError

    bad_policy_path = tmp_path / "empty_rules.yaml"
    bad_policy_path.write_text(
        "schema_version: v2\n"
        "resolution:\n"
        "  multiple_matches: evaluate_all\n"
        "  final_decision: most_restrictive\n"
        "rules: []\n"
    )
    try:
        load_policy(bad_policy_path)
        assert False, "expected PolicyLoadError"
    except PolicyLoadError:
        pass


def test_load_policy_rejects_unknown_resolution_value(tmp_path):
    from extension_surface_policy_matcher import PolicyLoadError

    bad_policy_path = tmp_path / "unknown_resolution.yaml"
    bad_policy_path.write_text(
        "schema_version: v2\n"
        "resolution:\n"
        "  multiple_matches: first_match_only\n"
        "  final_decision: most_restrictive\n"
        "rules:\n"
        "  - id: r1\n"
        "    selectors:\n"
        "      - source_scope: project\n"
        "        path_globs: ['foo/**']\n"
        "    default_decision: immediate\n"
    )
    try:
        load_policy(bad_policy_path)
        assert False, "expected PolicyLoadError"
    except PolicyLoadError:
        pass


def test_load_policy_accepts_minimal_valid_policy(tmp_path):
    good_policy_path = tmp_path / "good.yaml"
    good_policy_path.write_text(
        "schema_version: v2\n"
        "resolution:\n"
        "  multiple_matches: evaluate_all\n"
        "  final_decision: most_restrictive\n"
        "rules:\n"
        "  - id: r1\n"
        "    selectors:\n"
        "      - source_scope: project\n"
        "        path_globs: ['foo/**']\n"
        "    default_decision: immediate\n"
    )
    policy = load_policy(good_policy_path)
    assert policy["schema_version"] == "v2"


# ---------------------------------------------------------------------------
# Issue #2356 AC3: `CANDIDATE_ONLY_PATH_GLOBS` hardcoded frozenset is removed
# in favour of the YAML/schema-driven `issue_time_enforcement` field.
# ---------------------------------------------------------------------------


def test_candidate_only_path_globs_removed():
    assert not hasattr(extension_surface_policy_matcher, "CANDIDATE_ONLY_PATH_GLOBS")


# ---------------------------------------------------------------------------
# Issue #2356 AC5: a project selector that intentionally OMITS
# `issue_time_enforcement` must be treated as `hard` -- this is the required
# runtime fallback for existing/future rules that never declare the field
# (e.g. `claude-gpt-lifecycle-invocation-change`). This is a *synthetic*
# selector constructed directly in this test, independent of any existing
# rule's current unlabeled state, so the backward-compat meaning stays fixed
# even if an existing rule later gains an explicit `issue_time_enforcement`.
# ---------------------------------------------------------------------------


def test_selector_omitting_issue_time_enforcement_defaults_to_hard():
    synthetic_policy = {
        "resolution": {"multiple_matches": "evaluate_all", "final_decision": "most_restrictive"},
        "rules": [
            {
                "id": "synthetic-rule-no-issue-time-enforcement",
                "selectors": [
                    # Deliberately omits `issue_time_enforcement` entirely.
                    {"source_scope": "project", "path_globs": ["synthetic/no-enforcement-field/**"]},
                ],
                "default_decision": "immediate",
                "verification_profile": "profile-synthetic",
            },
        ],
    }
    result = evaluate_allowed_paths(
        allowed_path_entries=["synthetic/no-enforcement-field/foo.py"],
        policy=synthetic_policy,
    )
    matched_rule = result["matched_rules"][0]
    assert matched_rule["enforcement"] == "hard"
    assert result["final_decision"] == "immediate"
    assert matched_rule["matches"][0]["issue_time_enforcement"] == "hard"


# ---------------------------------------------------------------------------
# PR #2359 OWNER review fix_delta (iteration 1, P1 blocker): an invalid
# `issue_time_enforcement` value on a project selector (e.g. a typo such as
# "hrad" instead of "hard") must fail closed -- raising `PolicyLoadError` --
# rather than silently flowing through as a string that is neither "hard"
# nor "advisory", failing the `== "hard"` comparison, and thereby degrading
# the rule to "advisory" (a malformed policy value must never silently
# WEAKEN the gate; this is the same class of self-application false-negative
# that caused the P0 regression in PR #2335 / Issue #2290).
# ---------------------------------------------------------------------------


def test_invalid_issue_time_enforcement_fails_closed():
    bad_policy = {
        "resolution": {"multiple_matches": "evaluate_all", "final_decision": "most_restrictive"},
        "rules": [
            {
                "id": "synthetic-rule-invalid-issue-time-enforcement",
                "selectors": [
                    {
                        "source_scope": "project",
                        "path_globs": ["synthetic/invalid-enforcement-field/**"],
                        "issue_time_enforcement": "hrad",
                    },
                ],
                "default_decision": "immediate",
                "verification_profile": "profile-synthetic-invalid",
            },
        ],
    }
    try:
        evaluate_allowed_paths(
            allowed_path_entries=["synthetic/invalid-enforcement-field/foo.py"],
            policy=bad_policy,
        )
        assert False, "expected PolicyLoadError for invalid issue_time_enforcement value"
    except extension_surface_policy_matcher.PolicyLoadError as exc:
        assert "hrad" in str(exc)
