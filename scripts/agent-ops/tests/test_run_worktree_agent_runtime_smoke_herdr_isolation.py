"""Regression tests for Issue #2176's P0-3 fix-delta (external adversarial
review https://github.com/squne121/loop-protocol/pull/2162#issuecomment-5301559498).

Background: the isolated interactive herdr lane (``run_interactive_herdr_isolated``)
already ran ``herdr workspace create`` / ``herdr agent start|prompt|get|explain|read``
against an explicit, stripped ``isolated_env`` (ambient
``HERDR_SESSION``/``HERDR_SOCKET_PATH``/... removed, then pinned to this run's own
session/socket). The session-*management* commands at the end of the
lifecycle -- ``herdr session stop`` / ``herdr session delete`` / the post-cleanup
``herdr session list --json`` removal confirmation -- did NOT: they were invoked
with ``env=None`` (Python's default), which means they silently inherited
whatever ambient ``HERDR_SESSION``/``HERDR_SOCKET_PATH`` the CALLING process
happened to have (e.g. a human operator's own attached herdr session, or a
stale/poisoned value from a nested invocation). The initial collision check in
``new_isolated_session_name`` had the same gap.

This file adds:

1. An end-to-end regression proving cleanup (session stop / session delete /
   removal confirmation) is pinned to the SAME explicit identity as session
   creation, even when the ambient environment is deliberately poisoned with
   a DIFFERENT ``HERDR_SESSION``/``HERDR_SOCKET_PATH`` (a fake-herdr
   "poison test": the poisoned value must never leak into any herdr
   invocation this lane makes).
2. Unit coverage for the two new baseline-preservation primitives
   (``snapshot_herdr_sessions`` / ``diff_herdr_session_baseline``): a
   pre-existing session's ``None``-listing failure is fail-closed, and only
   the run's own created session is excluded from the diff.
3. An end-to-end ``--require-session-baseline-preservation`` regression: a
   clean run (no pre-existing session disturbed) still exits 0 with the
   baseline diff recorded as empty.
4. A real-``herdr``, live-environment "poison test" (Issue #2176 P0-3 item
   3): with an actual local herdr server, deliberately export a bogus
   ambient ``HERDR_SESSION``/``HERDR_SOCKET_PATH`` pointing at a
   *different* (non-existent) session/socket before calling the module's
   own session-management helpers directly, and confirm they still observe
   and can manage the real default control plane (never silently no-op
   against the ambient/poisoned identity). Skipped (not FAILed) when no
   local herdr server is reachable, per the existing SKIP convention
   (Runtime Verification Applicability: herdr unavailable is a controlled
   SKIP, never promoted to a false PASS).
"""

from __future__ import annotations

import importlib.util
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
        "run_worktree_agent_runtime_smoke_herdr_isolation", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root", str(repo),
            "--worktree", str(worktree),
            *args,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


_HELP_BRANCH = """
if [ "$1" = "--help" ]; then
  echo "--output-format --include-hook-events --no-session-persistence --max-turns"
  exit 0
fi
"""

# ``<subcommand>:HERDR_SESSION=<value>:HERDR_SOCKET_PATH=<value>`` to
# $FAKE_HERDR_ENV_LOG for control-plane calls.  Default-lane tests assert
# that only their own stop/delete calls occur; opt-in tests additionally
# exercise the baseline list/snapshot observation.
_FAKE_ISOLATED_HERDR_BODY_ENV_LOGGING = """
STATE_DIR="$FAKE_HERDR_STATE_DIR"
mkdir -p "$STATE_DIR"
log_env() {
  if [ -n "$FAKE_HERDR_ENV_LOG" ]; then
    sess="${HERDR_SESSION:-<unset>}"
    sock="${HERDR_SOCKET_PATH:-<unset>}"
    printf '%s:HERDR_SESSION=%s:HERDR_SOCKET_PATH=%s\\n' "$1" "$sess" "$sock" >> "$FAKE_HERDR_ENV_LOG"
  fi
}
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
        log_env "list"
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
        log_env "stop"
        touch "$STATE_DIR/$3.stopped"
        exit 0
        ;;
      delete)
        log_env "delete"
        rm -f "$STATE_DIR/$3.session" "$STATE_DIR/$3.stopped"
        exit 0
        ;;
    esac
    ;;
  workspace)
    case "$2" in
      create)
        touch "$STATE_DIR/${HERDR_SESSION}.session"
        echo '{"result":{"root_pane":{"pane_id":"pane-xyz"},"workspace":{"workspace_id":"w1"}}}'
        exit 0
        ;;
    esac
    ;;
  agent)
    case "$2" in
      start) exit 0 ;;
      prompt) exit 0 ;;
      get) echo '{"state":"idle"}'; exit 0 ;;
      explain) echo '{"agent":"claude","confidence":"high"}'; exit 0 ;;
      read) echo "OBSERVED_MARKER pane transcript line"; exit 0 ;;
    esac
    ;;
  api)
    case "$2" in
      snapshot)
        log_env "snapshot"
        # Issue #2174 AC7: deterministic, unchanging workspace/agent/focus
        # snapshot -- the same content on every call means before/after
        # comparisons in the runner under test always observe zero drift.
        echo '{"result":{"snapshot":{"agents":[],"focused_workspace_id":"w0",'\
'"focused_tab_id":"w0:t0","focused_pane_id":"w0:p0"}}}'
        exit 0
        ;;
    esac
    ;;
esac
exit 0
"""


# ---------------------------------------------------------------------------
# 1. Ambient-poison regression (identity consistency across the whole
#    isolated-session lifecycle, including cleanup).
# ---------------------------------------------------------------------------


def test_given_ambient_herdr_identity_poisoned_when_default_interactive_lane_runs_then_only_own_cleanup_is_used(
    repo_with_worktree, tmp_path
):
    """The normal lane strips poisoned ambient identity and operates only on
    its generated session.  It must not list or snapshot any pre-existing
    namespace; explicit opt-in is tested separately."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY_ENV_LOGGING)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    env_log = tmp_path / "herdr-env-log.txt"
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"

    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--expect-marker", "OBSERVED_MARKER",
        fake_bin_dir=fake_bin,
        extra_env={
            "HERDR_ENV": "1",
            "FAKE_HERDR_STATE_DIR": str(state_dir),
            "FAKE_HERDR_ENV_LOG": str(env_log),
            # Ambient poison: a DIFFERENT session name / socket path than
            # anything this run will actually create.
            "HERDR_SESSION": "ambient-poison-session",
            "HERDR_SOCKET_PATH": "/ambient/poison/herdr.sock",
        },
    )
    assert result.returncode == 0, result.stderr

    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    session_line = next(line for line in summary.splitlines() if line.startswith("- session_name:"))
    created_session_name = session_line.split(":", 1)[1].strip()
    assert created_session_name.startswith("rts-")

    log_lines = env_log.read_text(encoding="utf-8").splitlines()
    # Only the two own-session cleanup calls are permitted.  In particular,
    # default execution must not list or snapshot any existing namespace.
    assert any(line.startswith("stop:") for line in log_lines)
    assert any(line.startswith("delete:") for line in log_lines)
    assert not any(line.startswith("list:") for line in log_lines)
    assert not any(line.startswith("snapshot:") for line in log_lines)

    for line in log_lines:
        assert "ambient-poison-session" not in line, (
            f"poisoned ambient HERDR_SESSION leaked into a herdr session-management "
            f"call: {line!r}"
        )
        assert "/ambient/poison/herdr.sock" not in line, (
            f"poisoned ambient HERDR_SOCKET_PATH leaked into a herdr "
            f"session-management call: {line!r}"
        )

    # The stop/delete calls specifically must carry THIS run's own session
    # identity, not a blank/unrelated one -- proving they share the exact
    # same explicit isolated_env as session creation.
    stop_lines = [line for line in log_lines if line.startswith("stop:")]
    delete_lines = [line for line in log_lines if line.startswith("delete:")]
    assert all(f"HERDR_SESSION={created_session_name}" in line for line in stop_lines)
    assert all(f"HERDR_SESSION={created_session_name}" in line for line in delete_lines)


# ---------------------------------------------------------------------------
# 2. snapshot_herdr_sessions / diff_herdr_session_baseline unit coverage.
# ---------------------------------------------------------------------------


def test_given_session_list_call_fails_when_snapshotting_then_none_returned(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "_herdr_sessions", lambda _herdr_bin, env=None: None)
    assert module.snapshot_herdr_sessions("herdr", {}) is None


def test_given_sessions_present_when_snapshotting_then_normalized_and_sorted(monkeypatch):
    module = _load_module()

    def fake_sessions(_herdr_bin, env=None):
        return [
            {"name": "zzz", "running": True, "default": False, "socket_path": "/z.sock",
             "session_dir": "/z", "secret_should_be_dropped": "nope"},
            {"name": "aaa", "running": True, "default": True, "socket_path": "/a.sock",
             "session_dir": "/a"},
        ]

    monkeypatch.setattr(module, "_herdr_sessions", fake_sessions)
    snapshot = module.snapshot_herdr_sessions("herdr", {})
    assert snapshot is not None
    assert [entry["name"] for entry in snapshot] == ["aaa", "zzz"]
    assert "secret_should_be_dropped" not in snapshot[1]


def test_given_before_or_after_is_none_when_diffing_baseline_then_fail_closed():
    module = _load_module()
    assert module.diff_herdr_session_baseline(None, [], new_session_names=set())
    assert module.diff_herdr_session_baseline([], None, new_session_names=set())


def test_given_only_own_created_session_added_and_removed_when_diffing_baseline_then_no_diff():
    module = _load_module()
    before = [{"name": "default", "running": True, "default": True,
               "socket_path": "/a.sock", "session_dir": "/a"}]
    after_with_new = before + [{"name": "rts-abc", "running": True, "default": False,
                                 "socket_path": "/b.sock", "session_dir": "/b"}]
    # Simulate: right after creation (new session present, not yet cleaned up).
    diffs = module.diff_herdr_session_baseline(before, after_with_new, new_session_names={"rts-abc"})
    assert diffs == []
    # Simulate: after cleanup, the created session is gone again.
    diffs_after_cleanup = module.diff_herdr_session_baseline(before, before, new_session_names={"rts-abc"})
    assert diffs_after_cleanup == []


def test_given_pre_existing_session_disturbed_when_diffing_baseline_then_diff_reported():
    module = _load_module()
    before = [{"name": "default", "running": True, "default": True,
               "socket_path": "/a.sock", "session_dir": "/a"},
              {"name": "human-session", "running": True, "default": False,
               "socket_path": "/h.sock", "session_dir": "/h"}]
    after_missing_human = [before[0]]
    diffs = module.diff_herdr_session_baseline(before, after_missing_human, new_session_names=set())
    assert diffs
    assert any("human-session" in diff for diff in diffs)


def test_given_pre_existing_session_field_changed_when_diffing_baseline_then_diff_reported():
    module = _load_module()
    before = [{"name": "human-session", "running": True, "default": False,
               "socket_path": "/h.sock", "session_dir": "/h"}]
    after = [{"name": "human-session", "running": False, "default": False,
              "socket_path": "/h.sock", "session_dir": "/h"}]
    diffs = module.diff_herdr_session_baseline(before, after, new_session_names=set())
    assert diffs
    assert any("human-session" in diff for diff in diffs)


# ---------------------------------------------------------------------------
# 3. End-to-end --require-session-baseline-preservation regression.
# ---------------------------------------------------------------------------


def test_given_require_session_baseline_preservation_and_clean_run_when_interactive_lane_runs_then_exit0(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY_ENV_LOGGING)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"

    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--expect-marker", "OBSERVED_MARKER",
        "--require-session-baseline-preservation",
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "session_baseline_before_captured: True" in summary
    assert "session_baseline_after_captured: True" in summary
    assert "session_baseline_diffs: []" in summary


def test_given_no_baseline_preservation_flag_when_interactive_lane_runs_then_baseline_fields_absent(
    repo_with_worktree, tmp_path
):
    """AC6-equivalent: the default lane never observes existing namespaces."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY_ENV_LOGGING)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    env_log = tmp_path / "herdr-env-log.txt"
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"

    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--expect-marker", "OBSERVED_MARKER",
        fake_bin_dir=fake_bin,
        extra_env={
            "HERDR_ENV": "1",
            "FAKE_HERDR_STATE_DIR": str(state_dir),
            "FAKE_HERDR_ENV_LOG": str(env_log),
        },
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "session_baseline_before_captured" not in summary
    assert "session_baseline_diffs" not in summary
    assert "herdr_workspace_snapshot_before_captured" not in summary
    assert "preexisting_herdr_preserved: None" in summary
    log_lines = env_log.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith(("list:", "snapshot:")) for line in log_lines)


# ---------------------------------------------------------------------------
# 3b. Issue #2183 AC10 (PR #2220 OWNER REQUEST_CHANGES P0-1,
#     https://github.com/squne121/loop-protocol/pull/2220#issuecomment-5309790514,
#     re-narrowed by the follow-up P0-1 fix-delta): a fix-delta that landed
#     after the initial P0-1 patch over-applied the OWNER's proposed
#     ``args.runtime != "claude" or args.mode != "structured"`` argparse
#     guard, which rejected --require-subagent-causal-evidence for EVERY
#     interactive-lane run (including --runtime claude), directly
#     contradicting AC10 ("interactive lane ... requires causal-evidence
#     only on --require-subagent-causal-evidence opt-in"). The guard is
#     scoped to ``args.runtime != "claude"`` only -- claude + interactive
#     is a genuine opt-in gate (see the test directly below), it just tends
#     to observe no_evidence/marker_only_insufficient in practice because
#     the herdr pane render does not echo the --include-hook-events
#     stream-json hook payload subagent_causal_evidence_verdict() parses.
#     Only a non-claude --runtime remains rejected at argparse time (no
#     causal-evidence channel exists for any other runtime at all).
# ---------------------------------------------------------------------------


def test_given_non_claude_runtime_with_require_subagent_causal_evidence_when_parsed_then_rejected(
    repo_with_worktree, tmp_path
):
    """PR #2220 OWNER REQUEST_CHANGES P0-1 (narrowed): --require-subagent-
    causal-evidence combined with any --runtime other than claude is
    rejected at argparse time (exit 2, before any process is spawned),
    since subagent_causal_evidence_verdict() never has a hook-lifecycle
    channel to evaluate outside --runtime claude -- the flag would
    otherwise always force a FAIL regardless of run outcome. This must
    reject regardless of --mode (structured shown here)."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY_ENV_LOGGING)
    state_dir = tmp_path / "herdr-state"
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"

    result = _run(
        repo, worktree,
        "--runtime", "codex", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--expect-marker", "OBSERVED_MARKER",
        "--require-subagent-causal-evidence",
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 2
    # Issue #2161 (native Codex CLI retirement): --runtime codex is no
    # longer a valid argparse choice at all, so this now surfaces the
    # argparse-level "invalid choice" rejection instead of the (now
    # unreachable) --runtime-specific parser.error() check.
    assert "invalid choice: 'codex'" in result.stderr
    assert not out_dir.exists()


def test_given_claude_interactive_lane_with_require_subagent_causal_evidence_when_marker_only_then_fails(
    repo_with_worktree, tmp_path
):
    """Issue #2183 AC10 regression (fixes the over-broad P0-1 fix-delta
    that previously rejected this exact combination at argparse time):
    --runtime claude --mode interactive combined with
    --require-subagent-causal-evidence must be ACCEPTED at argparse time
    (no --mode restriction) and must actually gate exit_code on the
    computed causal-evidence verdict, exactly like the structured lane
    does. The herdr pane in this fake-binary scenario only ever contains
    the plain marker text (no --include-hook-events stream-json), so the
    verdict resolves to marker_only_insufficient -- which is NOT
    hook_id_correlated -- and the opt-in gate must FAIL the run."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY_ENV_LOGGING)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"

    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--expect-marker", "OBSERVED_MARKER",
        "--require-subagent-causal-evidence",
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 1, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "'causal_evidence_source': 'marker_only_insufficient'" in summary
    assert "subagent causal evidence insufficient (--require-subagent-causal-evidence)" in summary


def test_given_herdr_interactive_lane_pane_marker_only_when_causal_evidence_not_required_then_exit0(
    repo_with_worktree, tmp_path
):
    """Issue #2183 AC10 (opt-in default): the SAME marker-only herdr pane
    shape as the test above, but WITHOUT --require-subagent-causal-evidence
    -- unlike the structured lane's --expect-marker default forcing, the
    interactive lane must NOT gate exit_code on causal evidence unless the
    caller explicitly opts in. The verdict is still computed and recorded
    (never silently dropped), just not consulted for exit_code."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY_ENV_LOGGING)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"

    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--expect-marker", "OBSERVED_MARKER",
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "'causal_evidence_source': 'marker_only_insufficient'" in summary


# ---------------------------------------------------------------------------
# 4. Real-herdr live poison test (Issue #2176 P0-3 item 3).
# ---------------------------------------------------------------------------


def _real_herdr_available() -> bool:
    exe = None
    for candidate in os.environ.get("PATH", "").split(os.pathsep):
        p = Path(candidate) / "herdr"
        if p.is_file() and os.access(p, os.X_OK):
            exe = str(p)
            break
    if exe is None:
        return False
    try:
        proc = subprocess.run([exe, "status", "server"], capture_output=True, text=True, timeout=10.0)
    except OSError:
        return False
    return proc.returncode == 0


@pytest.mark.skipif(
    not _real_herdr_available(),
    reason="no local herdr server reachable (SKIP, never promoted to a false PASS)",
)
def test_given_real_herdr_and_poisoned_ambient_identity_when_helpers_run_then_control_plane_still_reachable():
    """Live-environment confirmation (real herdr binary, real supervisor,
    Issue #2176 P0-3 item 3 'poison test'): deliberately export a bogus
    ambient HERDR_SESSION/HERDR_SOCKET_PATH pointing at a session/socket
    that does not exist, then call the module's own session-management
    helpers with the module's own explicit ``_isolated_env()`` (exactly
    what ``run_interactive_herdr_isolated`` does) and confirm the real
    control plane is still reachable and unaffected by the poison --
    proving the isolation the collision-check / creation / cleanup path
    relies on is not merely theoretical against this exact herdr build."""
    module = _load_module()

    poisoned_environ = dict(os.environ)
    poisoned_environ["HERDR_SESSION"] = "definitely-nonexistent-poison-session"
    poisoned_environ["HERDR_SOCKET_PATH"] = "/definitely/nonexistent/poison/herdr.sock"
    old_environ = os.environ.copy()
    os.environ.clear()
    os.environ.update(poisoned_environ)
    try:
        isolated_env = module._isolated_env()
        # The isolated_env must have the poison stripped out entirely.
        assert "HERDR_SESSION" not in isolated_env
        assert "HERDR_SOCKET_PATH" not in isolated_env

        before = module.snapshot_herdr_sessions("herdr", isolated_env)
        assert before is not None, "real herdr session list failed even though the server is reachable"
        # The real supervisor's own bookkeeping (e.g. its always-present
        # "default" session) must be visible -- proving this call reached
        # the genuine control plane, not a no-op against the poisoned
        # ambient socket path.
        assert len(before) >= 1

        # A fresh isolated session, created and torn down through the
        # SAME helper functions run_interactive_herdr_isolated uses,
        # must not disturb that pre-existing inventory.
        session_name = module.new_isolated_session_name("herdr", env=isolated_env)
        assert session_name not in {entry["name"] for entry in before}
        try:
            proc = module.create_isolated_session("herdr", session_name, isolated_env, timeout_seconds=20.0)
        except module.HerdrLaneError as exc:
            if exc.skip:
                pytest.skip(f"SKIP: isolated Herdr launch unavailable: {exc}")
            raise
        try:
            mid = module.snapshot_herdr_sessions("herdr", isolated_env)
            assert mid is not None
            assert session_name in {entry["name"] for entry in mid}
        finally:
            subprocess.run(["herdr", "session", "stop", session_name, "--json"],
                            env=isolated_env, capture_output=True, text=True, timeout=20.0)
            subprocess.run(["herdr", "session", "delete", session_name, "--json"],
                            env=isolated_env, capture_output=True, text=True, timeout=20.0)
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    proc.kill()

        after = module.snapshot_herdr_sessions("herdr", isolated_env)
        assert after is not None
        diffs = module.diff_herdr_session_baseline(before, after, new_session_names={session_name})
        assert diffs == [], f"pre-existing herdr sessions were disturbed: {diffs}"
    finally:
        os.environ.clear()
        os.environ.update(old_environ)
