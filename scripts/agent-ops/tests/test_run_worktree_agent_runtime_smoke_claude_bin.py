"""Regression tests for Issue #2174 AC1/AC6: ``--claude-bin`` input.

AC1: ``run_worktree_agent_runtime_smoke.py`` accepts ``--claude-bin
<absolute path>`` and, when supplied, uses that absolute path directly as
the claude executable -- bypassing ``shutil.which("claude")`` PATH
resolution entirely.

AC6: when ``--claude-bin`` is NOT supplied, the pre-existing
``shutil.which("claude")`` PATH-resolution default behavior is unchanged.

This is a dedicated new test file (not appended to the pre-existing general
smoke suite), per Issue #1960 / #2174's Current Validated Scope test-location
convention (also followed by
``test_run_worktree_agent_runtime_smoke_runtime_evidence.py``).

These tests use a fake ``claude`` binary rather than the real Claude Code
CLI or a real ``herdr``/claude-gpt launcher (no live-environment dependency
in the general test suite). Live-environment runtime verification of
AC3/AC4 (the actual claude-gpt launcher, structured + isolated herdr
interactive lane) is a separate, human-observed step recorded in
``summary.md`` evidence per the Issue's Runtime Verification Applicability
section -- it is not simulated here.
"""

from __future__ import annotations

import hashlib
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


def _fake_claude_script(marker: Path, version: str = "1.2.3") -> str:
    return f"""
if [ "$1" = "--version" ]; then
  echo "{version} (Claude Code)"
  exit 0
fi
cat > /dev/null
touch "{marker}"
echo '{{"type":"result","subtype":"success"}}'
exit 0
"""


def _summary_field(summary: str, key: str) -> str:
    """Extract the ``- key: value`` line's value from a persisted summary.md.

    Issue #2421: ``resolved_executable`` is always ``<redacted>`` on success,
    so identity assertions must instead compare ``resolved_executable_sha256``
    against a fixture-local expected digest (never a raw path).
    """
    line = next(line for line in summary.splitlines() if line.startswith(f"- {key}:"))
    return line.split(":", 1)[1].strip()


def test_claude_bin_absolute_path_bypasses_path_resolution(repo_with_worktree, tmp_path):
    """AC1: with ``--claude-bin <absolute path>``, the runner uses that exact
    executable, never consulting ``PATH`` at all. A decoy ``claude`` on
    ``PATH`` (which would fail loudly if invoked) proves ``--claude-bin`` was
    used instead of any PATH-resolved binary."""
    repo, worktree = repo_with_worktree

    decoy_bin = tmp_path / "decoy-bin"
    decoy_bin.mkdir()
    decoy_marker = tmp_path / "decoy-invoked.marker"
    _write_fake_exe(decoy_bin / "claude", f'touch "{decoy_marker}"\necho "DECOY SHOULD NEVER RUN" >&2\nexit 99\n')

    override_dir = tmp_path / "claude-gpt-launcher"
    override_dir.mkdir()
    override_marker = tmp_path / "override-invoked.marker"
    override_bin = override_dir / "launch.sh"
    _write_fake_exe(override_bin, _fake_claude_script(override_marker, version="9.0.0-claude-gpt"))

    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--claude-bin", str(override_bin),
        fake_bin_dir=decoy_bin,
    )
    assert result.returncode == 0, result.stderr
    assert override_marker.exists(), "runner did not invoke the --claude-bin override executable"
    assert not decoy_marker.exists(), "runner invoked the PATH-resolved decoy instead of --claude-bin"
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "runtime_version: 9.0.0-claude-gpt" in summary
    # Issue #2421: resolved_executable is always redacted on success
    # (field-level special case, independent of the value's absolute-path
    # prefix). Identity is proven via resolved_executable_sha256 against the
    # fixture-local expected digest of the resolved executable's own bytes.
    assert _summary_field(summary, "resolved_executable") == "<redacted>"
    expected_sha256 = hashlib.sha256(Path(override_bin).read_bytes()).hexdigest()
    assert _summary_field(summary, "resolved_executable_sha256") == expected_sha256


def test_claude_bin_nonexecutable_path_is_skip_not_crash(repo_with_worktree, tmp_path):
    """AC1: an invalid ``--claude-bin`` (does not exist / not executable) is
    a controlled SKIP (exit 77), never a crash and never silently falling
    back to PATH resolution."""
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    missing_bin = tmp_path / "does-not-exist" / "launch.sh"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--claude-bin", str(missing_bin),
    )
    assert result.returncode == 77, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "resolved_executable: None" in summary


def test_claude_bin_rejects_relative_path(repo_with_worktree, tmp_path):
    """AC1: ``--claude-bin`` only accepts absolute paths; a relative path is
    an argparse usage error (exit 2), not a silently-accepted relative
    lookup."""
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--claude-bin", "relative/launch.sh",
    )
    assert result.returncode == 2, result.stderr
    assert "--claude-bin must be an absolute path" in result.stderr


def test_claude_bin_unspecified_default_behavior_is_unchanged(repo_with_worktree, tmp_path):
    """AC6: omitting ``--claude-bin`` entirely leaves the pre-existing
    ``shutil.which("claude")`` PATH-resolution default behavior unchanged --
    the PATH-resolved binary is invoked exactly as before this flag
    existed."""
    repo, worktree = repo_with_worktree
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    path_marker = tmp_path / "path-invoked.marker"
    _write_fake_exe(fake_bin / "claude", _fake_claude_script(path_marker, version="1.0.0-path"))

    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    assert path_marker.exists(), "PATH-resolved claude was not invoked when --claude-bin is omitted"
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "runtime_version: 1.0.0-path" in summary
    # Issue #2421: resolved_executable is always redacted on success, even
    # for the pre-existing PATH-resolution default lane. Identity is proven
    # via resolved_executable_sha256 against the fixture-local expected
    # digest of the resolved executable's own bytes.
    assert _summary_field(summary, "resolved_executable") == "<redacted>"
    expected_sha256 = hashlib.sha256((fake_bin / "claude").read_bytes()).hexdigest()
    assert _summary_field(summary, "resolved_executable_sha256") == expected_sha256


def test_claude_bin_argparse_default_is_none():
    """AC6 (unit-level): the parser's ``--claude-bin`` default is ``None``,
    so pre-existing callers that never pass this flag get an unchanged
    ``args.claude_bin is None`` -- the exact pre-#2174 argv shape."""
    module = _load_module()
    parser = module.build_parser()
    args = parser.parse_args([
        "--runtime", "claude", "--mode", "structured",
        "--worktree", "/tmp/does-not-matter",
        "--prompt-file", "/tmp/does-not-matter.md",
        "--output-dir", "/tmp/does-not-matter-out",
    ])
    assert args.claude_bin is None


def test_preflight_claude_available_without_override_uses_path(monkeypatch, tmp_path):
    """AC6 (unit-level): ``preflight_claude_available()`` called with no
    argument (or ``None``) is byte-for-byte the pre-#2174
    ``shutil.which("claude")`` PATH-resolution path."""
    module = _load_module()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude_path = fake_bin / "claude"
    _write_fake_exe(claude_path, "exit 0\n")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    resolved, skip_reason = module.preflight_claude_available()
    assert skip_reason is None
    assert resolved == os.path.realpath(str(claude_path))

    resolved_none_override, skip_reason_none_override = module.preflight_claude_available(None)
    assert skip_reason_none_override is None
    assert resolved_none_override == resolved


def test_preflight_claude_available_with_override_ignores_path(monkeypatch, tmp_path):
    """AC1 (unit-level): a supplied ``claude_bin_override`` is used directly,
    regardless of what (if anything) is on ``PATH``."""
    module = _load_module()
    monkeypatch.setenv("PATH", "/nonexistent-path-entry")

    override_dir = tmp_path / "claude-gpt-launcher"
    override_dir.mkdir()
    override_bin = override_dir / "launch.sh"
    _write_fake_exe(override_bin, "exit 0\n")

    resolved, skip_reason = module.preflight_claude_available(str(override_bin))
    assert skip_reason is None
    assert resolved == os.path.realpath(str(override_bin))


# ---------------------------------------------------------------------------
# Interactive herdr lane: forwarder + explicit --env PATH + causal receipt
# (PR #2176 OWNER REQUEST_CHANGES Findings 1+2:
# https://github.com/squne121/loop-protocol/pull/2176#issuecomment-5302819792)
#
# The fake herdr below deliberately mimics the ONE thing the prior
# implementation got wrong: it resolves "claude" via ``command -v`` inside
# ``agent start`` using whatever PATH THAT SPECIFIC subprocess invocation
# received (exactly the process boundary Finding 1 is about -- a real
# Herdr's persistent PTY would only ever see the shim if it is threaded
# through explicitly, e.g. via ``workspace create --env PATH=...``, not
# merely set on the Python client's own environment). It then actually
# execs whatever "claude" resolves to, so the launcher fixture used here
# (which sources a sibling ``lib.sh`` exactly like the real, merged
# ``scripts/claude-gpt/launch.sh``) genuinely exercises whether the shim is
# a working forwarder (Finding 2) rather than a symlink that would break
# ``$0``-based sibling sourcing.
# ---------------------------------------------------------------------------

_FORWARDER_CAUSAL_PROOF_HERDR_BODY = """
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
        printf '%s\n' "$@" > "$STATE_DIR/workspace_create_argv.log"
        touch "$STATE_DIR/${HERDR_SESSION}.session"
        echo '{"result":{"root_pane":{"pane_id":"pane-xyz"},"workspace":{"workspace_id":"w1"}}}'
        exit 0
        ;;
    esac
    ;;
  agent)
    case "$2" in
      start)
        resolved="$(command -v claude || true)"
        printf '%s\n' "$resolved" > "$STATE_DIR/resolved_claude_path.log"
        if [ -n "$resolved" ]; then
          "$resolved" launched-by-fake-herdr-pty > "$STATE_DIR/claude_invocation_output.log" 2>&1
        fi
        exit 0
        ;;
      prompt) exit 0 ;;
      get) echo '{"state":"idle"}'; exit 0 ;;
      explain) echo '{"agent":"claude","confidence":"high"}'; exit 0 ;;
      read)
        cat "$STATE_DIR/claude_invocation_output.log" 2>/dev/null || true
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


def test_interactive_lane_forwarder_reaches_specified_launcher_not_ambient_decoy(
    monkeypatch, tmp_path
):
    """Findings 1+2 end-to-end regression: an ambient decoy ``claude`` sits
    on PATH (would exit 99 and never write a receipt if invoked); the
    specified ``--claude-bin`` launcher sources a sibling ``lib.sh`` exactly
    like the real merged ``scripts/claude-gpt/launch.sh`` (this FAILS if the
    shim is a symlink, per Finding 2's ``$0``/``dirname`` argument). The
    fake herdr's ``agent start`` resolves and invokes "claude" via its own
    PATH (simulating the real Herdr PTY boundary), so this only observes
    the launcher's own marker output -- never the decoy's -- if Findings 1
    and 2 are both genuinely fixed."""
    module = _load_module()

    state_dir = tmp_path / "herdr-state"
    fake_herdr = tmp_path / "herdr"
    _write_fake_exe(fake_herdr, _FORWARDER_CAUSAL_PROOF_HERDR_BODY)

    decoy_dir = tmp_path / "decoy-bin"
    decoy_dir.mkdir()
    decoy_marker = tmp_path / "decoy-invoked.marker"
    _write_fake_exe(
        decoy_dir / "claude",
        'touch "' + str(decoy_marker) + '"\necho "DECOY_SHOULD_NEVER_RUN" >&2\nexit 99\n',
    )

    launcher_dir = tmp_path / "claude-gpt-launcher"
    launcher_dir.mkdir()
    (launcher_dir / "lib.sh").write_text("# sibling helper sourced by launch.sh\n", encoding="utf-8")
    launcher = launcher_dir / "launch.sh"
    _write_fake_exe(
        launcher,
        "SELF_PATH=$0\n"
        'SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)\n'
        '. "$SCRIPT_DIR/lib.sh"\n'
        'echo "REAL_LAUNCHER_OBSERVED_MARKER $*"\n',
    )

    monkeypatch.setenv("PATH", str(decoy_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_HERDR_STATE_DIR", str(state_dir))

    evidence = {"cleanup": {}}
    pane_lines = module.run_interactive_herdr_isolated(
        "claude", str(tmp_path), "hello", 20.0, "abc123", evidence,
        herdr_bin=str(fake_herdr),
        claude_bin_override=str(launcher),
    )

    assert not decoy_marker.exists(), "ambient decoy claude must never be invoked"
    assert any("REAL_LAUNCHER_OBSERVED_MARKER" in line for line in pane_lines), pane_lines
    assert evidence.get("claude_bin_launcher_receipt_verified") is True

    argv_log = (state_dir / "workspace_create_argv.log").read_text(encoding="utf-8")
    assert "--env" in argv_log
    assert "PATH=" in argv_log

    resolved_log = (state_dir / "resolved_claude_path.log").read_text(encoding="utf-8").strip()
    assert resolved_log, "fake herdr PTY could not resolve claude via PATH at all"
    assert str(decoy_dir) not in resolved_log


def test_interactive_lane_claude_bin_shim_is_forwarder_not_symlink(monkeypatch, tmp_path):
    """Finding 2 (unit-level): the generated shim executable is a real
    forwarder script, never a symlink, so a launcher relying on ``$0``
    (e.g. to locate a sibling ``lib.sh``) is never broken by shim creation
    itself."""
    module = _load_module()

    state_dir = tmp_path / "herdr-state"
    fake_herdr = tmp_path / "herdr"
    _write_fake_exe(fake_herdr, _FORWARDER_CAUSAL_PROOF_HERDR_BODY)

    launcher_dir = tmp_path / "claude-gpt-launcher"
    launcher_dir.mkdir()
    launcher = launcher_dir / "launch.sh"
    _write_fake_exe(launcher, 'echo "OK"\n')

    monkeypatch.setenv("FAKE_HERDR_STATE_DIR", str(state_dir))

    captured_shim_dir: dict[str, str] = {}
    real_mkdtemp = module.tempfile.mkdtemp

    def _spy_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        captured_shim_dir["dir"] = d
        return d

    monkeypatch.setattr(module.tempfile, "mkdtemp", _spy_mkdtemp)

    evidence = {"cleanup": {}}
    module.run_interactive_herdr_isolated(
        "claude", str(tmp_path), "hello", 20.0, "abc123", evidence,
        herdr_bin=str(fake_herdr),
        claude_bin_override=str(launcher),
    )
    assert evidence.get("claude_bin_shim_kind") == "forwarder"
    assert captured_shim_dir.get("dir")


# ---------------------------------------------------------------------------
# Issue #2176 (live-environment finding, 2026-08-16): CLAUDE_GPT_CLAUDE_BIN
# self-recursion regression. Once the shim directory built above is
# prepended to the isolated session's PATH, ``scripts/claude-gpt/
# launch.sh`` (invoked via the shim once Herdr's own ``agent start``
# resolves it) would ALSO see that shim on its own PATH -- so its internal
# ``claude_gpt_resolve_claude_bin()`` (``scripts/claude-gpt/lib.sh``),
# which falls back to ``command -v claude`` when ``CLAUDE_GPT_CLAUDE_BIN``
# is unset, resolves back to the shim itself and launch.sh execs itself
# again (self-recursion), which its own top-level parser then rejects.
# These tests confirm ``claude_adapter == "claude-gpt"`` resolves the REAL
# native claude binary against the pre-shim PATH and threads it through as
# ``CLAUDE_GPT_CLAUDE_BIN`` to both the ``herdr workspace create --env``
# call and the ``herdr pane run ... export`` re-pin step, and that the
# ``native`` adapter (default) never does this (AC6 backward compatibility).
# ---------------------------------------------------------------------------

_FORWARDER_CAUSAL_PROOF_HERDR_BODY_WITH_PANE_LOG = _FORWARDER_CAUSAL_PROOF_HERDR_BODY.replace(
    'mkdir -p "$STATE_DIR"\n',
    'mkdir -p "$STATE_DIR"\n'
    'if [ "$1" = "pane" ] && [ "$2" = "run" ]; then\n'
    '  printf \'%s\\n\' "$@" >> "$STATE_DIR/pane_run_argv.log"\n'
    'fi\n',
    1,
)


def test_interactive_lane_claude_gpt_adapter_threads_claude_gpt_claude_bin_env(monkeypatch, tmp_path):
    """The real native ``claude`` binary's absolute path must be resolved
    against the ORIGINAL (pre-shim) PATH and passed through as
    ``CLAUDE_GPT_CLAUDE_BIN`` to both the ``workspace create --env`` call
    and the pane re-pin ``export`` step, whenever ``claude_adapter ==
    "claude-gpt"``."""
    module = _load_module()

    state_dir = tmp_path / "herdr-state"
    fake_herdr = tmp_path / "herdr"
    _write_fake_exe(fake_herdr, _FORWARDER_CAUSAL_PROOF_HERDR_BODY_WITH_PANE_LOG)

    real_claude_dir = tmp_path / "real-claude-bin"
    real_claude_dir.mkdir()
    _write_fake_exe(real_claude_dir / "claude", 'echo "REAL_NATIVE_CLAUDE"\n')

    launcher_dir = tmp_path / "claude-gpt-launcher"
    launcher_dir.mkdir()
    launcher = launcher_dir / "launch.sh"
    _write_fake_exe(launcher, 'echo "OK"\n')

    monkeypatch.setenv("PATH", str(real_claude_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_HERDR_STATE_DIR", str(state_dir))

    evidence = {"cleanup": {}}
    module.run_interactive_herdr_isolated(
        "claude", str(tmp_path), "hello", 20.0, "abc123", evidence,
        herdr_bin=str(fake_herdr),
        claude_bin_override=str(launcher),
        claude_adapter="claude-gpt",
    )

    expected_real_claude = os.path.realpath(str(real_claude_dir / "claude"))
    assert evidence.get("claude_gpt_claude_bin_resolved") == expected_real_claude

    argv_log = (state_dir / "workspace_create_argv.log").read_text(encoding="utf-8")
    assert f"CLAUDE_GPT_CLAUDE_BIN={expected_real_claude}" in argv_log

    pane_log_path = state_dir / "pane_run_argv.log"
    assert pane_log_path.exists(), "pane run re-pin step was never invoked"
    pane_log = pane_log_path.read_text(encoding="utf-8")
    assert f"export CLAUDE_GPT_CLAUDE_BIN='{expected_real_claude}'" in pane_log


def test_interactive_lane_native_adapter_never_sets_claude_gpt_claude_bin_env(monkeypatch, tmp_path):
    """Negative control (AC6): the default ``native`` adapter must never
    inject ``CLAUDE_GPT_CLAUDE_BIN`` -- this is exclusively a
    ``claude-gpt``-adapter concern, and every pre-existing caller's
    isolated-session env must be unchanged."""
    module = _load_module()

    state_dir = tmp_path / "herdr-state"
    fake_herdr = tmp_path / "herdr"
    _write_fake_exe(fake_herdr, _FORWARDER_CAUSAL_PROOF_HERDR_BODY_WITH_PANE_LOG)

    real_claude_dir = tmp_path / "real-claude-bin"
    real_claude_dir.mkdir()
    _write_fake_exe(real_claude_dir / "claude", 'echo "REAL_NATIVE_CLAUDE"\n')

    launcher_dir = tmp_path / "native-launcher"
    launcher_dir.mkdir()
    launcher = launcher_dir / "launch.sh"
    _write_fake_exe(launcher, 'echo "OK"\n')

    monkeypatch.setenv("PATH", str(real_claude_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_HERDR_STATE_DIR", str(state_dir))

    evidence = {"cleanup": {}}
    module.run_interactive_herdr_isolated(
        "claude", str(tmp_path), "hello", 20.0, "abc123", evidence,
        herdr_bin=str(fake_herdr),
        claude_bin_override=str(launcher),
    )

    assert "claude_gpt_claude_bin_resolved" not in evidence
    argv_log = (state_dir / "workspace_create_argv.log").read_text(encoding="utf-8")
    assert "CLAUDE_GPT_CLAUDE_BIN" not in argv_log
    pane_log_path = state_dir / "pane_run_argv.log"
    if pane_log_path.exists():
        assert "CLAUDE_GPT_CLAUDE_BIN" not in pane_log_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Finding 6 (PR #2176 OWNER REQUEST_CHANGES): invalid runtime/flag
# combinations must be rejected at argparse time (exit 2), never silently
# accepted and ignored while a different runtime actually runs.
#
# Issue #2161 (native Codex CLI retirement): --runtime codex is no longer a
# valid argparse choice at all (the ``codex`` runtime lane was removed), so
# these tests now assert the argparse-level "invalid choice" rejection
# instead of the (now-unreachable) --runtime-specific parser.error() checks
# below it. The parser.error() checks themselves are retained in
# run_worktree_agent_runtime_smoke.py as defensive dead code.
# ---------------------------------------------------------------------------


def test_codex_runtime_with_claude_bin_is_rejected(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "codex", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--claude-bin", "/tmp/does-not-matter/launch.sh",
    )
    assert result.returncode == 2, result.stderr
    assert "invalid choice: 'codex'" in result.stderr


def test_codex_runtime_with_claude_adapter_is_rejected(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "codex", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--claude-bin", "/tmp/does-not-matter/launch.sh",
        "--claude-adapter", "claude-gpt",
    )
    assert result.returncode == 2, result.stderr


def test_codex_runtime_with_claude_agent_name_is_rejected(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "codex", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--claude-agent-name", "some-agent",
    )
    assert result.returncode == 2, result.stderr
    assert "invalid choice: 'codex'" in result.stderr


def test_codex_runtime_with_hermetic_agent_definition_is_rejected(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "codex", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--hermetic-agent-definition",
    )
    assert result.returncode == 2, result.stderr
    assert "invalid choice: 'codex'" in result.stderr


def test_hermetic_agent_definition_outside_structured_mode_is_rejected(repo_with_worktree, tmp_path):
    repo, worktree = repo_with_worktree
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--claude-agent-name", "some-agent",
        "--hermetic-agent-definition",
    )
    assert result.returncode == 2, result.stderr
    assert "--hermetic-agent-definition requires --mode structured" in result.stderr


def test_claude_runtime_with_claude_bin_is_still_accepted(repo_with_worktree, tmp_path):
    """Negative control: the same claude-specific flags remain accepted
    (never over-rejected) when --runtime claude IS specified."""
    module = _load_module()
    parser = module.build_parser()
    args = parser.parse_args([
        "--runtime", "claude", "--mode", "structured",
        "--worktree", "/tmp/does-not-matter",
        "--prompt-file", "/tmp/does-not-matter.md",
        "--output-dir", "/tmp/does-not-matter-out",
        "--claude-bin", "/tmp/does-not-matter/launch.sh",
        "--claude-adapter", "claude-gpt",
    ])
    assert args.claude_bin == "/tmp/does-not-matter/launch.sh"
    assert args.claude_adapter == "claude-gpt"
