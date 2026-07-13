"""AC1-AC4: `implement-issue` SKILL.md Section 2 が contract-aware overlap
preflight を正本として routing することを検証するドキュメントテスト（#1452、
PR #1455 レビュー修正版）。

Section 2 は Allowed Paths の literal 一致だけで人間判断へ停止せず、
`check_implementation_overlap.py` の `IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1`
route を消費して continue / fail-closed を決定論的に分岐する。

AC1-AC4 は SKILL.md の文言存在確認だけでなく、`check_implementation_overlap.py`
を実際の subprocess 経路で実行し、記述された route 挙動が実装と一致することを
併せて検証する。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "implement-issue" / "SKILL.md"
HELPER = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "implement-issue"
    / "scripts"
    / "check_implementation_overlap.py"
)
FIXTURES_DIR = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "implement-issue"
    / "tests"
    / "fixtures"
    / "overlap"
)


def _skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


DEFAULT_REPO = "squne121/loop-protocol"


def _run_cli(issue_number: int, current_file: Path, candidates_file: Path):
    # AC1/AC10 (#1462): dry-run も --repo が必須になったため、既存テストヘルパー
    # に既定の --repo を追加する（後方互換維持のための Scope Delta 内変更）。
    proc = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--issue-number",
            str(issue_number),
            "--dry-run",
            "--current-file",
            str(current_file),
            "--candidates-file",
            str(candidates_file),
            "--repo",
            DEFAULT_REPO,
        ],
        capture_output=True,
        text=True,
    )
    return proc.returncode, json.loads(proc.stdout)


def test_skill_file_exists() -> None:
    assert SKILL_PATH.is_file(), f"missing SKILL.md: {SKILL_PATH}"


def test_given_section_2_when_rendered_then_contract_aware_preflight_replaces_path_literal_only_stop() -> None:
    """AC1: Section 2 が path literal 一致だけを停止条件にせず、
    `check_issue_overlap.py` の result schema（`check_implementation_overlap.py`
    経由）を消費する手順を示す。実際に adapter を実行し、`route` が消費されて
    いることを実行経路でも確認する。
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

    # 実行経路: 同一 Allowed Paths だが disjoint な candidate は route を消費して
    # proceed_with_collision_evidence になる（path literal 一致だけで止まらない）。
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    exit_code, payload = _run_cli(
        current_number, current_file, FIXTURES_DIR / "candidates_path_only_false_positive.json"
    )
    assert exit_code == 0
    assert payload["route"] == "proceed_with_collision_evidence"


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


def test_given_section_2_when_rendered_then_all_success_routes_document_exit_0() -> None:
    """Major 2: 分類成功時は route を問わず exit 0 になることを SKILL.md が
    明記し、実行経路でも `wait_for_predecessor` / `human_review_required` /
    `duplicate` すべてが exit 0 を返すことを確認する。"""
    content = _skill_text()
    assert "exit 0" in content
    assert "$?" in content and "分岐条件に使ってはならない" in content

    dep_current = FIXTURES_DIR / "current_with_open_dependency.json"
    dep_number = json.loads(dep_current.read_text(encoding="utf-8"))["number"]
    exit_code, payload = _run_cli(
        dep_number, dep_current, FIXTURES_DIR / "candidates_path_only_false_positive.json"
    )
    assert payload["route"] == "wait_for_predecessor"
    assert exit_code == 0

    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    exit_code, payload = _run_cli(
        current_number, current_file, FIXTURES_DIR / "candidates_duplicate.json"
    )
    assert payload["route"] == "duplicate"
    assert exit_code == 0


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


def test_given_missing_allowed_paths_when_described_then_candidate_is_excluded_with_evidence() -> None:
    """AC6: Allowed Paths 未記載候補は validation error ではなく、証跡を残して
    collision classifier の入力から除外する契約を Section 2 に明記する。"""
    content = _skill_text()
    assert "Allowed Paths 未記載" in content
    assert "ignored_missing_allowed_paths" in content
    assert "collision classifier" in content


def test_given_fail_closed_routes_when_described_then_ambiguous_and_duplicate_route_to_human() -> None:
    """AC3: `ambiguous_requires_human`（`human_review_required` /
    `wait_for_predecessor`）と `duplicate` が fail-closed で
    人間判断へ route されることを示す。実行経路でも missing-field candidate が
    human_review_required に fail-closed で倒れることを確認する。
    """
    content = _skill_text()
    assert "fail-closed" in content
    assert "人間判断へ停止" in content
    for route in ("wait_for_predecessor", "human_review_required", "duplicate", "runtime_error"):
        assert route in content

    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    exit_code, payload = _run_cli(
        current_number, current_file, FIXTURES_DIR / "candidates_missing_body.json"
    )
    assert payload["route"] == "human_review_required"
    assert exit_code == 0


def test_given_candidate_contract_readback_when_described_then_merge_pr_is_not_proposed_before_readback() -> None:
    """AC4: candidate contract の Outcome/In Scope/Out of Scope/Delivery Rule を
    readback 前に統合 PR を提案しないことを明記する。実行経路でも同一
    Allowed Paths だが disjoint な body の candidate（duplicate 候補）が
    readback 確認を経て初めて duplicate/proceed のいずれかへ倒れることを確認する。
    """
    content = _skill_text()
    assert "readback" in content
    assert "統合 PR を提案してはならない" in content
    for heading in ("Outcome", "In Scope"):
        assert heading in content
    assert "same_path_set" in content

    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    exit_code, payload = _run_cli(
        current_number, current_file, FIXTURES_DIR / "candidates_pathset_disjoint_contract.json"
    )
    assert exit_code == 0
    assert payload["route"] == "proceed_with_collision_evidence"
    for cand in payload["candidates"]:
        assert cand["readback_complete"] is True


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
    """PR 作成直前の stale evidence（updated_at / body_sha256 drift）再確認手順
    が deterministic gate（自然言語のみの指示ではない）として明記されていることを
    確認する。"""
    content = _skill_text()
    assert "drift" in content
    assert "updated_at" in content
    assert "body_sha256" in content
    assert "deterministic" in content
    assert "再実行" in content


def test_given_check_issue_overlap_scoring_when_described_then_it_is_out_of_scope() -> None:
    """`check_issue_overlap.py` 本体の scoring / schema 変更は Section 2 の対象外
    （#1452 Out of Scope）であることを明記する。
    """
    content = _skill_text()
    assert "scoring" in content
    assert "対象外" in content


def test_given_open_pr_handoff_when_described_then_overlap_preflight_contract_is_explicit() -> None:
    """Major 1: `open-pr` への handoff に `overlap_preflight` の必須フィールド
    （`required` / `evidence_file` / `expected_digest`）を明示し、open-pr 側の
    validator 変更が本 Issue の Allowed Paths 外である旨を follow-up として
    明記する。"""
    content = _skill_text()
    assert "overlap_preflight" in content
    assert "evidence_file" in content
    assert "expected_digest" in content
    assert "Allowed Paths 外" in content
    assert "follow-up" in content


def test_given_dependency_contract_when_described_then_blocked_by_and_native_dependency_are_covered() -> None:
    """Blocker 2: Machine-Readable Contract の `blocked_by` / `depends_on` /
    `supersedes`（YAML）と GitHub native dependency（`blockedBy` / `blocking`）
    の両方を解析することを明記する。"""
    content = _skill_text()
    assert "blocked_by" in content
    assert "depends_on" in content
    assert "supersedes" in content
    assert "blockedBy" in content
    assert "native dependency" in content
    assert "個別に readback" in content


def test_given_structural_signal_when_described_then_natural_language_similarity_is_advisory_only() -> None:
    """Blocker 1: 自然言語類似度は補助 signal に留め、collision 判定の唯一根拠に
    しないことを明記する。"""
    content = _skill_text()
    assert "構造的シグナル" in content
    assert "補助 signal" in content
    assert "唯一の根拠にはしない" in content
