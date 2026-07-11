"""AC1-AC4: `implement-issue` SKILL.md Section 2 が contract-aware overlap
preflight を正本として routing することを検証するドキュメントテスト（#1452）。

Section 2 は Allowed Paths の literal 一致だけで人間判断へ停止せず、
`check_implementation_overlap.py` の `IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1`
route を消費して continue / fail-closed を決定論的に分岐する。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "implement-issue" / "SKILL.md"


def _skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def test_skill_file_exists() -> None:
    assert SKILL_PATH.is_file(), f"missing SKILL.md: {SKILL_PATH}"


def test_given_section_2_when_rendered_then_contract_aware_preflight_replaces_path_literal_only_stop() -> None:
    """AC1: Section 2 が path literal 一致だけを停止条件にせず、
    `check_issue_overlap.py` の result schema（`check_implementation_overlap.py`
    経由）を消費する手順を示す。
    """
    content = _skill_text()

    # 旧: naive な gh issue list --search による path literal 一致だけの停止手順は除去済み
    assert 'gh issue list --search "\\"$path\\" is:open"' not in content, (
        "naive path-literal-only gh search must be removed from Section 2"
    )

    # 新: contract-aware overlap preflight adapter を正本として使う
    assert "check_implementation_overlap.py" in content
    assert "IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1" in content
    assert "check_issue_overlap.py" in content
    assert "classify_overlap" in content or "pure classifier" in content


def test_given_section_2_when_rendered_then_route_table_covers_closed_route_enum() -> None:
    """AC7 (SKILL.md 側の記述整合性): route enum の closed set が Section 2 に
    明記されていることを確認する（AC1 の result schema 消費手順の一部）。
    """
    content = _skill_text()
    for route in (
        "proceed",
        "proceed_with_collision_evidence",
        "wait_for_predecessor",
        "human_review_required",
        "duplicate",
        "runtime_error",
    ):
        assert route in content, f"Section 2 must document route: {route}"


def test_given_continue_routes_when_described_then_proceed_and_evidence_route_continue_implementation() -> None:
    """AC2: `safe_new_issue`（`proceed`）と `overlap_requires_comment`
    （`proceed_with_collision_evidence`）は、必要な evidence 記録後に
    実装継続へ route されることを示す。
    """
    content = _skill_text()
    assert "実装を継続する" in content
    assert "proceed_with_collision_evidence" in content
    assert "evidence" in content
    assert "Issue コメント" in content or "worktree artifact" in content


def test_given_fail_closed_routes_when_described_then_ambiguous_and_duplicate_route_to_human() -> None:
    """AC3: `ambiguous_requires_human`（`human_review_required` /
    `wait_for_predecessor`）と `duplicate` が fail-closed で
    人間判断へ route されることを示す。
    """
    content = _skill_text()
    assert "fail-closed" in content
    assert "人間判断へ停止" in content
    for route in ("wait_for_predecessor", "human_review_required", "duplicate", "runtime_error"):
        assert route in content


def test_given_candidate_contract_readback_when_described_then_merge_pr_is_not_proposed_before_readback() -> None:
    """AC4: candidate contract の Outcome/In Scope/Out of Scope/Delivery Rule を
    readback 前に統合 PR を提案しないことを明記する。
    """
    content = _skill_text()
    assert "readback" in content
    assert "統合 PR を提案してはならない" in content
    for heading in ("Outcome", "In Scope"):
        assert heading in content


def test_given_self_exclusion_when_described_then_issue_number_is_required() -> None:
    """AC6 の SKILL.md 側の記述整合性: `--issue-number` が必須であり、
    対象 Issue 自身が自己除外されることを明記する。
    """
    content = _skill_text()
    assert "--issue-number" in content
    assert "自己除外" in content


def test_given_out_of_scope_when_described_then_local_concurrent_worktree_is_a_separate_gate() -> None:
    """Issue #1452 の Out of Scope: contract-aware preflight の continue 判定は
    OPEN Issue 間の意味的適合性のみを示し、active worktree / dirty path との
    同時編集安全性は証明しないことを Section 2 に明記する。
    """
    content = _skill_text()
    assert "同時編集安全性は証明しない" in content


def test_given_stale_evidence_when_described_then_drift_check_before_pr_creation_is_present() -> None:
    """PR 作成直前の stale evidence（updated_at / body_sha256 drift）再確認手順の存在確認。"""
    content = _skill_text()
    assert "drift" in content
    assert "updated_at" in content
    assert "body_sha256" in content


def test_given_check_issue_overlap_scoring_when_described_then_it_is_out_of_scope() -> None:
    """`check_issue_overlap.py` 本体の scoring / schema 変更は Section 2 の対象外
    （#1452 Out of Scope）であることを明記する。
    """
    content = _skill_text()
    assert "scoring" in content
    assert "対象外" in content
