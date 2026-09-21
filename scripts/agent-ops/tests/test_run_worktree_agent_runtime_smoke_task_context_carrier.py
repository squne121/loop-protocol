"""Issue #2568 In Scope: purely additive Task Context env/carrier
passthrough (--task-context-scope / --task-context-state-root). This runner
never interprets these values -- see
scripts/task-context/task_context_runtime_smoke_verifier.py for the Task
Context-specific semantics these carriers feed into."""

from __future__ import annotations

import importlib.util
import shlex
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_worktree_agent_runtime_smoke", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_fake_exe(path: Path, script_body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{script_body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TestTaskContextEnvPairsPureFunction:
    def test_given_neither_value_when_computed_then_empty(self) -> None:
        module = _load_module()
        assert module._task_context_env_pairs(None, None) == []

    def test_given_both_values_when_computed_then_both_pairs_in_order(self) -> None:
        module = _load_module()
        pairs = module._task_context_env_pairs("runtime_smoke", "/abs/state-root")
        assert pairs == [
            ("LOOP_TASK_CONTEXT_SCOPE", "runtime_smoke"),
            ("LOOP_TASK_CONTEXT_STATE_ROOT", "/abs/state-root"),
        ]

    def test_given_only_scope_when_computed_then_only_scope_pair(self) -> None:
        module = _load_module()
        assert module._task_context_env_pairs("runtime_smoke", None) == [
            ("LOOP_TASK_CONTEXT_SCOPE", "runtime_smoke")
        ]

    def test_given_only_state_root_when_computed_then_only_state_root_pair(self) -> None:
        module = _load_module()
        assert module._task_context_env_pairs(None, "/abs/state-root") == [
            ("LOOP_TASK_CONTEXT_STATE_ROOT", "/abs/state-root")
        ]

    def test_given_empty_strings_when_computed_then_empty(self) -> None:
        module = _load_module()
        assert module._task_context_env_pairs("", "") == []


class TestStructuredLaneCarrierPassthrough:
    def test_omitted_by_default_run_structured_claude_env_unchanged(self) -> None:
        module = _load_module()
        import inspect

        params = inspect.signature(module.run_structured_claude).parameters
        assert "task_context_scope" in params
        assert params["task_context_scope"].default is None
        assert "task_context_state_root" in params
        assert params["task_context_state_root"].default is None

    def test_given_carrier_flags_when_child_launched_then_child_process_env_carries_both_vars(
        self, tmp_path: Path,
    ) -> None:
        module = _load_module()
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        observed = tmp_path / "env-observed.txt"
        _write_fake_exe(fake_bin / "claude", f"""
OBSERVED_FILE={shlex.quote(str(observed))}
printf 'SCOPE=%s\\nSTATE_ROOT=%s\\n' "$LOOP_TASK_CONTEXT_SCOPE" "$LOOP_TASK_CONTEXT_STATE_ROOT" > "$OBSERVED_FILE"
cat > /dev/null
printf '%s\\n' '{{"type":"system","subtype":"init"}}'
printf '%s\\n' '{{"type":"result","subtype":"success"}}'
""")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        state_root = tmp_path / "isolated-state-root"
        rc, _out, _err, _timed_out = module.run_structured_claude(
            str(worktree), "hello", 30.0, 4, claude_bin=str(fake_bin / "claude"),
            task_context_scope="runtime_smoke",
            task_context_state_root=str(state_root),
        )
        assert rc == 0
        observed_lines = observed.read_text(encoding="utf-8").splitlines()
        assert observed_lines == ["SCOPE=runtime_smoke", f"STATE_ROOT={state_root}"]

    def test_given_carrier_flags_omitted_when_child_launched_then_vars_absent(
        self, tmp_path: Path,
    ) -> None:
        module = _load_module()
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        observed = tmp_path / "env-observed.txt"
        _write_fake_exe(fake_bin / "claude", f"""
OBSERVED_FILE={shlex.quote(str(observed))}
printf 'SCOPE=%s\\nSTATE_ROOT=%s\\n' "$LOOP_TASK_CONTEXT_SCOPE" "$LOOP_TASK_CONTEXT_STATE_ROOT" > "$OBSERVED_FILE"
cat > /dev/null
printf '%s\\n' '{{"type":"system","subtype":"init"}}'
printf '%s\\n' '{{"type":"result","subtype":"success"}}'
""")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        rc, _out, _err, _timed_out = module.run_structured_claude(
            str(worktree), "hello", 30.0, 4, claude_bin=str(fake_bin / "claude"),
        )
        assert rc == 0
        observed_lines = observed.read_text(encoding="utf-8").splitlines()
        assert observed_lines == ["SCOPE=", "STATE_ROOT="]

    def test_given_carrier_flags_with_claude_gpt_adapter_when_child_launched_then_vars_present(
        self, tmp_path: Path,
    ) -> None:
        """The claude-gpt branch builds its own launch_env (os.environ.copy())
        independently of the native branch -- confirm the carrier is applied
        there too, not just the native branch's launch_env=None default
        path."""
        module = _load_module()
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        observed = tmp_path / "env-observed.txt"
        _write_fake_exe(fake_bin / "claude", f"""
OBSERVED_FILE={shlex.quote(str(observed))}
printf 'SCOPE=%s\\nSTATE_ROOT=%s\\n' "$LOOP_TASK_CONTEXT_SCOPE" "$LOOP_TASK_CONTEXT_STATE_ROOT" > "$OBSERVED_FILE"
cat > /dev/null
printf '%s\\n' '{{"type":"system","subtype":"init"}}'
printf '%s\\n' '{{"type":"result","subtype":"success"}}'
""")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        state_root = tmp_path / "isolated-state-root"
        rc, _out, _err, _timed_out = module.run_structured_claude(
            str(worktree), "hello", 30.0, 4, claude_bin=str(fake_bin / "claude"),
            claude_adapter="claude-gpt",
            task_context_scope="runtime_smoke",
            task_context_state_root=str(state_root),
        )
        assert rc == 0
        observed_lines = observed.read_text(encoding="utf-8").splitlines()
        assert observed_lines == ["SCOPE=runtime_smoke", f"STATE_ROOT={state_root}"]


class TestInteractiveLaneCarrierSignature:
    def test_run_interactive_herdr_isolated_accepts_carrier_params_defaulting_to_none(self) -> None:
        module = _load_module()
        import inspect

        params = inspect.signature(module.run_interactive_herdr_isolated).parameters
        assert "task_context_scope" in params
        assert params["task_context_scope"].default is None
        assert "task_context_state_root" in params
        assert params["task_context_state_root"].default is None


class TestCLIFlags:
    def test_task_context_flags_registered_in_source(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        assert '"--task-context-scope"' in source
        assert '"--task-context-state-root"' in source
