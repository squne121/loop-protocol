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


# ---------------------------------------------------------------------------
# Issue #2219 fix_delta iteration 1 (Option B reintroduction): interactive
# herdr lane multi-turn support (--additional-prompt), wired into the SAME
# classify_claude_multi_child_lifecycle() / verify_same_main_session_across_
# turns() / verify_no_forbidden_marker() functions Option A already built
# for the structured lane, via the persisted session transcript this lane's
# own Claude Code process writes to ~/.claude/projects/*/<session_id>.jsonl
# (see _find_claude_interactive_transcript). These tests fake the herdr CLI
# (mirroring the existing test_run_worktree_agent_runtime_smoke.py /
# test_run_worktree_agent_runtime_smoke_herdr_isolation.py convention -- no
# live herdr session is required) and, where the persisted-transcript wiring
# is exercised, pre-seed a fake ~/.claude/projects transcript file under an
# isolated HOME so the module's own Path.home() lookup never touches the
# real machine's Claude Code project directory.
# ---------------------------------------------------------------------------


_HELP_BRANCH = """
if [ "$1" = "--help" ]; then
  echo "--output-format --include-hook-events --no-session-persistence --max-turns"
  exit 0
fi
"""


_FAKE_ISOLATED_HERDR_BODY_MULTI_TURN = """
STATE_DIR="$FAKE_HERDR_STATE_DIR"
mkdir -p "$STATE_DIR"
if [ "$1" = "--session" ]; then
  touch "$STATE_DIR/$2.session"
  sleep 300
  exit 0
fi
case "$1 $2" in
  "status server")
    exit 0
    ;;
esac
case "$1" in
  session)
    case "$2" in
      list)
        out="{\\"sessions\\":["
        first=1
        for f in "$STATE_DIR"/*.session; do
          [ -e "$f" ] || continue
          name=$(basename "$f" .session)
          if [ -e "$STATE_DIR/$name.stopped" ]; then running=false; else running=true; fi
          if [ $first -eq 0 ]; then out="$out,"; fi
          out="$out{\\"name\\":\\"$name\\",\\"running\\":$running}"
          first=0
        done
        out="$out]}"
        echo "$out"
        exit 0
        ;;
      stop)
        touch "$STATE_DIR/$3.stopped"
        exit 0
        ;;
      delete)
        rm -f "$STATE_DIR/$3.session" "$STATE_DIR/$3.stopped"
        exit 0
        ;;
    esac
    ;;
  workspace)
    case "$2" in
      create)
        touch "$STATE_DIR/${HERDR_SESSION}.session"
        if [ "$FAKE_HERDR_EMIT_HOOK_SINK" = "1" ] && [ -n "$CLAUDE_GPT_HOOK_SINK_PATH" ]; then
          N="$CLAUDE_GPT_HOOK_SINK_NONCE"
          S="sink-sess"
          {
            printf '{"run_nonce":"%s","event":"UserPromptSubmit","session_id":"%s","ts":1}\n' "$N" "$S"
            printf '{"run_nonce":"%s","event":"Stop","session_id":"%s","ts":2}\n' "$N" "$S"
            printf '{"run_nonce":"%s","event":"UserPromptSubmit","session_id":"%s","ts":3}\n' "$N" "$S"
            printf '{"run_nonce":"%s","event":"Stop","session_id":"%s","ts":4}\n' "$N" "$S"
            printf '{"run_nonce":"%s","event":"SubagentStart","session_id":"%s","agent_id":"a","ts":5}\n' "$N" "$S"
            printf '{"run_nonce":"%s","event":"SubagentStart","session_id":"%s","agent_id":"b","ts":6}\n' "$N" "$S"
            printf '{"run_nonce":"%s","event":"SubagentStop","session_id":"%s","agent_id":"a","ts":7}\n' "$N" "$S"
            printf '{"run_nonce":"%s","event":"SubagentStop","session_id":"%s","agent_id":"b","ts":8}\n' "$N" "$S"
          } >> "$CLAUDE_GPT_HOOK_SINK_PATH"
        fi
        echo '{"result":{"root_pane":{"pane_id":"pane-xyz"},"workspace":{"workspace_id":"w1"}}}'
        exit 0
        ;;
    esac
    ;;
  agent)
    case "$2" in
      start) exit 0 ;;
      prompt)
        count_file="$STATE_DIR/prompt_call_count"
        n=0
        [ -f "$count_file" ] && n=$(cat "$count_file")
        n=$((n + 1))
        echo "$n" > "$count_file"
        if [ -n "$FAIL_PROMPT_CALL_NUMBER" ] && [ "$n" = "$FAIL_PROMPT_CALL_NUMBER" ]; then
          echo "fake prompt failure (not a stall)" >&2
          exit 1
        fi
        exit 0
        ;;
      get) echo '{"state":"idle"}'; exit 0 ;;
      explain) echo '{"agent":"claude","confidence":"high"}'; exit 0 ;;
      read) echo "OBSERVED_MARKER pane transcript line"; exit 0 ;;
    esac
    ;;
  api)
    case "$2" in
      snapshot)
        echo '{"result":{"snapshot":{"agents":[],"focused_workspace_id":"w0",'\
'"focused_tab_id":"w0:t0","focused_pane_id":"w0:p0"}}}'
        exit 0
        ;;
    esac
    ;;
esac
exit 0
"""


# Issue #2219 fix_delta iteration 2: same as _FAKE_ISOLATED_HERDR_BODY_MULTI_TURN
# above, but its "agent start" case actually resolves and invokes "claude"
# via PATH (mirroring _FORWARDER_CAUSAL_PROOF_HERDR_BODY in
# test_run_worktree_agent_runtime_smoke_claude_bin.py) -- required so the
# --claude-bin launcher-receipt causal check passes when a test needs
# --claude-adapter claude-gpt (which itself requires --claude-bin).
_FAKE_ISOLATED_HERDR_BODY_MULTI_TURN_WITH_CLAUDE_BIN_RECEIPT = """
STATE_DIR="$FAKE_HERDR_STATE_DIR"
mkdir -p "$STATE_DIR"
if [ "$1" = "--session" ]; then
  touch "$STATE_DIR/$2.session"
  sleep 300
  exit 0
fi
case "$1 $2" in
  "status server")
    exit 0
    ;;
esac
case "$1" in
  session)
    case "$2" in
      list)
        out="{\\"sessions\\":["
        first=1
        for f in "$STATE_DIR"/*.session; do
          [ -e "$f" ] || continue
          name=$(basename "$f" .session)
          if [ -e "$STATE_DIR/$name.stopped" ]; then running=false; else running=true; fi
          if [ $first -eq 0 ]; then out="$out,"; fi
          out="$out{\\"name\\":\\"$name\\",\\"running\\":$running}"
          first=0
        done
        out="$out]}"
        echo "$out"
        exit 0
        ;;
      stop) touch "$STATE_DIR/$3.stopped"; exit 0 ;;
      delete) rm -f "$STATE_DIR/$3.session" "$STATE_DIR/$3.stopped"; exit 0 ;;
    esac
    ;;
  workspace)
    case "$2" in
      create)
        touch "$STATE_DIR/${HERDR_SESSION}.session"
        if [ "$FAKE_HERDR_EMIT_HOOK_SINK" = "1" ] && [ -n "$CLAUDE_GPT_HOOK_SINK_PATH" ]; then
          N="$CLAUDE_GPT_HOOK_SINK_NONCE"
          S="sink-sess"
          {
            printf '{"run_nonce":"%s","event":"UserPromptSubmit","session_id":"%s","ts":1}\n' "$N" "$S"
            printf '{"run_nonce":"%s","event":"Stop","session_id":"%s","ts":2}\n' "$N" "$S"
            printf '{"run_nonce":"%s","event":"UserPromptSubmit","session_id":"%s","ts":3}\n' "$N" "$S"
            printf '{"run_nonce":"%s","event":"Stop","session_id":"%s","ts":4}\n' "$N" "$S"
            printf '{"run_nonce":"%s","event":"SubagentStart","session_id":"%s","agent_id":"a","ts":5}\n' "$N" "$S"
            printf '{"run_nonce":"%s","event":"SubagentStart","session_id":"%s","agent_id":"b","ts":6}\n' "$N" "$S"
            printf '{"run_nonce":"%s","event":"SubagentStop","session_id":"%s","agent_id":"a","ts":7}\n' "$N" "$S"
            printf '{"run_nonce":"%s","event":"SubagentStop","session_id":"%s","agent_id":"b","ts":8}\n' "$N" "$S"
          } >> "$CLAUDE_GPT_HOOK_SINK_PATH"
        fi
        echo '{"result":{"root_pane":{"pane_id":"pane-xyz"},"workspace":{"workspace_id":"w1"}}}'
        exit 0
        ;;
    esac
    ;;
  pane)
    exit 0
    ;;
  agent)
    case "$2" in
      start)
        resolved="$(command -v claude || true)"
        if [ -n "$resolved" ]; then
          "$resolved" launched-by-fake-herdr-pty > /dev/null 2>&1 || true
        fi
        exit 0
        ;;
      prompt)
        count_file="$STATE_DIR/prompt_call_count"
        n=0
        [ -f "$count_file" ] && n=$(cat "$count_file")
        n=$((n + 1))
        echo "$n" > "$count_file"
        if [ -n "$FAIL_PROMPT_CALL_NUMBER" ] && [ "$n" = "$FAIL_PROMPT_CALL_NUMBER" ]; then
          echo "fake prompt failure (not a stall)" >&2
          exit 1
        fi
        exit 0
        ;;
      get) echo '{"state":"idle"}'; exit 0 ;;
      explain) echo '{"agent":"claude","confidence":"high"}'; exit 0 ;;
      read) echo "OBSERVED_MARKER pane transcript line"; exit 0 ;;
    esac
    ;;
  api)
    case "$2" in
      snapshot)
        echo '{"result":{"snapshot":{"agents":[],"focused_workspace_id":"w0",'\\
'"focused_tab_id":"w0:t0","focused_pane_id":"w0:p0"}}}'
        exit 0
        ;;
    esac
    ;;
esac
exit 0
"""


def _seed_fake_claude_transcript(
    fake_home: Path, worktree: Path, lines: list[dict], claude_adapter: str = "native"
) -> Path:
    """Write a fake persisted Claude Code session transcript reproducing the
    REAL on-disk shape (Issue #2219 fix_delta iteration 2 live finding,
    https://github.com/squne121/loop-protocol/pull/2222#issuecomment-5307351011):
    a genuine transcript's leading line(s) are session-bookkeeping records
    (``{"type": "mode", ...}``) with NO ``cwd`` field at all -- ``cwd`` only
    appears on the first actual message record, several lines in -- so the
    ``cwd``-carrying record is deliberately placed as the SECOND line here
    (never the first), matching what ``_find_claude_interactive_transcript``'s
    ``_TRANSCRIPT_CWD_SCAN_LINES``-line scan window must actually handle.

    ``claude_adapter="native"`` writes under
    ``<fake_home>/.claude/projects/<any-slug>/<any-name>.jsonl`` (the
    default adapter's projects root). ``claude_adapter="claude-gpt"`` writes
    under ``<fake_home>/.claude-gpt/claude/projects/<any-slug>/<any-name>.jsonl``
    instead, mirroring ``scripts/claude-gpt/lib.sh``'s own
    ``CLAUDE_GPT_HOME``-default (``$HOME/.claude-gpt``) resolution exactly."""
    if claude_adapter == "claude-gpt":
        projects_root = fake_home / ".claude-gpt" / "claude" / "projects"
    else:
        projects_root = fake_home / ".claude" / "projects"
    project_dir = projects_root / "fixture-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = project_dir / "fake-session.jsonl"
    resolved_worktree = str(worktree.resolve())
    bookkeeping_line = {"type": "mode", "mode": "normal"}
    body_lines = (
        [bookkeeping_line, dict(lines[0], cwd=resolved_worktree)] + [dict(line) for line in lines[1:]]
    )
    transcript_path.write_text(
        "\n".join(json.dumps(line) for line in body_lines) + "\n", encoding="utf-8"
    )
    return transcript_path


def test_given_additional_prompt_when_interactive_lane_runs_then_turns_completed_records_all_turns(
    repo_with_worktree, tmp_path
):
    """Positive control: --additional-prompt drives a second turn through
    the SAME already-started herdr agent/session (turns_completed counts
    both), with no --require-min-* flags so the persisted-transcript
    wiring is not exercised in this test."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY_MULTI_TURN)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"

    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--expect-marker", "OBSERVED_MARKER",
        "--additional-prompt", "second turn: confirm same session",
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "turns_completed: 2" in summary


def test_given_second_additional_prompt_turn_never_settles_when_interactive_lane_runs_then_fail(
    repo_with_worktree, tmp_path
):
    """AC6-spirit negative: the second (--additional-prompt) turn's own
    ``herdr agent prompt`` call fails (not a stall -- a genuine failure),
    so it must never silently be treated as settled. turns_completed must
    stay at 1 (only the initial turn settled) and the run must FAIL."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY_MULTI_TURN)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"

    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--additional-prompt", "second turn never settles",
        fake_bin_dir=fake_bin,
        extra_env={
            "HERDR_ENV": "1",
            "FAKE_HERDR_STATE_DIR": str(state_dir),
            "FAIL_PROMPT_CALL_NUMBER": "2",
        },
    )
    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "turns_completed: 1" in summary
    assert "herdr agent prompt failed" in summary


def test_given_additional_prompt_when_argument_error_outside_interactive_mode(
    repo_with_worktree, tmp_path
):
    """--additional-prompt requires --mode interactive."""
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    out_dir = worktree / "artifacts" / "runtime-smoke" / "additional-prompt-structured"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--additional-prompt", "not allowed here",
    )
    assert result.returncode == 2
    assert "--additional-prompt" in result.stderr


def test_given_require_min_turns_without_enough_additional_prompts_when_interactive_then_argument_error(
    repo_with_worktree, tmp_path
):
    """--require-min-turns 2 in interactive mode requires at least 1
    --additional-prompt entry (1 initial turn + N additional >= 2)."""
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    out_dir = worktree / "artifacts" / "runtime-smoke" / "require-min-turns-interactive-invalid"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--require-min-turns", "2",
    )
    assert result.returncode == 2
    assert "--require-min-turns" in result.stderr


def test_given_interactive_multi_turn_with_matching_session_identity_when_transcript_wired_then_pass(
    repo_with_worktree, tmp_path
):
    """Positive control: a genuine 2-turn interactive journey whose
    persisted transcript carries ONE session_id across both turns plus two
    genuinely paired SubAgents -- classify_claude_multi_child_lifecycle()
    and verify_same_main_session_across_turns() (the SAME Option A
    functions) both PASS when wired through the interactive lane's own
    persisted-transcript lookup."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY_MULTI_TURN)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    _seed_fake_claude_transcript(
        fake_home, worktree,
        [
            {"type": "system", "subtype": "init", "session_id": "same-sess-1"},
            {"type": "assistant", "session_id": "same-sess-1"},
            {"type": "user", "tool_use_result": {"agentId": "agent-a", "status": "completed"}},
            {"type": "user", "tool_use_result": {"agentId": "agent-b", "status": "completed"}},
            {"type": "assistant", "session_id": "same-sess-1"},
        ],
    )
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"

    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--additional-prompt", "second turn",
        "--require-min-turns", "2",
        "--require-min-subagents", "2",
        "--scan-forbidden-markers",
        fake_bin_dir=fake_bin,
        extra_env={
            "HERDR_ENV": "1",
            "FAKE_HERDR_STATE_DIR": str(state_dir),
            "HOME": str(fake_home),
            "FAKE_HERDR_EMIT_HOOK_SINK": "1",
        },
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    # Issue #2219 (OWNER anchor decision): PASS authority is the hook-event
    # evidence channel, not transcript existence -- the transcript is
    # advisory only, so it MAY still be found here (fixture seeds one), but
    # the multi_child_lifecycle/same_session_across_turns verdicts below are
    # what actually gate the run.
    assert "multi_child_lifecycle_source: hook_event_sink" in summary
    assert "same_session_across_turns_source: hook_event_sink" in summary
    assert "'verified': True" in summary


def test_given_interactive_multi_turn_with_session_identity_change_when_transcript_wired_then_fail(
    repo_with_worktree, tmp_path
):
    """Negative: the persisted transcript's session_id CHANGES between the
    two turns (a genuine same-session-identity violation) -- must FAIL,
    never silently accepted as PASS."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY_MULTI_TURN)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    _seed_fake_claude_transcript(
        fake_home, worktree,
        [
            {"type": "system", "subtype": "init", "session_id": "sess-turn-1"},
            {"type": "assistant", "session_id": "sess-turn-1"},
            {"type": "assistant", "session_id": "sess-turn-2-DIFFERENT"},
        ],
    )
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"

    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--additional-prompt", "second turn",
        "--require-min-turns", "2",
        fake_bin_dir=fake_bin,
        extra_env={
            "HERDR_ENV": "1",
            "FAKE_HERDR_STATE_DIR": str(state_dir),
            "HOME": str(fake_home),
        },
    )
    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "same_session_across_turns" in summary
    assert "'verified': False" in summary


def test_given_interactive_lane_no_transcript_found_when_require_min_turns_then_fail_closed(
    repo_with_worktree, tmp_path
):
    """No persisted transcript exists at all (fake HOME has no
    ~/.claude/projects tree): interactive_transcript_found must be False
    and the run must FAIL closed, never silently PASS with no evidence."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY_MULTI_TURN)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    fake_home = tmp_path / "fake-home-empty"
    fake_home.mkdir()
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"

    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--additional-prompt", "second turn",
        "--require-min-turns", "2",
        fake_bin_dir=fake_bin,
        extra_env={
            "HERDR_ENV": "1",
            "FAKE_HERDR_STATE_DIR": str(state_dir),
            "HOME": str(fake_home),
        },
    )
    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "interactive_transcript_found: False" in summary


# ---------------------------------------------------------------------------
# Issue #2219 fix_delta iteration 2: live-verification-discovered claude-gpt
# adapter transcript root bug (PR #2222
# https://github.com/squne121/loop-protocol/pull/2222#issuecomment-5307351011)
# ---------------------------------------------------------------------------


def test_given_claude_gpt_adapter_when_resolving_projects_root_then_uses_isolated_config_dir():
    """Pure-function unit test (Issue #2219 fix_delta iteration 2): the
    claude-gpt adapter's projects root is $CLAUDE_GPT_HOME/claude/projects
    (default $HOME/.claude-gpt/claude/projects when CLAUDE_GPT_HOME is
    unset), never the native ~/.claude/projects."""
    original = os.environ.pop("CLAUDE_GPT_HOME", None)
    try:
        native_root = MODULE._resolve_claude_projects_root("native")
        gpt_root = MODULE._resolve_claude_projects_root("claude-gpt")
        assert native_root == Path.home() / ".claude" / "projects"
        assert gpt_root == Path.home() / ".claude-gpt" / "claude" / "projects"
        assert native_root != gpt_root
    finally:
        if original is not None:
            os.environ["CLAUDE_GPT_HOME"] = original


def test_given_claude_gpt_home_override_when_resolving_projects_root_then_honored():
    """A caller-set CLAUDE_GPT_HOME (matching scripts/claude-gpt/lib.sh's own
    override mechanism) must be honored identically, not silently ignored
    in favor of a re-derived default."""
    original = os.environ.get("CLAUDE_GPT_HOME")
    os.environ["CLAUDE_GPT_HOME"] = "/tmp/custom-claude-gpt-home"
    try:
        gpt_root = MODULE._resolve_claude_projects_root("claude-gpt")
        assert gpt_root == Path("/tmp/custom-claude-gpt-home") / "claude" / "projects"
    finally:
        if original is None:
            os.environ.pop("CLAUDE_GPT_HOME", None)
        else:
            os.environ["CLAUDE_GPT_HOME"] = original


def test_given_claude_gpt_adapter_interactive_transcript_under_isolated_config_dir_when_wired_then_pass(
    repo_with_worktree, tmp_path
):
    """Genuine live/synthetic-gap regression (Issue #2219 fix_delta
    iteration 2): a claude-gpt interactive session's persisted transcript
    lives under $HOME/.claude-gpt/claude/projects, NOT $HOME/.claude/projects.
    Before this fix, --claude-adapter claude-gpt always produced
    interactive_transcript_found: False (a false FAIL) because the scan
    hardcoded the native root. This reproduces the fake claude-gpt-shaped
    session directory observed via live filesystem inspection during the
    PR #2222 live run and proves the fix genuinely finds and correctly
    classifies multi-turn/multi-subagent evidence for THIS adapter shape."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY_MULTI_TURN_WITH_CLAUDE_BIN_RECEIPT)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    _seed_fake_claude_transcript(
        fake_home, worktree,
        [
            {"type": "system", "subtype": "init", "session_id": "gpt-sess-1"},
            {"type": "assistant", "session_id": "gpt-sess-1"},
            {"type": "user", "tool_use_result": {"agentId": "agent-a", "status": "completed"}},
            {"type": "user", "tool_use_result": {"agentId": "agent-b", "status": "completed"}},
            {"type": "assistant", "session_id": "gpt-sess-1"},
        ],
        claude_adapter="claude-gpt",
    )
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"

    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--claude-bin", str(fake_bin / "claude"), "--claude-adapter", "claude-gpt",
        "--additional-prompt", "second turn",
        "--require-min-turns", "2",
        "--require-min-subagents", "2",
        "--scan-forbidden-markers",
        fake_bin_dir=fake_bin,
        extra_env={
            "HERDR_ENV": "1",
            "FAKE_HERDR_STATE_DIR": str(state_dir),
            "HOME": str(fake_home),
            "FAKE_HERDR_EMIT_HOOK_SINK": "1",
        },
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    # Issue #2219 (OWNER anchor decision): PASS authority is the hook-event
    # evidence channel, not transcript existence -- the transcript is
    # advisory only, so it MAY still be found here (fixture seeds one), but
    # the multi_child_lifecycle/same_session_across_turns verdicts below are
    # what actually gate the run.
    assert "multi_child_lifecycle_source: hook_event_sink" in summary
    assert "same_session_across_turns_source: hook_event_sink" in summary
    assert "'verified': True" in summary


def test_given_claude_gpt_transcript_only_under_native_root_when_adapter_is_claude_gpt_then_not_found(
    repo_with_worktree, tmp_path
):
    """Negative control proving root selection is genuinely adapter-aware
    (not a change that makes every root match): a transcript seeded ONLY
    under the NATIVE root while --claude-adapter claude-gpt is requested
    must NOT be found -- the isolated adapter must never accidentally read
    another adapter's (or another isolation boundary's) session data."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY_MULTI_TURN_WITH_CLAUDE_BIN_RECEIPT)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    _seed_fake_claude_transcript(
        fake_home, worktree,
        [{"type": "system", "subtype": "init", "session_id": "native-only-sess"}],
        claude_adapter="native",
    )
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"

    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--claude-bin", str(fake_bin / "claude"), "--claude-adapter", "claude-gpt",
        "--additional-prompt", "second turn",
        "--require-min-turns", "2",
        fake_bin_dir=fake_bin,
        extra_env={
            "HERDR_ENV": "1",
            "FAKE_HERDR_STATE_DIR": str(state_dir),
            "HOME": str(fake_home),
        },
    )
    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "interactive_transcript_found: False" in summary


# ---------------------------------------------------------------------------
# Issue #2219 fix_delta iteration 2: async task-notification completion
# channel (live verification finding -- an async-launched spawn's own
# tool_use_result never transitions to "completed" in place; the real
# completion signal is a separate <task-notification> block).
# ---------------------------------------------------------------------------


def test_given_task_notification_completed_block_when_extracted_then_agent_id_returned():
    text = (
        '{"type":"queue-operation","operation":"enqueue","content":'
        '"<task-notification>\\n<task-id>a2be4b1bac93c3190</task-id>\\n'
        '<status>completed</status>\\n</task-notification>"}'
    )
    completed = MODULE.extract_claude_task_notification_completions(text)
    assert completed == {"a2be4b1bac93c3190"}


def test_given_task_notification_non_completed_status_when_extracted_then_not_returned():
    text = (
        '<task-notification><task-id>agent-x</task-id><status>running</status></task-notification>'
    )
    completed = MODULE.extract_claude_task_notification_completions(text)
    assert completed == set()


def test_given_async_launched_spawn_with_task_notification_completion_when_classified_then_verified():
    """The exact live-observed shape (Issue #2219 fix_delta iteration 2):
    spawn's tool_use_result reports status "async_launched" (never
    "completed" in place); the real completion signal arrives later as a
    separate queue-operation record embedding a <task-notification> block.
    classify_claude_multi_child_lifecycle must bind that notification's
    agent_id to the already-observed spawn and verify successfully."""
    stdout = "\n".join([
        _sse({
            "type": "user",
            "tool_use_result": {"agentId": "agent-a", "status": "async_launched"},
        }),
        _sse({
            "type": "user",
            "tool_use_result": {"agentId": "agent-b", "status": "async_launched"},
        }),
        _sse({
            "type": "queue-operation",
            "operation": "enqueue",
            "content": (
                "<task-notification><task-id>agent-a</task-id>"
                "<status>completed</status></task-notification>"
            ),
        }),
        _sse({
            "type": "queue-operation",
            "operation": "enqueue",
            "content": (
                "<task-notification><task-id>agent-b</task-id>"
                "<status>completed</status></task-notification>"
            ),
        }),
    ])
    result = MODULE.classify_claude_multi_child_lifecycle(stdout, 2)
    assert result["verified"] is True, result
    assert result["paired_agent_ids"] == ["agent-a", "agent-b"]
    assert result["orphan_starts"] == []


def test_given_async_launched_spawn_with_no_task_notification_when_classified_then_not_verified():
    """Negative control: an async-launched spawn with NO completion
    notification anywhere must remain an orphan start -- never silently
    treated as complete just because it was launched."""
    stdout = _sse({
        "type": "user",
        "tool_use_result": {"agentId": "agent-a", "status": "async_launched"},
    })
    result = MODULE.classify_claude_multi_child_lifecycle(stdout, 1)
    assert result["verified"] is False
    assert result["orphan_starts"] == ["agent-a"]


def test_given_task_notification_completion_with_no_matching_spawn_when_classified_then_unknown_child():
    """A task-notification completion for an agent_id that was NEVER
    observed as a spawn must be treated as an unknown child, never silently
    manufacture a spawn event out of a completion notification alone."""
    stdout = _sse({
        "type": "queue-operation",
        "operation": "enqueue",
        "content": (
            "<task-notification><task-id>ghost-agent</task-id>"
            "<status>completed</status></task-notification>"
        ),
    })
    result = MODULE.classify_claude_multi_child_lifecycle(stdout, 1)
    assert result["verified"] is False
    assert result["spawned_agent_ids"] == []


# ---------------------------------------------------------------------------
# Issue #2219 AC13-AC17 (OWNER anchor decision, hook-event evidence
# channel): the interactive lane's hook sink JSONL parsing / classification
# / staleness / concurrency / orphan-on-crash tests.
# ---------------------------------------------------------------------------


def _sink_line(**kwargs) -> str:
    record = {
        "run_nonce": "nonce-a",
        "event": "SubagentStart",
        "session_id": "s1",
        "agent_id": None,
        "ts": 1.0,
        "prompt_digest": None,
    }
    record.update(kwargs)
    return json.dumps(record)


def _json_record(**kwargs) -> dict:
    record = {"run_nonce": "nonce-a", "event": "SubagentStart", "session_id": "s1", "agent_id": "a"}
    record.update(kwargs)
    return record


def test_given_hook_sink_records_when_scanned_for_no_raw_prompt_then_none_found():
    """AC13: a well-formed sink record never carries raw prompt/response
    text, credentials, or tokens -- only a salted sha256 prompt_digest."""
    digest = "a" * 64
    records = [
        {"run_nonce": "n", "event": "UserPromptSubmit", "session_id": "s1", "prompt_digest": digest},
        {"run_nonce": "n", "event": "Stop", "session_id": "s1", "prompt_digest": None},
    ]
    result = MODULE.verify_claude_gpt_hook_sink_no_raw_content(records)
    assert result["verified"] is True
    assert result["violating_events"] == []


def test_given_hook_sink_record_with_non_digest_prompt_field_when_scanned_then_flagged():
    """AC13 poison case: a ``prompt_digest`` value that is not a genuine
    64-hex-char sha256 digest (e.g. raw text leaked through) must fail
    closed."""
    records = [
        {"run_nonce": "n", "event": "UserPromptSubmit", "session_id": "s1", "prompt_digest": "hello world"},
    ]
    result = MODULE.verify_claude_gpt_hook_sink_no_raw_content(records)
    assert result["verified"] is False
    assert "UserPromptSubmit" in result["violating_events"]


def test_given_hook_sink_path_when_derived_then_built_only_from_launcher_owned_constant(monkeypatch):
    """AC14: the claude-gpt sink path is built ONLY from
    ``claude_gpt_proxy_state_dir_python()`` (mirrors ``lib.sh``'s
    ``claude_gpt_proxy_state_dir()``) and the nonce -- never from a
    caller-supplied worktree path or CLI argument. Setting a caller-
    controlled env var that has NOTHING to do with the launcher-owned
    ``CLAUDE_GPT_HOME`` constant must have zero effect on the resolved
    path."""
    monkeypatch.delenv("CLAUDE_GPT_HOME", raising=False)
    home_default = MODULE.claude_gpt_proxy_state_dir_python()
    path_a = MODULE.claude_gpt_hook_sink_path("nonce-x")
    assert str(path_a).startswith(str(home_default))
    assert "nonce-x" in path_a.name
    # A caller-supplied value that is NOT the launcher-owned CLAUDE_GPT_HOME
    # constant (e.g. a worktree path) must never influence the sink path.
    monkeypatch.setenv("SOME_CALLER_SUPPLIED_WORKTREE_PATH", "/tmp/attacker-controlled")
    path_b = MODULE.claude_gpt_hook_sink_path("nonce-x")
    assert path_a == path_b


def test_given_hook_sink_concurrent_write_when_parsed_then_not_corrupted(tmp_path):
    """AC15: two records appended back-to-back (simulating near-
    simultaneous SubagentStart events from concurrent SubAgents) must both
    be parsed intact -- no truncated/interleaved JSON line."""
    sink_path = tmp_path / "hook-sink-nonce-a.jsonl"
    with sink_path.open("a", encoding="utf-8") as fh:
        fh.write(_sink_line(event="SubagentStart", agent_id="agent-a") + "\n")
    with sink_path.open("a", encoding="utf-8") as fh:
        fh.write(_sink_line(event="SubagentStart", agent_id="agent-b") + "\n")
    records, malformed = MODULE.parse_claude_gpt_hook_sink_records(sink_path)
    assert malformed == 0
    assert {r["agent_id"] for r in records} == {"agent-a", "agent-b"}


def test_given_hook_sink_with_truncated_trailing_line_when_parsed_then_only_that_line_dropped(tmp_path):
    """AC15 poison case: a corrupted (truncated) trailing line must not
    poison the whole file -- every well-formed preceding line is still
    parsed, and only the corrupted line is counted as malformed."""
    sink_path = tmp_path / "hook-sink-nonce-a.jsonl"
    sink_path.write_text(
        _sink_line(event="SubagentStart", agent_id="agent-a") + "\n" + '{"run_nonce": "nonce-a", "event": "Su',
        encoding="utf-8",
    )
    records, malformed = MODULE.parse_claude_gpt_hook_sink_records(sink_path)
    assert malformed == 1
    assert len(records) == 1
    assert records[0]["agent_id"] == "agent-a"


def test_given_hook_sink_line_with_unexpected_key_when_parsed_then_dropped_as_malformed(tmp_path):
    """AC13/AC15: a line whose parsed JSON object carries a key outside the
    fixed allowlist (e.g. a smuggled ``raw_prompt`` field) is treated as
    malformed and dropped, never trusted as a well-formed record."""
    sink_path = tmp_path / "hook-sink-nonce-a.jsonl"
    poisoned = json.loads(_sink_line(event="SubagentStart", agent_id="agent-a"))
    poisoned["raw_prompt"] = "leaked raw prompt text"
    sink_path.write_text(json.dumps(poisoned) + "\n", encoding="utf-8")
    records, malformed = MODULE.parse_claude_gpt_hook_sink_records(sink_path)
    assert malformed == 1
    assert records == []


def test_given_matching_three_way_nonce_when_staleness_checked_then_verified():
    """AC16: settings.json nonce == harness-expected nonce == every sink
    record's run_nonce -> verified."""
    records = [_json_record(run_nonce="nonce-a"), _json_record(run_nonce="nonce-a", event="Stop")]
    result = MODULE.verify_claude_gpt_hook_sink_not_stale(records, "nonce-a")
    assert result["verified"] is True


def test_given_stale_run_nonce_when_staleness_checked_then_rejected():
    """AC16: a sink record carrying a DIFFERENT (past-run) run_nonce than
    the harness expects for THIS run must fail closed -- independent of
    ``verify_evidence_not_stale`` (repo-state/tested_head based)."""
    records = [_json_record(run_nonce="stale-nonce-from-prior-run")]
    result = MODULE.verify_claude_gpt_hook_sink_not_stale(records, "nonce-a")
    assert result["verified"] is False
    assert result["reason"] == "run_nonce_mismatch"


def test_given_empty_sink_records_when_staleness_checked_then_rejected():
    """AC16: an empty sink (never populated, e.g. hook delivery silently
    failed) must never be treated as fresh-by-default."""
    result = MODULE.verify_claude_gpt_hook_sink_not_stale([], "nonce-a")
    assert result["verified"] is False
    assert result["reason"] == "no_records"


def test_given_hook_sink_orphan_start_when_classified_then_fails_closed():
    """AC17: a process killed (e.g. SIGKILL) before its ``SubagentStop``
    fires leaves an orphan_starts entry via the hook sink -- fails closed,
    no grace-window/timeout-based auto-PASS."""
    records = [
        _json_record(event="SubagentStart", agent_id="agent-a"),
        _json_record(event="SubagentStart", agent_id="agent-b"),
        _json_record(event="SubagentStop", agent_id="agent-a"),
    ]
    result = MODULE.classify_claude_hook_sink_multi_child_lifecycle(records, 2)
    assert result["verified"] is False
    assert result["orphan_starts"] == ["agent-b"]


def test_given_all_subagents_paired_from_sink_when_classified_then_verified():
    """Positive control mirroring the orphan test above: both SubAgents
    have a matching SubagentStop -> verified."""
    records = [
        _json_record(event="SubagentStart", agent_id="agent-a"),
        _json_record(event="SubagentStart", agent_id="agent-b"),
        _json_record(event="SubagentStop", agent_id="agent-a"),
        _json_record(event="SubagentStop", agent_id="agent-b"),
    ]
    result = MODULE.classify_claude_hook_sink_multi_child_lifecycle(records, 2)
    assert result["verified"] is True
    assert result["paired_agent_ids"] == ["agent-a", "agent-b"]


def test_given_two_prompt_turns_with_stops_when_multi_turn_checked_from_sink_then_verified():
    """Positive control for the hook-sink multi-turn primitive: >=2
    UserPromptSubmit records sharing one session_id, each with a
    corresponding Stop record, zero StopFailure -> verified."""
    records = [
        _json_record(event="UserPromptSubmit", session_id="s1", agent_id=None),
        _json_record(event="Stop", session_id="s1", agent_id=None),
        _json_record(event="UserPromptSubmit", session_id="s1", agent_id=None),
        _json_record(event="Stop", session_id="s1", agent_id=None),
    ]
    result = MODULE.verify_claude_gpt_hook_sink_multi_turn(records, 2)
    assert result["verified"] is True
    assert result["turn_count"] == 2
    assert result["stop_failure_count"] == 0


def test_given_stop_failure_record_when_multi_turn_checked_from_sink_then_rejected():
    """Negative control: a single StopFailure for the session under test
    must fail the multi-turn verdict closed, even with enough
    UserPromptSubmit/Stop pairs."""
    records = [
        _json_record(event="UserPromptSubmit", session_id="s1", agent_id=None),
        _json_record(event="Stop", session_id="s1", agent_id=None),
        _json_record(event="UserPromptSubmit", session_id="s1", agent_id=None),
        _json_record(event="Stop", session_id="s1", agent_id=None),
        _json_record(event="StopFailure", session_id="s1", agent_id=None),
    ]
    result = MODULE.verify_claude_gpt_hook_sink_multi_turn(records, 2)
    assert result["verified"] is False
    assert result["stop_failure_count"] == 1
