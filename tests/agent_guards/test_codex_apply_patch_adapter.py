#!/usr/bin/env python3
"""test_codex_apply_patch_adapter.py — contract tests for
scripts/agent-guards/codex_apply_patch_adapter.py (Issue #1657 AC5/AC6).

Covers: main-from-outside (reject), target-worktree (allow), other-worktree
(reject), relative path, absolute path, Move operations (source AND
destination containment), unparseable/absent patch bodies (fail-closed), and
Edit/Write delegation to the shared worktree_scope_guard core.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from worktree_scope_guard_testkit import (
    _apply_patch_payload,
    _make_repo_with_worktree,
    _run_codex_apply_patch_adapter,
    _write_tool_payload,
)


def _add_file_patch(path: str) -> str:
    return f"*** Begin Patch\n*** Add File: {path}\n+print('hi')\n*** End Patch\n"


def _update_file_patch(path: str) -> str:
    return f"*** Begin Patch\n*** Update File: {path}\n@@\n-a\n+b\n*** End Patch\n"


def _delete_file_patch(path: str) -> str:
    return f"*** Begin Patch\n*** Delete File: {path}\n*** End Patch\n"


def _move_patch(old_path: str, new_path: str) -> str:
    return f"*** Begin Patch\n*** Update File: {old_path}\n*** Move to: {new_path}\n@@\n-a\n+b\n*** End Patch\n"


class TestApplyPatchWorktreeContainment:
    def test_apply_patch_from_main_root_is_blocked(self, tmp_path: Path) -> None:
        """GIVEN an active issue worktree exists WHEN apply_patch runs from the
        main root (cwd outside the worktree) THEN it is blocked."""
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        payload = _apply_patch_payload(_add_file_patch("foo.py"), cwd=str(repo["root"]))
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue="942")
        assert result.returncode == 2, result.stderr

    def test_apply_patch_inside_target_worktree_is_allowed(self, tmp_path: Path) -> None:
        """GIVEN an active issue worktree WHEN apply_patch runs with cwd inside
        that worktree and a relative target path THEN it is allowed."""
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        payload = _apply_patch_payload(_add_file_patch("foo.py"), cwd=str(repo["worktree"]))
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue="942")
        assert result.returncode == 0, result.stderr

    def test_apply_patch_from_other_worktree_is_blocked(self, tmp_path: Path) -> None:
        """GIVEN issue 942's worktree is active WHEN apply_patch runs with cwd
        inside a DIFFERENT issue's worktree THEN it is blocked."""
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x", extra_worktrees=[("943", "y")])
        other_wt = repo["worktrees"]["943"]
        payload = _apply_patch_payload(_add_file_patch("foo.py"), cwd=str(other_wt))
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue="942")
        assert result.returncode == 2, result.stderr

    def test_apply_patch_relative_path_inside_worktree_is_allowed(self, tmp_path: Path) -> None:
        """GIVEN cwd is inside the worktree WHEN the patch target is a nested
        relative path THEN it resolves inside the worktree and is allowed."""
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        payload = _apply_patch_payload(_update_file_patch("src/nested/mod.py"), cwd=str(repo["worktree"]))
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue="942")
        assert result.returncode == 0, result.stderr

    def test_apply_patch_absolute_path_is_blocked(self, tmp_path: Path) -> None:
        """GIVEN an apply_patch target header carries an absolute path THEN it
        is blocked unconditionally (Codex apply_patch paths are always
        repo-relative)."""
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        payload = _apply_patch_payload(_add_file_patch("/etc/passwd"), cwd=str(repo["worktree"]))
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue="942")
        assert result.returncode == 2, result.stderr

    def test_apply_patch_absolute_path_escaping_worktree_is_blocked_even_with_no_issue(
        self, tmp_path: Path
    ) -> None:
        """Absolute-path rejection is unconditional, independent of worktree
        resolution."""
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        payload = _apply_patch_payload(_add_file_patch("/tmp/evil.py"), cwd=str(repo["root"]))
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue=None)
        assert result.returncode == 2, result.stderr

    def test_apply_patch_move_source_inside_destination_inside_is_allowed(self, tmp_path: Path) -> None:
        """GIVEN a Move operation (Update File + Move to) WHEN both source and
        destination resolve inside the worktree THEN it is allowed."""
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        payload = _apply_patch_payload(_move_patch("old.py", "new.py"), cwd=str(repo["worktree"]))
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue="942")
        assert result.returncode == 0, result.stderr

    def test_apply_patch_move_destination_outside_worktree_is_blocked(self, tmp_path: Path) -> None:
        """GIVEN a Move operation WHEN the destination (Move to) path escapes
        the worktree via traversal THEN it is blocked (destination containment
        is checked independently of the source)."""
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        payload = _apply_patch_payload(
            _move_patch("old.py", "../../../escape.py"), cwd=str(repo["worktree"])
        )
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue="942")
        assert result.returncode == 2, result.stderr

    def test_apply_patch_delete_file_target_is_containment_checked(self, tmp_path: Path) -> None:
        """GIVEN a Delete File patch WHEN the target path is inside the
        worktree THEN it is allowed (Delete headers are containment-checked
        the same as Add/Update)."""
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        payload = _apply_patch_payload(_delete_file_patch("stale.py"), cwd=str(repo["worktree"]))
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue="942")
        assert result.returncode == 0, result.stderr

    def test_apply_patch_unparseable_body_is_blocked_fail_closed(self, tmp_path: Path) -> None:
        """GIVEN a patch body with no recognizable Add/Update/Delete/Move
        header THEN it is blocked fail-closed (cannot prove containment)."""
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        payload = _apply_patch_payload("not a patch at all", cwd=str(repo["worktree"]))
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue="942")
        assert result.returncode == 2, result.stderr

    def test_apply_patch_missing_command_is_blocked_fail_closed(self, tmp_path: Path) -> None:
        """GIVEN tool_input.command is absent THEN it is blocked fail-closed."""
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        payload = {"tool_name": "apply_patch", "tool_input": {}, "cwd": str(repo["worktree"])}
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue="942")
        assert result.returncode == 2, result.stderr

    def test_apply_patch_nul_byte_in_target_is_blocked(self, tmp_path: Path) -> None:
        """GIVEN a target path containing a NUL byte THEN it is blocked
        unconditionally."""
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        payload = _apply_patch_payload(_add_file_patch("foo\x00.py"), cwd=str(repo["worktree"]))
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue="942")
        assert result.returncode == 2, result.stderr

    def test_apply_patch_with_no_active_issue_is_allowed(self, tmp_path: Path) -> None:
        """GIVEN no active issue can be resolved (no LOOP_ISSUE_NUMBER, cwd not
        an issue worktree) THEN apply_patch is not scoped and is allowed."""
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        payload = _apply_patch_payload(_add_file_patch("foo.py"), cwd=str(repo["root"]))
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue=None)
        assert result.returncode == 0, result.stderr

    def test_given_version_mismatch_when_canonical_apply_patch_then_fail_closed(self, tmp_path: Path) -> None:
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        payload = _apply_patch_payload(_add_file_patch("foo.py"), cwd=str(repo["worktree"]))
        payload["runtime_version"] = "0.999.0"
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue="942")
        assert result.returncode == 2, result.stderr

    def test_given_legacy_apply_patch_alias_when_adapter_runs_then_fail_closed(self, tmp_path: Path) -> None:
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        payload = _apply_patch_payload(_add_file_patch("foo.py"), cwd=str(repo["worktree"]))
        payload["tool_name"] = "ApplyPatch"
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue="942")
        assert result.returncode == 2, result.stderr


class TestEditWriteDelegation:
    """AC6: the same adapter script is wired to the `apply_patch|Edit|Write`
    matcher, so Edit/Write tool calls must also be authorized (delegating to
    the shared worktree_scope_guard core, not re-implemented here)."""

    def test_edit_inside_worktree_is_allowed(self, tmp_path: Path) -> None:
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        target = str(repo["worktree"] / "bar.py")
        payload = _write_tool_payload("Edit", target, cwd=str(repo["worktree"]))
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue="942")
        assert result.returncode == 0, result.stderr

    def test_write_outside_worktree_is_blocked(self, tmp_path: Path) -> None:
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        target = str(repo["root"] / "bar.py")
        payload = _write_tool_payload("Write", target, cwd=str(repo["root"]))
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue="942")
        assert result.returncode == 2, result.stderr

    def test_other_tool_names_are_allowed_passthrough(self, tmp_path: Path) -> None:
        """A tool_name outside {apply_patch, Edit, Write, MultiEdit, Bash} is
        not matched by this adapter's contract and is allowed."""
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        payload = {"tool_name": "SomeOtherTool", "tool_input": {}, "cwd": str(repo["root"])}
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue="942")
        assert result.returncode == 0, result.stderr


class TestMalformedPayload:
    def test_malformed_json_stdin_is_blocked_fail_closed(self, tmp_path: Path) -> None:
        import os
        import subprocess

        from worktree_scope_guard_testkit import CODEX_APPLY_PATCH_ADAPTER_PY

        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        env = dict(os.environ)
        env["CLAUDE_PROJECT_DIR"] = str(repo["root"])
        env["LOOP_ISSUE_NUMBER"] = "942"
        result = subprocess.run(
            ["python3", str(CODEX_APPLY_PATCH_ADAPTER_PY)],
            input="{not valid json",
            text=True,
            capture_output=True,
            env=env,
        )
        assert result.returncode == 2, result.stderr

    def test_given_non_object_json_when_adapter_runs_then_fail_closed(self, tmp_path: Path) -> None:
        import os
        import subprocess

        from worktree_scope_guard_testkit import CODEX_APPLY_PATCH_ADAPTER_PY

        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "942"}
        result = subprocess.run(
            ["python3", str(CODEX_APPLY_PATCH_ADAPTER_PY)], input="[]", text=True,
            capture_output=True, env=env,
        )
        assert result.returncode == 2, result.stderr

    def test_given_non_object_tool_input_when_adapter_runs_then_fail_closed(self, tmp_path: Path) -> None:
        repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
        payload = {"tool_name": "apply_patch", "tool_input": [], "cwd": str(repo["worktree"])}
        result = _run_codex_apply_patch_adapter(payload, repo["root"], issue="942")
        assert result.returncode == 2, result.stderr


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
