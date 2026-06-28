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


def test_given_executor_result_schema_when_checking_field_names_then_worktree_path_is_the_key() -> None:
    """B1: executor JSON uses 'worktree_path'; consumer (implementation-worker.md) must map it to IMPLEMENT_RESULT_V1.worktree."""
    import json
    import subprocess
    import sys
    script = REPO_ROOT / "scripts" / "agent-ops" / "worktree_bootstrap_exec.py"
    # Run with intentionally blocked args to get a JSON result containing worktree_path
    result = subprocess.run(
        [sys.executable, str(script), "--issue-number", "0",
         "--slug", "x", "--branch-name", "y",
         "--worktree-path", "z", "--base-ref", "main", "--json"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )
    payload = json.loads(result.stdout)
    # Key must be "worktree_path" (not "worktree")
    assert "worktree_path" in payload, "executor must return 'worktree_path', not 'worktree'"
    assert "worktree" not in payload, "executor must not return a bare 'worktree' key"
    # Consumer doc must document the mapping
    worker_doc = (REPO_ROOT / ".claude" / "agents" / "implementation-worker.md").read_text(encoding="utf-8")
    assert "worktree_path" in worker_doc, "implementation-worker.md must reference 'worktree_path'"
    assert "IMPLEMENT_RESULT_V1.worktree" in worker_doc, "implementation-worker.md must document the worktree_path → worktree mapping"


def test_given_implement_issue_skill_when_step_3_text_is_read_then_worktree_path_and_cd_are_present() -> None:
    """B2: Step 3 must include both worktree_path JSON extraction and cd "$WORKTREE"."""
    content = (REPO_ROOT / ".claude" / "skills" / "implement-issue" / "SKILL.md").read_text(encoding="utf-8")
    assert "worktree_path" in content, "SKILL.md Step 3 must extract worktree_path from executor JSON"
    assert 'cd "$WORKTREE"' in content, "SKILL.md Step 3 must cd into the worktree after executor succeeds"
    assert "git branch --show-current" in content, "SKILL.md Step 3 must verify branch after cd"
