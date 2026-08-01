from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"


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
  echo "--output-format --include-hook-events --no-session-persistence"
  exit 0
fi
"""


# ---------------------------------------------------------------------------
# AC2: worktree / repository identity rejection
# ---------------------------------------------------------------------------


def test_given_root_checkout_when_runner_starts_then_rejected(repo_with_worktree, tmp_path):
    repo, _worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, repo,
        "--runtime", "claude", "--mode", "structured", "--transport", "direct",
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
        "--runtime", "claude", "--mode", "structured", "--transport", "direct",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
    )
    assert result.returncode == 1
    assert "different repository" in result.stderr


def test_given_missing_worktree_path_when_runner_starts_then_rejected(repo_with_worktree, tmp_path):
    repo, _worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, repo / ".claude" / "worktrees" / "does-not-exist",
        "--runtime", "claude", "--mode", "structured", "--transport", "direct",
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
        "--runtime", "claude", "--mode", "structured", "--transport", "direct",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
    )
    assert result.returncode == 1


# ---------------------------------------------------------------------------
# AC3 / AC8: structured Claude lane — argv, exit codes, timeout
# ---------------------------------------------------------------------------


def test_given_fake_claude_success_when_structured_lane_runs_then_exit0_and_events_captured(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + """
cat > /dev/null
echo '{"type":"system","subtype":"init"}'
echo '{"type":"result","subtype":"success","marker":"MARKER_TOKEN_1"}'
exit 0
""")
    prompt = _prompt_file(tmp_path, "MARKER_TOKEN_1\n")
    out_dir = worktree / "artifacts" / "runtime-smoke" / "claude-structured"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured", "--transport", "direct",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--timeout-seconds", "30", "--expect-marker", "MARKER_TOKEN_1",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "native_event_count: 2" in summary
    events = (out_dir / "native-events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(events) == 2


def test_given_fake_claude_help_missing_flags_when_preflight_runs_then_skip77(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", """
if [ "$1" = "--help" ]; then
  echo "no relevant flags here"
  exit 0
fi
exit 0
""")
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured", "--transport", "direct",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 77
    assert result.stderr.startswith("SKIP:")


def test_given_no_claude_binary_when_preflight_runs_then_skip77(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured", "--transport", "direct",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        # Keep system dirs (git) reachable while excluding the real claude/codex
        # binaries that normally live under ~/.local/bin.
        extra_env={"PATH": f"{empty_bin}:/usr/bin:/bin"},
    )
    assert result.returncode == 77


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
        "--runtime", "claude", "--mode", "structured", "--transport", "direct",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1
    assert "exited non-zero" in result.stderr


def test_given_fake_claude_hangs_when_timeout_elapses_then_exit1_and_process_cleaned_up(
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
        "--runtime", "claude", "--mode", "structured", "--transport", "direct",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--timeout-seconds", "2",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1
    assert "timed out" in result.stderr


# ---------------------------------------------------------------------------
# AC4: structured Codex lane — argv (-C worktree --json --ephemeral)
# ---------------------------------------------------------------------------


def test_given_fake_codex_when_structured_lane_runs_then_argv_contains_worktree_cwd_json_ephemeral(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    argv_log = tmp_path / "codex_argv.txt"
    _write_fake_exe(fake_bin / "codex", f"""
if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then
  echo "--json --ephemeral -C"
  exit 0
fi
echo "$@" > "{argv_log}"
echo '{{"type":"item.completed"}}'
exit 0
""")
    prompt = _prompt_file(tmp_path, "codex prompt text")
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "codex", "--mode", "structured", "--transport", "direct",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    argv_text = argv_log.read_text(encoding="utf-8")
    assert "-C" in argv_text
    assert str(worktree) in argv_text
    assert "--json" in argv_text
    assert "--ephemeral" in argv_text
    assert "codex prompt text" in argv_text


# ---------------------------------------------------------------------------
# AC9 / postcondition
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
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = worktree / "artifacts" / "runtime-smoke" / "run1"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured", "--transport", "direct",
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
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = worktree / "artifacts" / "runtime-smoke" / "run2"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured", "--transport", "direct",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--require-clean-postcondition",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# AC5 / AC6: interactive herdr lane
# ---------------------------------------------------------------------------


_FAKE_HERDR_BODY = """
case "$1 $2" in
  "status server")
    exit 0
    ;;
esac
case "$1" in
  pane)
    case "$2" in
      split) echo '{"pane_id":"pane-xyz"}'; exit 0 ;;
      close) exit 0 ;;
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
esac
exit 0
"""


def test_given_fake_herdr_lane_when_interactive_runs_then_pane_lifecycle_completes(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_HERDR_BODY)
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + "exit 0\n")
    prompt = _prompt_file(tmp_path, "OBSERVED_MARKER\n")
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive", "--transport", "herdr",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--expect-marker", "OBSERVED_MARKER",
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert (out_dir / "pane-output.txt").exists()
    assert (out_dir / "agent-detection.json").exists()
    detection = json.loads((out_dir / "agent-detection.json").read_text(encoding="utf-8"))
    assert detection["agent"] == "claude"


def test_given_herdr_env_unset_when_interactive_lane_runs_then_skip77(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "herdr", _FAKE_HERDR_BODY)
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive", "--transport", "herdr",
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
    body = _FAKE_HERDR_BODY.replace('echo \'{"state":"idle"}\'', 'echo \'{"state":"blocked"}\'')
    _write_fake_exe(fake_bin / "herdr", body)
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive", "--transport", "herdr",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1"},
    )
    assert result.returncode == 1
    assert "blocked" in result.stderr


def test_given_agent_state_unknown_when_interactive_lane_runs_then_not_treated_as_pass(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    body = _FAKE_HERDR_BODY.replace('echo \'{"state":"idle"}\'', 'echo \'{"state":"unknown"}\'')
    _write_fake_exe(fake_bin / "herdr", body)
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive", "--transport", "herdr",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1"},
    )
    assert result.returncode in (1, 77)
    assert "unusable" in result.stderr or "unknown" in result.stderr


def test_given_pane_split_succeeds_when_lane_finishes_then_pane_close_invoked_for_cleanup(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    close_log = tmp_path / "close.log"
    body = _FAKE_HERDR_BODY.replace(
        'close) exit 0 ;;', f'close) echo "$3" >> "{close_log}"; exit 0 ;;'
    )
    _write_fake_exe(fake_bin / "herdr", body)
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive", "--transport", "herdr",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert close_log.exists()


def test_given_keep_pane_flag_when_lane_finishes_then_pane_close_not_invoked(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    close_log = tmp_path / "close.log"
    body = _FAKE_HERDR_BODY.replace(
        'close) exit 0 ;;', f'close) echo "$3" >> "{close_log}"; exit 0 ;;'
    )
    _write_fake_exe(fake_bin / "herdr", body)
    prompt = _prompt_file(tmp_path)
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive", "--transport", "herdr",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        "--keep-pane",
        fake_bin_dir=fake_bin,
        extra_env={"HERDR_ENV": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert not close_log.exists()


# ---------------------------------------------------------------------------
# AC6 / AC7: evidence hygiene, redaction, session-log metadata
# ---------------------------------------------------------------------------


def test_given_home_path_and_long_token_in_output_when_evidence_written_then_redacted(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    secret_token = "A" * 60
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + f"""
cat > /dev/null
echo '{{"type":"result","path":"/home/someone/.ssh/id_rsa","token":"{secret_token}"}}'
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured", "--transport", "direct",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    events_text = (out_dir / "native-events.jsonl").read_text(encoding="utf-8")
    assert "/home/someone" not in events_text
    assert secret_token not in events_text
    assert "<redacted>" in events_text


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
        "--runtime", "claude", "--mode", "structured", "--transport", "direct",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    for evidence_file in out_dir.glob("*"):
        assert secret_prompt not in evidence_file.read_text(encoding="utf-8")


def test_given_many_native_events_when_evidence_written_then_output_is_bounded(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + """
cat > /dev/null
for i in $(seq 1 1000); do echo "{\\"type\\":\\"event\\",\\"i\\":$i}"; done
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured", "--transport", "direct",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    events = (out_dir / "native-events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(events) <= 400


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
        "--runtime", "claude", "--mode", "structured", "--transport", "direct",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--require-session-log-metadata",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 77
    assert "session-log metadata" in result.stderr


def test_given_inspect_session_log_metadata_when_events_have_allowlist_fields_then_extracted(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + """
cat > /dev/null
echo '{"type":"hook_event","subagent":"test-runner","timestamp":"2026-01-01T00:00:00Z","reasoning":"should not leak"}'
exit 0
""")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured", "--transport", "direct",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--inspect-session-log-metadata",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    metadata_text = (out_dir / "session-log-metadata.txt").read_text(encoding="utf-8")
    assert "subagent" in metadata_text
    assert "should not leak" not in metadata_text


# ---------------------------------------------------------------------------
# AC8: exit code matrix sanity
# ---------------------------------------------------------------------------


def test_given_no_flags_when_runner_invoked_without_required_args_then_exits_nonzero():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# Thin Codex wrapper contract (AC1)
# ---------------------------------------------------------------------------


def test_given_repo_when_thin_wrapper_checked_then_points_to_canonical_body():
    wrapper = REPO_ROOT / ".agents" / "skills" / "worktree-agent-runtime-smoke" / "SKILL.md"
    canonical = REPO_ROOT / ".claude" / "skills" / "worktree-agent-runtime-smoke" / "SKILL.md"
    assert wrapper.is_file()
    assert canonical.is_file()
    text = wrapper.read_text(encoding="utf-8")
    assert "derived/non-canonical" in text
    assert "../../../.claude/skills/worktree-agent-runtime-smoke/SKILL.md" in text
    assert "## Procedure" not in text
