#!/usr/bin/env python3
"""Issue #1241 regression coverage for repair hints and rtk git policy."""

from pathlib import Path

from worktree_scope_guard_testkit import _bash_payload, _make_repo_with_worktree, _run_guard


def test_issue1241_rtk_git_add_inside_worktree_allowed(tmp_path: Path):
    repo = _make_repo_with_worktree(tmp_path, issue="1241", slug="repair-hint")
    target = repo["worktree"] / "src" / "file.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x")
    payload = _bash_payload("rtk git add src/file.txt", str(repo["worktree"]))
    result = _run_guard(
        payload,
        repo["root"],
        issue="1241",
        extra_env={"CODEX_ALLOWED_PATHS": "src/file.txt\n"},
    )
    assert result.returncode == 0, result.stderr


def test_issue1241_rtk_git_add_broad_pathspec_emits_hint(tmp_path: Path):
    repo = _make_repo_with_worktree(tmp_path, issue="1241", slug="repair-hint")
    payload = _bash_payload("rtk git add .", str(repo["worktree"]))
    result = _run_guard(
        payload,
        repo["root"],
        issue="1241",
        extra_env={"CODEX_ALLOWED_PATHS": "src/file.txt\n"},
    )
    assert result.returncode == 2
    assert "HOOK_COMMAND_REPAIR_HINT_V1:" in result.stderr
    assert 'reason_code: "git_add_requires_explicit_pathspec"' in result.stderr


def test_issue1241_rtk_git_add_outside_allowed_paths_emits_hint(tmp_path: Path):
    repo = _make_repo_with_worktree(tmp_path, issue="1241", slug="repair-hint")
    target = repo["worktree"] / "src" / "file.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x")
    payload = _bash_payload("rtk git add src/file.txt", str(repo["worktree"]))
    result = _run_guard(
        payload,
        repo["root"],
        issue="1241",
        extra_env={"CODEX_ALLOWED_PATHS": "docs/dev/hook-boundaries.md\n"},
    )
    assert result.returncode == 2
    assert "HOOK_COMMAND_REPAIR_HINT_V1:" in result.stderr
    assert 'reason_code: "git_add_outside_allowed_paths"' in result.stderr


def test_issue1241_local_main_root_rtk_git_push_wrong_refspec_emits_hint(tmp_path: Path):
    repo = _make_repo_with_worktree(tmp_path, issue="1241", slug="repair-hint")
    payload = _bash_payload("rtk git push origin main", str(repo["root"]))
    result = _run_guard(payload, repo["root"], issue="1241")
    assert result.returncode == 2
    assert "HOOK_COMMAND_REPAIR_HINT_V1:" in result.stderr
    assert 'reason_code: "push_refspec_requires_active_branch"' in result.stderr


def test_issue1241_rtk_git_without_issue_context_emits_hint(tmp_path: Path):
    repo = _make_repo_with_worktree(tmp_path, issue="1241", slug="repair-hint")
    payload = _bash_payload("rtk git add src/file.txt", str(repo["root"]))
    result = _run_guard(
        payload,
        repo["root"],
        issue=None,
        extra_env={"CODEX_ALLOWED_PATHS": "src/file.txt\n"},
    )
    assert result.returncode == 2
    assert "HOOK_COMMAND_REPAIR_HINT_V1:" in result.stderr
    assert 'reason_code: "issue_context_required"' in result.stderr
