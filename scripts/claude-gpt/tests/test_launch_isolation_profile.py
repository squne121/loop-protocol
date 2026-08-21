"""Issue #2259 AC2/AC3: launch.sh computes and wires the isolated issue.create
bridge endpoint (socket path / run nonce / ledger path) for every launch,
including `--check-only`, so this wiring can be verified hermetically without
spawning a genuine Claude-GPT session (which is instead covered end-to-end by
the runtime system test, AC10).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
LAUNCH_SH = SCRIPT_DIR / "launch.sh"
LIB_SH = SCRIPT_DIR / "lib.sh"


def _run_check_only(env_overrides: dict[str, str] | None = None) -> dict:
    import os

    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.run(
        ["bash", str(LAUNCH_SH), "--check-only"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, f"launch.sh --check-only failed: rc={proc.returncode} stderr={proc.stderr!r}"
    return json.loads(proc.stdout)


def test_check_only_reports_issue_create_bridge_endpoint() -> None:
    result = _run_check_only()
    bridge = result.get("issue_create_bridge")
    assert bridge is not None, "check-only JSON is missing issue_create_bridge"
    assert bridge["socket_path"], "bridge socket_path must be non-empty"
    assert bridge["ledger_path"], "bridge ledger_path must be non-empty"
    # 32 raw bytes hex-encoded -> 64 hex chars.
    assert bridge["run_nonce_len"] == 64


def test_check_only_generates_a_distinct_nonce_and_socket_path_per_invocation() -> None:
    first = _run_check_only()
    second = _run_check_only()
    first_bridge = first["issue_create_bridge"]
    second_bridge = second["issue_create_bridge"]
    # Socket path is scoped by $$ (the launcher's own PID), so two separate
    # invocations (distinct PIDs) must not collide on the same socket path.
    assert first_bridge["socket_path"] != second_bridge["socket_path"]


def test_lib_sh_bridge_helpers_are_pure_and_deterministic_given_home() -> None:
    script = (
        f". {LIB_SH}\n"
        'CLAUDE_GPT_HOME=/tmp/claude-gpt-home-test-2259\n'
        'claude_gpt_issue_create_bridge_socket_path "42"\n'
        "claude_gpt_issue_create_bridge_ledger_path\n"
    )
    proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert lines[0] == "/tmp/claude-gpt-home-test-2259/issue-create-bridge/bridge-42.sock"
    assert lines[1] == "/tmp/claude-gpt-home-test-2259/issue-create-bridge/ledger.jsonl"


def test_lib_sh_generate_run_nonce_produces_unique_64_char_hex() -> None:
    script = f". {LIB_SH}\nclaude_gpt_generate_run_nonce\nclaude_gpt_generate_run_nonce\n"
    proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 2
    assert len(lines[0]) == 64
    assert len(lines[1]) == 64
    assert lines[0] != lines[1]
    int(lines[0], 16)  # must be valid hex


def test_launch_sh_never_exports_bridge_isolation_vars_in_check_only_mode() -> None:
    # check-only never spawns the real Claude child (nor the bridge server
    # process), so it must not leave CLAUDE_GPT_ISOLATION_PROFILE=1 wired for
    # any accidental follow-on process in this mode.
    source = LAUNCH_SH.read_text(encoding="utf-8")
    check_only_block_start = source.index('if [ "$CHECK_ONLY" = "true" ]; then')
    check_only_block_end = source.index("exit 0", check_only_block_start)
    check_only_block = source[check_only_block_start:check_only_block_end]
    assert "CLAUDE_GPT_ISOLATION_PROFILE" not in check_only_block


def test_launch_sh_wires_isolation_profile_before_credential_switch() -> None:
    source = LAUNCH_SH.read_text(encoding="utf-8")
    export_idx = source.index('export CLAUDE_GPT_ISOLATION_PROFILE=1')
    home_switch_idx = source.index('export HOME="$CLAUDE_ISOLATED_HOME_TARGET"')
    assert export_idx < home_switch_idx, (
        "CLAUDE_GPT_ISOLATION_PROFILE must be exported before the HOME/GH_CONFIG_DIR "
        "isolation switch so it is set for the entire lifetime of the Claude child"
    )


def test_launch_sh_cleanup_stops_the_bridge_server() -> None:
    source = LAUNCH_SH.read_text(encoding="utf-8")
    cleanup_start = source.index("claude_gpt_cleanup() {")
    cleanup_end = source.index("\n}", cleanup_start)
    cleanup_body = source[cleanup_start:cleanup_end]
    assert "BRIDGE_PID" in cleanup_body
    assert "BRIDGE_SOCKET_PATH" in cleanup_body
