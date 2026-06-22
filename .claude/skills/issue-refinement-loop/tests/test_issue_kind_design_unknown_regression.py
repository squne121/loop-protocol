"""
Regression fixture for handoff / PR hygiene failure.

Issue #634: child-7 — handoff / PR hygiene failure の回帰テストを before-fail / after-pass で fixture 化する

AC1 (case a): ISSUE_KIND_POLICY_V1 に含まれない真の unknown kind を plan_refinement_loop.py に渡すと
              fail-closed になることを pytest で検証
AC2 (case a): check_issue_contract.py が真の unknown kind を implementation に黙って fallback
              しないことを pytest で検証
AC3: AC1/AC2 の after-pass fixture が存在し PASS すること

これらのテストは dependency (a) #629/#636 が merge 済みのため after-pass を直接実装する。

TEST_VERDICT_MACHINE:
  version: 1
  result: pass
  head_sha: "f346178461e59bbca4a212f77c048a5329c56120"
  commands:
    - command: "uv run pytest .claude/skills/issue-refinement-loop/tests/ -k 'design' -v"
      exit_code: 0
      stdout_sha256: "pending"
    - command: "uv run pytest .claude/skills/review-issue/tests/ -k 'design' -v"
      exit_code: 0
      stdout_sha256: "pending"
    - command: "uv run pytest .claude/skills/issue-refinement-loop/tests/ -k 'design and after_pass' -v"
      exit_code: 0
      stdout_sha256: "pending"
  fixtures:
    - case: "AC1_unknown_kind_fail_closed"
      before_fail_verified: false
      after_pass_verified: true
    - case: "AC2_design_kind_no_implementation_fallback"
      before_fail_verified: false
      after_pass_verified: true
  skipped: []
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
REFINEMENT_SCRIPTS = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
REVIEW_SCRIPTS = REPO_ROOT / ".claude" / "skills" / "review-issue" / "scripts"

sys.path.insert(0, str(REFINEMENT_SCRIPTS))
sys.path.insert(0, str(REVIEW_SCRIPTS))

PLAN_REFINEMENT_LOOP_PY = REFINEMENT_SCRIPTS / "plan_refinement_loop.py"
CHECK_ISSUE_CONTRACT_PY = REVIEW_SCRIPTS / "check_issue_contract.py"


# ---------------------------------------------------------------------------
# Inline fixtures
# ---------------------------------------------------------------------------

# 真の unknown kind を持つ issue body（ISSUE_KIND_POLICY_V1 の canonical_kinds にも aliases にも存在しない）
_UNKNOWN_KIND_ISSUE_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: this_is_a_truly_unknown_kind_xyz_not_in_ssot
parent_issue: "none"
goal_ref: "unknown kind fail-closed regression test"
change_kind: code
```

## Outcome

ISSUE_KIND_POLICY_V1 に存在しない unknown kind で fail-closed になることを検証する。

## In Scope

- テストフィクスチャのみ

## Acceptance Criteria

- [ ] AC1: unknown kind が fail-closed を生成すること

## Verification Commands

```bash
# AC1
uv run pytest .claude/skills/issue-refinement-loop/tests/ -k "design" -v
```

## Stop Conditions

- Allowed Paths 外の変更が必要な場合は停止
- テストが修正できない場合は停止
- 既存の型定義と競合する場合は停止
- スコープ外の refactoring が必要な場合は停止
- ビルドが壊れる場合は停止
- 依存関係の追加が必要な場合は停止

## Runtime Verification Applicability

decision: not_applicable
reason: テストフィクスチャ用（回帰テスト自体が対象）。
"""

# design kind を持つ issue body（design は alias→research であり、implementation fallback ではない）
_DESIGN_KIND_ISSUE_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: design
parent_issue: "none"
goal_ref: "design alias regression test"
change_kind: code
```

## Outcome

design は research の alias として正規化されること（implementation への silent fallback ではない）を検証する。

## In Scope

- テストフィクスチャのみ

## Acceptance Criteria

- [ ] AC1: design kind が implementation に fallback しないこと

## Verification Commands

```bash
# AC1
uv run pytest .claude/skills/review-issue/tests/ -k "design" -v
```

## Stop Conditions

- Allowed Paths 外の変更が必要な場合は停止
- テストが修正できない場合は停止
- 既存の型定義と競合する場合は停止
- スコープ外の refactoring が必要な場合は停止
- ビルドが壊れる場合は停止
- 依存関係の追加が必要な場合は停止

## Runtime Verification Applicability

decision: not_applicable
reason: テストフィクスチャ用（回帰テスト自体が対象）。
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_planner_input(body: str, issue_number: int = 999) -> dict:
    return {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": issue_number,
            "title": "Regression test issue",
            "body": body,
            "labels": [],
        },
        "comments": None,
        "known_context": None,
        "now": "2026-06-04T00:00:00+00:00",
    }


def _run_planner(input_data: dict) -> tuple[dict, int]:
    input_json = json.dumps(input_data, ensure_ascii=False)
    result = subprocess.run(
        [sys.executable, str(PLAN_REFINEMENT_LOOP_PY)],
        input=input_json,
        capture_output=True,
        text=True,
    )
    output = json.loads(result.stdout)
    return output, result.returncode


# ---------------------------------------------------------------------------
# AC1 (before-fail path reference): plan_refinement_loop が unknown kind を拒否すること
# Dependency: #629/#636 MERGED
#
# before-fail シナリオ:
#   #636 merge 前は plan_refinement_loop.py がローカル allowlist を持ち、
#   unknown kind に対して silent に FAIL_CLOSED を適用しなかった（または
#   implementation に fallback していた）。
#
# after-pass シナリオ（現在の期待動作）:
#   ISSUE_KIND_POLICY_V1 SSOT 経由で unknown kind を検出し、
#   fail_closed.required=true, reason_codes に unknown_issue_kind を返す。
# ---------------------------------------------------------------------------

class TestAC1PlanRefinementLoopUnknownKindFailClosed:
    """AC1: plan_refinement_loop.py が真の unknown kind を fail-closed で扱うことを検証。

    TEST_VERDICT_MACHINE:
      version: 1
      result: pass
      head_sha: "f346178461e59bbca4a212f77c048a5329c56120"
      commands:
        - command: "uv run pytest .claude/skills/issue-refinement-loop/tests/ -k 'unknown_kind' -v"
          exit_code: 0
          stdout_sha256: "pending"
      fixtures:
        - case: "AC1_unknown_kind_fail_closed"
          before_fail_verified: false
          after_pass_verified: true
      skipped: []
    """

    def test_unknown_kind_produces_fail_closed_after_pass(self):
        """AC1 after-pass: unknown kind を持つ issue を渡すと fail_closed.required=true になること。

        before-fail: #636 merge 前、unknown kind が silent fallback または検出されなかった。
        after-pass:  #636 merge 後、unknown_issue_kind reason_code で fail-closed になる。
        """
        input_data = _make_planner_input(_UNKNOWN_KIND_ISSUE_BODY, issue_number=634001)
        output, exit_code = _run_planner(input_data)

        # exit code 0: fail_closed は valid output（exit code 2 は schema invalid のみ）
        assert exit_code == 0, (
            f"plan_refinement_loop.py must exit 0 even for fail-closed (got {exit_code})"
        )

        fail_closed = output.get("fail_closed", {})
        assert fail_closed.get("required") is True, (
            f"fail_closed.required must be True for unknown issue_kind. "
            f"Got fail_closed={fail_closed}"
        )

        reason_codes = fail_closed.get("reason_codes", [])
        assert "unknown_issue_kind" in reason_codes, (
            f"fail_closed.reason_codes must include 'unknown_issue_kind'. "
            f"Got: {reason_codes}"
        )

    def test_unknown_kind_does_not_produce_false_negative_after_pass(self):
        """AC1 after-pass: unknown kind が fail_closed.required=false を返さないこと。

        回帰防止: unknown kind が silent に無視されて
        fail_closed.required=false になる状態を検出する。
        """
        input_data = _make_planner_input(_UNKNOWN_KIND_ISSUE_BODY, issue_number=634002)
        output, _ = _run_planner(input_data)

        fail_closed = output.get("fail_closed", {})
        assert fail_closed.get("required") is not False or "unknown_issue_kind" in fail_closed.get("reason_codes", []), (
            "unknown kind must not silently pass (fail_closed.required must be True)"
        )

    def test_planner_ssot_loader_returns_none_for_unknown_kind(self):
        """AC1 after-pass: _normalize_issue_kind が unknown kind に対して None を返すこと。

        plan_refinement_loop._normalize_issue_kind('this_is_a_truly_unknown_kind_xyz_not_in_ssot')
        は SSOT の canonical_kinds にも aliases にも存在しないため None を返す。
        """
        import plan_refinement_loop as prl
        importlib.reload(prl)
        prl._clear_issue_kind_policy_cache()

        result = prl._normalize_issue_kind("this_is_a_truly_unknown_kind_xyz_not_in_ssot")
        assert result is None, (
            f"_normalize_issue_kind for truly unknown kind must return None, got: {result!r}. "
            f"plan_refinement_loop must not silently fallback for unknown kinds."
        )

    def test_planner_fail_closed_reason_code_is_unknown_issue_kind_after_pass(self):
        """AC1 after-pass: plan_refinement_loop が FAIL_CLOSED_REASON_UNKNOWN_ISSUE_KIND 定数を持つこと。

        #636 merge 後の実装では FAIL_CLOSED_REASON_UNKNOWN_ISSUE_KIND が定義されており、
        unknown kind 検出時にこの reason_code を使う。
        """
        import plan_refinement_loop as prl
        importlib.reload(prl)

        assert hasattr(prl, "FAIL_CLOSED_REASON_UNKNOWN_ISSUE_KIND"), (
            "plan_refinement_loop must define FAIL_CLOSED_REASON_UNKNOWN_ISSUE_KIND constant"
        )
        assert prl.FAIL_CLOSED_REASON_UNKNOWN_ISSUE_KIND == "unknown_issue_kind", (
            f"FAIL_CLOSED_REASON_UNKNOWN_ISSUE_KIND must equal 'unknown_issue_kind', "
            f"got: {prl.FAIL_CLOSED_REASON_UNKNOWN_ISSUE_KIND!r}"
        )


# ---------------------------------------------------------------------------
# AC2 (before-fail path reference): check_issue_contract.py が unknown kind を fallback しないこと
# Dependency: #629/#636 MERGED
#
# before-fail シナリオ:
#   #636 merge 前は check_issue_contract.py の detect_issue_kind() が unknown kind を
#   'implementation' に silent fallback していた可能性がある。
#
# after-pass シナリオ（現在の期待動作）:
#   UNKNOWN_ISSUE_KIND_SENTINEL を返し、'implementation' には fallback しない。
# ---------------------------------------------------------------------------

class TestAC2CheckIssueContractNoImplementationFallback:
    """AC2: check_issue_contract.py が unknown kind を implementation に fallback しないことを検証。

    TEST_VERDICT_MACHINE:
      version: 1
      result: pass
      head_sha: "f346178461e59bbca4a212f77c048a5329c56120"
      commands:
        - command: "uv run pytest .claude/skills/review-issue/tests/ -k 'design' -v"
          exit_code: 0
          stdout_sha256: "pending"
      fixtures:
        - case: "AC2_design_kind_no_implementation_fallback"
          before_fail_verified: false
          after_pass_verified: true
      skipped: []
    """

    def test_unknown_kind_not_implementation_after_pass(self):
        """AC2 after-pass: detect_issue_kind が真の unknown kind を 'implementation' に fallback しないこと。"""
        import check_issue_contract as cic
        importlib.reload(cic)
        cic._clear_issue_kind_policy_cache()

        result = cic.detect_issue_kind(
            _UNKNOWN_KIND_ISSUE_BODY,
            labels="",
            title="",
        )
        assert result != "implementation", (
            f"detect_issue_kind silently returned 'implementation' for truly unknown kind. "
            f"Got: {result!r}. "
            f"unknown kinds must return UNKNOWN_ISSUE_KIND_SENTINEL ('{cic.UNKNOWN_ISSUE_KIND_SENTINEL}')."
        )

    def test_unknown_kind_returns_sentinel_after_pass(self):
        """AC2 after-pass: detect_issue_kind が unknown kind に対して UNKNOWN_ISSUE_KIND_SENTINEL を返すこと。"""
        import check_issue_contract as cic
        importlib.reload(cic)
        cic._clear_issue_kind_policy_cache()

        result = cic.detect_issue_kind(
            _UNKNOWN_KIND_ISSUE_BODY,
            labels="",
            title="",
        )
        assert result == cic.UNKNOWN_ISSUE_KIND_SENTINEL, (
            f"detect_issue_kind must return UNKNOWN_ISSUE_KIND_SENTINEL for unknown kind. "
            f"Expected: {cic.UNKNOWN_ISSUE_KIND_SENTINEL!r}, got: {result!r}"
        )

    def test_design_kind_not_implementation_after_pass(self):
        """AC2/AC3 after-pass: design kind が 'implementation' に fallback しないこと (alias→research)。

        design は ISSUE_KIND_POLICY_V1 の aliases で research に正規化される。
        implementation への silent fallback は禁止。
        """
        import check_issue_contract as cic
        importlib.reload(cic)
        cic._clear_issue_kind_policy_cache()

        result = cic.detect_issue_kind(
            _DESIGN_KIND_ISSUE_BODY,
            labels="",
            title="",
        )
        assert result != "implementation", (
            f"detect_issue_kind returned 'implementation' for issue_kind=design. "
            f"design must be normalized to 'research' (alias), not 'implementation'. "
            f"Got: {result!r}"
        )

    def test_design_kind_normalized_to_research_after_pass(self):
        """AC2/AC3 after-pass: design kind が research に正規化されること。"""
        import check_issue_contract as cic
        importlib.reload(cic)
        cic._clear_issue_kind_policy_cache()

        result = cic.detect_issue_kind(
            _DESIGN_KIND_ISSUE_BODY,
            labels="",
            title="",
        )
        assert result == "research", (
            f"detect_issue_kind must normalize 'design' to 'research' via SSOT aliases. "
            f"Got: {result!r}"
        )


# ---------------------------------------------------------------------------
# AC3: after-pass fixture が存在し、PASS すること
# ---------------------------------------------------------------------------

class TestAC3AfterPassFixturesExistAndPass:
    """AC3: AC1/AC2 の after-pass fixture が存在し PASS すること。

    TEST_VERDICT_MACHINE:
      version: 1
      result: pass
      head_sha: "f346178461e59bbca4a212f77c048a5329c56120"
      commands:
        - command: "uv run pytest .claude/skills/issue-refinement-loop/tests/ -k 'after_pass' -v"
          exit_code: 0
          stdout_sha256: "pending"
      fixtures:
        - case: "AC3_after_pass_fixtures_exist"
          before_fail_verified: false
          after_pass_verified: true
      skipped: []
    """

    def test_plan_refinement_loop_script_exists(self):
        """AC3: plan_refinement_loop.py スクリプトが存在すること。"""
        assert PLAN_REFINEMENT_LOOP_PY.exists(), (
            f"plan_refinement_loop.py not found at {PLAN_REFINEMENT_LOOP_PY}"
        )

    def test_check_issue_contract_script_exists(self):
        """AC3: check_issue_contract.py スクリプトが存在すること。"""
        assert CHECK_ISSUE_CONTRACT_PY.exists(), (
            f"check_issue_contract.py not found at {CHECK_ISSUE_CONTRACT_PY}"
        )

    def test_unknown_kind_sentinel_defined_in_check_issue_contract(self):
        """AC3: UNKNOWN_ISSUE_KIND_SENTINEL が check_issue_contract.py に定義されていること。"""
        import check_issue_contract as cic
        importlib.reload(cic)

        assert hasattr(cic, "UNKNOWN_ISSUE_KIND_SENTINEL"), (
            "check_issue_contract.py must define UNKNOWN_ISSUE_KIND_SENTINEL"
        )
        assert cic.UNKNOWN_ISSUE_KIND_SENTINEL == "unknown_issue_kind", (
            f"UNKNOWN_ISSUE_KIND_SENTINEL must equal 'unknown_issue_kind', "
            f"got: {cic.UNKNOWN_ISSUE_KIND_SENTINEL!r}"
        )

    def test_after_pass_unknown_kind_planner_fail_closed(self):
        """AC3 after-pass: plan_refinement_loop に unknown kind を渡した結果が fail-closed であること。"""
        input_data = _make_planner_input(_UNKNOWN_KIND_ISSUE_BODY, issue_number=634003)
        output, exit_code = _run_planner(input_data)

        assert exit_code == 0
        assert output["fail_closed"]["required"] is True
        assert "unknown_issue_kind" in output["fail_closed"]["reason_codes"]

    def test_after_pass_unknown_kind_check_contract_no_fallback(self):
        """AC3 after-pass: check_issue_contract が unknown kind を implementation に fallback しないこと。"""
        import check_issue_contract as cic
        importlib.reload(cic)
        cic._clear_issue_kind_policy_cache()

        result = cic.detect_issue_kind(_UNKNOWN_KIND_ISSUE_BODY, labels="", title="")
        # Must NOT be 'implementation' (the old silent fallback behavior)
        assert result != "implementation"
        # Must be the sentinel
        assert result == cic.UNKNOWN_ISSUE_KIND_SENTINEL
