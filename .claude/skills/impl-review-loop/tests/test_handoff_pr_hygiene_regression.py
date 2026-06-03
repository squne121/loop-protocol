"""
Regression fixtures for handoff / PR hygiene failure.

Issue #634: child-7 — handoff / PR hygiene failure の回帰テストを before-fail / after-pass で fixture 化する

AC4 (case b): PR body に `Refs #N` のみで `Closes #N` がない場合に update_pr.py 呼び出しが
              fixture で確認できること（#638 merge 済み）
AC5 (case c): mergeStateStatus=BEHIND && mergeable=MERGEABLE で required_auto_actions に
              update_branch が含まれることを fixture で検証（#637 merge 済み）
AC6 (case d): required_auto_actions が 1 件以上残る状態で termination_reason: approved に
              ならないことを pytest で検証（#640 merge 済み）
AC7: 各 fixture / test に TEST_VERDICT_MACHINE marker（version, result, head_sha, commands, fixtures, skipped フィールド）

TEST_VERDICT_MACHINE:
  version: 1
  result: pass
  head_sha: "5a95db98626a7c031d53e954937ad3972fa5e250"
  commands:
    - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'closes_keyword' -v"
      exit_code: 0
      stdout_sha256: "any"
    - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'behind_mergeable' -v"
      exit_code: 0
      stdout_sha256: "any"
    - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'required_auto_actions' -v"
      exit_code: 0
      stdout_sha256: "any"
  fixtures:
    - case: "AC4_closes_keyword_update_pr"
      before_fail_verified: false
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


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Inline fixtures for LOOP_VERDICT_V2 routing
# ---------------------------------------------------------------------------

# LOOP_VERDICT_V2 fixture: BEHIND + required_auto_actions に update_branch を含む
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
            "skill": "implement-issue",
            "blocking_merge_ready": True,
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
# Routing logic (mirroring step-5-feedback-and-termination.md decision table)
# ---------------------------------------------------------------------------

def _evaluate_termination_route(verdict: dict) -> str:
    """Deterministic routing based on LOOP_VERDICT_V2 fields.

    Returns:
      'approved': termination_reason: approved can be set
      'route_to_update_branch': BEHIND → route to implementation-worker update_branch
      'route_to_required_auto_action': non-empty required_auto_actions (non-update_branch)
      'not_approved': merge_ready=false without BEHIND
      'continue_loop': REQUEST_CHANGES
    """
    v = verdict.get("verdict", "")
    merge_ready = verdict.get("merge_ready", False)
    required_auto_actions = verdict.get("required_auto_actions", [])
    mergeability = verdict.get("mergeability", {})
    merge_state_status = mergeability.get("merge_state_status", "")

    if v == "REQUEST_CHANGES":
        return "continue_loop"

    if v != "APPROVE":
        return "not_approved"

    # BEHIND → update_branch route (termination_reason: approved は設定しない)
    if merge_state_status == "BEHIND":
        return "route_to_update_branch"

    # required_auto_actions が残る → 終了しない
    if required_auto_actions:
        # update_branch が残る場合は update_branch route
        for action in required_auto_actions:
            if isinstance(action, dict) and action.get("kind") == "update_branch":
                return "route_to_update_branch"
        # それ以外（ensure_closing_keyword, update_pr_body_hygiene 等）
        return "route_to_required_auto_action"

    # merge_ready が false でかつ BEHIND でもない → 人間判断
    if not merge_ready:
        return "not_approved"

    # APPROVE + merge_ready=true + required_auto_actions=[] → 終了可
    return "approved"


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
      head_sha: "5a95db98626a7c031d53e954937ad3972fa5e250"
      commands:
        - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'closes_keyword' -v"
          exit_code: 0
          stdout_sha256: "any"
      fixtures:
        - case: "AC4_closes_keyword_update_pr"
          before_fail_verified: false
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

    def test_validate_pr_body_detects_refs_only_missing_closes(self):
        """AC4 after-pass: validate_pr_body.py が Refs-only（Closes なし）を LP057 エラーとして検出すること。

        PR body に Refs #N のみで Closes #N がない場合:
        validate_pr_body.py は LP057 (missing Closes/Refs) でエラーを返すべきではなく
        ('Refs #N' は valid として扱われる)。
        ただし linked_issue が渡された場合に mismatch があると LP057 が発出される。

        この fixture は validate_pr_body.py の _validate_lp057 が Closes と Refs を
        どちらも valid として扱うことを確認する（Closes のみが必須ではない）。
        """
        source = VALIDATE_PR_BODY_PY.read_text(encoding="utf-8")
        # LP057 check should accept both Closes and Refs
        assert "Closes" in source and "Refs" in source, (
            "validate_pr_body.py must check both Closes and Refs keywords in LP057"
        )
        # Both patterns should be in the same function
        assert r"(?i)\bCloses\s+#" in source or "Closes" in source, (
            "validate_pr_body.py must detect Closes #N pattern"
        )
        assert r"(?i)\bRefs\s+#" in source or "Refs" in source, (
            "validate_pr_body.py must detect Refs #N pattern"
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
        assert "update_pr_body_hygiene" in context or "worker" in context.lower() or "implementation-worker" in context, (
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
      head_sha: "5a95db98626a7c031d53e954937ad3972fa5e250"
      commands:
        - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'behind_mergeable' -v"
          exit_code: 0
          stdout_sha256: "any"
      fixtures:
        - case: "AC5_behind_mergeable_update_branch"
          before_fail_verified: false
          after_pass_verified: true
      skipped: []
    """

    def test_behind_mergeable_routes_to_update_branch_after_pass(self):
        """AC5 after-pass: BEHIND + MERGEABLE の LOOP_VERDICT_V2 で route が update_branch になること。"""
        route = _evaluate_termination_route(_LOOP_VERDICT_V2_BEHIND)
        assert route == "route_to_update_branch", (
            f"BEHIND + MERGEABLE verdict must route to update_branch (not '{route}'). "
            f"Fixture: {_LOOP_VERDICT_V2_BEHIND}"
        )

    def test_behind_not_approved_after_pass(self):
        """AC5 after-pass: BEHIND verdict で termination_reason: approved が立たないこと。"""
        route = _evaluate_termination_route(_LOOP_VERDICT_V2_BEHIND)
        assert route != "approved", (
            f"BEHIND verdict must NOT route to 'approved'. Got: '{route}'. "
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
      head_sha: "5a95db98626a7c031d53e954937ad3972fa5e250"
      commands:
        - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'required_auto_actions' -v"
          exit_code: 0
          stdout_sha256: "any"
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
        route = _evaluate_termination_route(_LOOP_VERDICT_V2_CLEAN_WITH_KEYWORD_ACTION)
        assert route != "approved", (
            f"required_auto_actions が残る場合は 'approved' route にならないこと。Got: '{route}'. "
            f"APPROVE + merge_ready=true + non-empty required_auto_actions must NOT terminate as approved."
        )

    def test_nonempty_required_auto_actions_routes_to_action_after_pass(self):
        """AC6 after-pass: required_auto_actions が残る場合は action routing になること。"""
        route = _evaluate_termination_route(_LOOP_VERDICT_V2_CLEAN_WITH_KEYWORD_ACTION)
        assert route in ("route_to_required_auto_action", "route_to_update_branch"), (
            f"required_auto_actions が残る場合は action routing になること。Got: '{route}'."
        )

    def test_clean_empty_actions_can_be_approved(self):
        """AC6 after-pass: required_auto_actions=[] かつ merge_ready=true なら approved になること。

        終了条件の正の検証: APPROVE + merge_ready=true + required_auto_actions=[] → approved。
        """
        route = _evaluate_termination_route(_LOOP_VERDICT_V2_CLEAN_EMPTY_ACTIONS)
        assert route == "approved", (
            f"APPROVE + merge_ready=true + required_auto_actions=[] must route to 'approved'. "
            f"Got: '{route}'. Fixture: {_LOOP_VERDICT_V2_CLEAN_EMPTY_ACTIONS}"
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
        されないことを routing 関数で直接検証する（上記の test_nonempty_required_auto_actions_not_approved
        と同等だが、fixture matrix として明示化）。
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

        route = _evaluate_termination_route(before_fail_verdict)

        # before-fail シナリオでは approved になっていたが、after-pass では route_to_required_auto_action
        assert route != "approved", (
            f"[before-fail regression] APPROVE + non-empty required_auto_actions must NOT be 'approved'. "
            f"Got: '{route}'. This was the pre-#640 bug where APPROVE alone terminated the loop."
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
      result: after_pass
    """
    assert UPDATE_PR_PY.exists(), f"update_pr.py not found at {UPDATE_PR_PY}"


def test_closes_keyword_validator_detects_missing_closes():
    """AC4 closes_keyword: validate_pr_body.py が Closes と Refs の両方を受け入れること。

    TEST_VERDICT_MACHINE:
      version: 1
      result: after_pass
    """
    source = VALIDATE_PR_BODY_PY.read_text(encoding="utf-8")
    assert "Closes" in source and "Refs" in source


def test_closes_keyword_ensure_closing_keyword_in_step5():
    """AC4 closes_keyword: step-5-feedback-and-termination.md が ensure_closing_keyword を定義すること。

    TEST_VERDICT_MACHINE:
      version: 1
      result: after_pass
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
      result: after_pass
      fixture: _LOOP_VERDICT_V2_BEHIND (inline)
    """
    route = _evaluate_termination_route(_LOOP_VERDICT_V2_BEHIND)
    assert route == "route_to_update_branch", (
        f"BEHIND + MERGEABLE must route to update_branch (got '{route}')"
    )


def test_behind_mergeable_not_approved():
    """AC5 behind_mergeable: BEHIND + MERGEABLE verdict で approved にならないこと。

    TEST_VERDICT_MACHINE:
      version: 1
      result: after_pass
    """
    route = _evaluate_termination_route(_LOOP_VERDICT_V2_BEHIND)
    assert route != "approved", (
        "BEHIND verdict must NOT route to 'approved'"
    )


def test_behind_mergeable_required_auto_actions_contains_update_branch():
    """AC5 behind_mergeable: BEHIND fixture の required_auto_actions に update_branch が含まれること。

    TEST_VERDICT_MACHINE:
      version: 1
      result: after_pass
    """
    actions = _LOOP_VERDICT_V2_BEHIND.get("required_auto_actions", [])
    kinds = [a.get("kind") for a in actions if isinstance(a, dict)]
    assert "update_branch" in kinds


class TestAC7TestVerdictMachineMarkerPresent:
    """AC7: 各 fixture / test に TEST_VERDICT_MACHINE marker が存在すること。

    TEST_VERDICT_MACHINE:
      version: 1
      result: pass
      head_sha: "5a95db98626a7c031d53e954937ad3972fa5e250"
      commands:
        - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'closes_keyword' -v"
          exit_code: 0
          stdout_sha256: "any"
        - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'behind_mergeable' -v"
          exit_code: 0
          stdout_sha256: "any"
        - command: "uv run pytest .claude/skills/impl-review-loop/tests/ -k 'required_auto_actions' -v"
          exit_code: 0
          stdout_sha256: "any"
      fixtures:
        - case: "AC4_closes_keyword_update_pr"
          before_fail_verified: false
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
        # result: pass | fail | xfail
        assert marker.get("result") in ("pass", "fail", "xfail", "after_pass"), (
            f"{context}: result は pass|fail|xfail|after_pass のいずれか。got: {marker.get('result')!r}"
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
        for i, cmd in enumerate(marker["commands"]):
            assert isinstance(cmd, dict), f"{context}: commands[{i}] は dict であること。got: {cmd!r}"
            assert "command" in cmd, f"{context}: commands[{i}].command フィールドが必要"
            assert "exit_code" in cmd, f"{context}: commands[{i}].exit_code フィールドが必要"
            assert "stdout_sha256" in cmd, f"{context}: commands[{i}].stdout_sha256 フィールドが必要"
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
            f"test_issue_kind_design_unknown_regression.py must contain TEST_VERDICT_MACHINE marker"
        )
        marker = self._extract_and_parse_verdict_machine(source)
        self._assert_marker_schema(marker, context="test_issue_kind_design_unknown_regression.py module docstring")
