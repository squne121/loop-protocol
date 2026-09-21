"""scripts/claude-gpt/tests/test_native_session_registry_bridge.py

Issue #2567 PR #2696 review fix_delta (P1-2, OWNER REQUEST_CHANGES) -- the
Claude-GPT launcher's isolated `CLAUDE_CONFIG_DIR/sessions` directory must
actually be made visible to / shared with Native Claude Code's own session
registration directory, so native cross-session messaging (`ListAgents` /
`SendMessage`) can discover a Claude-GPT peer session -- not merely fixing
the Task Context guard's OWN `resolve_session_registry_dir()` lookup (a
separate, already-existing concern this fix does not touch or weaken).

Per official docs (code.claude.com/docs/en/cross-session-messaging, "Message
sessions on other machines"): "Each session registers itself in files on
disk. ... two sessions can reach each other only when they can see the same
files." -- confirmed against this environment's real
`~/.claude/sessions/<pid>.json` registration file shape. Native's session
registry lives at `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/sessions/`.

- fixture-level: `lib.sh`'s `claude_gpt_link_native_sessions_dir()` in
  isolation (symlink creation, idempotent no-op when something already
  exists, best-effort failure tolerance).
- launcher-boundary: a real `launch.sh --check-only` invocation with a
  test-owned Native `HOME` whose `.claude/sessions/` already contains a
  fixture registration file -- proving the isolated Claude-GPT sessions
  directory becomes a symlink through which that SAME fixture file is
  actually visible (genuine shared filesystem visibility, not just a
  string-equal path assertion).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # scripts/claude-gpt/
LAUNCH_SH = SCRIPT_DIR / "launch.sh"
LIB_SH = SCRIPT_DIR / "lib.sh"

_HELPER_PATH = Path(__file__).resolve().parent / "_latitude_check_only_helper.py"
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_check_only_helper_2567_session_registry_bridge", _HELPER_PATH
)
_helper = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = _helper
_spec.loader.exec_module(_helper)

FAKE_PROXY_SOURCE = _helper.FAKE_PROXY_SOURCE
write_executable = _helper.write_executable


# ---------------------------------------------------------------------------
# fixture-level: `claude_gpt_link_native_sessions_dir()` in isolation.
# ---------------------------------------------------------------------------


def _run_link_fn(native_dir: str, isolated_dir: str) -> subprocess.CompletedProcess[str]:
    script = (
        f'. "{LIB_SH}"; '
        f'claude_gpt_link_native_sessions_dir "{native_dir}" "{isolated_dir}"'
    )
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=20)


def test_fixture_creates_symlink_when_isolated_dir_absent(tmp_path):
    """GIVEN the isolated sessions directory does not exist yet
    WHEN claude_gpt_link_native_sessions_dir is called
    THEN it becomes a symlink pointing at the native sessions directory
    (which is created if it didn't already exist)
    """
    native_dir = tmp_path / "native-home" / ".claude" / "sessions"
    isolated_dir = tmp_path / "claude-gpt-home" / "claude" / "sessions"
    isolated_dir.parent.mkdir(parents=True)

    result = _run_link_fn(str(native_dir), str(isolated_dir))
    assert result.returncode == 0, result.stderr
    assert isolated_dir.is_symlink()
    assert isolated_dir.resolve() == native_dir.resolve()
    assert native_dir.is_dir()


def test_fixture_never_touches_preexisting_real_directory(tmp_path):
    """GIVEN the isolated sessions directory already exists as a REAL
    directory with pre-existing content (e.g. a pre-fix launcher run)
    WHEN claude_gpt_link_native_sessions_dir is called
    THEN it leaves that directory and its content completely untouched
    (never deletes/replaces existing on-disk session data)
    """
    native_dir = tmp_path / "native-home" / ".claude" / "sessions"
    isolated_dir = tmp_path / "claude-gpt-home" / "claude" / "sessions"
    isolated_dir.mkdir(parents=True)
    preexisting = isolated_dir / "12345.json"
    preexisting.write_text('{"pid": 12345}', encoding="utf-8")

    result = _run_link_fn(str(native_dir), str(isolated_dir))
    assert result.returncode == 0, result.stderr
    assert not isolated_dir.is_symlink()
    assert isolated_dir.is_dir()
    assert preexisting.read_text(encoding="utf-8") == '{"pid": 12345}'


def test_fixture_never_repoints_preexisting_symlink(tmp_path):
    """GIVEN the isolated sessions directory is already a symlink (e.g. an
    idempotent re-launch, or a symlink to a stale/different target)
    WHEN claude_gpt_link_native_sessions_dir is called with a DIFFERENT
    native_dir argument
    THEN the existing symlink is left exactly as-is (never repointed)
    """
    native_dir = tmp_path / "native-home" / ".claude" / "sessions"
    other_native_dir = tmp_path / "other-native-home" / ".claude" / "sessions"
    other_native_dir.mkdir(parents=True)
    isolated_dir = tmp_path / "claude-gpt-home" / "claude" / "sessions"
    isolated_dir.parent.mkdir(parents=True)
    isolated_dir.symlink_to(other_native_dir)

    result = _run_link_fn(str(native_dir), str(isolated_dir))
    assert result.returncode == 0, result.stderr
    assert isolated_dir.is_symlink()
    assert isolated_dir.resolve() == other_native_dir.resolve()
    assert not native_dir.exists()


def test_fixture_is_best_effort_and_never_errors_on_empty_args():
    """GIVEN either argument is empty
    WHEN claude_gpt_link_native_sessions_dir is called
    THEN it returns 0 without attempting anything (best-effort, non-blocking)
    """
    result = _run_link_fn("", "/tmp/should-not-be-created-by-this-test")
    assert result.returncode == 0, result.stderr
    assert not Path("/tmp/should-not-be-created-by-this-test").exists()


# ---------------------------------------------------------------------------
# launcher-boundary: real `launch.sh --check-only` with a fixture Native
# sessions directory already populated.
# ---------------------------------------------------------------------------


def _run_check_only_with_native_sessions_fixture(tmp_path, *, prepopulate_native_sessions: bool):
    native_home = tmp_path / "native-home"
    native_sessions_dir = native_home / ".claude" / "sessions"
    native_sessions_dir.mkdir(parents=True)
    fixture_marker = None
    if prepopulate_native_sessions:
        fixture_marker = native_sessions_dir / "999999.json"
        fixture_marker.write_text(
            json.dumps({"pid": 999999, "name": "issue-2567-fixture-native-session"}),
            encoding="utf-8",
        )

    claude_gpt_home = tmp_path / "claude-gpt-home"
    fake_proxy = write_executable(tmp_path / "fake-claude-code-proxy", FAKE_PROXY_SOURCE)

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(native_home),
        "CLAUDE_GPT_HOME": str(claude_gpt_home),
        "CLAUDE_GPT_PROXY_BIN": str(fake_proxy),
    }

    result = subprocess.run(
        [str(LAUNCH_SH), "--check-only"],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    isolated_sessions_dir = claude_gpt_home / "claude" / "sessions"
    return result, native_sessions_dir, isolated_sessions_dir, fixture_marker


def test_launcher_boundary_isolated_sessions_dir_becomes_symlink_to_native(tmp_path):
    """GIVEN a real launch.sh --check-only invocation, with a Native HOME
    whose .claude/sessions/ already contains a fixture registration file
    WHEN the launcher runs
    THEN the isolated Claude-GPT sessions directory is a symlink to that
    SAME native sessions directory, and the pre-existing fixture file is
    genuinely visible THROUGH the isolated path (proving real shared
    filesystem visibility, not just a matching path string)
    """
    (
        result,
        native_sessions_dir,
        isolated_sessions_dir,
        fixture_marker,
    ) = _run_check_only_with_native_sessions_fixture(tmp_path, prepopulate_native_sessions=True)

    assert result.returncode == 0, result.stdout + "\n---stderr---\n" + result.stderr
    assert isolated_sessions_dir.is_symlink(), (
        "isolated Claude-GPT sessions dir must become a symlink to Native's sessions dir"
    )
    assert isolated_sessions_dir.resolve() == native_sessions_dir.resolve()

    # The fixture file was written to the NATIVE side before launch; it must
    # be visible when read through the ISOLATED path -- real shared
    # visibility, the actual precondition native ListAgents/SendMessage
    # peer discovery depends on per official docs.
    seen_via_isolated_path = isolated_sessions_dir / fixture_marker.name
    assert seen_via_isolated_path.exists()
    assert seen_via_isolated_path.read_text(encoding="utf-8") == fixture_marker.read_text(
        encoding="utf-8"
    )

    # And the reverse: a file written into the isolated path (as the real
    # Claude-GPT child process would when it registers itself) is visible
    # from the native side too.
    written_from_isolated = isolated_sessions_dir / "888888.json"
    written_from_isolated.write_text('{"pid": 888888}', encoding="utf-8")
    seen_via_native_path = native_sessions_dir / "888888.json"
    assert seen_via_native_path.exists()
    assert seen_via_native_path.read_text(encoding="utf-8") == '{"pid": 888888}'


def test_launcher_boundary_works_when_native_sessions_dir_not_yet_created(tmp_path):
    """GIVEN Native has never registered a session yet (no
    ~/.claude/sessions/ fixture file at all, only the parent .claude/ exists)
    WHEN launch.sh --check-only runs
    THEN the bridge still creates the (now-empty) native sessions directory
    and symlinks the isolated one to it, without erroring
    """
    (
        result,
        native_sessions_dir,
        isolated_sessions_dir,
        _fixture_marker,
    ) = _run_check_only_with_native_sessions_fixture(tmp_path, prepopulate_native_sessions=False)

    assert result.returncode == 0, result.stdout + "\n---stderr---\n" + result.stderr
    assert isolated_sessions_dir.is_symlink()
    assert isolated_sessions_dir.resolve() == native_sessions_dir.resolve()
