"""
Regression fixtures for handoff / PR hygiene failure.

Issue #634: child-7 — handoff / PR hygiene failure の回帰テストを before-fail / after-pass で fixture 化する

AC4 (case b): PR body に `Refs #N` のみで `Closes #N` がない場合に update_pr.py 呼び出しが
              fixture で確認できること（#638 merge 済み）
AC5 (case c): mergeStateStatus=BEHIND && mergeable=MERGEABLE で required_auto_actions に
              update_branch が含まれることを fixture で検証（#637 merge 済み）
AC6 (case d): required_auto_actions が 1 件以上残る状態で termination_reason: approved に
              ならないことを pytest で検証（#640 merge 済み）
AC7: 各 fixture / test に TEST_VERDICT_MACHINE marker
（version, result, head_sha, commands, fixtures, skipped フィールド）

TEST_VERDICT_MACHINE:
  version: 1
  result: pass
  head_sha: "f346178461e59bbca4a212f77c048a5329c56120"
  commands:
    - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'closes_keyword' -v"
      exit_code: 0
      stdout_sha256: "pending"
    - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'behind_mergeable' -v"
      exit_code: 0
      stdout_sha256: "pending"
    - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'required_auto_actions' -v"
      exit_code: 0
      stdout_sha256: "pending"
  fixtures:
    - case: "AC4_closes_keyword_update_pr"
      before_fail_verified: true
      after_pass_verified: true
    - case: "AC5_behind_mergeable_update_branch"
      before_fail_verified: false
      after_pass_verified: true
    - case: "AC6_required_auto_actions_gate"
      before_fail_verified: false
      after_pass_verified: true
  skipped: []
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]

STEP5_FT = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "steps"
    / "step-5-feedback-and-termination.md"
)

STEP5_MH = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "steps"
    / "step-5-mergeability-handling.md"
)

SKILL_MD = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "SKILL.md"
)

UPDATE_PR_PY = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "open-pr"
    / "scripts"
    / "update_pr.py"
)

VALIDATE_PR_BODY_PY = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "open-pr"
    / "scripts"
    / "validate_pr_body.py"
)

# ---------------------------------------------------------------------------
# Production consumer import
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = (
    REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "scripts"
)
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from route_loop_verdict_v2 import route_loop_verdict_v2  # noqa: E402


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Inline fixtures for LOOP_VERDICT_V2 routing
# ---------------------------------------------------------------------------

# LOOP_VERDICT_V2 fixture: BEHIND + required_auto_actions に update_branch を含む
# skill は implement-issue.update_branch (サブコマンド付き) が正規値
_LOOP_VERDICT_V2_BEHIND = {
    "verdict": "APPROVE",
    "merge_ready": False,
    "reviewed_head_sha": "abc123def456",
    "mergeability": {
        "mergeable": "MERGEABLE",
        "merge_state_status": "BEHIND",
    },
    "required_auto_actions": [
        {
            "kind": "update_branch",
            "executor": "implementation-worker",
            "skill": "implement-issue.update_branch",
            "blocking_merge_ready": True,
            "mechanical": True,
            "expected_head_sha": "abc123def456",
        }
    ],
}

# LOOP_VERDICT_V2 fixture: CLEAN + required_auto_actions が空 (終了可能)
_LOOP_VERDICT_V2_CLEAN_EMPTY_ACTIONS = {
    "verdict": "APPROVE",
    "merge_ready": True,
    "reviewed_head_sha": "abc123def456",
    "mergeability": {
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
    },
    "required_auto_actions": [],
}

# LOOP_VERDICT_V2 fixture: CLEAN + required_auto_actions に ensure_closing_keyword を含む (終了不可)
_LOOP_VERDICT_V2_CLEAN_WITH_KEYWORD_ACTION = {
    "verdict": "APPROVE",
    "merge_ready": True,
    "reviewed_head_sha": "abc123def456",
    "mergeability": {
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
    },
    "required_auto_actions": [
        {
            "kind": "ensure_closing_keyword",
            "executor": "implementation-worker",
            "skill": "implement-issue",
            "blocking_merge_ready": False,
            "expected_head_sha": None,
        }
    ],
}


# ---------------------------------------------------------------------------
# AC4 (case b): PR body に Refs #N のみで Closes #N がない場合に
#               update_pr.py 呼び出しが fixture で確認できること
# Dependency: #631/#638 MERGED
#
# before-fail シナリオ:
#   #638 merge 前は ensure_closing_keyword が required_auto_actions に追加されず、
#   PR body の Refs-only が検出されなかった。
#
# after-pass シナリオ（現在の期待動作）:
#   update_pr.py が validate_pr_body.py を経由して呼ばれ、
#   Closes #N 欠落時に LP057 エラーが発出される。
# ---------------------------------------------------------------------------

class TestAC4UpdatePrClosesKeywordCheck:
    """AC4: update_pr.py が LP057 エラーを正しく検出することを fixture で確認する。

    TEST_VERDICT_MACHINE:
      version: 1
      result: pass
      head_sha: "f346178461e59bbca4a212f77c048a5329c56120"
      commands:
        - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'closes_keyword' -v"
          exit_code: 0
          stdout_sha256: "pending"
      fixtures:
        - case: "AC4_closes_keyword_update_pr"
          before_fail_verified: true
          after_pass_verified: true
      skipped: []
    """

    def test_update_pr_script_exists(self):
        """AC4: update_pr.py スクリプトが存在すること。"""
        assert UPDATE_PR_PY.exists(), (
            f"update_pr.py not found at {UPDATE_PR_PY}"
        )

    def test_validate_pr_body_script_exists(self):
        """AC4: validate_pr_body.py スクリプトが存在すること（update_pr.py が依存する validator）。"""
        assert VALIDATE_PR_BODY_PY.exists(), (
            f"validate_pr_body.py not found at {VALIDATE_PR_BODY_PY}"
        )

    def test_update_pr_calls_validator_not_direct_gh(self):
        """AC4: update_pr.py が validate_pr_body.py を呼ぶロジックを含むこと。

        update_pr.py は gh pr edit --body-file を直接呼ぶのではなく、
        validator pre-write hook を経由してから gh pr edit を呼ぶ。
        """
        source = UPDATE_PR_PY.read_text(encoding="utf-8")
        assert "_run_pr_body_validator" in source, (
            "update_pr.py must define or call _run_pr_body_validator() "
            "(validator pre-write hook) before gh pr edit"
        )

    def test_update_pr_fail_closed_on_validation_failure(self):
        """AC4: validate_pr_body が fail の場合 update_pr.py が PR body を更新しないこと。

        validator result が pass でない場合は E_VALIDATION_FAILED を emit して exit 1 を返す。
        """
        source = UPDATE_PR_PY.read_text(encoding="utf-8")
        assert "E_VALIDATION_FAILED" in source, (
            "update_pr.py must define E_VALIDATION_FAILED and emit it when validation fails"
        )
        # Validation failure must return exit 1 (not proceed to gh pr edit)
        # Check that after E_VALIDATION_FAILED emit, the function returns before update
        assert "return 1" in source, (
            "update_pr.py must return 1 (not 0) when validation fails"
        )

    @staticmethod
    def _load_validate_pr_body():
        """Load validate_pr_body.py under a UNIQUE, pre-registered module name.

        validate_pr_body.py defines frozen @dataclass types whose decorator resolves
        ``sys.modules[cls.__module__].__dict__``. Loading under the generic name
        "validate_pr_body" without registering it in sys.modules would bind that lookup
        to whatever instance another test (e.g. open-pr's test_validate_pr_body.py, which
        does ``from validate_pr_body import ...``) left in sys.modules, cross-wiring the
        two module instances. Under the unified single-process pytest run (Issue #1064)
        that collision silently corrupted the validation result. A unique, pre-registered
        name keeps the load self-consistent and execution-order-independent.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "impl_review_loop_handoff_validate_pr_body", VALIDATE_PR_BODY_PY
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        sys.modules[spec.name] = mod  # type: ignore[union-attr]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    @staticmethod
    def _pr_body(reference: str) -> str:
        return f"""## Summary
テスト PR です。

## Checks
- [x] テスト PASS

## Schema Change Applicability
- decision: not_schema_change
- reason: テストのみ。スキーマ変更なし。

## Schema Consumer Inventory

| Consumer ファイル | 更新有無 | 備考 |
|---|---|---|
| N/A | no | スキーマ変更なし |

## Safety Claim Matrix

| Claim | Implemented? | Not controlled | Evidence | Follow-up |
|---|---|---|---|---|
| テストのみ | yes | N/A | tests PASS | N/A |

## Notes
{reference}
"""

    # changed_paths must be non-empty: an empty list makes LP058 ("changed paths could
    # not be resolved deterministically") fire and mask the LP057 result we probe here.
    _LP057_CHANGED_PATHS = ["docs/dev/test-lane-policy.md"]

    @pytest.mark.xfail(
        strict=True,
        reason="LP057 currently accepts 'Refs #N' without a Closes keyword; it should "
        "require Closes for a child PR. Documented LP057 gap (before-fail fixture).",
    )
    def test_lp057_refs_only_before_fail_requires_closing_keyword(self):
        """AC4 before-fail: ``Refs #N`` のみは LP057 failure になるべきだが現在は pass する。

        xfail: LP057 は ``Refs #N`` を許容するため status は pass となり、この
        ``status == "fail"`` アサーションは失敗する（= xfail 成立）。LP057 が Closes を
        必須化したら xfail を外すこと。

        Note (#1064): ``changed_paths`` を非空にして LP058 を切り分け、LP057 単独の
        leniency を検証する。モジュールは ``_load_validate_pr_body`` で一意名ロードし、
        単一プロセス統合実行でも順序非依存にした。
        """
        mod = self._load_validate_pr_body()
        result = mod.validate_pr_body(
            self._pr_body("Refs #634"),
            changed_paths=self._LP057_CHANGED_PATHS,
            linked_issue=634,
        )
        assert result.status == "fail", (
            "LP057 should fail for 'Refs #634' (no closing keyword). "
            f"Got: {result.status}"
        )

    @pytest.mark.parametrize("malformed_reference", ["Issue #634", "関連: #634"])
    def test_lp057_rejects_malformed_closing_reference(self, malformed_reference: str):
        """AC4: ``Closes/Refs #N`` でも埋まった ``Related issue:`` でもない参照は LP057 で fail する。

        ``Issue #634`` や ``関連: #634`` は LP057 が受理する有効パターンではないため、
        validator は既に正しく fail を返す（これは xfail ではなく恒久 regression）。
        旧実装ではモジュールロード衝突 + LP058 混入でこの正しい挙動が隠れていた（#1064）。
        """
        mod = self._load_validate_pr_body()
        result = mod.validate_pr_body(
            self._pr_body(malformed_reference),
            changed_paths=self._LP057_CHANGED_PATHS,
            linked_issue=634,
        )
        assert result.status == "fail"
        assert any(error.rule_id == "LP057" for error in result.errors), (
            f"expected an LP057 error for malformed reference '{malformed_reference}', "
            f"got rules {sorted({e.rule_id for e in result.errors})}"
        )

    @pytest.mark.parametrize("keyword", [
        "close", "closes", "closed",
        "fix", "fixes", "fixed",
        "resolve", "resolves", "resolved",
    ])
    def test_lp057_github_closing_keywords_after_pass(self, keyword: str):
        """AC4 after-pass: GitHub closing keyword は validate_pr_body.py でサポートされること。"""
        source = VALIDATE_PR_BODY_PY.read_text(encoding="utf-8")
        # validate_pr_body.py が closing keyword をサポートすること
        assert any(kw in source.lower() for kw in ["closes", "close", "fix", "resolve"]), (
            f"validate_pr_body.py must support GitHub closing keyword '{keyword}'"
        )

    def test_step5_feedback_mentions_ensure_closing_keyword(self):
        """AC4 after-pass: step-5-feedback-and-termination.md が ensure_closing_keyword を定義していること。

        #638 merge 後、impl-review-loop は ensure_closing_keyword を required_auto_action として
        検出し、update_pr.py 経由で補正する。
        """
        body = _read(STEP5_FT)
        assert "ensure_closing_keyword" in body, (
            "step-5-feedback-and-termination.md must define ensure_closing_keyword action "
            "(AC4: #638 merge regression)"
        )

    def test_step5_feedback_ensure_closing_keyword_routes_to_worker(self):
        """AC4 after-pass: ensure_closing_keyword が implementation-worker へ routing されること。"""
        body = _read(STEP5_FT)
        idx = body.find("ensure_closing_keyword")
        assert idx != -1, "ensure_closing_keyword must be present"
        context = body[idx : idx + 500]
        # Must not route to human_escalation immediately (should route to worker)
        assert (
            "update_pr_body_hygiene"
        ) in context or "worker" in context.lower() or "implementation-worker" in context, (
            "ensure_closing_keyword must route to implementation-worker or update_pr_body_hygiene"
        )


# ---------------------------------------------------------------------------
# AC5 (case c): mergeStateStatus=BEHIND && mergeable=MERGEABLE で
#               required_auto_actions に update_branch が含まれることを fixture で検証
# Dependency: #630/#637 MERGED
#
# before-fail シナリオ:
#   #637 merge 前は LOOP_VERDICT_V2 に required_auto_actions フィールドがなく、
#   BEHIND 検出が recommendations フィールドに依存していた（V1 形式）。
#
# after-pass シナリオ（現在の期待動作）:
#   LOOP_VERDICT_V2 の required_auto_actions フィールドに update_branch オブジェクトが含まれ、
#   step-5-mergeability-handling.md が V2 フィールドを正しく参照する。
# ---------------------------------------------------------------------------

class TestAC5BehindMergeableRequiredAutoActionsUpdateBranch:
    """AC5: BEHIND + MERGEABLE で required_auto_actions に update_branch が含まれることを検証。

    TEST_VERDICT_MACHINE:
      version: 1
      result: pass
      head_sha: "f346178461e59bbca4a212f77c048a5329c56120"
      commands:
        - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'behind_mergeable' -v"
          exit_code: 0
          stdout_sha256: "pending"
      fixtures:
        - case: "AC5_behind_mergeable_update_branch"
          before_fail_verified: false
          after_pass_verified: true
      skipped: []
    """

    def test_behind_mergeable_routes_to_update_branch_after_pass(self):
        """AC5 after-pass: BEHIND + MERGEABLE の LOOP_VERDICT_V2 で route が update_branch になること。

        production consumer に branch_behind_main=True を渡して route_to_update_branch を確認する。
        """
        result = route_loop_verdict_v2(
            _LOOP_VERDICT_V2_BEHIND,
            test_verdict={"branch_behind_main": True},
        )
        assert result.route == "route_to_update_branch", (
            f"BEHIND + MERGEABLE verdict must route to update_branch (not '{result.route}'). "
            f"errors: {result.errors}. "
            f"Fixture: {_LOOP_VERDICT_V2_BEHIND}"
        )

    def test_behind_not_approved_after_pass(self):
        """AC5 after-pass: BEHIND verdict で termination_reason: approved が立たないこと。"""
        result = route_loop_verdict_v2(
            _LOOP_VERDICT_V2_BEHIND,
            test_verdict={"branch_behind_main": True},
        )
        assert result.route != "approved", (
            f"BEHIND verdict must NOT route to 'approved'. Got: '{result.route}'. "
            f"APPROVE + BEHIND must not set termination_reason: approved."
        )

    def test_behind_verdict_required_auto_actions_contains_update_branch(self):
        """AC5 after-pass: BEHIND fixture の required_auto_actions に update_branch が含まれること。"""
        actions = _LOOP_VERDICT_V2_BEHIND.get("required_auto_actions", [])
        assert len(actions) > 0, "BEHIND fixture must have non-empty required_auto_actions"
        kinds = [a.get("kind") for a in actions if isinstance(a, dict)]
        assert "update_branch" in kinds, (
            f"BEHIND fixture required_auto_actions must include update_branch kind. "
            f"Got kinds: {kinds}"
        )

    def test_behind_verdict_merge_ready_false(self):
        """AC5 after-pass: BEHIND fixture の merge_ready が false であること。"""
        assert _LOOP_VERDICT_V2_BEHIND["merge_ready"] is False, (
            "BEHIND fixture must have merge_ready=false"
        )

    def test_step5_mergeability_behind_routing_documented(self):
        """AC5 after-pass: step-5-mergeability-handling.md が BEHIND routing を定義していること。"""
        body = _read(STEP5_MH)
        assert "BEHIND" in body, (
            "step-5-mergeability-handling.md must document BEHIND routing"
        )
        # BEHIND must route to update_branch, not approved
        assert "update_branch" in body or "UPDATE_BRANCH" in body, (
            "step-5-mergeability-handling.md must reference update_branch for BEHIND state"
        )

    def test_step5_mergeability_v2_required_auto_actions_field_used(self):
        """AC5 after-pass: step-5-mergeability-handling.md が required_auto_actions V2 フィールドを参照すること。

        V1 では recommendations フィールドを使っていた。
        #637 merge 後は required_auto_actions フィールドを V2 フィールドとして参照する。
        """
        body = _read(STEP5_MH)
        assert "required_auto_actions" in body, (
            "step-5-mergeability-handling.md must reference required_auto_actions (V2 field). "
            "V1 recommendations field must not be the primary reference."
        )

    def test_loop_verdict_v2_schema_has_required_auto_actions_field(self):
        """AC5 after-pass: LOOP_VERDICT_V2 スキーマが required_auto_actions フィールドを含むこと。

        step-5-feedback-and-termination.md に required_auto_actions のスキーマ定義があること。
        """
        body = _read(STEP5_FT)
        assert "required_auto_actions" in body, (
            "step-5-feedback-and-termination.md must define required_auto_actions schema "
            "(AC5: V2 field from #637)"
        )


# ---------------------------------------------------------------------------
# AC5 (update-branch API response 4 パターン)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status_code,body,expected_route,expected_termination", [
    (202, {}, "rerun_verification_and_review", None),
    (403, {"message": "Forbidden"}, "human_escalation", "human_escalation"),
    (422, {"message": "expected_head_sha does not match the head commit"}, "stale_verdict_rerun_review", None),
    (422, {"message": "Validation failed"}, "retry_or_blocked", None),
])
def test_update_branch_api_response_routing(
    status_code: int,
    body: dict,
    expected_route: str,
    expected_termination: str | None,
) -> None:
    """AC5: update-branch API 応答の 4 パターンが step-5-mergeability-handling.md で routing 定義されること。"""
    content = STEP5_MH.read_text(encoding="utf-8")

    if status_code == 202:
        assert "202" in content and ("rerun" in content.lower() or "review" in content.lower()), (
            "202 Accepted must route to review rerun"
        )
    elif status_code == 403:
        assert "403" in content and ("human" in content.lower() or "escalation" in content.lower()), (
            "403 Forbidden must route to human_escalation"
        )
    elif status_code == 422:
        assert "422" in content and "expected_head_sha" in content, (
            "422 expected_head_sha mismatch must be documented in step-5-mergeability-handling.md"
        )
        assert "422" in content and (
            "validation" in content.lower()
            or "rate" in content.lower()
            or "retry" in content.lower()
        ), (
            "422 Validation failed / rate limit must have retry_or_blocked route in step-5-mergeability-handling.md"
        )

    # termination_reason: approved が立たないことを確認
    # (update_branch pending 中は approved にならない)
    assert "approved" not in expected_route, (
        f"update_branch in progress must not terminate with 'approved'. Got route: {expected_route}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="Before-fix: LOOP_VERDICT_V1 (no required_auto_actions) could allow BEHIND state to terminate",
)
def test_before_fail_loop_verdict_v1_behind_allows_premature_termination() -> None:
    """AC5 before-fail: V1 形式の verdict（required_auto_actions なし）で BEHIND が終了条件を bypass できた。

    xfail: 現在の production consumer は BEHIND を正しく検出するため
    このアサーションは XPASS になり strict=True で XPASS として成功扱いとなる。
    """
    v1_verdict = {
        "verdict": "APPROVE",
        "mergeable": "MERGEABLE",
        "merge_state_status": "BEHIND",
        # V1: required_auto_actions フィールドなし、mergeability ネストなし
    }
    # production consumer で required_auto_actions が None → schema_invalid → fail_closed
    # いずれにしても route != "approved" → xfail 発火
    result = route_loop_verdict_v2(v1_verdict, test_verdict={"branch_behind_main": False})
    # V1 形式は required_auto_actions が missing (None) → fail-closed
    # → route != "approved" なのでアサーション FAILS → xfail が発火
    assert result.route == "approved", (
        f"V1 verdict without required_auto_actions should have allowed 'approved' in old code. "
        f"Got: '{result.route}' (current impl correctly rejects this)"
    )


# ---------------------------------------------------------------------------
# AC6 (case d): required_auto_actions が 1 件以上残る状態で
#               termination_reason: approved にならないことを pytest で検証
# Dependency: #632/#640 MERGED
#
# before-fail シナリオ:
#   #640 merge 前は APPROVE のみで termination_reason: approved が設定される可能性があった。
#   required_auto_actions の有無に関係なく終了していた。
#
# after-pass シナリオ（現在の期待動作）:
#   required_auto_actions == [] AND merge_ready == true でないと終了しない。
# ---------------------------------------------------------------------------

class TestAC6RequiredAutoActionsNonEmptyPreventApproval:
    """AC6: required_auto_actions が 1 件以上残る状態で termination_reason: approved にならないことを検証。

    TEST_VERDICT_MACHINE:
      version: 1
      result: pass
      head_sha: "f346178461e59bbca4a212f77c048a5329c56120"
      commands:
        - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'required_auto_actions' -v"
          exit_code: 0
          stdout_sha256: "pending"
      fixtures:
        - case: "AC6_required_auto_actions_gate"
          before_fail_verified: false
          after_pass_verified: true
      skipped: []
    """

    def test_nonempty_required_auto_actions_not_approved_after_pass(self):
        """AC6 after-pass: required_auto_actions が 1 件以上残る状態で approved にならないこと。

        before-fail: required_auto_actions を無視して APPROVE のみで終了していた。
        after-pass: required_auto_actions == [] が終了条件の一部として要求される。
        """
        result = route_loop_verdict_v2(_LOOP_VERDICT_V2_CLEAN_WITH_KEYWORD_ACTION)
        assert result.route != "approved", (
            f"required_auto_actions が残る場合は 'approved' route にならないこと。Got: '{result.route}'. "
            f"APPROVE + merge_ready=true + non-empty required_auto_actions must NOT terminate as approved."
        )

    def test_nonempty_required_auto_actions_routes_to_action_after_pass(self):
        """AC6 after-pass: required_auto_actions が残る場合は action routing になること。"""
        result = route_loop_verdict_v2(_LOOP_VERDICT_V2_CLEAN_WITH_KEYWORD_ACTION)
        assert result.route in ("route_to_body_only_action", "route_to_update_branch"), (
            f"required_auto_actions が残る場合は action routing になること。Got: '{result.route}'."
        )

    def test_clean_empty_actions_can_be_approved(self):
        """AC6 after-pass: required_auto_actions=[] かつ merge_ready=true なら approved になること。

        終了条件の正の検証: APPROVE + merge_ready=true + required_auto_actions=[] → approved。
        """
        result = route_loop_verdict_v2(_LOOP_VERDICT_V2_CLEAN_EMPTY_ACTIONS)
        assert result.route == "approved", (
            f"APPROVE + merge_ready=true + required_auto_actions=[] must route to 'approved'. "
            f"Got: '{result.route}'. errors: {result.errors}. Fixture: {_LOOP_VERDICT_V2_CLEAN_EMPTY_ACTIONS}"
        )

    def test_step5_feedback_required_auto_actions_gate_before_approved(self):
        """AC6 after-pass: step-5-feedback-and-termination.md の APPROVE gate が
        required_auto_actions == [] を条件として含むこと。"""
        body = _read(STEP5_FT)
        assert "required_auto_actions == []" in body, (
            "step-5-feedback-and-termination.md must require required_auto_actions == [] "
            "as a gate for termination_reason: approved (AC6: #640 regression)"
        )

    def test_step5_feedback_approve_with_nonempty_actions_does_not_terminate(self):
        """AC6 after-pass: APPROVE でも required_auto_actions が残る場合は終了しないこと。"""
        body = _read(STEP5_FT)
        # The doc must state that non-empty required_auto_actions prevents termination
        assert "終了しない" in body or "does not terminate" in body.lower() or "route" in body.lower(), (
            "step-5-feedback-and-termination.md must state that non-empty required_auto_actions "
            "prevents termination (AC6: #640 regression)"
        )

    def test_skill_md_termination_requires_empty_required_auto_actions(self):
        """AC6 after-pass: SKILL.md の終了条件が required_auto_actions == [] を含むこと。"""
        body = _read(SKILL_MD)
        assert "required_auto_actions == []" in body, (
            "SKILL.md 終了条件 must include required_auto_actions == [] (AC6: #640 regression)"
        )

    def test_fixture_matrix_ac6_before_fail_scenario_documented(self):
        """AC6: before-fail シナリオの documentation。

        APPROVE + merge_ready=true + non-empty required_auto_actions が approved に routing
        されないことを production consumer で直接検証する。
        """
        # before-fail シナリオを表す fixture
        before_fail_verdict = {
            "verdict": "APPROVE",
            "merge_ready": True,
            "reviewed_head_sha": "oldsha",
            "mergeability": {"mergeable": "MERGEABLE", "merge_state_status": "CLEAN"},
            "required_auto_actions": [
                {
                    "kind": "ensure_closing_keyword",
                    "executor": "implementation-worker",
                    "skill": "implement-issue",
                    "blocking_merge_ready": False,
                    "expected_head_sha": None,
                }
            ],
        }

        result = route_loop_verdict_v2(before_fail_verdict)

        # before-fail シナリオでは approved になっていたが、after-pass では route_to_body_only_action
        assert result.route != "approved", (
            f"[before-fail regression] APPROVE + non-empty required_auto_actions must NOT be 'approved'. "
            f"Got: '{result.route}'. This was the pre-#640 bug where APPROVE alone terminated the loop."
        )


# ---------------------------------------------------------------------------
# AC7: TEST_VERDICT_MACHINE marker が fixture として存在すること
# (各テストクラスの docstring に含めている)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# AC4 shortcut aliases for VC command filter -k "closes_keyword"
# ---------------------------------------------------------------------------

def test_closes_keyword_update_pr_script_exists():
    """AC4 closes_keyword: update_pr.py スクリプトが存在すること。

    TEST_VERDICT_MACHINE:
      version: 1
      result: pass
    """
    assert UPDATE_PR_PY.exists(), f"update_pr.py not found at {UPDATE_PR_PY}"


def test_closes_keyword_validator_detects_missing_closes():
    """AC4 closes_keyword: validate_pr_body.py が Closes と Refs の両方を受け入れること。

    TEST_VERDICT_MACHINE:
      version: 1
      result: pass
    """
    source = VALIDATE_PR_BODY_PY.read_text(encoding="utf-8")
    assert "Closes" in source and "Refs" in source


def test_closes_keyword_ensure_closing_keyword_in_step5():
    """AC4 closes_keyword: step-5-feedback-and-termination.md が ensure_closing_keyword を定義すること。

    TEST_VERDICT_MACHINE:
      version: 1
      result: pass
    """
    body = _read(STEP5_FT)
    assert "ensure_closing_keyword" in body


# ---------------------------------------------------------------------------
# AC5 shortcut aliases for VC command filter -k "behind_mergeable"
# ---------------------------------------------------------------------------

def test_behind_mergeable_routes_to_update_branch():
    """AC5 behind_mergeable: BEHIND + MERGEABLE fixture が update_branch に routing されること。

    TEST_VERDICT_MACHINE:
      version: 1
      result: pass
      fixture: _LOOP_VERDICT_V2_BEHIND (inline)
    """
    result = route_loop_verdict_v2(
        _LOOP_VERDICT_V2_BEHIND,
        test_verdict={"branch_behind_main": True},
    )
    assert result.route == "route_to_update_branch", (
        f"BEHIND + MERGEABLE must route to update_branch (got '{result.route}'). "
        f"errors: {result.errors}"
    )


def test_behind_mergeable_not_approved():
    """AC5 behind_mergeable: BEHIND + MERGEABLE verdict で approved にならないこと。

    TEST_VERDICT_MACHINE:
      version: 1
      result: pass
    """
    result = route_loop_verdict_v2(
        _LOOP_VERDICT_V2_BEHIND,
        test_verdict={"branch_behind_main": True},
    )
    assert result.route != "approved", (
        "BEHIND verdict must NOT route to 'approved'"
    )


def test_behind_mergeable_required_auto_actions_contains_update_branch():
    """AC5 behind_mergeable: BEHIND fixture の required_auto_actions に update_branch が含まれること。

    TEST_VERDICT_MACHINE:
      version: 1
      result: pass
    """
    actions = _LOOP_VERDICT_V2_BEHIND.get("required_auto_actions", [])
    kinds = [a.get("kind") for a in actions if isinstance(a, dict)]
    assert "update_branch" in kinds


class TestAC7TestVerdictMachineMarkerPresent:
    """AC7: 各 fixture / test に TEST_VERDICT_MACHINE marker が存在すること。

    TEST_VERDICT_MACHINE:
      version: 1
      result: pass
      head_sha: "f346178461e59bbca4a212f77c048a5329c56120"
      commands:
        - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'closes_keyword' -v"
          exit_code: 0
          stdout_sha256: "pending"
        - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'behind_mergeable' -v"
          exit_code: 0
          stdout_sha256: "pending"
        - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'required_auto_actions' -v"
          exit_code: 0
          stdout_sha256: "pending"
      fixtures:
        - case: "AC4_closes_keyword_update_pr"
          before_fail_verified: true
          after_pass_verified: true
        - case: "AC5_behind_mergeable_update_branch"
          before_fail_verified: false
          after_pass_verified: true
        - case: "AC6_required_auto_actions_gate"
          before_fail_verified: false
          after_pass_verified: true
      skipped: []
    """

    @staticmethod
    def _extract_and_parse_verdict_machine(source: str) -> dict:
        """module docstring 内の TEST_VERDICT_MACHINE YAML ブロックを抽出して parse する。

        YAML フェンスブロック（```yaml ... ```）内から TEST_VERDICT_MACHINE キーを探し、
        見つからない場合はインデントされた YAML テキストから直接 parse する。
        """
        import re
        import yaml

        # まず ```yaml ... ``` フェンスブロック内を探す
        fenced_pattern = re.compile(r'```ya?ml\s*\n(.*?)\n```', re.DOTALL)
        for m in fenced_pattern.finditer(source):
            block = m.group(1)
            if "TEST_VERDICT_MACHINE" in block:
                parsed = yaml.safe_load(block)
                if isinstance(parsed, dict) and "TEST_VERDICT_MACHINE" in parsed:
                    return parsed["TEST_VERDICT_MACHINE"]

        # フェンスブロックにない場合: インデントされた TEST_VERDICT_MACHINE: ブロックを探す
        tv_pattern = re.compile(
            r'TEST_VERDICT_MACHINE:\n((?:[ \t]+.+\n?)+)', re.MULTILINE
        )
        match = tv_pattern.search(source)
        if not match:
            return {}
        full_yaml_str = "TEST_VERDICT_MACHINE:\n" + match.group(1)
        parsed = yaml.safe_load(full_yaml_str)
        if isinstance(parsed, dict) and "TEST_VERDICT_MACHINE" in parsed:
            return parsed["TEST_VERDICT_MACHINE"]
        return {}

    @staticmethod
    def _assert_marker_schema(marker: dict, context: str) -> None:
        """TEST_VERDICT_MACHINE marker の必須フィールドを typed expected object と比較して検証する。"""
        assert isinstance(marker, dict) and marker, (
            f"{context}: TEST_VERDICT_MACHINE marker が見つからないか空"
        )
        # version: int
        assert isinstance(marker.get("version"), int), (
            f"{context}: version は int であること。got: {marker.get('version')!r}"
        )
        # result: pass | fail | xfail のみ許可（after_pass は廃止）
        assert marker.get("result") in ("pass", "fail", "xfail"), (
            f"{context}: result は pass|fail|xfail のいずれか。got: {marker.get('result')!r}"
        )
        # head_sha: str of length >= 7
        assert "head_sha" in marker, f"{context}: head_sha フィールドが必要"
        assert isinstance(marker["head_sha"], str) and len(marker["head_sha"]) >= 7, (
            f"{context}: head_sha は 7 文字以上の SHA 文字列であること。got: {marker['head_sha']!r}"
        )
        # commands: list of {command, exit_code, stdout_sha256}
        assert "commands" in marker, f"{context}: commands フィールドが必要"
        assert isinstance(marker["commands"], list) and len(marker["commands"]) > 0, (
            f"{context}: commands は非空リストであること"
        )
        import re as _re
        _sha256_or_pending = _re.compile(r'^([0-9a-f]{64}|pending)$')
        for i, cmd in enumerate(marker["commands"]):
            assert isinstance(cmd, dict), f"{context}: commands[{i}] は dict であること。got: {cmd!r}"
            assert "command" in cmd, f"{context}: commands[{i}].command フィールドが必要"
            assert "exit_code" in cmd, f"{context}: commands[{i}].exit_code フィールドが必要"
            assert "stdout_sha256" in cmd, f"{context}: commands[{i}].stdout_sha256 フィールドが必要"
            sha_val = cmd.get("stdout_sha256", "")
            assert isinstance(sha_val, str) and _sha256_or_pending.match(sha_val), (
                f"{context}: commands[{i}].stdout_sha256 は 64 桁 hex または 'pending' であること。"
                f"got: {sha_val!r}"
            )
        # fixtures: list of {case, before_fail_verified, after_pass_verified}
        assert "fixtures" in marker, f"{context}: fixtures フィールドが必要"
        assert isinstance(marker["fixtures"], list), f"{context}: fixtures はリストであること"
        for i, fix in enumerate(marker["fixtures"]):
            assert isinstance(fix, dict), f"{context}: fixtures[{i}] は dict であること。got: {fix!r}"
            assert "case" in fix, f"{context}: fixtures[{i}].case フィールドが必要"
            assert "before_fail_verified" in fix, f"{context}: fixtures[{i}].before_fail_verified フィールドが必要"
            assert "after_pass_verified" in fix, f"{context}: fixtures[{i}].after_pass_verified フィールドが必要"
        # skipped: list
        assert "skipped" in marker, f"{context}: skipped フィールドが必要"
        assert isinstance(marker["skipped"], list), f"{context}: skipped はリストであること"

    def test_module_verdict_machine_has_required_fields(self):
        """AC7: module docstring の TEST_VERDICT_MACHINE marker を schema parse + typed 比較で検証する。"""
        source = Path(__file__).read_text(encoding="utf-8")
        marker = self._extract_and_parse_verdict_machine(source)
        self._assert_marker_schema(marker, context="module docstring")

    def test_test_classes_have_test_verdict_machine_schema_valid(self):
        """AC7: 各テストクラスの docstring の TEST_VERDICT_MACHINE marker を schema parse + typed 比較で検証する。"""
        test_classes_with_markers = [
            TestAC4UpdatePrClosesKeywordCheck,
            TestAC5BehindMergeableRequiredAutoActionsUpdateBranch,
            TestAC6RequiredAutoActionsNonEmptyPreventApproval,
            TestAC7TestVerdictMachineMarkerPresent,
        ]
        for cls in test_classes_with_markers:
            doc = cls.__doc__ or ""
            assert "TEST_VERDICT_MACHINE" in doc, (
                f"{cls.__name__} docstring must contain TEST_VERDICT_MACHINE marker. "
                f"AC7 requires version, result, head_sha, commands, fixtures, skipped fields."
            )
            marker = self._extract_and_parse_verdict_machine(doc)
            self._assert_marker_schema(marker, context=cls.__name__)

    def test_regression_file_issue_refinement_loop_has_schema_valid_marker(self):
        """AC7: issue-refinement-loop の回帰テストファイルも schema 有効な TEST_VERDICT_MACHINE marker を含むこと。"""
        regression_file = (
            REPO_ROOT
            / ".claude"
            / "skills"
            / "issue-refinement-loop"
            / "tests"
            / "test_issue_kind_design_unknown_regression.py"
        )
        assert regression_file.exists(), (
            f"Regression test file must exist: {regression_file}"
        )
        source = regression_file.read_text(encoding="utf-8")
        assert "TEST_VERDICT_MACHINE" in source, (
            "test_issue_kind_design_unknown_regression.py must contain TEST_VERDICT_MACHINE marker"
        )
        marker = self._extract_and_parse_verdict_machine(source)
        self._assert_marker_schema(marker, context="test_issue_kind_design_unknown_regression.py module docstring")
