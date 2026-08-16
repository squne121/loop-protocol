"""Issue #2219 negative/poison regression suite for the claude-gpt-focused
multi-turn / multi-SubAgent lifecycle / cleanup-independence evidence added
to ``run_worktree_agent_runtime_smoke.py``.

Most tests here exercise the new pure functions directly (hermetic,
sub-second) against hand-built stream-json fixtures reproducing each AC4
negative scenario. One test (``test_live_invocation_...``) is a genuine
end-to-end subprocess invocation of the runner script against a fake
``claude`` executable placed on ``PATH`` (mirroring the existing
``test_run_worktree_agent_runtime_smoke.py`` convention), exercising real
env injection / ``--settings`` hook wiring / stream-json boundary parsing,
per AC9.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "run_worktree_agent_runtime_smoke_operator_journey", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=env)


@pytest.fixture()
def repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "seed", cwd=repo)

    worktree = repo / ".claude" / "worktrees" / "issue-0000-fixture"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git("branch", "worktree-fixture", cwd=repo)
    _git("worktree", "add", str(worktree), "worktree-fixture", cwd=repo)
    return repo, worktree


def _write_fake_exe(path: Path, script_body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{script_body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _prompt_file(tmp_path: Path, text: str = "hello from test\n") -> Path:
    prompt = tmp_path / "prompt.md"
    prompt.write_text(text, encoding="utf-8")
    return prompt


def _run(
    repo: Path,
    worktree: Path,
    *args: str,
    fake_bin_dir: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if fake_bin_dir is not None:
        env["PATH"] = f"{fake_bin_dir}:{env['PATH']}"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(repo), "--worktree", str(worktree), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _sse(payload: dict) -> str:
    return json.dumps(payload)


def _hook_event(hook_event: str, agent_id: str, agent_type: str = "general-purpose") -> str:
    return _sse({
        "type": "system",
        "hook_event": hook_event,
        "hook_name": f"{hook_event}:{agent_type}",
        "stdout": json.dumps({"agent_id": agent_id, "agent_type": agent_type}),
    })


# ---------------------------------------------------------------------------
# AC4: multi-SubAgent lifecycle negative scenarios (agent_id exact pairing)
# ---------------------------------------------------------------------------


def test_given_start_only_subagent_when_multi_child_lifecycle_classified_then_not_verified():
    stdout = "\n".join([
        _hook_event("SubagentStart", "agent-a"),
        _hook_event("SubagentStart", "agent-b"),
    ])
    result = MODULE.classify_claude_multi_child_lifecycle(stdout, 2)
    assert result["verified"] is False
    assert result["orphan_starts"] == ["agent-a", "agent-b"]


def test_given_stop_only_subagent_when_multi_child_lifecycle_classified_then_not_verified():
    stdout = "\n".join([
        _hook_event("SubagentStart", "agent-a"),
        _hook_event("SubagentStop", "agent-a"),
        _hook_event("SubagentStop", "agent-b"),  # no matching start -> unknown child
    ])
    result = MODULE.classify_claude_multi_child_lifecycle(stdout, 2)
    assert result["verified"] is False
    assert result["unknown_children"] == ["agent-b"]


def test_given_agent_id_mismatch_when_multi_child_lifecycle_classified_then_not_verified():
    stdout = "\n".join([
        _hook_event("SubagentStart", "agent-a"),
        _hook_event("SubagentStop", "agent-a-typo"),
    ])
    result = MODULE.classify_claude_multi_child_lifecycle(stdout, 1)
    assert result["verified"] is False
    assert result["orphan_starts"] == ["agent-a"]
    assert result["unknown_children"] == ["agent-a-typo"]


def test_given_duplicate_completion_when_multi_child_lifecycle_classified_then_not_verified():
    stdout = "\n".join([
        _hook_event("SubagentStart", "agent-a"),
        _hook_event("SubagentStop", "agent-a"),
        _hook_event("SubagentStop", "agent-a"),
    ])
    result = MODULE.classify_claude_multi_child_lifecycle(stdout, 1)
    assert result["verified"] is False
    assert result["duplicate_completions"] == ["agent-a"]


def test_given_unknown_child_when_multi_child_lifecycle_classified_then_not_verified():
    stdout = "\n".join([
        _hook_event("SubagentStop", "agent-ghost"),
    ])
    result = MODULE.classify_claude_multi_child_lifecycle(stdout, 1)
    assert result["verified"] is False
    assert result["unknown_children"] == ["agent-ghost"]
    assert result["spawned_agent_ids"] == []


def test_given_two_genuinely_paired_subagents_when_multi_child_lifecycle_classified_then_verified():
    """Positive control: proves the negative tests above are actually
    exercising the pairing logic, not a function that never verifies."""
    stdout = "\n".join([
        _hook_event("SubagentStart", "agent-a"),
        _hook_event("SubagentStart", "agent-b"),
        _hook_event("SubagentStop", "agent-a"),
        _hook_event("SubagentStop", "agent-b"),
    ])
    result = MODULE.classify_claude_multi_child_lifecycle(stdout, 2)
    assert result["verified"] is True
    assert result["paired_agent_ids"] == ["agent-a", "agent-b"]


# ---------------------------------------------------------------------------
# AC5: marker-only PASS rejection (poison test #1)
# ---------------------------------------------------------------------------


def test_marker_present_no_spawn_event_fails():
    """A main-agent marker string alone (no SubagentStart/SubagentStop hook
    event, no tool_use_result agentId) must never satisfy multi-child
    lifecycle verification -- the model's own self-report is not
    authority."""
    stdout = _sse({"type": "result", "subtype": "success", "marker": "MARKER_TOKEN_WT"})
    result = MODULE.classify_claude_multi_child_lifecycle(stdout, 1)
    assert result["verified"] is False
    assert result["spawned_agent_ids"] == []
    assert result["completed_agent_ids"] == []


# ---------------------------------------------------------------------------
# AC2: same-main-session-across-turns negative scenarios
# ---------------------------------------------------------------------------


def test_given_session_identity_changes_across_turns_when_verified_then_not_verified():
    stdout = "\n".join([
        _sse({"type": "assistant", "session_id": "sess-1"}),
        _sse({"type": "assistant", "session_id": "sess-2"}),
    ])
    result = MODULE.verify_same_main_session_across_turns(stdout, 2)
    assert result["verified"] is False
    assert result["session_ids_observed"] == ["sess-1", "sess-2"]


def test_given_second_turn_not_executed_when_verified_then_not_verified():
    stdout = _sse({"type": "assistant", "session_id": "sess-1"})
    result = MODULE.verify_same_main_session_across_turns(stdout, 2)
    assert result["verified"] is False
    assert result["turn_count"] == 1


def test_given_min_turns_satisfied_same_session_when_verified_then_verified():
    stdout = "\n".join([
        _sse({"type": "assistant", "session_id": "sess-1"}),
        _sse({"type": "assistant", "session_id": "sess-1"}),
    ])
    result = MODULE.verify_same_main_session_across_turns(stdout, 2)
    assert result["verified"] is True
    assert result["turn_count"] == 2


# ---------------------------------------------------------------------------
# AC8: session-log metadata missing is SKIP, not PASS (existing
# --require-session-log-metadata flag; regression-pinned here per Issue
# #2219's explicit negative-test list).
# ---------------------------------------------------------------------------


def test_given_session_log_metadata_missing_when_required_then_skip_not_pass(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # No JSON stream line carries an allowlisted session-log-metadata key
    # (the runner's own _ALLOWLIST_SESSION_LOG_KEYS treats bare "type" as
    # allowlisted, so a plain non-JSON line is used here -- matching
    # test_given_require_session_log_metadata_and_unavailable_when_lane_runs_then_skip
    # in test_run_worktree_agent_runtime_smoke.py).
    _write_fake_exe(fake_bin / "claude", """
if [ "$1" = "--help" ]; then
  echo "--output-format --include-hook-events --no-session-persistence --max-turns"
  exit 0
fi
cat > /dev/null
echo 'not-json-output-at-all'
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = worktree / "artifacts" / "runtime-smoke" / "session-log-missing"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--timeout-seconds", "30", "--require-session-log-metadata",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 77
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# AC6: forbidden failure marker detection
# ---------------------------------------------------------------------------


def test_given_forbidden_marker_observed_when_scanned_then_not_verified():
    stdout = "some ordinary output\nPlease run /login to continue\n"
    result = MODULE.verify_no_forbidden_marker(stdout, "")
    assert result["verified"] is False
    assert result["matched_markers"] == ["Please run /login"]


def test_given_forbidden_marker_only_in_stderr_when_scanned_then_not_verified():
    result = MODULE.verify_no_forbidden_marker("", "403 WebSocket upgrade rejected by proxy")
    assert result["verified"] is False
    assert "403 WebSocket upgrade" in result["matched_markers"]


def test_given_no_forbidden_marker_when_scanned_then_verified():
    result = MODULE.verify_no_forbidden_marker("all clear", "also clear")
    assert result["verified"] is True
    assert result["matched_markers"] == []


# ---------------------------------------------------------------------------
# AC7: independent proxy cleanup re-confirmation (never trust self-report)
# ---------------------------------------------------------------------------


def test_given_proxy_pid_still_alive_when_cleanup_verified_independently_then_not_confirmed():
    """A live process (this test's own interpreter pid, guaranteed alive)
    must never be reported as cleaned up, regardless of what a launcher's
    own CLAUDE_GPT_PROXY_CLEANUP_OK self-report claims."""
    result = MODULE.verify_claude_gpt_proxy_cleanup_independent(
        os.getpid(), None, max_attempts=1, sleep_seconds=0.0
    )
    assert result["checked"] is True
    assert result["pid_alive"] is True
    assert result["cleanup_confirmed"] is False


def test_given_proxy_pid_and_port_both_none_when_cleanup_verified_independently_then_unchecked():
    result = MODULE.verify_claude_gpt_proxy_cleanup_independent(None, None)
    assert result["checked"] is False
    assert result["cleanup_confirmed"] is None


def test_given_launcher_stderr_sidechannel_when_parsed_then_fields_extracted():
    stderr = (
        "CLAUDE_GPT_PROXY_PORT=18080\n"
        "CLAUDE_GPT_PROXY_LOG=/tmp/proxy.log\n"
        "CLAUDE_GPT_PROXY_PID=99999999\n"
        "CLAUDE_GPT_PROXY_CLEANUP_OK=true\n"
        "CLAUDE_GPT_CLAUDE_EXIT_CODE=0\n"
    )
    result = MODULE.extract_claude_gpt_proxy_sidechannel(stderr)
    assert result["proxy_port"] == 18080
    assert result["proxy_pid"] == 99999999
    assert result["proxy_cleanup_ok_self_reported"] is True
    assert result["claude_exit_code_self_reported"] == 0


def test_given_launcher_self_reports_cleanup_ok_true_but_pid_still_alive_when_independent_recheck_runs_then_fails():
    """Poison test: the launcher's own self-report says cleanup succeeded,
    but this process's own pid (guaranteed alive) is passed as the
    'still-there' proxy pid -- independent re-check must override the
    self-report and fail closed."""
    sidechannel = MODULE.extract_claude_gpt_proxy_sidechannel(
        f"CLAUDE_GPT_PROXY_PID={os.getpid()}\nCLAUDE_GPT_PROXY_CLEANUP_OK=true\n"
    )
    assert sidechannel["proxy_cleanup_ok_self_reported"] is True
    result = MODULE.verify_claude_gpt_proxy_cleanup_independent(
        sidechannel["proxy_pid"], sidechannel["proxy_port"], max_attempts=1, sleep_seconds=0.0
    )
    assert result["cleanup_confirmed"] is False


# ---------------------------------------------------------------------------
# AC8: SKIP (exit 77) must never be promoted to PASS (exit 0)
# ---------------------------------------------------------------------------


def test_given_no_claude_binary_when_run_then_skip_exit77_not_promoted_to_pass(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    out_dir = worktree / "artifacts" / "runtime-smoke" / "no-claude-binary"
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        # Keep system dirs (git) reachable while excluding any real
        # claude/codex binary that may live under ~/.local/bin on the host
        # running this test suite (mirrors
        # test_given_no_claude_binary_when_preflight_runs_then_skip77 in
        # test_run_worktree_agent_runtime_smoke.py).
        extra_env={"PATH": f"{empty_bin}:/usr/bin:/bin"},
    )
    assert result.returncode == 77
    assert result.returncode != 0
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "exit_code: 77" in summary
    assert "exit_code: 0" not in summary


# ---------------------------------------------------------------------------
# AC10: stale-head evidence rejection
# ---------------------------------------------------------------------------


def test_given_stale_head_when_evidence_reuse_checked_then_stale_head_evidence_rejected():
    result = MODULE.verify_evidence_not_stale(
        "old-head-sha", {"a": "1"}, "new-head-sha", {"a": "1"}
    )
    assert result["stale"] is True
    assert result["reason"] == "tested_head_mismatch"


def test_given_fresh_fingerprint_mismatch_when_evidence_reuse_checked_then_stale_head_evidence_rejected():
    result = MODULE.verify_evidence_not_stale(
        "same-head", {"a": "1"}, "same-head", {"a": "2"}
    )
    assert result["stale"] is True
    assert result["reason"] == "repo_fingerprint_mismatch"


def test_given_matching_head_and_fingerprint_when_evidence_reuse_checked_then_not_stale():
    result = MODULE.verify_evidence_not_stale(
        "same-head", {"a": "1"}, "same-head", {"a": "1"}
    )
    assert result["stale"] is False


# ---------------------------------------------------------------------------
# AC11: new CLI flags are opt-in and validated
# ---------------------------------------------------------------------------


def test_given_require_min_turns_exceeds_max_turns_when_parsed_then_argument_error(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    out_dir = worktree / "artifacts" / "runtime-smoke" / "require-min-turns-invalid"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--max-turns", "1", "--require-min-turns", "2",
    )
    assert result.returncode == 2
    assert "--require-min-turns" in result.stderr


def test_given_require_min_subagents_with_codex_runtime_when_parsed_then_argument_error(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    out_dir = worktree / "artifacts" / "runtime-smoke" / "require-min-subagents-codex"
    result = _run(
        repo, worktree,
        "--runtime", "codex", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--require-min-subagents", "1",
    )
    assert result.returncode == 2
    assert "--require-min-subagents" in result.stderr


# ---------------------------------------------------------------------------
# AC9: live invocation (fake claude on PATH), exercising real env injection
# / --settings hook wiring / stream-json boundary parsing end-to-end.
# ---------------------------------------------------------------------------


_LIVE_HELP_BRANCH = """
if [ "$1" = "--help" ]; then
  echo "--output-format --include-hook-events --no-session-persistence --max-turns --settings"
  exit 0
fi
"""


def test_live_invocation_fake_claude_multi_subagent_and_multi_turn_session_pass(repo_with_worktree, tmp_path):
    """Live invocation (real subprocess of the runner script + a fake
    ``claude`` executable on PATH, exactly like the pre-existing
    structured-lane tests in test_run_worktree_agent_runtime_smoke.py) that
    genuinely exercises: --settings hook-observability wiring (the fixed
    SubagentStart/SubagentStop --settings JSON this runner always appends
    for the native adapter), a real 'fake hook' command (``cat``, the same
    literal command production's own
    _CLAUDE_SPAWN_HOOK_OBSERVABILITY_SETTINGS_JSON configures) piped a
    real agent_id/agent_type payload through it via a shell pipe, and
    stream-json boundary parsing of TWO SubagentStart/SubagentStop pairs
    plus TWO assistant turns sharing one session_id -- proving AC2/AC3/AC9
    together in one live pass."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    hook1 = json.dumps({"agent_id": "live-agent-1", "agent_type": "general-purpose"})
    hook2 = json.dumps({"agent_id": "live-agent-2", "agent_type": "general-purpose"})
    # emit_hook <event> <payload-json>: pipes <payload-json> through the
    # SAME literal 'cat' hook command production's own
    # _CLAUDE_SPAWN_HOOK_OBSERVABILITY_SETTINGS_JSON configures (a real
    # 'fake hook' invocation, not a canned literal), then JSON-encodes the
    # roundtripped text into a valid stream-json system/hook event line.
    helper = """
emit_hook() {
  local event="$1"
  local payload="$2"
  local piped
  piped=$(printf '%s' "$payload" | cat)
  local encoded
  encoded=$(printf '%s' "$piped" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
  printf '{"type":"system","session_id":"live-sess-1",'
  printf '"hook_event":"%s","hook_name":"%s:general-purpose",' "$event" "$event"
  printf '"output":%s}\\n' "$encoded"
}
"""
    script = _LIVE_HELP_BRANCH + helper + f"""
cat > /dev/null
echo '{{"type":"system","subtype":"init","session_id":"live-sess-1"}}'
echo '{{"type":"assistant","session_id":"live-sess-1"}}'
emit_hook SubagentStart '{hook1}'
emit_hook SubagentStart '{hook2}'
echo '{{"type":"assistant","session_id":"live-sess-1"}}'
emit_hook SubagentStop '{hook1}'
emit_hook SubagentStop '{hook2}'
echo '{{"type":"result","subtype":"success"}}'
exit 0
"""
    _write_fake_exe(fake_bin / "claude", script)
    prompt = _prompt_file(tmp_path)
    out_dir = worktree / "artifacts" / "runtime-smoke" / "live-multi-subagent"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--timeout-seconds", "30",
        "--require-min-subagents", "2",
        "--require-min-turns", "2",
        "--max-turns", "2",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "multi_child_lifecycle" in summary
    assert "same_session_across_turns" in summary
