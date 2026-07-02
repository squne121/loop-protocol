#!/usr/bin/env python3
"""test_contract_review_no_worktree_creation.py — AC7 / AC12 of Issue #1284.

AC7:  SKILL.md に worktree 作成経路が残っておらず、metadata mutation は
      controlled executor lane と明記されている。
AC12: issue-contract-review の production path は raw `gh issue edit` /
      `gh issue comment` を呼ばず、`local_main_branch_guard.py` の既存 raw gh
      allowlist を成功経路の認可根拠として使わないことが test で固定されている。
"""

from __future__ import annotations

import re
from pathlib import Path

_THIS_FILE = Path(__file__).resolve()
_SKILL_DIR = _THIS_FILE.parent.parent  # .claude/skills/issue-contract-review
_SKILL_MD = _SKILL_DIR / "SKILL.md"


def _skill_md_text() -> str:
    return _SKILL_MD.read_text(encoding="utf-8")


def _skill_scripts() -> list[Path]:
    scripts_dir = _SKILL_DIR / "scripts"
    if not scripts_dir.exists():
        return []
    return sorted(scripts_dir.glob("*.py"))


class TestAC7SkillMdNoWorktreeCreation:
    def test_no_worktree_add_command_in_skill_md(self):
        text = _skill_md_text()
        assert "git worktree add" not in text, (
            "SKILL.md must not contain a worktree creation command "
            "(issue-contract-review is preflight-only, Issue #1284 AC7)"
        )

    def test_skill_md_states_worktree_not_created_by_this_skill(self):
        text = _skill_md_text()
        assert "worktree を本 skill で作らない" in text or "worktree を作らない" in text

    def test_skill_md_mentions_controlled_executor_lane(self):
        text = _skill_md_text()
        assert "controlled_skill_mutation_exec.py" in text
        assert "contract_snapshot.publish" in text

    def test_skill_md_does_not_state_direct_gh_post_for_contract_snapshot(self):
        """AC7/AC8: SKILL.md must not instruct posting the Contract Snapshot via
        raw `gh issue comment` (the controlled executor lane must be used);
        any mention of `gh issue comment` near a Contract Snapshot posting
        description must be explicitly disclaimed as unused."""
        text = _skill_md_text()
        for line in text.splitlines():
            if "Contract Snapshot" in line and "コメント投稿" in line and "gh issue comment" in line:
                assert "使わない" in line


class TestAC12ProductionPathNoRawGhMutation:
    def test_no_raw_gh_issue_edit_in_scripts(self):
        pattern = re.compile(r"\bgh\s+issue\s+edit\b")
        for script in _skill_scripts():
            text = script.read_text(encoding="utf-8")
            assert not pattern.search(text), f"raw 'gh issue edit' found in {script}"

    def test_no_raw_gh_issue_comment_in_scripts(self):
        pattern = re.compile(r"\bgh\s+issue\s+comment\b")
        for script in _skill_scripts():
            text = script.read_text(encoding="utf-8")
            assert not pattern.search(text), f"raw 'gh issue comment' found in {script}"

    def test_no_direct_post_flag_invocation_in_scripts(self):
        """AC8: no direct `--post` invocation of ensure_contract_snapshot.py
        outside the controlled executor exists in the issue-contract-review
        production path."""
        for script in _skill_scripts():
            text = script.read_text(encoding="utf-8")
            assert "ensure_contract_snapshot" not in text or "--post" not in text, (
                f"{script} appears to invoke ensure_contract_snapshot.py with "
                f"--post directly; must go through controlled_skill_mutation_exec.py"
            )

    def test_skill_md_does_not_cite_local_main_branch_guard_as_authorization(self):
        """AC12: SKILL.md must not use local_main_branch_guard.py's raw gh
        allowlist as the authorization basis for the success path. Every
        mention must be within a nearby window that explicitly disclaims it."""
        text = _skill_md_text()
        assert "local_main_branch_guard" in text, "expected an explicit disclaimer mention"
        for m in re.finditer("local_main_branch_guard", text):
            window = text[max(0, m.start() - 40): m.end() + 120]
            assert "使わない" in window, (
                f"mention of local_main_branch_guard must be near a disclaimer: {window!r}"
            )
