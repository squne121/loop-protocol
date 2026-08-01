"""#1679: `implement-issue` は target-only executor である。

旧 #1452/#1458/#1470/#1493/#1509/#1860 で構築された peer OPEN Issue の
overlap preflight（`check_implementation_overlap.py` の production 呼び出し、
`IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1` evidence、`proceed_with_collision_
evidence` 等の route enum、`overlap_preflight` handoff フィールド）は #1679
により production path から完全に撤去された（#1860 Owner Decision: OPEN
Issue 全件収集・semantic overlap 判定は通常実装を停止させる新しい semantic
planning authority を持たない）。

本ファイルは、旧 route を固定する内容から、target-only executor が実行判断
入力として許可する closed set（target Issue、canonical repository、
worktree、実 diff、実 test、target PR、CI、review、human stop）と、禁止する
closed set（peer OPEN Issue 全件収集・semantic overlap 判定・overlap
evidence handoff）を検証する内容へ書き換えたもの。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "implement-issue" / "SKILL.md"
OPEN_PR_SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "open-pr" / "SKILL.md"


def _skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def _open_pr_skill_text() -> str:
    return OPEN_PR_SKILL_PATH.read_text(encoding="utf-8")


# Closed set of legacy overlap tokens that must not appear in the
# production-path SKILL.md documents (except the create-issue allowlisted
# asset, which this test suite does not touch).
_FORBIDDEN_OVERLAP_TOKENS = (
    "check_implementation_overlap",
    "IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1",
    "overlap_preflight",
    "E_OVERLAP_PREFLIGHT_",
    "OVERLAP_PREFLIGHT_WARNING",
    "overlap_readback_waiver",
)


def test_skill_file_exists() -> None:
    assert SKILL_PATH.is_file(), f"missing SKILL.md: {SKILL_PATH}"


def test_given_implement_issue_skill_when_scanned_then_forbidden_overlap_tokens_are_absent() -> None:
    """AC6/AC11 相当: implement-issue SKILL.md から overlap evidence の
    producer/consumer 語彙が完全に消えていることを確認する。"""
    content = _skill_text()
    for token in _FORBIDDEN_OVERLAP_TOKENS:
        assert token not in content, f"forbidden overlap token still present: {token}"


def test_given_open_pr_skill_when_scanned_then_forbidden_overlap_tokens_are_absent() -> None:
    """AC4/AC6 相当: open-pr SKILL.md から overlap evidence の
    producer/consumer 語彙が完全に消えていることを確認する。"""
    content = _open_pr_skill_text()
    for token in _FORBIDDEN_OVERLAP_TOKENS:
        assert token not in content, f"forbidden overlap token still present: {token}"


def test_given_check_implementation_overlap_script_when_checked_then_it_does_not_exist() -> None:
    """AC3/AC10: `check_implementation_overlap.py`（checker 本体）が
    完全に削除されていることを確認する。"""
    script = (
        REPO_ROOT
        / ".claude"
        / "skills"
        / "implement-issue"
        / "scripts"
        / "check_implementation_overlap.py"
    )
    assert not script.exists(), f"checker script must be deleted: {script}"


def test_given_verify_overlap_pagination_runtime_script_when_checked_then_it_does_not_exist() -> None:
    """AC10: checker と併せて `verify_overlap_pagination_runtime.py` も
    削除されていることを確認する。"""
    script = (
        REPO_ROOT
        / ".claude"
        / "skills"
        / "implement-issue"
        / "scripts"
        / "verify_overlap_pagination_runtime.py"
    )
    assert not script.exists(), f"pagination runtime verifier must be deleted: {script}"


def test_given_check_implementation_overlap_tests_when_checked_then_they_do_not_exist() -> None:
    """AC10: checker を固定していた既存 tests が完全に削除されていることを
    確認する。"""
    tests_dir = REPO_ROOT / ".claude" / "skills" / "implement-issue" / "tests"
    deleted_test_files = (
        "test_check_implementation_overlap.py",
        "test_check_implementation_overlap_false_positive_signals.py",
        "test_check_implementation_overlap_native_dependencies.py",
        "test_check_implementation_overlap_pagination.py",
        "test_check_implementation_overlap_repository_binding.py",
        "test_check_implementation_overlap_successor_dependency.py",
    )
    for name in deleted_test_files:
        path = tests_dir / name
        assert not path.exists(), f"deleted overlap test file must not exist: {path}"


def test_given_implement_issue_skill_when_scanned_then_target_only_executor_boundary_is_documented() -> None:
    """implement-issue が target-only executor（target Issue、canonical
    repository、worktree、実 diff、実 test、target PR、CI、review、human
    stop だけを実行判断入力とする）であることを SKILL.md が明記することを
    確認する。"""
    content = _skill_text()
    assert "target-only executor" in content
    assert "peer OPEN Issue" in content
    assert "全件収集" in content


def test_given_implement_issue_skill_when_scanned_then_actual_git_conflict_is_the_only_stop_condition() -> None:
    """実際の停止条件が Verification Commands の失敗、または target PR の
    GitHub mergeability（`CONFLICTING` / `DIRTY`）に限定されることを
    SKILL.md が明記することを確認する。"""
    content = _skill_text()
    assert "mergeable == CONFLICTING" in content
    assert "mergeStateStatus == DIRTY" in content
    assert "UNKNOWN" in content and "BLOCKED" in content and "BEHIND" in content and "UNSTABLE" in content
