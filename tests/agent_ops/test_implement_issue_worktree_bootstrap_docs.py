from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_given_implement_issue_skill_when_step_3_is_rendered_then_raw_git_worktree_add_is_removed() -> None:
    content = (REPO_ROOT / ".claude" / "skills" / "implement-issue" / "SKILL.md").read_text(encoding="utf-8")
    assert "git worktree add \"$WORKTREE\"" not in content
    assert "scripts/agent-ops/worktree_bootstrap_exec.py" in content
    assert "--issue-number" in content
    assert "--worktree-path" in content


def test_given_implementation_worker_doc_when_v1_dispatch_is_described_then_executor_result_fields_are_named() -> None:
    content = (REPO_ROOT / ".claude" / "agents" / "implementation-worker.md").read_text(encoding="utf-8")
    assert "worktree_bootstrap_exec.py" in content
    assert "IMPLEMENT_RESULT_V1" in content
    assert "worktree" in content
    assert "branch" in content
