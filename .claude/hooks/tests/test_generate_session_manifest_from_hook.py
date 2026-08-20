#!/usr/bin/env python3

import importlib.util
import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
)

HOOK_WRAPPER_PATH = REPO_ROOT / ".claude" / "hooks" / "generate_session_manifest_from_hook.mjs"
SETTINGS_JSON_PATH = REPO_ROOT / ".claude" / "settings.json"
CHECK_HOOK_BOUNDARIES_PATH = REPO_ROOT / "scripts" / "check_hook_boundaries.py"

spec = importlib.util.spec_from_file_location("check_hook_boundaries", CHECK_HOOK_BOUNDARIES_PATH)
check_hook_boundaries = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(check_hook_boundaries)


def test_hook_boundaries_sync():
    """Issue #2262 AC6 note: local_main_branch_guard and worktree_scope_guard
    manifest entries were removed from hook-boundaries.md (they are not
    registered in settings.json project PreToolUse; see #2193/#2256), so
    check_hook_boundaries.py drift is expected to be empty."""
    result = subprocess.run(
        ["uv", "run", "python3", "scripts/check_hook_boundaries.py"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    stderr_lines = {line for line in result.stderr.splitlines() if line.startswith("  [drift]")}
    assert stderr_lines == set(), (
        f"check_hook_boundaries.py drift must be empty after #2262 AC6 "
        f"manifest cleanup; got:\n{result.stderr}\n{result.stdout}"
    )
    assert result.returncode == 0, (
        f"check_hook_boundaries.py must exit 0 when drift is empty; "
        f"got returncode={result.returncode}\n{result.stderr}\n{result.stdout}"
    )


def test_wrapper_stdout_is_silent_and_artifact_path_is_overridable(tmp_path: Path):
    env = os.environ.copy()
    env["SESSION_MANIFEST_ARTIFACTS_DIR"] = str(tmp_path / "artifacts")
    env["SESSION_MANIFEST_PRODUCER_SCRIPT"] = str(REPO_ROOT / "scripts" / "generate-session-manifest.mjs")
    payload = {
        "hook_event_name": "Stop",
        "cwd": str(REPO_ROOT),
        "session_id": "wrapper-silent-test",
    }

    result = subprocess.run(
        ["node", str(HOOK_WRAPPER_PATH)],
        cwd=REPO_ROOT,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert any((tmp_path / "artifacts" / "manifests").glob("private-agent-session-manifest-stop-*.json"))


def test_wrapper_stderr_redacts_posix_windows_and_wsl_paths(tmp_path: Path):
    failing_script = tmp_path / "producer.mjs"
    failing_script.write_text(
        """
process.stderr.write("POSIX /home/user/private/file\\n")
process.stderr.write("WINDOWS C:\\\\Users\\\\Private\\\\file\\n")
process.stderr.write("WSL /mnt/c/Users/Private/file\\n")
process.exit(1)
""".strip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["SESSION_MANIFEST_PRODUCER_SCRIPT"] = str(failing_script)
    env["SESSION_MANIFEST_ARTIFACTS_DIR"] = str(tmp_path / "artifacts")

    result = subprocess.run(
        ["node", str(HOOK_WRAPPER_PATH)],
        cwd=REPO_ROOT,
        input=json.dumps({"hook_event_name": "Stop", "cwd": str(REPO_ROOT)}),
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0
    assert "/home/user/private" not in result.stderr
    assert "C:\\Users\\Private" not in result.stderr
    assert "/mnt/c/Users/Private" not in result.stderr


def test_settings_posttooluse_points_to_native_async_debounce_entrypoint():
    settings = json.loads(SETTINGS_JSON_PATH.read_text(encoding="utf-8"))
    post_tool_use = settings["hooks"]["PostToolUse"][0]["hooks"][0]
    assert post_tool_use["command"] == "node"
    assert post_tool_use["args"][0].endswith("session_manifest_debounce.mjs")
    assert post_tool_use["async"] is True


def test_hook_boundaries_narrative_checker_detects_stale_posttooluse_text():
    docs_text = (REPO_ROOT / "docs" / "dev" / "hook-boundaries.md").read_text(encoding="utf-8")
    stale_text = docs_text.replace(
        "`session_manifest_debounce.mjs` | telemetry | 継続 |",
        "`generate_session_manifest_from_hook.mjs` | telemetry | 継続 |",
        1,
    ).replace(
        "`session_manifest_debounce.mjs`（PostToolUse front gate）",
        "`generate_session_manifest_from_hook.mjs`（PostToolUse）",
        1,
    )
    errors = check_hook_boundaries.validate_narrative_consistency(stale_text)
    assert errors
    assert any("stale topology" in error for error in errors)
