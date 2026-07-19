"""AC3 (#1619): #1611(current) / #1612(candidate, native blockedBy: [1611],
共有 parent_refs) 相当の sibling successor dependency シナリオの回帰テスト。

`check_issue_overlap.classify_overlap`（pure classifier の正本）を直接
呼び出し、candidate が current に対して native dependency 経由で successor
関係（candidate が current に依存している = current を止めていない）を
持つ場合に、shared parent_refs があっても verdict が
`AMBIGUOUS_REQUIRES_HUMAN` / `parent_child_collision` にならないことを
確認する。

`.claude/skills/implement-issue/scripts/check_implementation_overlap.py` の
native dependency 取得ロジック自体は本 Issue の Out of Scope であり、本
テストは `check_issue_overlap.classify_overlap` への consumer 整合確認の
みを行う。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_issue_overlap as cio  # noqa: E402


def test_shared_parent_native_successor_route_not_parent_collision():
    # GIVEN: #1611（current）/ #1612（candidate）は sibling child として
    # 同一 parent を共有し、candidate は native `blockedBy: [1611]` により
    # current に依存している（= candidate が current の successor）。
    current = cio.IssueScope(
        title="実装: #1611 側の変更",
        number=1611,
        allowed_paths=(".claude/skills/implement-issue/scripts/check_implementation_overlap.py",),
        parent_refs=("#1600",),
    )
    candidate = cio.IssueScope(
        number=1612,
        title="実装: #1612 側の変更",
        allowed_paths=(".claude/skills/implement-issue/scripts/check_implementation_overlap.py",),
        parent_refs=("#1600",),
        depends_on=("#1611",),  # native blockedBy: [1611] 相当
        state="OPEN",
    )

    # WHEN
    result = cio.classify_overlap(current, [candidate])

    # THEN: successor 関係は current に依存する側（current を止めていない）
    # であり、shared parent_refs による無条件 parent_collision(C3) に倒れず、
    # 安全な直列化順序（overlap_requires_comment, C2a）になる。
    assert result.candidates[0].dependency_relation.relation == "successor"
    assert result.verdict != cio.AMBIGUOUS_REQUIRES_HUMAN
    assert result.reason_code != "parent_child_collision"
    assert result.verdict == cio.OVERLAP_REQUIRES_COMMENT
    assert result.policy_class == "C2a"
    assert result.reason_code == "successor_dependency_ordering"


def test_reverse_direction_current_depends_on_candidate_is_still_predecessor_c2b():
    # 回帰確認: 依存方向が逆（current が candidate に依存 = predecessor）の
    # 場合は既存の C2b 挙動のまま変わらない。
    current = cio.IssueScope(
        title="実装: #1612 側の変更",
        number=1612,
        allowed_paths=(".claude/skills/implement-issue/scripts/check_implementation_overlap.py",),
        parent_refs=("#1600",),
        depends_on=("#1611",),
    )
    candidate = cio.IssueScope(
        number=1611,
        title="実装: #1611 側の変更",
        allowed_paths=(".claude/skills/implement-issue/scripts/check_implementation_overlap.py",),
        parent_refs=("#1600",),
        state="OPEN",
    )

    result = cio.classify_overlap(current, [candidate])

    assert result.candidates[0].dependency_relation.relation == "predecessor"
    assert result.verdict == cio.AMBIGUOUS_REQUIRES_HUMAN
    assert result.policy_class == "C2b"


# ------------------------------------------------------------
# #1621 AC2: adapter-level (check_implementation_overlap.py) consumer
# integration -- candidate raw WITHOUT blockedBy, current native blocking
# alone must be enough for check_issue_overlap._dependency_relation() to
# return "successor" (no dependency on the second-stage candidate native
# dependency readback).
# ------------------------------------------------------------

import importlib.util as _importlib_util  # noqa: E402

_IMPL_SCRIPT_PATH = (
    REPO_ROOT / ".claude" / "skills" / "implement-issue" / "scripts" / "check_implementation_overlap.py"
)
_impl_spec = _importlib_util.spec_from_file_location(
    "check_implementation_overlap_successor_dep_adapter", _IMPL_SCRIPT_PATH
)
assert _impl_spec is not None and _impl_spec.loader is not None
impl_module = _importlib_util.module_from_spec(_impl_spec)
sys.modules[_impl_spec.name] = impl_module
_impl_spec.loader.exec_module(impl_module)


def test_adapter_current_blocking_only_without_candidate_blocked_by_produces_successor_relation() -> None:
    """#1621 AC2: candidate raw に blockedBy が存在しない状態でも、current の
    native blocking だけから check_issue_overlap._dependency_relation() が
    successor を返す（第二段階の candidate native dependency 読み戻しに
    依存しない）。#1611(current)/#1612(candidate) 相当の adapter 層向け
    同型 fixture。
    """
    current_raw = {
        "number": 1611,
        "title": "実装: #1611 側の変更",
        "body": "## Allowed Paths\n\n- a.py\n",
        "blocking": [{"repository": "squne121/loop-protocol", "number": 1612, "state": "OPEN"}],
    }
    candidate_raw = {
        "number": 1612,
        "title": "実装: #1612 側の変更",
        "body": "## Allowed Paths\n\n- a.py\n",
        "state": "OPEN",
        # 注意: blockedBy は意図的に存在しない（AC2 の検証対象）
    }
    assert "blockedBy" not in candidate_raw  # precondition

    successor_numbers = impl_module._current_native_successor_numbers(current_raw, "squne121/loop-protocol")
    assert 1612 in successor_numbers

    current_scope = cio.IssueScope(
        title=current_raw["title"],
        number=current_raw["number"],
        allowed_paths=("a.py",),
    )
    extra_depends_on = (str(current_raw["number"]),) if candidate_raw["number"] in successor_numbers else ()
    candidate_scope = impl_module._issue_scope_from_raw(
        candidate_raw, current_repo="squne121/loop-protocol", extra_depends_on=extra_depends_on
    )

    relation = cio._dependency_relation(current_scope, candidate_scope)
    assert relation.relation == "successor"
