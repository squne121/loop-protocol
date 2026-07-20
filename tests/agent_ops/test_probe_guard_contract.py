"""tests/agent_ops/test_probe_guard_contract.py — guard parity tests for probe scripts (Issue #1197).

Covers:
- AC6: worktree_scope_guard and local_main_branch_guard both allow probe scripts
       as deterministic checkers; malformed variants are denied (deterministic_checker)
"""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-guards"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-ops"))

from local_main_branch_guard import (  # noqa: E402
    DETERMINISTIC_CHECKER_ALLOWLIST,
    is_deterministic_checker_command,
)


# ─── AC6: deterministic_checker ───────────────────────────────────────────────


class TestDeterministicCheckerParity:
    def test_deterministic_checker_ref_probe_in_allowlist(self) -> None:
        """AC6: git_ref_probe.py must be in DETERMINISTIC_CHECKER_ALLOWLIST."""
        assert "scripts/agent-ops/git_ref_probe.py" in DETERMINISTIC_CHECKER_ALLOWLIST

    def test_deterministic_checker_worktree_probe_in_allowlist(self) -> None:
        """AC6: git_worktree_probe.py must be in DETERMINISTIC_CHECKER_ALLOWLIST."""
        assert "scripts/agent-ops/git_worktree_probe.py" in DETERMINISTIC_CHECKER_ALLOWLIST

    def test_deterministic_checker_ref_probe_exact_cmd_allowed(self) -> None:
        """AC6: exact uv run python3 git_ref_probe.py --branch main --json is allowed."""
        cmd = "uv run python3 scripts/agent-ops/git_ref_probe.py --branch main --json"
        assert is_deterministic_checker_command(cmd, str(REPO_ROOT))

    def test_deterministic_checker_worktree_probe_exact_cmd_allowed(self) -> None:
        """AC6: exact uv run python3 git_worktree_probe.py --json is allowed."""
        cmd = "uv run python3 scripts/agent-ops/git_worktree_probe.py --json"
        assert is_deterministic_checker_command(cmd, str(REPO_ROOT))

    def test_deterministic_checker_ref_probe_custom_branch_allowed(self) -> None:
        """AC6: ref probe with different branch name is still allowed."""
        cmd = "uv run python3 scripts/agent-ops/git_ref_probe.py --branch feature-xyz --json"
        assert is_deterministic_checker_command(cmd, str(REPO_ROOT))

    def test_deterministic_checker_ref_probe_unknown_flag_denied(self) -> None:
        """AC6/B1: ref probe with unknown flag --verbose is denied by is_deterministic_checker_command.

        B1 introduced argv validation into is_deterministic_checker_command via _validate_probe_argv,
        mirroring worktree_scope_guard._validate_agent_ops_argv. Unknown flags are now rejected
        at the local_main_branch_guard level (not just the worktree_scope_guard level).
        """
        cmd = "uv run python3 scripts/agent-ops/git_ref_probe.py --branch main --verbose"
        assert not is_deterministic_checker_command(cmd, str(REPO_ROOT))

    def test_deterministic_checker_cleanup_exec_not_in_allowlist(self) -> None:
        """AC6: adding probe scripts must not break existing cleanup_exec allowlist."""
        # cleanup_exec is handled by a different mechanism (agent_ops_allowed_scripts)
        # but the deterministic checker allowlist should only have probe scripts
        assert "scripts/agent-ops/cleanup_exec.py" not in DETERMINISTIC_CHECKER_ALLOWLIST

    def test_deterministic_checker_non_probe_script_not_in_allowlist(self) -> None:
        """AC6: arbitrary scripts are not in the deterministic checker allowlist."""
        cmd = "uv run python3 scripts/agent-ops/worktree_catalog.py --json"
        assert not is_deterministic_checker_command(cmd, str(REPO_ROOT))


class TestWorktreeScopeGuardArgSpecs:
    """Test that worktree_scope_guard._AGENT_OPS_ARG_SPECS includes probe scripts."""

    def _get_guard_module(self):
        """Import worktree_scope_guard for inspection."""
        import importlib.util

        guard_py = REPO_ROOT / "scripts" / "agent-guards" / "worktree_scope_guard.py"
        spec = importlib.util.spec_from_file_location("worktree_scope_guard", str(guard_py))
        mod = importlib.util.module_from_spec(spec)
        # Set up sys.path for guard dependencies
        agent_guards = str(REPO_ROOT / "scripts" / "agent-guards")
        agent_ops = str(REPO_ROOT / "scripts" / "agent-ops")
        old_path = sys.path[:]
        if agent_guards not in sys.path:
            sys.path.insert(0, agent_guards)
        if agent_ops not in sys.path:
            sys.path.insert(0, agent_ops)
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.path[:] = old_path
        return mod

    def test_ref_probe_in_allowed_scripts(self) -> None:
        """AC6: worktree_scope_guard._AGENT_OPS_ALLOWED_SCRIPTS includes git_ref_probe.py."""
        mod = self._get_guard_module()
        assert "scripts/agent-ops/git_ref_probe.py" in mod._AGENT_OPS_ALLOWED_SCRIPTS

    def test_worktree_probe_in_allowed_scripts(self) -> None:
        """AC6: worktree_scope_guard._AGENT_OPS_ALLOWED_SCRIPTS includes git_worktree_probe.py."""
        mod = self._get_guard_module()
        assert "scripts/agent-ops/git_worktree_probe.py" in mod._AGENT_OPS_ALLOWED_SCRIPTS

    def test_ref_probe_arg_spec_present(self) -> None:
        """AC6: _AGENT_OPS_ARG_SPECS has entry for git_ref_probe.py."""
        mod = self._get_guard_module()
        assert "scripts/agent-ops/git_ref_probe.py" in mod._AGENT_OPS_ARG_SPECS

    def test_worktree_probe_arg_spec_present(self) -> None:
        """AC6: _AGENT_OPS_ARG_SPECS has entry for git_worktree_probe.py."""
        mod = self._get_guard_module()
        assert "scripts/agent-ops/git_worktree_probe.py" in mod._AGENT_OPS_ARG_SPECS

    def test_ref_probe_arg_spec_requires_branch(self) -> None:
        """AC6: git_ref_probe.py arg spec requires --branch."""
        mod = self._get_guard_module()
        spec = mod._AGENT_OPS_ARG_SPECS["scripts/agent-ops/git_ref_probe.py"]
        assert "--branch" in spec["required"]

    def test_ref_probe_argv_valid(self) -> None:
        """AC6: valid argv for git_ref_probe.py passes _validate_agent_ops_argv."""
        mod = self._get_guard_module()
        args = ["--branch", "main", "--json"]
        assert mod._validate_agent_ops_argv("scripts/agent-ops/git_ref_probe.py", args)

    def test_ref_probe_argv_unknown_flag_rejected(self) -> None:
        """AC6: unknown flag in git_ref_probe.py argv is rejected."""
        mod = self._get_guard_module()
        args = ["--branch", "main", "--verbose"]
        assert not mod._validate_agent_ops_argv("scripts/agent-ops/git_ref_probe.py", args)

    def test_ref_probe_argv_flag_equals_form_rejected(self) -> None:
        """AC6: --flag=value form is rejected for probe scripts."""
        mod = self._get_guard_module()
        args = ["--branch=main", "--json"]
        assert not mod._validate_agent_ops_argv("scripts/agent-ops/git_ref_probe.py", args)

    def test_worktree_probe_argv_valid(self) -> None:
        """AC6: valid argv for git_worktree_probe.py passes _validate_agent_ops_argv."""
        mod = self._get_guard_module()
        args = ["--json"]
        assert mod._validate_agent_ops_argv("scripts/agent-ops/git_worktree_probe.py", args)

    def test_worktree_probe_argv_extra_positional_rejected(self) -> None:
        """AC6: extra positional arg in git_worktree_probe.py argv is rejected."""
        mod = self._get_guard_module()
        args = ["--json", "extra_positional"]
        assert not mod._validate_agent_ops_argv("scripts/agent-ops/git_worktree_probe.py", args)

    def test_worktree_probe_argv_unknown_flag_rejected(self) -> None:
        """AC6: unknown flag in git_worktree_probe.py argv is rejected."""
        mod = self._get_guard_module()
        args = ["--json", "--verbose"]
        assert not mod._validate_agent_ops_argv("scripts/agent-ops/git_worktree_probe.py", args)
