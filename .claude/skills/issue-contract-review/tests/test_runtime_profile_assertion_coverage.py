"""Tests for `check_runtime_assertion_binding_coverage()`
(`contract_readiness_check.py`, Issue #2771 AC5/AC6), plus cross-consumer
parity with `check_issue_contract.py`'s `check_c15_runtime_assertion_binding_
coverage()`.

Covers AC5 (each declared binding's `ac` is checked for: existence,
`applicable_acs` membership, decision-level runtime-verification tag
consistency, and a canonical `# AC<N>` `## Verification Commands`
reference -- each failure mode reported distinctly) and AC6 (an Issue-side
declaration defect escalates to `needs_fix`, while a policy-side integrity
defect -- dangling profile reference / duplicate assertion id -- escalates
to `human_judgment` instead, mirroring the existing `check_extension_
surface_risk_trigger()` / EXTSURF002 precedent).

Fixtures use the real production policy via `.claude/hooks/**` (hook-chain-
runtime-smoke, hard, 2 assertions, no overlap with any other rule's
path_globs -- see `.claude/skills/review-issue/tests/test_runtime_profile_
assertion_coverage.py` docstring for why `.claude/agents/**` /
`.claude/skills/**/SKILL.md` are avoided here).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[3]
_REVIEW_ISSUE_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "review-issue" / "scripts"
_CONTRACT_READINESS_SCRIPTS = _HERE.parent / "scripts"


def _load_module_from_path(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _load_consumers(suffix: str):
    check_issue_contract = _load_module_from_path(
        f"check_issue_contract_for_runtime_assertion_binding_{suffix}",
        _REVIEW_ISSUE_SCRIPTS / "check_issue_contract.py",
    )
    contract_readiness_check = _load_module_from_path(
        f"contract_readiness_check_for_runtime_assertion_binding_{suffix}",
        _CONTRACT_READINESS_SCRIPTS / "contract_readiness_check.py",
    )
    return check_issue_contract, contract_readiness_check


_HOOK_ASSERTION_1 = "all_matching_hooks_observed"
_HOOK_ASSERTION_2 = "sibling_side_effect_inventory_complete"


_BODY_TEMPLATE = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "test"
change_kind: workflow
```

## Outcome

Concrete outcome sentence for runtime assertion binding coverage fixture testing.

## Acceptance Criteria

{ac_section}

## Verification Commands

```bash
{vc_section}
```

## Allowed Paths

- .claude/hooks/foo.py

## Stop Conditions

- one
- two
- three
- four
- five
- six

## Runtime Verification Applicability

```yaml
decision: immediate
applicable_acs:
{applicable_acs}
execution_environment:
  cli_tools:
    - python3
skip_conditions:
  - "none"
fallback_policy:
  fallback_success_is_pass: false
artifact_requirements:
  - "artifacts/out.json"
runtime_assertion_bindings:
{runtime_assertion_bindings}
```

## Required Skills

none
"""


def _binding(profile: str, assertion: str, ac: str) -> str:
    return f"  - profile: {profile}\n    assertion: {assertion}\n    ac: {ac}\n"


def _build_body(
    *,
    ac_lines: str,
    vc_lines: str,
    applicable_acs: str,
    runtime_assertion_bindings: str,
) -> str:
    return _BODY_TEMPLATE.format(
        ac_section=ac_lines,
        vc_section=vc_lines,
        applicable_acs=applicable_acs,
        runtime_assertion_bindings=runtime_assertion_bindings,
    )


# --- AC5: each of the 4 ac validity checks reported distinctly -----------


def test_ac_not_found_is_reported():
    """GIVEN a binding whose ac (AC99) does not exist in Acceptance Criteria
    WHEN the coverage check evaluates
    THEN it FAILs with an 'ac_not_found' reason."""
    _, contract_readiness_check = _load_consumers("ac_not_found")
    body = _build_body(
        ac_lines=(
            "- [ ] AC1: concrete AC 1 <!-- runtime-verification: true -->\n"
            "- [ ] AC2: concrete AC 2 <!-- runtime-verification: true -->"
        ),
        vc_lines="# AC1\n$ rg -n 'concrete' file1.py\n# AC2\n$ rg -n 'concrete' file2.py",
        applicable_acs="  - AC1\n  - AC2",
        runtime_assertion_bindings=(
            _binding("hook-chain-runtime-smoke", _HOOK_ASSERTION_1, "AC1")
            + _binding("hook-chain-runtime-smoke", _HOOK_ASSERTION_2, "AC99")
        ),
    )
    errors = contract_readiness_check.check_runtime_assertion_binding_coverage(body)
    assert len(errors) == 1
    assert errors[0]["category"] == "runtime_assertion_binding_coverage"
    minimal_context = " ".join(errors[0]["minimal_context"])
    assert "ac_not_found" in minimal_context
    assert "AC99" in minimal_context


def test_ac_not_in_applicable_acs_is_reported():
    """GIVEN a binding whose ac exists and has VC coverage, but is not
    listed in `applicable_acs`
    WHEN the coverage check evaluates
    THEN it FAILs with an 'ac_not_in_applicable_acs' reason."""
    _, contract_readiness_check = _load_consumers("ac_not_applicable")
    body = _build_body(
        ac_lines=(
            "- [ ] AC1: concrete AC 1 <!-- runtime-verification: true -->\n"
            "- [ ] AC2: concrete AC 2 <!-- runtime-verification: true -->"
        ),
        vc_lines="# AC1\n$ rg -n 'concrete' file1.py\n# AC2\n$ rg -n 'concrete' file2.py",
        # AC2 deliberately excluded from applicable_acs.
        applicable_acs="  - AC1",
        runtime_assertion_bindings=(
            _binding("hook-chain-runtime-smoke", _HOOK_ASSERTION_1, "AC1")
            + _binding("hook-chain-runtime-smoke", _HOOK_ASSERTION_2, "AC2")
        ),
    )
    errors = contract_readiness_check.check_runtime_assertion_binding_coverage(body)
    assert len(errors) == 1
    minimal_context = " ".join(errors[0]["minimal_context"])
    assert "ac_not_in_applicable_acs" in minimal_context


def test_runtime_verification_tag_missing_is_reported():
    """GIVEN decision: immediate but the Acceptance Criteria section carries
    no `<!-- runtime-verification: true -->` tag anywhere
    WHEN the coverage check evaluates
    THEN it FAILs with a 'runtime_verification_tag_inconsistent' reason for
    every declared binding (decision-level, Issue #2771 AC5 point 3)."""
    _, contract_readiness_check = _load_consumers("tag_missing")
    body = _build_body(
        # No <!-- runtime-verification: true --> tag anywhere below.
        ac_lines=("- [ ] AC1: concrete AC 1\n- [ ] AC2: concrete AC 2"),
        vc_lines="# AC1\n$ rg -n 'concrete' file1.py\n# AC2\n$ rg -n 'concrete' file2.py",
        applicable_acs="  - AC1\n  - AC2",
        runtime_assertion_bindings=(
            _binding("hook-chain-runtime-smoke", _HOOK_ASSERTION_1, "AC1")
            + _binding("hook-chain-runtime-smoke", _HOOK_ASSERTION_2, "AC2")
        ),
    )
    errors = contract_readiness_check.check_runtime_assertion_binding_coverage(body)
    assert len(errors) == 1
    minimal_context = " ".join(errors[0]["minimal_context"])
    assert "runtime_verification_tag_inconsistent" in minimal_context


def test_vc_reference_missing_is_reported():
    """GIVEN a binding whose ac exists, is applicable, and is tagged, but
    has no canonical `# AC<N>` reference in `## Verification Commands`
    WHEN the coverage check evaluates
    THEN it FAILs with an 'ac_missing_vc_reference' reason."""
    _, contract_readiness_check = _load_consumers("vc_ref_missing")
    body = _build_body(
        ac_lines=(
            "- [ ] AC1: concrete AC 1 <!-- runtime-verification: true -->\n"
            "- [ ] AC2: concrete AC 2 <!-- runtime-verification: true -->"
        ),
        # AC2 deliberately has no VC comment reference.
        vc_lines="# AC1\n$ rg -n 'concrete' file1.py\n$ rg -n 'other' file2.py",
        applicable_acs="  - AC1\n  - AC2",
        runtime_assertion_bindings=(
            _binding("hook-chain-runtime-smoke", _HOOK_ASSERTION_1, "AC1")
            + _binding("hook-chain-runtime-smoke", _HOOK_ASSERTION_2, "AC2")
        ),
    )
    errors = contract_readiness_check.check_runtime_assertion_binding_coverage(body)
    assert len(errors) == 1
    minimal_context = " ".join(errors[0]["minimal_context"])
    assert "ac_missing_vc_reference" in minimal_context


def test_fully_valid_binding_passes():
    """GIVEN a hard-required profile with complete, referentially valid
    bindings
    WHEN the coverage check evaluates
    THEN it returns no errors (AC8: valid fixtures do not regress)."""
    _, contract_readiness_check = _load_consumers("fully_valid")
    body = _build_body(
        ac_lines=(
            "- [ ] AC1: concrete AC 1 <!-- runtime-verification: true -->\n"
            "- [ ] AC2: concrete AC 2 <!-- runtime-verification: true -->"
        ),
        vc_lines="# AC1\n$ rg -n 'concrete' file1.py\n# AC2\n$ rg -n 'concrete' file2.py",
        applicable_acs="  - AC1\n  - AC2",
        runtime_assertion_bindings=(
            _binding("hook-chain-runtime-smoke", _HOOK_ASSERTION_1, "AC1")
            + _binding("hook-chain-runtime-smoke", _HOOK_ASSERTION_2, "AC2")
        ),
    )
    errors = contract_readiness_check.check_runtime_assertion_binding_coverage(body)
    assert errors == []


# --- AC6: policy integrity failure vs. Issue-side needs_fix ---------------


class _StubPolicyUnavailableEvaluator:
    """Stand-in for a freshly-loaded `extension_surface_policy_matcher`
    module whose policy's verification_profiles mapping is malformed
    (dangling profile reference / duplicate assertion id) -- both consumer
    functions only ever touch `evaluator.PolicyLoadError` /
    `evaluator.evaluate_runtime_assertion_binding_coverage`, so this minimal
    stub is sufficient to force the "policy unavailable" branch
    deterministically without a real broken policy YAML file on disk
    (mirrors `test_extension_surface_advisory_routing_parity.py`'s
    `_StubPolicyUnavailableEvaluator` pattern for the pre-existing
    EXTSURF002 escalation)."""

    class PolicyLoadError(Exception):
        pass

    @classmethod
    def evaluate_runtime_assertion_binding_coverage(cls, **kwargs):
        raise cls.PolicyLoadError(
            "synthetic dangling profile reference / duplicate assertion id for AC6 test"
        )


_ORDINARY_HARD_MATCH_BODY = _BODY_TEMPLATE.format(
    ac_section="- [ ] AC1: concrete AC 1 <!-- runtime-verification: true -->",
    vc_section="# AC1\n$ rg -n 'concrete' file1.py",
    applicable_acs="  - AC1",
    runtime_assertion_bindings="",
)


def test_policy_integrity_failure_escalates_to_human_judgment_not_needs_fix():
    """GIVEN the shared evaluator raises PolicyLoadError (a policy-side
    integrity defect: dangling profile reference / duplicate assertion id)
    WHEN `check_runtime_assertion_binding_coverage()` evaluates
    THEN it reports a single `runtime_assertion_binding_coverage_policy_
    unavailable` (RUNTIMEASSERT002) finding -- distinguishable from the
    ordinary Issue-side `runtime_assertion_binding_coverage` (RUNTIMEASSERT001)
    needs_fix finding (Issue #2771 AC6)."""
    _, contract_readiness_check = _load_consumers("policy_integrity")
    contract_readiness_check._load_extension_surface_policy_matcher = (
        lambda: _StubPolicyUnavailableEvaluator
    )

    errors = contract_readiness_check.check_runtime_assertion_binding_coverage(
        _ORDINARY_HARD_MATCH_BODY
    )
    assert len(errors) == 1
    assert errors[0]["category"] == "runtime_assertion_binding_coverage_policy_unavailable"
    assert errors[0]["rule_id"] == "RUNTIMEASSERT002"
    assert "synthetic dangling profile reference" in errors[0]["minimal_context"][0]


def test_review_issue_downgrades_policy_integrity_failure_to_warn():
    """Parity companion: `check_issue_contract.py`'s C15 downgrades the same
    PolicyLoadError to a non-blocking `CheckResult.WARN` (never FAIL),
    mirroring the pre-existing C14/EXTSURF002 divergence -- both sides
    surface the defect, but only the readiness gate hard-escalates it."""
    check_issue_contract, _ = _load_consumers("policy_integrity_review_issue")
    check_issue_contract._load_extension_surface_policy_matcher = (
        lambda: _StubPolicyUnavailableEvaluator
    )

    status, issues = check_issue_contract.check_c15_runtime_assertion_binding_coverage(
        _ORDINARY_HARD_MATCH_BODY, "implementation"
    )
    assert status == check_issue_contract.CheckResult.WARN
    assert status != check_issue_contract.CheckResult.FAIL
    assert issues and "synthetic dangling profile reference" in issues[0]


# --- cross-consumer parity ------------------------------------------------


def test_review_issue_and_issue_contract_review_agree_on_needs_fix():
    """Both consumers must reach the same verdict (needs_fix) for the same
    fixture body, because both call the exact same shared evaluator
    function -- structural parity, not independently re-derived logic
    (Issue #2771 In Scope: 'review-issue と issue-contract-review の双方で
    同一 shared evaluator を使い parity を維持する')."""
    check_issue_contract, contract_readiness_check = _load_consumers("parity_needs_fix")

    body = _build_body(
        ac_lines="- [ ] AC1: concrete AC 1 <!-- runtime-verification: true -->",
        vc_lines="# AC1\n$ rg -n 'concrete' file1.py",
        applicable_acs="  - AC1",
        # Only 1 of 2 hard-required assertions bound -> missing.
        runtime_assertion_bindings=_binding("hook-chain-runtime-smoke", _HOOK_ASSERTION_1, "AC1"),
    )

    review_status, review_issues = check_issue_contract.check_c15_runtime_assertion_binding_coverage(
        body, "implementation"
    )
    readiness_errors = contract_readiness_check.check_runtime_assertion_binding_coverage(body)

    assert review_status == check_issue_contract.CheckResult.FAIL
    assert review_issues
    assert len(readiness_errors) == 1
    assert readiness_errors[0]["category"] == "runtime_assertion_binding_coverage"


def test_review_issue_and_issue_contract_review_agree_on_approve():
    """Both consumers must agree a fully-valid fixture is approve/no-error."""
    check_issue_contract, contract_readiness_check = _load_consumers("parity_approve")

    body = _build_body(
        ac_lines=(
            "- [ ] AC1: concrete AC 1 <!-- runtime-verification: true -->\n"
            "- [ ] AC2: concrete AC 2 <!-- runtime-verification: true -->"
        ),
        vc_lines="# AC1\n$ rg -n 'concrete' file1.py\n# AC2\n$ rg -n 'concrete' file2.py",
        applicable_acs="  - AC1\n  - AC2",
        runtime_assertion_bindings=(
            _binding("hook-chain-runtime-smoke", _HOOK_ASSERTION_1, "AC1")
            + _binding("hook-chain-runtime-smoke", _HOOK_ASSERTION_2, "AC2")
        ),
    )

    review_status, review_issues = check_issue_contract.check_c15_runtime_assertion_binding_coverage(
        body, "implementation"
    )
    readiness_errors = contract_readiness_check.check_runtime_assertion_binding_coverage(body)

    assert review_status == check_issue_contract.CheckResult.PASS
    assert review_issues == []
    assert readiness_errors == []
