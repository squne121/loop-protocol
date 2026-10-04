"""Issue #2852 AC9 (cross-checker parity): `review-issue` (C15 + carrier) and
`issue-contract-review` (RUNTIMEASSERT001/003) must return the same verdict and
the same classification for the same input -- both call the exact same shared
evaluator and the same carrier formatter, so parity is structural.

Covered: 3 dispositions + legacy mixed input, explicit-dispositive-only,
pure legacy (no carrier on either side), a needs_fix input (RUNTIMEASSERT001 and
C15 FAIL, no carrier on either side), a policy-unavailable input (no carrier on
either side), carrier shape identity (`category` == `code`), and the readiness
status invariant (the carrier never drives `overall_status`; it follows the
EXTSURF003 non-blocking precedent).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[3]
_REVIEW_ISSUE_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "review-issue" / "scripts"
_CONTRACT_READINESS_SCRIPTS = _HERE.parent / "scripts"

_CODE = "runtime_assertion_disposition_classification"
_SKILL = "skill-invocation-runtime-smoke"
_SKILL_A1 = "procedure_steps_executed_in_declared_order"
_SKILL_A2 = "output_contract_schema_fields_present"
_SUBAGENT = "subagent-lifecycle-causal-evidence-smoke"
_SUBAGENT_A1 = "subagent_start_stop_causal_evidence_correlated"


def _load_module_from_path(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _load_consumers(suffix: str):
    check_issue_contract = _load_module_from_path(
        f"check_issue_contract_for_assertion_disposition_{suffix}",
        _REVIEW_ISSUE_SCRIPTS / "check_issue_contract.py",
    )
    contract_readiness_check = _load_module_from_path(
        f"contract_readiness_check_for_assertion_disposition_{suffix}",
        _CONTRACT_READINESS_SCRIPTS / "contract_readiness_check.py",
    )
    return check_issue_contract, contract_readiness_check


_BODY_TEMPLATE = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "assertion disposition parity fixture"
change_kind: workflow
```

## Parent Issue

none

## Outcome

`.claude/skills/foo/SKILL.md` の変更に対し runtime_assertion_bindings の分類が両 checker で一致する。

## Parent Goal Ref

- Goal: disposition 分類の checker parity
- Desired Destination: 同一入力で同一 verdict と分類

## Current Validated Scope

- `.claude/skills/foo/SKILL.md` を変更する

## Remaining Parent Gaps

なし

## In Scope

- `.claude/skills/foo/SKILL.md` を変更する

## Out of Scope

- その他のファイルの変更

## Required Skills

なし

## Acceptance Criteria

- [ ] AC1: 手順 AC が実行順を検証する <!-- runtime-verification: true -->
- [ ] AC2: 別の deterministic test が出力契約を検証する

## Verification Commands

```bash
# AC1
$ rg -n "procedure" .claude/skills/foo/SKILL.md

# AC2
$ rg -n "output" .claude/skills/foo/SKILL.md
```

## Stop Conditions

- Allowed Paths 外の変更が必要な場合は停止
- テストが修正できない場合は停止
- 既存の型定義と競合する場合は停止
- スコープ外の refactoring が必要な場合は停止
- ビルドが壊れる場合は停止
- 依存関係の追加が必要な場合は停止

## Runtime Verification Applicability

```yaml
decision: immediate
applicable_acs:
  - AC1
execution_environment:
  cli_tools:
    - rg
skip_conditions:
  - "none"
fallback_policy:
  fallback_success_is_pass: false
artifact_requirements:
  - "artifacts/out.json"
runtime_assertion_bindings:
__BINDINGS__
```

## Allowed Paths

- `.claude/skills/foo/SKILL.md`
"""


def _body(bindings: str) -> str:
    return _BODY_TEMPLATE.replace("__BINDINGS__", bindings.rstrip("\n"))


_LEGACY = (
    f"  - profile: {_SKILL}\n    assertion: {_SKILL_A1}\n    ac: AC1\n"
    f"  - profile: {_SKILL}\n    assertion: {_SKILL_A2}\n    ac: AC1\n"
    f"  - profile: {_SUBAGENT}\n    assertion: {_SUBAGENT_A1}\n    ac: AC1\n"
)
_MIXED = (
    f"  - profile: {_SKILL}\n    assertion: {_SKILL_A1}\n    ac: AC1\n"
    f"  - profile: {_SKILL}\n    assertion: {_SKILL_A2}\n"
    "    disposition: non_dispositive_readiness_compat\n"
    "    demonstrated_by: AC2\n"
    '    reason: "AC2 が出力契約の実証を所有する"\n'
    f"  - profile: {_SUBAGENT}\n    assertion: {_SUBAGENT_A1}\n"
    "    disposition: not_applicable\n"
    '    reason: "handoff が production path に存在しない"\n'
)
_EXPLICIT_DISPOSITIVE_ONLY = (
    f"  - profile: {_SKILL}\n    assertion: {_SKILL_A1}\n    ac: AC1\n    disposition: dispositive\n"
    f"  - profile: {_SKILL}\n    assertion: {_SKILL_A2}\n    ac: AC1\n"
    f"  - profile: {_SUBAGENT}\n    assertion: {_SUBAGENT_A1}\n    ac: AC1\n"
)
_NEEDS_FIX = (
    f"  - profile: {_SKILL}\n    assertion: {_SKILL_A1}\n    ac: AC1\n"
    f"  - profile: {_SUBAGENT}\n    assertion: {_SUBAGENT_A1}\n"
    "    disposition: not_applicable\n"
    '    reason: "handoff が存在しない"\n'
)
_MIXED_EVIDENCE = [
    f"{_SKILL}/{_SKILL_A1}: disposition=dispositive; source=legacy_default; demonstrated_by=-; reason=-",
    f"{_SKILL}/{_SKILL_A2}: disposition=non_dispositive_readiness_compat; source=explicit; "
    "demonstrated_by=AC2; reason=AC2 が出力契約の実証を所有する",
    f"{_SUBAGENT}/{_SUBAGENT_A1}: disposition=not_applicable; source=explicit; "
    "demonstrated_by=-; reason=handoff が production path に存在しない",
]


def _readiness_result(crc, body: str) -> dict:
    return crc.build_result(body, "static", crc.run_validate_issue_body(body), None, None)


def _carrier_errors(readiness_result: dict) -> list[dict]:
    return [e for e in readiness_result["errors"] if e.get("category") == _CODE]


def test_mixed_input_same_verdict_and_same_classification_on_both_checkers():
    """GIVEN 3 dispositions + a legacy binding
    WHEN both checkers evaluate the identical body
    THEN both approve and expose the identical classification as the same verbatim evidence lines."""
    cic, crc = _load_consumers("mixed")
    body = _body(_MIXED)

    c15_status, c15_issues = cic.check_c15_runtime_assertion_binding_coverage(body, "implementation")
    assert c15_status == cic.CheckResult.PASS and c15_issues == []
    assert crc.check_runtime_assertion_binding_coverage(body) == []

    review_lines = cic.get_runtime_assertion_disposition_carrier(body, "implementation")
    readiness_errors = crc.check_runtime_assertion_disposition_classification(body)
    assert len(readiness_errors) == 1
    readiness_lines = readiness_errors[0]["minimal_context"]
    assert review_lines == readiness_lines == _MIXED_EVIDENCE


def test_carrier_shape_identity_category_equals_code():
    """GIVEN a carrier-producing input
    WHEN each checker publishes the carrier
    THEN readiness category and review code are both runtime_assertion_disposition_classification,
    the readiness entry is info severity and the review entry is advisory."""
    cic, crc = _load_consumers("shape")
    body = _body(_MIXED)
    readiness_error = crc.check_runtime_assertion_disposition_classification(body)[0]
    assert readiness_error["category"] == _CODE
    assert readiness_error["rule_id"] == "RUNTIMEASSERT003"
    assert readiness_error["severity"] == "info"
    assert readiness_error["autofixable"] is False

    review = cic.result_to_dict(
        cic.run_checks(body, labels="phase/implementation,kind/implementation", title="実装: parity")
    )
    entry = next(e for e in review["non_blocking_improvements"] if e["code"] == _CODE)
    assert entry["severity"] == "advisory"
    assert entry["evidence"] == readiness_error["minimal_context"]
    assert _CODE in cic._NON_BLOCKING_READINESS_ERROR_CATEGORIES
    assert _CODE in crc._NON_BLOCKING_READINESS_ERROR_CATEGORIES


def test_readiness_status_is_not_driven_by_the_carrier():
    """GIVEN an approving mixed input
    WHEN the full readiness result is built
    THEN status is go, the carrier is in errors only as the non-blocking category, and no
    blocking fix_hint/minimal_context is taken from it."""
    _, crc = _load_consumers("status")
    body = _body(_MIXED)
    result = _readiness_result(crc, body)
    assert result["status"] == "go"
    assert len(_carrier_errors(result)) == 1
    assert result["fix_hint"] is None and result["minimal_context"] == []

    legacy_result = _readiness_result(crc, _body(_LEGACY))
    assert legacy_result["status"] == "go"
    assert _carrier_errors(legacy_result) == []


def test_explicit_dispositive_only_input_emits_the_carrier_on_both_sides():
    """GIVEN one explicit `disposition: dispositive` among legacy bindings
    WHEN both checkers evaluate
    THEN both emit the same 3-line carrier."""
    cic, crc = _load_consumers("explicit")
    body = _body(_EXPLICIT_DISPOSITIVE_ONLY)
    review_lines = cic.get_runtime_assertion_disposition_carrier(body, "implementation")
    readiness_errors = crc.check_runtime_assertion_disposition_classification(body)
    assert len(review_lines) == 3
    assert [e["minimal_context"] for e in readiness_errors] == [review_lines]


def test_pure_legacy_input_emits_no_carrier_on_either_side():
    """GIVEN a pure legacy 3-field input
    WHEN both checkers evaluate
    THEN neither emits a carrier and both approve."""
    cic, crc = _load_consumers("legacy")
    body = _body(_LEGACY)
    assert cic.check_c15_runtime_assertion_binding_coverage(body, "implementation")[0] == cic.CheckResult.PASS
    assert crc.check_runtime_assertion_binding_coverage(body) == []
    assert cic.get_runtime_assertion_disposition_carrier(body, "implementation") == []
    assert crc.check_runtime_assertion_disposition_classification(body) == []


def test_needs_fix_input_same_failure_and_no_carrier_on_either_side():
    """GIVEN a not_applicable next to a missing required assertion
    WHEN both checkers evaluate
    THEN C15 FAILs and RUNTIMEASSERT001 is raised with the same reasons, and neither emits a carrier."""
    cic, crc = _load_consumers("needs_fix")
    body = _body(_NEEDS_FIX)
    c15_status, c15_issues = cic.check_c15_runtime_assertion_binding_coverage(body, "implementation")
    readiness_errors = crc.check_runtime_assertion_binding_coverage(body)
    assert c15_status == cic.CheckResult.FAIL
    assert len(readiness_errors) == 1 and readiness_errors[0]["rule_id"] == "RUNTIMEASSERT001"
    assert readiness_errors[0]["minimal_context"] == c15_issues
    assert any(_SKILL_A2 in issue for issue in c15_issues)

    assert cic.get_runtime_assertion_disposition_carrier(body, "implementation") == []
    assert crc.check_runtime_assertion_disposition_classification(body) == []
    status = _readiness_result(crc, body)["status"]
    assert status == "needs_fix"
    assert _carrier_errors(_readiness_result(crc, body)) == []


class _StubPolicyUnavailableEvaluator:
    class PolicyLoadError(Exception):
        pass

    @classmethod
    def evaluate_issue_risk_trigger(cls, **kwargs):
        raise cls.PolicyLoadError("synthetic")

    @classmethod
    def evaluate_allowed_paths(cls, *args, **kwargs):
        raise cls.PolicyLoadError("synthetic")

    @classmethod
    def evaluate_runtime_assertion_binding_coverage(cls, **kwargs):
        raise cls.PolicyLoadError("synthetic")


def test_policy_unavailable_emits_no_carrier_on_either_side(monkeypatch):
    """GIVEN a policy integrity failure
    WHEN both checkers evaluate
    THEN C15 WARNs, RUNTIMEASSERT002 is raised, and neither emits a carrier."""
    cic, crc = _load_consumers("policy_unavailable")
    monkeypatch.setattr(cic, "_load_extension_surface_policy_matcher", lambda: _StubPolicyUnavailableEvaluator)
    monkeypatch.setattr(crc, "_load_extension_surface_policy_matcher", lambda: _StubPolicyUnavailableEvaluator)
    body = _body(_MIXED)

    assert cic.check_c15_runtime_assertion_binding_coverage(body, "implementation")[0] == cic.CheckResult.WARN
    errors = crc.check_runtime_assertion_binding_coverage(body)
    assert [e["rule_id"] for e in errors] == ["RUNTIMEASSERT002"]
    assert cic.get_runtime_assertion_disposition_carrier(body, "implementation") == []
    assert crc.check_runtime_assertion_disposition_classification(body) == []


def test_carrier_projection_through_readiness_merge_keeps_classification_and_single_entry():
    """GIVEN review-issue output and the full readiness result of the same body
    WHEN merge_readiness_into_review_result projects them together
    THEN the merged result holds exactly one carrier whose evidence equals both sides' evidence,
    and the verdict stays approve."""
    cic, crc = _load_consumers("merge")
    body = _body(_MIXED)
    review = cic.result_to_dict(
        cic.run_checks(body, labels="phase/implementation,kind/implementation", title="実装: parity")
    )
    readiness = _readiness_result(crc, body)
    readiness["body_sha256"] = review["body_sha256"]
    merged = cic.merge_readiness_into_review_result(
        review,
        readiness,
        readiness_artifact_path="test_artifact_path/readiness.json",
        iteration_id="parity-merge",
    )
    carriers = [e for e in merged["non_blocking_improvements"] if e["code"] == _CODE]
    assert len(carriers) == 1
    assert carriers[0]["evidence"] == _MIXED_EVIDENCE
    assert merged["verdict"] == "approve"
    assert merged["blocking_issues"] == [] and merged["structured_blockers"] == []
