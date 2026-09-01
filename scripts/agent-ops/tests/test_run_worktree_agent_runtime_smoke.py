from __future__ import annotations

import importlib.util
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_worktree_agent_runtime_smoke", SCRIPT)
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


def _build_repo_with_worktree(tmp_path: Path, *, include_runner_script: bool = False) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    if include_runner_script:
        # Mirror the real repo layout where this runner script is itself
        # checked out inside linked worktrees under scripts/agent-ops/, so a
        # fixture-built worktree can exercise --repo-root default resolution
        # exactly as it happens for a real .claude/worktrees/<slug>/ checkout
        # (Issue #1887 fix-delta iteration 1).
        script_dst = repo / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
        script_dst.parent.mkdir(parents=True, exist_ok=True)
        script_dst.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
        _git("add", str(script_dst.relative_to(repo)), cwd=repo)
    _git("commit", "-m", "seed", cwd=repo)

    worktree = repo / ".claude" / "worktrees" / "issue-0000-fixture"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git("branch", "worktree-fixture", cwd=repo)
    _git("worktree", "add", str(worktree), "worktree-fixture", cwd=repo)
    return repo, worktree


@pytest.fixture()
def repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    return _build_repo_with_worktree(tmp_path)


@pytest.fixture()
def repo_with_worktree_and_script(tmp_path: Path) -> tuple[Path, Path]:
    """Like ``repo_with_worktree``, but the runner script itself is checked
    out at scripts/agent-ops/ so the worktree contains a real, physical copy
    of it (mirroring the real .claude/worktrees/<slug>/ layout)."""
    return _build_repo_with_worktree(tmp_path, include_runner_script=True)


@pytest.fixture()
def candidate_worktree_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Issue #2046 AC8: an explicit, disposable repo + linked worktree
    fixture name, used in place of a fixed reference to any single
    historical Issue's worktree (e.g. the now-superseded
    ``.claude/worktrees/issue-1734-...`` hardcoded in
    ``.claude/agents/tests/test_issue_editor_runtime_smoke.py`` before this
    Issue). An alias over ``_build_repo_with_worktree`` -- same hermetic
    tmp_path-backed repo/worktree construction already used throughout this
    file, given a name that documents the AC8 intent explicitly."""
    return _build_repo_with_worktree(tmp_path)


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


# claude --help fake branch. Must advertise every flag preflight_claude_flags
# requires, including --max-turns (Issue #1921 P1 fix-delta: bounded turns).
_HELP_BRANCH = """
if [ "$1" = "--help" ]; then
  echo "--output-format --include-hook-events --no-session-persistence --max-turns"
  exit 0
fi
"""


FAKE_CLAUDE_SUCCESS_BODY = (
    "\n"
    "cat > /dev/null\n"
    'echo \'{"type":"system","subtype":"init"}\'\n'
    'echo \'{"type":"result","subtype":"success","marker":"MARKER_TOKEN_WT"}\'\n'
    "exit 0\n"
)


def test_given_native_policy_payload_when_generated_then_exact_peer_policy_and_observability_hooks_are_present():
    """The harness-owned native overlay contains the exact peer policy and
    retains the existing lifecycle observability hooks; it is not a global
    settings mutation or caller-provided policy input."""
    module = _load_module()
    settings = json.loads(module._CLAUDE_SPAWN_HOOK_OBSERVABILITY_SETTINGS_JSON)

    assert settings["crossSessionInbound"] == "refuse"
    assert settings["permissions"]["deny"] == ["SendMessage", "ListAgents"]
    assert set(settings["hooks"]) == {"SubagentStart", "SubagentStop"}


def test_given_native_structured_spawn_when_fake_child_gets_settings_then_peer_tools_are_fixed_and_no_peer_is_started(
    repo_with_worktree,
    tmp_path,
):
    """A controlled child process validates only its own generated settings.
    It never starts, lists, sends to, or observes an independent peer."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    observed = tmp_path / "peer-policy-observed"
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + f"""
settings=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--settings" ]; then
    settings="$2"
    shift 2
    continue
  fi
  shift
done
python3 - "$settings" <<'PY'
import json
import sys
settings = json.loads(sys.argv[1])
assert settings["crossSessionInbound"] == "refuse"
assert settings["permissions"]["deny"] == ["SendMessage", "ListAgents"]
PY
printf '%s' absent_or_denied > {shlex.quote(str(observed))}
cat > /dev/null
printf '%s\\n' '{{"type":"system","subtype":"init"}}'
result='{{"type":"result","subtype":"success",'
result+='"permission_denials":[{{"tool_name":"SendMessage"}},'
result+='{{"tool_name":"ListAgents"}}]}}'
printf '%s\\n' "$result"
""")
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        "--evidence-json", str(tmp_path / "evidence.json"),
        fake_bin_dir=fake_bin,
    )

    assert result.returncode == 0, result.stderr
    assert observed.read_text(encoding="utf-8") == "absent_or_denied"
    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["peer_policy_configured"] is True
    assert evidence["cross_session_inbound_configured_refuse"] is True
    assert evidence["outbound_peer_tools_absent"] is True


def test_given_native_interactive_agent_start_when_constructed_then_settings_are_passed_after_herdr_separator():
    """Installed Herdr documents `agent start ... -- [AGENT_ARG]...`; the
    interactive lane uses that existing pass-through rather than changing its
    direct-vs-Herdr transport boundary."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'if claude_adapter == "native":' in source
    assert '"--", "--settings", _CLAUDE_SPAWN_HOOK_OBSERVABILITY_SETTINGS_JSON' in source
    assert "Claude-GPT keeps its launcher-owned" in source


def _subagent_hook_lines(
    agent_id: str = "child-fixture-1",
    transcript_path: str | None = None,
    tool_use_id: str = "toolu_fixture_agent_call",
    transcript_marker: str | None = None,
) -> str:
    """Issue #2183 PR #2220 review fix-delta (AC3/AC11 strengthening):
    correlated SubagentStart/SubagentStop stream-json hook lines PLUS a
    real ``Agent`` tool_use/tool_result envelope PLUS (when
    ``transcript_marker`` is given) an ACTUAL, non-empty, readable
    transcript file materialized on disk before the fake process exits --
    matching the real Claude Code hook/tool-invocation payload shapes
    ``extract_claude_hook_lifecycle_events`` /
    ``_claude_agent_tool_invocation_correlated`` /
    ``_read_claude_agent_transcript_content`` parse. A lone Start/Stop
    pair referencing a transcript path that was never actually written to
    disk (the prior shape of this fixture, before Issue #2183's AC3/AC11
    strengthening added ``tool_invocation_id_correlated`` and
    ``agent_transcript_verified``/``marker_provenance_verified`` to the
    ``causal_evidence_source`` promotion gate) is no longer sufficient for
    a fixture whose invocation also passes ``--expect-marker``."""
    resolved_transcript_path = transcript_path or f"/tmp/{agent_id}-{os.getpid()}-transcript.jsonl"

    def _hook_event(hook_event: str, *, with_transcript: bool) -> str:
        inner: dict[str, str] = {"agent_id": agent_id, "agent_type": "general-purpose"}
        if with_transcript:
            inner["agent_transcript_path"] = resolved_transcript_path
        inner_json = json.dumps(inner)
        payload = {
            "type": "system",
            "subtype": "hook_response",
            "hook_event": hook_event,
            "hook_name": hook_event,
            "session_id": "fixture-session",
            "stdout": inner_json,
            "output": inner_json,
        }
        return f"echo {shlex.quote(json.dumps(payload))}\n"

    def _agent_tool_use_line() -> str:
        payload = {
            "type": "assistant",
            "session_id": "fixture-session",
            "message": {"content": [{"type": "tool_use", "id": tool_use_id, "name": "Agent", "input": {}}]},
        }
        return f"echo {shlex.quote(json.dumps(payload))}\n"

    def _agent_tool_result_line() -> str:
        payload = {
            "type": "user",
            "session_id": "fixture-session",
            "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "done"}]},
            "tool_use_result": {"status": "completed", "agentId": agent_id, "agentType": "general-purpose"},
        }
        return f"echo {shlex.quote(json.dumps(payload))}\n"

    write_transcript_line = ""
    if transcript_marker is not None:
        transcript_content = json.dumps({"type": "assistant", "message": {"content": transcript_marker}}) + "\n"
        quoted_content = shlex.quote(transcript_content)
        quoted_path = shlex.quote(resolved_transcript_path)
        write_transcript_line = f"printf %s {quoted_content} > {quoted_path}\n"

    return (
        write_transcript_line
        + _agent_tool_use_line()
        + _hook_event("SubagentStart", with_transcript=False)
        + _agent_tool_result_line()
        + _hook_event("SubagentStop", with_transcript=True)
    )


FAKE_CLAUDE_SUCCESS_BODY_WITH_SUBAGENT_EVIDENCE = (
    "\n"
    "cat > /dev/null\n"
    'echo \'{"type":"system","subtype":"init"}\'\n'
    + _subagent_hook_lines(transcript_marker="MARKER_TOKEN_WT")
    + 'echo \'{"type":"result","subtype":"success","marker":"MARKER_TOKEN_WT"}\'\n'
    "exit 0\n"
)


# ---------------------------------------------------------------------------
# AC2: worktree / repository identity rejection
# ---------------------------------------------------------------------------


def test_given_root_checkout_when_runner_starts_then_rejected(repo_with_worktree, tmp_path):
    repo, _worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, repo,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
    )
    assert result.returncode == 1
    assert "root checkout" in result.stderr


def test_given_different_repository_when_runner_starts_then_rejected(repo_with_worktree, tmp_path):
    repo, _worktree = repo_with_worktree
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    _git("init", "-b", "main", cwd=other_repo)
    (other_repo / "f.txt").write_text("x\n", encoding="utf-8")
    _git("add", "f.txt", cwd=other_repo)
    _git("commit", "-m", "seed", cwd=other_repo)

    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, other_repo,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
    )
    assert result.returncode == 1
    assert "different repository" in result.stderr


def test_given_missing_worktree_path_when_runner_starts_then_rejected(repo_with_worktree, tmp_path):
    repo, _worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, repo / ".claude" / "worktrees" / "does-not-exist",
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
    )
    assert result.returncode == 1
    assert "does not exist" in result.stderr


def test_given_non_worktree_dir_under_claude_worktrees_when_runner_starts_then_cwd_mismatch(
    repo_with_worktree, tmp_path
):
    repo, _worktree = repo_with_worktree
    stray_dir = repo / ".claude" / "worktrees"
    prompt = _prompt_file(tmp_path)
    # ``.claude/worktrees`` itself resolves to the repo's own toplevel (still the
    # canonical repo checkout), so it is rejected for cwd mismatch, not accepted.
    result = _run(
        repo, stray_dir,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
    )
    assert result.returncode == 1


# ---------------------------------------------------------------------------
# Output directory exclusivity (Issue #1921 P0-4)
# ---------------------------------------------------------------------------


def test_given_output_dir_already_exists_when_runner_starts_then_rejected(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    out_dir = worktree / "artifacts" / "runtime-smoke" / "exists-run"
    out_dir.mkdir(parents=True)
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
    )
    assert result.returncode == 1
    assert "already exists" in result.stderr


def test_given_output_dir_is_symlink_when_runner_starts_then_rejected(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    real_target = tmp_path / "elsewhere"
    real_target.mkdir()
    out_dir = worktree / "artifacts" / "runtime-smoke" / "symlinked-run"
    out_dir.parent.mkdir(parents=True)
    out_dir.symlink_to(real_target)
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
    )
    assert result.returncode == 1
    assert "symlink" in result.stderr


# ---------------------------------------------------------------------------
# AC3 / AC8: structured Claude lane — argv, exit codes, timeout
# ---------------------------------------------------------------------------


def test_given_fake_claude_success_when_structured_lane_runs_then_exit0_and_summary_has_event_count(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(
        fake_bin / "claude",
        _HELP_BRANCH
        + """
cat > /dev/null
echo '{"type":"system","subtype":"init"}'
"""
        + _subagent_hook_lines(transcript_marker="MARKER_TOKEN_1")
        + """echo '{"type":"result","subtype":"success","marker":"MARKER_TOKEN_1"}'
exit 0
""",
    )
    prompt = _prompt_file(tmp_path, "MARKER_TOKEN_1\n")
    out_dir = worktree / "artifacts" / "runtime-smoke" / "claude-structured"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--timeout-seconds", "30", "--expect-marker", "MARKER_TOKEN_1",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    # Issue #2183 AC3/AC11 strengthening (PR #2220 review fix-delta): the
    # structured lane's --expect-marker default causal-evidence gate now
    # requires the fake claude fixture to also emit a real Agent
    # tool_use/tool_result envelope (tool_invocation_id_correlated) in
    # addition to the correlated SubagentStart/SubagentStop pair, which
    # adds 4 more parseable stream-json lines on top of the pre-existing
    # init + result lines.
    assert "native_event_count: 6" in summary
    assert "subagent_causal_evidence" in summary
    assert "'causal_evidence_source': 'hook_id_correlated'" in summary
    assert "terminal_event_observed: True" in summary
    # Only summary.md is persisted (Issue #1921 P1 evidence-hygiene fix-delta).
    assert sorted(p.name for p in out_dir.iterdir()) == ["summary.md"]


def test_given_marker_only_fake_claude_when_default_require_causal_evidence_gate_applies_then_structured_lane_fails(
    repo_with_worktree, tmp_path
):
    """Issue #2183 AC9: the structured lane requires
    causal_evidence_source == hook_id_correlated as the DEFAULT (no
    --require-subagent-causal-evidence flag needed) whenever the caller
    supplies --expect-marker. This fixture's fake claude emits the
    expected marker string in its stdout but NO SubagentStart/SubagentStop
    hook lifecycle events at all -- exactly the marker-only-PASS shape
    this Issue's causal-evidence gate exists to stop trusting -- so the
    run must FAIL (exit 1) even though the plain --expect-marker text
    match itself would have succeeded."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + FAKE_CLAUDE_SUCCESS_BODY)
    prompt = _prompt_file(tmp_path, "MARKER_TOKEN_WT\n")
    out_dir = worktree / "artifacts" / "runtime-smoke" / "claude-structured"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--timeout-seconds", "30", "--expect-marker", "MARKER_TOKEN_WT",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1
    assert "subagent causal evidence insufficient" in result.stderr
    assert "--expect-marker default gate" in result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "'causal_evidence_source': 'marker_only_insufficient'" in summary


def test_given_marker_only_fake_claude_when_no_expect_marker_then_default_causal_gate_does_not_apply(
    repo_with_worktree, tmp_path
):
    """Issue #2183 AC9 (negative-of-negative sanity check): the DEFAULT
    causal-evidence gate is scoped to callers who actually supply
    --expect-marker. A structured lane run with neither --expect-marker
    nor --require-subagent-causal-evidence must not be gated on causal
    evidence at all (the verdict is still computed and recorded, but never
    consulted for exit_code)."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + FAKE_CLAUDE_SUCCESS_BODY)
    prompt = _prompt_file(tmp_path)
    out_dir = worktree / "artifacts" / "runtime-smoke" / "claude-structured"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--timeout-seconds", "30",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    # No --expect-marker was supplied, so subagent_causal_evidence_verdict()
    # never had an expected_markers list to check stdout against at all --
    # this observes no_evidence (not marker_only_insufficient, which
    # requires an expected_markers match), and critically it must never
    # gate exit_code (asserted above via returncode == 0).
    assert "'causal_evidence_source': 'no_evidence'" in summary


def test_given_fake_claude_help_omits_max_turns_but_runtime_accepts_it_when_structured_lane_runs_then_exit0(
    repo_with_worktree, tmp_path
):
    """Issue #1960 fix: ``claude --help`` not advertising ``--max-turns``
    (observed for real in Claude Code 2.1.220) must not SKIP the structured
    lane as long as the actual fixed-argv invocation accepts the flag and
    returns a terminal result. This is the regression test for the exact
    false-SKIP unit test this Issue's Background/Problem section (point 7)
    identified as encoding the wrong behavior; it also unblocks the #1734
    consumer that was previously starved by this false SKIP."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", """
if [ "$1" = "--help" ]; then
  echo "--output-format --include-hook-events --no-session-persistence"
  exit 0
fi
cat > /dev/null
echo '{"type":"result","subtype":"success"}'
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "capability_decision: runtime_outcome" in summary


def test_given_fake_claude_rejects_max_turns_as_unknown_option_when_structured_lane_runs_then_skip77_with_summary(
    repo_with_worktree, tmp_path
):
    """Issue #1960 AC2: a real parser-level unknown/unrecognized-option
    rejection (not merely a help-text omission) is the only condition that
    SKIPs -- and it must still write summary.md with runtime version and
    capability reason evidence."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", """
if [ "$1" = "--version" ]; then
  echo "2.1.220 (Claude Code)"
  exit 0
fi
if [ "$1" = "--help" ]; then
  echo "--output-format --include-hook-events --no-session-persistence --max-turns"
  exit 0
fi
cat > /dev/null
echo "error: unknown option '--max-turns'" >&2
exit 1
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 77
    assert result.stderr.startswith("SKIP:")
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "capability_decision: capability_skip" in summary
    assert "runtime_version: 2.1.220" in summary


def test_given_fake_claude_reaches_max_turns_when_structured_lane_runs_then_fail_not_skip(
    repo_with_worktree, tmp_path
):
    """Issue #1960 AC4: reaching the ``--max-turns`` bound is evidence the
    flag WAS accepted -- it must classify as FAIL (exit 1), never as a
    capability SKIP."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + """
cat > /dev/null
echo "Error: Reached max turns limit: 3" >&2
exit 1
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--max-turns", "3",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1
    assert not result.stderr.startswith("SKIP:")
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "capability_decision: turn_limit_reached" in summary


def test_given_max_turns_zero_or_negative_when_parsed_then_argument_error(repo_with_worktree, tmp_path):
    """Issue #1960 AC6: ``--max-turns`` only accepts positive integers."""
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    for bad_value in ("0", "-1"):
        result = _run(
            repo, worktree,
            "--runtime", "claude", "--mode", "structured",
            "--prompt-file", str(prompt), "--output-dir", str(tmp_path / f"out-{bad_value}"),
            "--max-turns", bad_value,
        )
        assert result.returncode == 2, result.stderr
        assert "--max-turns" in result.stderr


def test_given_no_claude_binary_when_preflight_runs_then_skip77(repo_with_worktree, tmp_path):
    """Issue #1960 P1-1 fix-delta (PR #1976 owner REQUEST_CHANGES): a
    controlled SKIP 77 caused by ``claude`` preflight failure must still
    emit allowlist-only summary.md evidence, not merely return exit 77 with
    no evidence at all (AC7)."""
    repo, worktree = repo_with_worktree
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        # Keep system dirs (git) reachable while excluding the real claude/codex
        # binaries that normally live under ~/.local/bin.
        extra_env={"PATH": f"{empty_bin}:/usr/bin:/bin"},
    )
    assert result.returncode == 77
    summary_path = out_dir / "summary.md"
    assert summary_path.exists(), "no-claude preflight SKIP must still write summary.md (AC7)"
    summary = summary_path.read_text(encoding="utf-8")
    assert "exit_code: 77" in summary
    assert "required command not found: claude" in summary


# Issue #2161 (native Codex CLI retirement):
# test_given_codex_help_introspection_fails_when_preflight_runs_then_skip77_with_summary
# was removed -- it exercised preflight_codex_flags(), which was removed
# along with the ``codex`` runtime lane (and argparse now rejects
# --runtime codex before any preflight runs).


def test_given_herdr_preflight_fails_when_interactive_mode_runs_then_skip77_with_summary(
    repo_with_worktree, tmp_path
):
    """Issue #1960 P1-1 fix-delta: a controlled SKIP 77 caused by
    ``preflight_herdr`` failure (HERDR_ENV unset) must still emit
    summary.md evidence (AC7)."""
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        # Explicitly override (not merely omit) HERDR_ENV so a real ambient
        # HERDR_ENV=1 in the outer test-runner environment cannot leak in.
        extra_env={"HERDR_ENV": ""},
    )
    assert result.returncode == 77
    summary_path = out_dir / "summary.md"
    assert summary_path.exists(), "herdr preflight SKIP must still write summary.md (AC7)"
    summary = summary_path.read_text(encoding="utf-8")
    assert "exit_code: 77" in summary
    assert "HERDR_ENV=1 not set" in summary


def test_given_fake_claude_nonzero_exit_when_structured_lane_runs_then_exit1(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + """
cat > /dev/null
exit 1
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1
    assert "exited non-zero" in result.stderr


def test_given_fake_claude_hangs_when_timeout_elapses_then_exit1(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + """
sleep 30
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--timeout-seconds", "2",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1
    assert "timed out" in result.stderr


def test_given_declared_capability_window_when_fake_claude_hangs_then_exit77(
    repo_with_worktree, tmp_path
):
    """The opt-in policy yields a persisted SKIP, not an outer verifier timeout."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + """
sleep 30
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--timeout-seconds", "2", "--timeout-is-capability-unavailable",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 77
    assert result.stderr.startswith("SKIP:")
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "capability_decision: capability_skip_timeout" in summary
    assert "capability_error_classification: declared_capability_window_exceeded" in summary


def test_given_required_unavailable_runtime_field_when_fake_claude_succeeds_then_exit77(
    repo_with_worktree, tmp_path
):
    """Unavailable evidence is a persisted SKIP even when the child completes."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # PR #2220 review fix-delta: this fixture must emit correlated
    # SubagentStart/SubagentStop hook evidence, since --expect-marker
    # below now requires causal_evidence_source == hook_id_correlated by
    # default in the structured lane -- otherwise the causal-evidence
    # gate would FAIL before the --require-observed-runtime-field SKIP
    # classification below ever runs.
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + FAKE_CLAUDE_SUCCESS_BODY_WITH_SUBAGENT_EVIDENCE)
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--expect-marker", "MARKER_TOKEN_WT",
        "--require-observed-runtime-field", "executor",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 77
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "capability_decision: required_runtime_evidence_unavailable" in summary
    assert "unavailable_required_runtime_observations: ['executor']" in summary


def test_given_fake_claude_argv_when_structured_lane_runs_then_max_turns_flag_present(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    argv_log = tmp_path / "claude_argv.txt"
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + f"""
cat > /dev/null
echo "$@" > "{argv_log}"
echo '{{"type":"result"}}'
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--max-turns", "7",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    argv_text = argv_log.read_text(encoding="utf-8")
    assert "--max-turns 7" in argv_text


def test_given_structured_lane_events_with_no_terminal_event_when_run_then_exit1(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + """
cat > /dev/null
echo '{"type":"system","subtype":"init"}'
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1
    assert "no terminal/result event" in result.stderr


# ---------------------------------------------------------------------------
# Issue #2161 (native Codex CLI retirement):
# test_given_fake_codex_when_structured_lane_runs_then_prompt_delivered_via_stdin_not_argv
# and
# test_given_fake_codex_success_with_expected_marker_when_structured_lane_runs_then_exit0_and_causal_evidence_null
# were removed -- both exercised the structured ``codex`` runtime lane
# (run_structured_codex / preflight_codex_flags), which was retired along
# with the native Codex CLI ``codex`` runtime; argparse now rejects
# --runtime codex before any preflight runs.
# ---------------------------------------------------------------------------
# AC9 / postcondition (Issue #1921 P0-5: full repository fingerprint)
# ---------------------------------------------------------------------------


def test_given_require_clean_postcondition_and_unexpected_write_when_lane_runs_then_exit1(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + """
cat > /dev/null
echo "unexpected" > rogue-file.txt
echo '{"type":"result"}'
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = worktree / "artifacts" / "runtime-smoke" / "run1"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--require-clean-postcondition",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1
    assert "unexpected postcondition" in result.stderr
    (worktree / "rogue-file.txt").unlink()


def test_given_evidence_only_write_when_postcondition_checked_then_ignored_and_exit0(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + """
cat > /dev/null
echo '{"type":"result"}'
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = worktree / "artifacts" / "runtime-smoke" / "run2"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--require-clean-postcondition",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr


def test_given_output_dir_prefix_sibling_path_when_postcondition_checked_then_not_ignored(
    repo_with_worktree, tmp_path
):
    """A path that merely shares the output directory name as a *string*
    prefix (e.g. ``artifacts/runtime-smoke/run3-evil``) must not be silently
    excluded just because ``artifacts/runtime-smoke/run3`` is the evidence
    directory (Issue #1921 P0-5: exact directory-boundary matching, not
    ``str.startswith`` on the raw output-dir string)."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + """
cat > /dev/null
echo "unexpected" > artifacts/runtime-smoke/run3-evil-sibling.txt
echo '{"type":"result"}'
exit 0
""")
    prompt = _prompt_file(tmp_path)
    (worktree / "artifacts" / "runtime-smoke").mkdir(parents=True)
    out_dir = worktree / "artifacts" / "runtime-smoke" / "run3"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--require-clean-postcondition",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1
    assert "unexpected postcondition" in result.stderr
    (worktree / "artifacts" / "runtime-smoke" / "run3-evil-sibling.txt").unlink()


def test_given_already_dirty_file_further_modified_when_postcondition_checked_then_detected(
    repo_with_worktree, tmp_path
):
    """A file that was already dirty (status ``M``) before the run, and gets
    further modified by the agent (status stays ``M``), must still be
    detected — a status-line-set diff alone cannot see this (Issue #1921
    P0-5)."""
    repo, worktree = repo_with_worktree
    (worktree / "README.md").write_text("pre-dirty content\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + """
cat > /dev/null
echo "further changed content" > README.md
echo '{"type":"result"}'
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = worktree / "artifacts" / "runtime-smoke" / "dirty-run"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--require-clean-postcondition",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1
    assert "README.md" in result.stderr
    (worktree / "README.md").write_text("seed\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# AC5 / AC6: interactive herdr lane — isolated named session
# (Issue #1921 P0-1..P0-4 fix-delta)
# ---------------------------------------------------------------------------


# Fake herdr models an isolated named session as filesystem markers under
# $FAKE_HERDR_STATE_DIR: "<name>.session" (exists) / "<name>.stopped"
# (stopped). ``herdr session list --json`` reflects that state so the
# collision-check / creation / cleanup-confirmation flow can be exercised
# without a real herdr daemon.
_FAKE_ISOLATED_HERDR_BODY = """
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
        echo '{"result":{"root_pane":{"pane_id":"pane-xyz"},"workspace":{"workspace_id":"w1"}}}'
        exit 0
        ;;
    esac
    ;;
  agent)
    case "$2" in
      start)
        if [ -n "${FAKE_HERDR_AGENT_START_LOG:-}" ]; then
          printf '%s\\n' "$@" > "$FAKE_HERDR_AGENT_START_LOG"
        fi
        exit 0
        ;;
      prompt) exit 0 ;;
      get) echo '{"state":"idle"}'; exit 0 ;;
      explain) echo '{"agent":"claude","confidence":"high"}'; exit 0 ;;
      read) echo "OBSERVED_MARKER pane transcript line"; exit 0 ;;
    esac
    ;;
  api)
    case "$2" in
      snapshot)
        # Issue #2174 AC7: deterministic, unchanging workspace/agent/focus
        # snapshot -- the same content on every call means before/after
        # comparisons in the runner under test always observe zero drift.
        echo '{"result":{"snapshot":{"agents":[],"focused_workspace_id":"w0",'\
'"focused_tab_id":"w0:t0","focused_pane_id":"w0:p0"}}}'
        exit 0
        ;;
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


def test_given_nested_herdr_refusal_when_interactive_lane_runs_then_exit77_has_bounded_reason_code(
    repo_with_worktree, tmp_path
):
    """An unavailable isolated session is a SKIP, not a snapshot-preservation
    failure and never a fallback to an ambient human namespace."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    nested_refusal_body = _FAKE_ISOLATED_HERDR_BODY.replace(
        '  touch "$STATE_DIR/$2.session"\n  sleep 300\n  exit 0',
        '  printf "%s\\n" "nested herdr is disabled by default" 1>&2\n  exit 1',
    )
    _write_fake_exe(fake_bin / "herdr", nested_refusal_body)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\\n")
    evidence_json = tmp_path / "interactive-evidence.json"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(_prompt_file(tmp_path)), "--output-dir", str(tmp_path / "out"),
        "--evidence-json", str(evidence_json),
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(tmp_path / "herdr-state")},
    )

    assert result.returncode == 77
    evidence = json.loads(evidence_json.read_text(encoding="utf-8"))
    assert evidence["runtime_skip_reason_code"] == "herdr_isolated_session_unavailable"
    assert evidence["herdr_namespace_isolated"] is None
    assert evidence["preexisting_herdr_preserved"] is None


def test_given_native_interactive_lane_when_herdr_starts_agent_then_fixed_policy_uses_documented_passthrough(
    repo_with_worktree, tmp_path
):
    """The controlled isolated session records only the agent-start argv.
    No human or independent peer is created or observed."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\\n")
    start_log = tmp_path / "agent-start.argv"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(_prompt_file(tmp_path)), "--output-dir", str(tmp_path / "out"),
        fake_bin_dir=fake_bin,
        extra_env={
            "HERDR_ENV": "1",
            "FAKE_HERDR_STATE_DIR": str(tmp_path / "herdr-state"),
            "FAKE_HERDR_AGENT_START_LOG": str(start_log),
        },
    )

    assert result.returncode == 0, result.stderr
    argv = start_log.read_text(encoding="utf-8").splitlines()
    separator = argv.index("--")
    assert argv[separator + 1] == "--settings"
    settings = json.loads(argv[separator + 2])
    assert settings["crossSessionInbound"] == "refuse"
    assert settings["permissions"]["deny"] == ["SendMessage", "ListAgents"]


def test_given_isolated_herdr_lane_when_interactive_runs_then_session_created_and_cleaned_up(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY)
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
    assert "detected_agent: claude" in summary
    assert "cleanup_confirmed_removed: True" in summary
    assert "session_name: rts-" in summary
    # Only summary.md is persisted (no pane-output.txt / agent-detection.json).
    assert sorted(p.name for p in out_dir.iterdir()) == ["summary.md"]
    # Session marker files must be gone after cleanup.
    assert not list(state_dir.glob("*.session"))


def test_given_sigterm_during_interactive_lane_when_process_killed_then_isolated_session_still_cleaned_up(
    repo_with_worktree, tmp_path
):
    """SIGTERM's default disposition terminates a process immediately without
    running Python ``finally`` blocks. The runner must install a handler that
    converts SIGTERM into a raised exception so isolated-session cleanup
    (Issue #1921 P0-2) still runs even under a forceful external kill."""
    import signal
    import time as _time

    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # ``agent prompt`` sleeps well past our SIGTERM, so the runner is
    # guaranteed to still be inside run_interactive_herdr_isolated's try
    # block when the signal arrives.
    body = _FAKE_ISOLATED_HERDR_BODY.replace("prompt) exit 0 ;;", "prompt) sleep 30; exit 0 ;;")
    _write_fake_exe(fake_bin / "herdr", body)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"

    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["HERDR_ENV"] = "1"
    env["FAKE_HERDR_STATE_DIR"] = str(state_dir)

    proc = subprocess.Popen(
        [
            sys.executable, str(SCRIPT),
            "--repo-root", str(repo), "--worktree", str(worktree),
            "--runtime", "claude", "--mode", "interactive",
            "--prompt-file", str(prompt), "--output-dir", str(out_dir),
            "--timeout-seconds", "60",
        ],
        cwd=str(repo), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # Give the runner time to create the isolated session and reach the
    # (sleeping) ``agent prompt`` call before terminating it.
    deadline = _time.monotonic() + 10.0
    session_created = False
    while _time.monotonic() < deadline:
        if list(state_dir.glob("*.session")):
            session_created = True
            break
        _time.sleep(0.1)
    assert session_created, "isolated session was never created before SIGTERM"

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=15.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("runner did not exit within 15s of SIGTERM")

    assert proc.returncode == 1
    # Cleanup must have run: no dangling isolated session marker files.
    assert not list(state_dir.glob("*.session")), "isolated session leaked after SIGTERM"


def test_given_two_isolated_interactive_runs_when_executed_sequentially_then_distinct_sessions_used(
    repo_with_worktree, tmp_path
):
    """Distinct named sessions per run demonstrate the ownership boundary
    required for safe concurrent execution (Issue #1921 P0-4): each run's
    cleanup only ever targets the session name it created itself."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    state_dir = tmp_path / "herdr-state"
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")

    session_names = []
    for i in range(2):
        out_dir = tmp_path / f"out{i}"
        result = _run(
            repo, worktree,
            "--runtime", "claude", "--mode", "interactive",
            "--prompt-file", str(prompt), "--output-dir", str(out_dir),
            fake_bin_dir=fake_bin,
            extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(state_dir)},
        )
        assert result.returncode == 0, result.stderr
        summary = (out_dir / "summary.md").read_text(encoding="utf-8")
        for line in summary.splitlines():
            if line.startswith("- session_name:"):
                session_names.append(line.split(":", 1)[1].strip())
    assert len(session_names) == 2
    assert session_names[0] != session_names[1]


def test_given_herdr_env_unset_when_interactive_lane_runs_then_skip77(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_BODY)
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        fake_bin_dir=fake_bin,
        # Explicitly override (not merely omit) HERDR_ENV so a real ambient
        # HERDR_ENV=1 in the outer test-runner environment cannot leak in.
        extra_env={"HERDR_ENV": ""},
    )
    assert result.returncode == 77
    assert "HERDR_ENV" in result.stderr


def test_given_agent_state_blocked_when_interactive_lane_runs_then_not_treated_as_pass(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    body = _FAKE_ISOLATED_HERDR_BODY.replace('echo \'{"state":"idle"}\'', 'echo \'{"state":"blocked"}\'')
    _write_fake_exe(fake_bin / "herdr", body)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(tmp_path / "herdr-state")},
    )
    assert result.returncode == 1
    assert "blocked" in result.stderr


def test_given_agent_state_unknown_when_interactive_lane_runs_then_not_treated_as_pass(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    body = _FAKE_ISOLATED_HERDR_BODY.replace('echo \'{"state":"idle"}\'', 'echo \'{"state":"unknown"}\'')
    _write_fake_exe(fake_bin / "herdr", body)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(tmp_path / "herdr-state")},
    )
    assert result.returncode in (1, 77)
    assert "unusable" in result.stderr or "unknown" in result.stderr


def test_given_isolated_session_cleanup_not_confirmed_removed_when_lane_finishes_then_exit1(
    repo_with_worktree, tmp_path
):
    """Cleanup that cannot be confirmed removed must override an otherwise
    successful run to FAIL (Issue #1921 P0-2 fix-delta)."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # ``session delete`` is a no-op: the session marker is never removed, so
    # the post-cleanup ``session list --json`` still reports it.
    body = _FAKE_ISOLATED_HERDR_BODY.replace(
        "      delete)\n        rm -f \"$STATE_DIR/$3.session\" \"$STATE_DIR/$3.stopped\"\n        exit 0\n        ;;",
        "      delete)\n        exit 0\n        ;;",
    )
    _write_fake_exe(fake_bin / "herdr", body)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(tmp_path / "herdr-state")},
    )
    assert result.returncode == 1
    assert "cleanup" in result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "cleanup_confirmed_removed: False" in summary


# ---------------------------------------------------------------------------
# Session collision avoidance (Issue #1921 P0-4) — unit-level, exercised via
# direct import since it requires deterministic control over uuid4() to
# force a collision on the first candidate.
# ---------------------------------------------------------------------------


class _FakeUUID:
    def __init__(self, hex_value: str):
        self.hex = hex_value


def test_given_first_candidate_collides_when_new_session_name_generated_then_retries_until_unique(monkeypatch):
    module = _load_module()
    taken = f"rts-{'1' * 32}"[:32]
    fresh = f"rts-{'2' * 32}"[:32]
    calls = {"n": 0}

    def fake_uuid4():
        calls["n"] += 1
        return _FakeUUID("1" * 32 if calls["n"] == 1 else "2" * 32)

    def fake_names(_herdr_bin, env=None):
        return {taken}

    monkeypatch.setattr(module.uuid, "uuid4", fake_uuid4)
    monkeypatch.setattr(module, "_herdr_session_names", fake_names)

    name = module.new_isolated_session_name("herdr")
    assert name == fresh
    assert calls["n"] == 2


def test_given_collision_check_cannot_enumerate_sessions_when_generating_name_then_hard_error(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "_herdr_session_names", lambda _herdr_bin, env=None: None)
    with pytest.raises(module.HerdrLaneError):
        module.new_isolated_session_name("herdr")


# ---------------------------------------------------------------------------
# Fix-delta iteration 2: Claude Code multi-line prompt paste-collapse stall
# recovery (Issue #1887 PR #1921 review). ``herdr agent prompt`` can leave a
# multi-line prompt sitting as an unsubmitted "[Pasted text #N +M lines]"
# block in Claude Code's input box instead of submitting it, which herdr
# reports as ``agent_prompt_stalled`` after its own 5000ms post-submission
# state-change check. The runner must recover deterministically (send an
# explicit ``enter`` keypress, then re-observe via ``agent wait``) instead of
# treating the stall as a hard failure or silently downgrading it to SKIP.
# ---------------------------------------------------------------------------


_FAKE_ISOLATED_HERDR_STALL_THEN_RECOVER_BODY = """
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
        echo '{"result":{"root_pane":{"pane_id":"pane-xyz"},"workspace":{"workspace_id":"w1"}}}'
        exit 0
        ;;
    esac
    ;;
  agent)
    case "$2" in
      start) exit 0 ;;
      prompt)
        echo '{"error":{"code":"agent_prompt_stalled","message":"no state change within 5000 ms"}}' 1>&2
        exit 1
        ;;
      send-keys) exit 0 ;;
      wait) exit 0 ;;
      get) echo '{"state":"done"}'; exit 0 ;;
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


def test_given_agent_prompt_stalled_when_recovery_send_keys_and_wait_succeed_then_exit0_and_recovery_recorded(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_ISOLATED_HERDR_STALL_THEN_RECOVER_BODY)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    prompt = _prompt_file(tmp_path, "line one\nline two\nOBSERVED_MARKER\n")
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--expect-marker", "OBSERVED_MARKER",
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(tmp_path / "herdr-state")},
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "prompt_stall_recovered: True" in summary


def test_given_agent_prompt_stalled_when_recovery_send_keys_fails_then_exit1_not_skip(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    body = _FAKE_ISOLATED_HERDR_STALL_THEN_RECOVER_BODY.replace(
        "send-keys) exit 0 ;;", "send-keys) exit 1 ;;"
    )
    _write_fake_exe(fake_bin / "herdr", body)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    prompt = _prompt_file(tmp_path, "line one\nline two\n")
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(tmp_path / "herdr-state")},
    )
    assert result.returncode == 1
    assert "recovery send-keys failed" in result.stderr


def test_given_agent_prompt_stalled_when_recovery_wait_also_stalls_then_exit1_not_skip(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    body = _FAKE_ISOLATED_HERDR_STALL_THEN_RECOVER_BODY.replace(
        "wait) exit 0 ;;", "wait) exit 1 ;;"
    )
    _write_fake_exe(fake_bin / "herdr", body)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    prompt = _prompt_file(tmp_path, "line one\nline two\n")
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(tmp_path / "herdr-state")},
    )
    assert result.returncode == 1
    assert "recovery wait failed" in result.stderr


def test_given_agent_prompt_stalled_and_recovery_never_observes_state_change_then_exit1_not_false_pass(
    repo_with_worktree, tmp_path
):
    """``herdr agent wait`` matches immediately if the agent is already idle
    at call time (no observed-change requirement), so a naive
    prompt->send-keys->wait recovery can report success even when the
    paste-collapsed prompt was never actually submitted. The runner must
    poll for a genuine ``state_change_seq`` change before trusting
    ``agent wait``; a fake herdr whose ``state_change_seq`` never changes
    (and whose ``agent wait`` would otherwise trivially match) must still
    surface a hard failure, not a false pass."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    get_call_log = tmp_path / "get_calls.log"
    state_dir = tmp_path / "herdr-state"
    body = """
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
        echo '{"result":{"root_pane":{"pane_id":"pane-xyz"},"workspace":{"workspace_id":"w1"}}}'
        exit 0
        ;;
    esac
    ;;
  agent)
    case "$2" in
      start) exit 0 ;;
      prompt)
        echo '{"error":{"code":"agent_prompt_stalled","message":"no state change within 5000 ms"}}' 1>&2
        exit 1
        ;;
      send-keys) exit 0 ;;
      wait) exit 0 ;;
      get)
        echo "called" >> "%s"
        echo '{"result":{"agent":{"state_change_seq":42}}}'
        exit 0
        ;;
      explain) echo '{"agent":"claude","confidence":"high"}'; exit 0 ;;
      read) echo "pane transcript"; exit 0 ;;
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
""" % (get_call_log,)
    _write_fake_exe(fake_bin / "herdr", body)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    prompt = _prompt_file(tmp_path, "line one\nline two\n")
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        "--timeout-seconds", "3",
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 1
    assert "no observed state change" in result.stderr
    # ``agent get`` must have been polled (baseline + at least one recheck)
    # rather than trusting a single, possibly-stale ``agent wait`` match.
    assert get_call_log.read_text(encoding="utf-8").count("called") >= 2


def test_given_agent_prompt_fails_for_non_stall_reason_when_lane_runs_then_no_recovery_attempted(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    send_keys_log = tmp_path / "send_keys.log"
    state_dir = tmp_path / "herdr-state"
    body = """
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
        echo '{"result":{"root_pane":{"pane_id":"pane-xyz"},"workspace":{"workspace_id":"w1"}}}'
        exit 0
        ;;
    esac
    ;;
  agent)
    case "$2" in
      start) exit 0 ;;
      prompt)
        echo '{"error":{"code":"agent_pane_gone","message":"pane no longer exists"}}' 1>&2
        exit 1
        ;;
      send-keys) echo called >> "%s"; exit 0 ;;
      wait) exit 0 ;;
      get) echo '{"state":"done"}'; exit 0 ;;
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
""" % (send_keys_log,)
    _write_fake_exe(fake_bin / "herdr", body)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    prompt = _prompt_file(tmp_path, "single line prompt\n")
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1", "FAKE_HERDR_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 1
    assert "agent_pane_gone" in result.stderr
    assert not send_keys_log.exists()


# ---------------------------------------------------------------------------
# AC6 / AC7: evidence hygiene, redaction, session-log metadata
# (Issue #1921 P1 fix-delta: summary.md only, no raw native event / pane
# transcript / agent-explain persistence)
# ---------------------------------------------------------------------------


def test_given_home_path_and_long_token_in_error_output_when_evidence_written_then_redacted(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    secret_token = "A" * 60
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + f"""
cat > /dev/null
echo "failure at /home/someone/.ssh/id_rsa token={secret_token}" 1>&2
exit 1
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1
    summary_text = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "/home/someone" not in summary_text
    assert secret_token not in summary_text
    assert "<redacted>" in summary_text


def test_given_home_paths_in_nested_schema_fields_when_evidence_written_then_redacted(tmp_path):
    module = _load_module()
    output_dir = tmp_path / "out"

    module.write_evidence(
        output_dir,
        schema_summary={
            "resolved_executable": "/home/someone/.local/bin/claude",
            "subagent_causal_evidence": {
                "agent_transcript_path": "/home/someone/.claude/projects/project/agent.jsonl",
            },
            "worktree": ".claude/worktrees/issue-2437-fixture",
        },
    )

    summary_text = (output_dir / "summary.md").read_text(encoding="utf-8")
    assert "/home/someone" not in summary_text
    assert "<redacted>" in summary_text
    assert ".claude/worktrees/issue-2437-fixture" in summary_text


def test_given_secret_like_hex_tokens_when_redacted_then_only_named_public_evidence_is_preserved():
    module = _load_module()
    poison_hex_40 = "a" * 40
    poison_hex_64 = "b" * 64
    ordinary_long_token = "Z" * 60

    assert module._redact(poison_hex_40) == "<redacted>"
    assert module._redact(poison_hex_64) == "<redacted>"
    assert module._redact(ordinary_long_token) == "<redacted>"

    evidence = module._redact_evidence_value(
        {
            "untrusted_hex_40": poison_hex_40,
            "untrusted_hex_64": poison_hex_64,
            "untrusted_long_token": ordinary_long_token,
            "tested_head": poison_hex_40,
            "prompt_sha256": poison_hex_64,
            "mutation_boundary": {"settings_digest_sha256": "c" * 64},
        }
    )
    assert evidence["untrusted_hex_40"] == "<redacted>"
    assert evidence["untrusted_hex_64"] == "<redacted>"
    assert evidence["untrusted_long_token"] == "<redacted>"
    assert evidence["tested_head"] == poison_hex_40
    assert evidence["prompt_sha256"] == poison_hex_64
    assert evidence["mutation_boundary"]["settings_digest_sha256"] == "c" * 64


def test_given_ansi_escape_codes_in_error_output_when_evidence_written_then_stripped(
    repo_with_worktree, tmp_path
):
    """A real herdr binary emits ANSI color codes on stderr (observed live
    against herdr v0.7.5). These are cosmetic noise, not secrets, but must
    not be persisted verbatim into evidence."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + """
cat > /dev/null
printf '\\033[1merror:\\033[0m nested herdr is disabled\\n' 1>&2
exit 1
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1
    summary_text = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "\x1b[" not in summary_text
    assert "nested herdr is disabled" in summary_text


def test_given_raw_prompt_text_when_evidence_written_then_prompt_body_not_persisted(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + """
cat > /dev/null
echo '{"type":"result"}'
exit 0
""")
    secret_prompt = "UNIQUE_RAW_PROMPT_SENTINEL_TEXT"
    prompt = _prompt_file(tmp_path, secret_prompt + "\n")
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    for evidence_file in out_dir.glob("*"):
        assert secret_prompt not in evidence_file.read_text(encoding="utf-8")


def test_given_evidence_written_when_output_dir_listed_then_only_summary_present(
    repo_with_worktree, tmp_path
):
    """No native-events.jsonl / pane-output.txt / agent-detection.json /
    session-log-metadata.txt is persisted (Issue #1921 P1 fix-delta)."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + """
cat > /dev/null
for i in $(seq 1 5); do echo "{\\"type\\":\\"event\\",\\"i\\":$i}"; done
echo '{"type":"result"}'
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--inspect-session-log-metadata",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in out_dir.iterdir()) == ["summary.md"]


def test_given_require_session_log_metadata_and_unavailable_when_lane_runs_then_skip(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + """
cat > /dev/null
echo 'not-json-output-at-all'
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--require-session-log-metadata",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 77
    assert "session-log metadata" in result.stderr


def test_given_inspect_session_log_metadata_when_events_have_allowlist_fields_then_count_recorded(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + """
cat > /dev/null
echo '{"type":"hook_event","subagent":"test-runner","timestamp":"2026-01-01T00:00:00Z","reasoning":"should not leak"}'
echo '{"type":"result"}'
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--inspect-session-log-metadata",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    summary_text = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "session_log_metadata_count: 2" in summary_text
    assert "should not leak" not in summary_text
    assert "test-runner" not in summary_text


# ---------------------------------------------------------------------------
# Fix-delta iteration 1: --repo-root default resolution from inside a linked
# worktree (Issue #1887 PR #1921 review).
# ---------------------------------------------------------------------------


def test_given_script_checked_out_inside_linked_worktree_when_invoked_without_repo_root_then_not_rejected_as_root(
    repo_with_worktree_and_script, tmp_path
):
    repo, worktree = repo_with_worktree_and_script
    script_in_worktree = worktree / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
    assert script_in_worktree.is_file()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # PR #2220 review fix-delta: --expect-marker below now requires
    # correlated SubagentStart/SubagentStop hook evidence by default in
    # the structured lane.
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + FAKE_CLAUDE_SUCCESS_BODY_WITH_SUBAGENT_EVIDENCE)
    prompt = _prompt_file(tmp_path, "MARKER_TOKEN_WT\n")
    out_dir = worktree / "artifacts" / "runtime-smoke" / "claude-structured"
    env = dict(os.environ)
    path_key = "PATH"
    env[path_key] = str(fake_bin) + ":" + env[path_key]
    # Invoke the script's own on-disk copy that lives *inside the worktree*,
    # from the worktree cwd, without --repo-root -- this is exactly the
    # real-world invocation pattern for Issue #1887 AC3-AC7.
    result = subprocess.run(
        [
            sys.executable,
            str(script_in_worktree),
            "--worktree", str(worktree),
            "--runtime", "claude", "--mode", "structured",
            "--prompt-file", str(prompt), "--output-dir", str(out_dir),
            "--timeout-seconds", "30", "--expect-marker", "MARKER_TOKEN_WT",
        ],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert "root checkout rejected" not in result.stderr
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# AC8: exit code matrix sanity
# ---------------------------------------------------------------------------


def test_given_no_flags_when_runner_invoked_without_required_args_then_exits_nonzero():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0


def test_given_repo_when_runner_invoked_with_removed_transport_flag_then_argparse_rejects(
    repo_with_worktree, tmp_path
):
    """``--transport`` was removed entirely (Issue #1921 P0-1/P1 fix-delta:
    structured lane is always direct, interactive lane is always an isolated
    herdr session — there is no longer a caller-selectable transport)."""
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured", "--transport", "direct",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
    )
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr


def test_given_repo_when_runner_invoked_with_removed_keep_pane_flag_then_argparse_rejects(
    repo_with_worktree, tmp_path
):
    """``--keep-pane`` was removed entirely (Issue #1921 P0-2 fix-delta:
    cleanup is unconditional and success-verified, never opt-out)."""
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive", "--keep-pane",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
    )
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr


# ---------------------------------------------------------------------------
# Root Codex skill-directory symlink contract (AC1)
# ---------------------------------------------------------------------------


def test_given_repo_when_root_skill_symlink_checked_then_reads_canonical_body():
    surface = REPO_ROOT / ".agents" / "skills"
    wrapper = surface / "worktree-agent-runtime-smoke" / "SKILL.md"
    canonical = REPO_ROOT / ".claude" / "skills" / "worktree-agent-runtime-smoke" / "SKILL.md"
    assert surface.is_symlink()
    assert surface.readlink().as_posix() == "../.claude/skills"
    assert wrapper.is_file()
    assert canonical.is_file()
    assert wrapper.samefile(canonical)
    assert wrapper.read_text(encoding="utf-8") == canonical.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Issue #1733 Scope Delta (2026-08-02, owner-approved harness extension):
# structured telemetry fields -- tested_head / runtime_version /
# requested_agent_type / effective_agent_type / loaded_skills / spawn_events /
# child_spawn_event_count / self_restart_event_count /
# orchestration_action_count / prompt_sha256.
# ---------------------------------------------------------------------------


_FAKE_CLAUDE_VERSION_BRANCH = """
if [ "$1" = "--version" ]; then
  echo "2.1.220 (Claude Code)"
  exit 0
fi
"""


def _write_fake_agent_md(checkout_root: Path, agent_type: str, skills: list[str]) -> None:
    agents_dir = checkout_root / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    skills_yaml = "\n".join(f"  - {s}" for s in skills)
    (agents_dir / f"{agent_type}.md").write_text(
        f"---\nname: {agent_type}\nskills:\n{skills_yaml}\n---\n\nbody\n",
        encoding="utf-8",
    )


def test_given_agent_type_flag_and_declared_agent_md_when_structured_run_succeeds_then_loaded_skills_recorded(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    _write_fake_agent_md(worktree, "post-merge-cleanup-worker", ["post-merge-cleanup-executor"])
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + _FAKE_CLAUDE_VERSION_BRANCH + FAKE_CLAUDE_SUCCESS_BODY)
    prompt = _prompt_file(tmp_path, "MARKER_TOKEN_WT\n")
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--agent-type", "post-merge-cleanup-worker",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "requested_agent_type: post-merge-cleanup-worker" in summary
    assert "effective_agent_type: None" in summary
    assert "loaded_skills: ['post-merge-cleanup-executor']" in summary
    assert "loaded_skills_source: static_frontmatter" in summary


def test_given_no_agent_type_flag_when_structured_run_succeeds_then_defaults_to_unspecified_and_loaded_skills_none(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + _FAKE_CLAUDE_VERSION_BRANCH + FAKE_CLAUDE_SUCCESS_BODY)
    prompt = _prompt_file(tmp_path, "MARKER_TOKEN_WT\n")
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "requested_agent_type: unspecified" in summary
    assert "effective_agent_type: None" in summary
    assert "loaded_skills: None" in summary
    assert "loaded_skills_source: None" in summary


def test_given_fake_claude_success_when_structured_run_then_tested_head_runtime_version_prompt_sha256_recorded(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + _FAKE_CLAUDE_VERSION_BRANCH + FAKE_CLAUDE_SUCCESS_BODY)
    prompt_text = "MARKER_TOKEN_WT\n"
    prompt = _prompt_file(tmp_path, prompt_text)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    expected_head = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert f"tested_head: {expected_head}" in summary
    assert "runtime_version: 2.1.220 (Claude Code)" in summary
    import hashlib

    expected_sha256 = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    assert f"prompt_sha256: {expected_sha256}" in summary


def test_given_claude_agent_tool_use_event_when_structured_run_then_child_spawn_event_count_recorded(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + _FAKE_CLAUDE_VERSION_BRANCH + """
cat > /dev/null
echo '{"type":"assistant","message":{"content":[{"type":"tool_use",'\
'"name":"Agent","input":{"subagent_type":"implementation-worker"}}]}}'
echo '{"type":"result"}'
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "child_spawn_event_count: 1" in summary
    assert "spawn_events: [{'runtime': 'claude', 'tool': 'Agent'}]" in summary
    # No raw prompt/task content (e.g. the subagent_type input value) leaks
    # into the allowlist-only evidence (evidence-hygiene discipline).
    assert "implementation-worker" not in summary


def test_given_claude_bash_self_restart_command_when_structured_run_then_self_restart_event_count_recorded(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + _FAKE_CLAUDE_VERSION_BRANCH + """
cat > /dev/null
echo '{"type":"assistant","message":{"content":[{"type":"tool_use",'\
'"name":"Bash","input":{"command":"claude -p --output-format stream-json"}}]}}'
echo '{"type":"result"}'
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "self_restart_event_count: 1" in summary
    assert "child_spawn_event_count: 0" in summary


def test_given_claude_bash_orchestration_command_when_structured_run_then_orchestration_action_count_recorded(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + _FAKE_CLAUDE_VERSION_BRANCH + """
cat > /dev/null
echo '{"type":"assistant","message":{"content":[{"type":"tool_use",'\
'"name":"Bash","input":{"command":"gh issue close 1234"}}]}}'
echo '{"type":"result"}'
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "orchestration_action_count: 1" in summary


# Issue #2161 (native Codex CLI retirement):
# test_given_codex_collab_tool_call_item_when_structured_run_then_child_spawn_event_count_recorded
# was removed -- it exercised classify_codex_events()'s
# ``collab_tool_call`` detection, which was retired along with the
# ``codex`` runtime lane.


# ---------------------------------------------------------------------------
# Unit-level coverage for the classification / derivation helpers themselves
# (direct import, per the established pattern for internal-function tests).
# ---------------------------------------------------------------------------


def test_given_no_matching_events_when_classify_claude_events_then_empty_result():
    module = _load_module()
    stdout = "\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "result"}),
    ])
    spawn_events, self_restart, orchestration = module.classify_claude_events(stdout)
    assert spawn_events == []
    assert self_restart == 0
    assert orchestration == 0


def test_given_non_bash_non_agent_tool_use_when_classify_claude_events_then_not_counted():
    module = _load_module()
    stdout = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}}]},
    })
    spawn_events, self_restart, orchestration = module.classify_claude_events(stdout)
    assert spawn_events == []
    assert self_restart == 0
    assert orchestration == 0


def test_given_agent_type_and_missing_agent_md_when_load_static_declared_skills_then_none(tmp_path):
    module = _load_module()
    assert module.load_static_declared_skills(str(tmp_path), "does-not-exist-worker") is None


def test_given_unspecified_agent_type_when_load_static_declared_skills_then_none(tmp_path):
    module = _load_module()
    assert module.load_static_declared_skills(str(tmp_path), module._UNSPECIFIED_AGENT_TYPE) is None


def test_given_agent_md_with_skills_frontmatter_when_load_static_declared_skills_then_list_returned(tmp_path):
    module = _load_module()
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "some-worker.md").write_text(
        "---\nname: some-worker\nskills:\n  - alpha\n  - beta\n---\n\nbody\n", encoding="utf-8"
    )
    assert module.load_static_declared_skills(str(tmp_path), "some-worker") == ["alpha", "beta"]


def test_given_prompt_text_when_compute_prompt_sha256_then_matches_stdlib_hashlib():
    module = _load_module()
    import hashlib

    text = "hello world\n"
    assert module.compute_prompt_sha256(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Issue #2046 AC8: candidate_worktree_fixture wiring smoke (main_agent_identity
# / agent_definition are present in evidence even for a caller that does not
# request the hermetic lane -- they must honestly report unavailable, never
# be absent from the schema).
# ---------------------------------------------------------------------------


def test_given_candidate_worktree_fixture_when_non_hermetic_run_then_new_evidence_fields_present(
    candidate_worktree_fixture, tmp_path
):
    repo, worktree = candidate_worktree_fixture
    # Written directly on disk under the worktree (not committed): the new
    # evidence functions read static Agent/Skill files from the working
    # tree, not from a specific git ref, so no commit is required here.
    agents_dir = worktree / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "issue-creator.md").write_text(
        "---\nname: issue-creator\ndescription: fixture\nskills:\n  - create-issue\n---\nbody\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + FAKE_CLAUDE_SUCCESS_BODY)
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--agent-type", "issue-creator", "--claude-agent-name", "issue-creator",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "main_agent_identity" in summary
    assert "agent_definition" in summary
    assert "skill_evidence" in summary
    assert "mutation_boundary" in summary
    assert "settings_provenance" in summary
    assert "production_settings_lane" in summary
    # Non-hermetic project-discovery binding: status stays unavailable, but
    # the field itself is never omitted from the schema.
    assert "'binding_mode': 'project_discovery'" in summary


# ---------------------------------------------------------------------------
# Issue #1881 PR #2385 fix_delta -- Allowed Paths Scope Delta: 3 minimal,
# additive, backward-compatible extensions used by
# ``verify_pr_reviewer_permission_boundary.py`` (AC4/AC5 evidentiary
# mechanism). Function-level tests against synthetic stream-json event text,
# per the established pattern (see the "Unit-level coverage for the
# classification / derivation helpers themselves" section above).
# ---------------------------------------------------------------------------


def _system_init_line() -> str:
    return json.dumps({"type": "system", "subtype": "init", "session_id": "fixture-session"})


def _session_start_hook_json_payload_line(agent_type: str) -> str:
    """The pre-existing, byte-identical JSON-object recognition path."""
    official_payload = json.dumps(
        {"session_id": "fixture-session", "hook_event_name": "SessionStart", "agent_type": agent_type}
    )
    return json.dumps(
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_event": "SessionStart",
            "hook_name": "SessionStart",
            "session_id": "fixture-session",
            "stdout": official_payload,
            "output": official_payload,
        }
    )


def _session_start_hook_plain_marker_line(marker_text: str) -> str:
    """Extension 1: a plain-text SessionStart hook stdout/output marker
    (mirrors ``.claude/hooks/pr_reviewer_guard.py``'s ``observe-identity``
    opt-in probe channel output, e.g.
    ``reviewer-identity-observed agent_type=pr-reviewer``) -- NOT an
    embedded JSON object."""
    return json.dumps(
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_event": "SessionStart",
            "hook_name": "SessionStart",
            "session_id": "fixture-session",
            "stdout": marker_text,
            "output": marker_text,
        }
    )


def _read_tool_use_line(tool_use_id: str, file_path: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "session_id": "fixture-session",
            "message": {
                "content": [
                    {"type": "tool_use", "id": tool_use_id, "name": "Read", "input": {"file_path": file_path}}
                ]
            },
        }
    )


def _read_tool_result_line(tool_use_id: str, *, is_error: bool = False) -> str:
    return json.dumps(
        {
            "type": "user",
            "session_id": "fixture-session",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": is_error, "content": "ok"}
                ]
            },
        }
    )


def _result_event_line(permission_denials: list | None = None) -> str:
    payload: dict = {"type": "result", "subtype": "success", "session_id": "fixture-session"}
    if permission_denials is not None:
        payload["permission_denials"] = permission_denials
    return json.dumps(payload)


class TestExtension1PlainTextMarkerRecognition:
    """Issue #1881 Extension 1: ``extract_claude_session_start_identity``
    gains an additional, strictly-fallback plain-text ``agent_type=<value>``
    marker recognition path, tried only when the pre-existing JSON-object
    path finds nothing on the same text."""

    def test_plain_marker_is_recognized_as_fallback(self) -> None:
        module = _load_module()
        stdout = "\n".join(
            [
                _system_init_line(),
                _session_start_hook_plain_marker_line("reviewer-identity-observed agent_type=pr-reviewer"),
                _result_event_line(),
            ]
        )
        identity = module.extract_claude_session_start_identity(stdout)
        assert identity["agent_type"] == "pr-reviewer"
        assert identity["source"] == module.AGENT_TYPE_SOURCE_PLAIN_MARKER

    def test_json_payload_path_stays_byte_identical_and_takes_precedence(self) -> None:
        """The pre-existing JSON-object recognition path must remain
        untouched: when JSON parses successfully, the plain-text fallback
        must never run at all."""
        module = _load_module()
        stdout = "\n".join(
            [
                _system_init_line(),
                _session_start_hook_json_payload_line("issue-creator"),
                _result_event_line(),
            ]
        )
        identity = module.extract_claude_session_start_identity(stdout)
        assert identity["agent_type"] == "issue-creator"
        assert identity["source"] == module.AGENT_TYPE_SOURCE_HOOK_PAYLOAD

    def test_non_matching_plain_text_stays_unavailable(self) -> None:
        """Fail-closed: text with no JSON object and no ``agent_type=``
        marker never fabricates an agent_type."""
        module = _load_module()
        stdout = "\n".join(
            [
                _system_init_line(),
                _session_start_hook_plain_marker_line("some unrelated hook output"),
                _result_event_line(),
            ]
        )
        identity = module.extract_claude_session_start_identity(stdout)
        assert identity["agent_type"] is None
        assert identity["source"] is None

    def test_build_main_agent_identity_matches_via_plain_marker(self) -> None:
        """End-to-end through the consumer function: a plain-marker-only
        SessionStart hook is sufficient for ``main_agent_identity.matched``
        to become True for the requested persona."""
        module = _load_module()
        stdout = "\n".join(
            [
                _system_init_line(),
                _session_start_hook_plain_marker_line("reviewer-identity-observed agent_type=pr-reviewer"),
                _result_event_line(),
            ]
        )
        identity = module.build_main_agent_identity("pr-reviewer", stdout)
        assert identity["matched"] is True
        assert identity["observed"]["agent_type"] == "pr-reviewer"
        assert identity["observed"]["source"] == module.AGENT_TYPE_SOURCE_PLAIN_MARKER
        assert identity["status"] == module.EVIDENCE_STATUS_OBSERVED


class TestExtension2PrReviewerCanonicalPathAllowlist:
    """Issue #1881 Extension 2: a single ``pr-reviewer`` entry added to
    ``_PERSONA_CANONICAL_SKILL_PATH``; ``extract_claude_canonical_read_receipt``
    itself is genuinely persona-agnostic and untouched."""

    def test_pr_reviewer_entry_present_and_unchanged_others(self) -> None:
        module = _load_module()
        assert module._PERSONA_CANONICAL_SKILL_PATH["pr-reviewer"] == (
            ".claude/skills/pr-review-judge/references/allowed-paths-gate.md"
        )
        # Pre-existing entries are untouched.
        assert module._PERSONA_CANONICAL_SKILL_PATH["issue-creator"] == ".claude/skills/create-issue/SKILL.md"
        assert module._PERSONA_CANONICAL_SKILL_PATH["issue-editor"] == ".claude/skills/edit-issue/SKILL.md"

    def test_pr_reviewer_canonical_read_becomes_observed_on_successful_read(self, tmp_path: Path) -> None:
        module = _load_module()
        worktree = tmp_path / "worktree"
        rel_path = Path(".claude/skills/pr-review-judge/references/allowed-paths-gate.md")
        (worktree / rel_path.parent).mkdir(parents=True, exist_ok=True)
        (worktree / rel_path).write_text("# Allowed Paths Gate\n", encoding="utf-8")

        tool_use_id = "toolu_pr_reviewer_read_1"
        stdout = "\n".join(
            [
                _system_init_line(),
                _read_tool_use_line(tool_use_id, str(rel_path)),
                _read_tool_result_line(tool_use_id),
                _result_event_line(),
            ]
        )
        skill_evidence = module.build_skill_evidence("pr-reviewer", str(worktree), stdout)
        canonical_read = skill_evidence["canonical_read"]
        assert canonical_read["status"] == module.EVIDENCE_STATUS_OBSERVED
        assert canonical_read["observed_repo_relative_path"] == str(rel_path)
        assert canonical_read["read_result_status"] == "success"

    def test_other_persona_names_unaffected(self, tmp_path: Path) -> None:
        module = _load_module()
        assert module._PERSONA_CANONICAL_SKILL_PATH.get("general-purpose") is None


class TestExtension3PermissionDenialsExposure:
    """Issue #1881 Extension 3: ``extract_claude_permission_denials`` surfaces
    Claude Code's own native ``permission_denials`` array from the final
    ``type: "result"`` stream-json event -- purely additive, never gated
    behind ``--hermetic-agent-definition``, never touching
    ``mutation_boundary``."""

    def test_present_permission_denials_array_is_extracted_verbatim(self) -> None:
        module = _load_module()
        denials = [
            {
                "tool_name": "Bash",
                "tool_use_id": "toolu_denied_1",
                "tool_input": {"command": "git worktree prune --dry-run"},
            }
        ]
        stdout = "\n".join([_system_init_line(), _result_event_line(permission_denials=denials)])
        assert module.extract_claude_permission_denials(stdout) == denials

    def test_absent_permission_denials_field_defaults_to_empty_list(self) -> None:
        module = _load_module()
        stdout = "\n".join([_system_init_line(), _result_event_line()])
        assert module.extract_claude_permission_denials(stdout) == []

    def test_no_result_event_at_all_defaults_to_empty_list(self) -> None:
        module = _load_module()
        stdout = _system_init_line()
        assert module.extract_claude_permission_denials(stdout) == []

    def test_none_stdout_defaults_to_empty_list_never_raises(self) -> None:
        module = _load_module()
        assert module.extract_claude_permission_denials(None) == []

    def test_permission_denials_never_gated_by_hermetic_or_mutation_boundary(self) -> None:
        """Purely additive: the function signature takes only ``stdout`` --
        it has no hermetic/mutation_boundary parameter to gate behind at
        all, so this is a structural (signature-level), not merely
        behavioral, guarantee."""
        module = _load_module()
        import inspect

        params = list(inspect.signature(module.extract_claude_permission_denials).parameters)
        assert params == ["stdout"]
