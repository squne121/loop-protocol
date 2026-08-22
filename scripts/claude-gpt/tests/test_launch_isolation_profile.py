"""Issue #2259 AC2/AC3: launch.sh computes and wires the isolated issue.create
bridge endpoint (socket path / run nonce / ledger path) for every launch,
including `--check-only`, so this wiring can be verified hermetically without
spawning a genuine Claude-GPT session (which is instead covered end-to-end by
the runtime system test, AC10).

Issue #2259 PR #2286 CI fix_delta (2026-08-22): `_run_check_only()` previously
ran `launch.sh --check-only` against the ambient environment as-is. `launch.sh`
unconditionally calls `preflight.sh` (even in `--check-only` mode, since the
proxy is started and its `/v1/models` endpoint is probed to confirm model
alias resolution before the check-only JSON is emitted), and `preflight.sh`
returns exit code 3 when no genuine `claude-code-proxy` binary can be resolved
(`claude_gpt_resolve_proxy_bin`). `launch.sh` propagates that non-zero
`preflight.sh` exit code as its own exit code. On a developer machine with the
real `claude-code-proxy` binary installed this test passed, but GitHub
Actions runners do not have it installed, so `launch.sh --check-only` failed
deterministically in CI with `rc=3` -- not because of a flaky/racy dirty-repo
false positive (the `dirty=...` field in the launcher's own stderr diagnostic
line is unrelated logging, not the cause of the non-zero exit code; the exit
code comes entirely from `preflight.sh`'s proxy-binary-missing check, which
runs before the dirty check result is ever consulted for any exit-code
decision). The sibling hermetic test file
(`scripts/claude-gpt/test_launch_strict_mcp_config_normalization.py`) already
solves exactly this by writing a fake `claude-code-proxy` stub and pointing
`CLAUDE_GPT_PROXY_BIN` at it; this file now reuses that same pattern (fake
proxy binary + isolated per-invocation `CLAUDE_GPT_HOME`) so these two tests
no longer depend on a real proxy binary being present on the host, and no
longer share a single ambient `$HOME/.claude-gpt` directory with any
concurrently-running test in the same suite.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
LAUNCH_SH = SCRIPT_DIR / "launch.sh"
LIB_SH = SCRIPT_DIR / "lib.sh"

# --- Minimal fake `claude-code-proxy` stub, reused from the pattern already
#     established in test_launch_strict_mcp_config_normalization.py (Issue
#     #2189), so `launch.sh --check-only` (which starts the proxy and probes
#     its `/v1/models` endpoint via preflight.sh, unconditionally, even in
#     check-only mode) never needs a real proxy binary installed on the host
#     to resolve hermetically in CI. ---
FAKE_PROXY_SOURCE = r"""#!/usr/bin/env python3
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS = ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"]


def _serve(port: int) -> int:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
            if self.path == "/v1/models":
                body = json.dumps({"data": [{"id": m} for m in MODELS]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):  # noqa: A002 - silence test server logs
            return

    httpd = HTTPServer(("127.0.0.1", port), Handler)
    httpd.serve_forever()
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return 1
    if args[0] == "--version":
        print("fake-claude-code-proxy 0.0.0-test")
        return 0
    if args[0] == "codex" and len(args) >= 3 and args[1] == "auth" and args[2] == "status":
        print("Account: fake-test-account")
        return 0
    if args[0] == "serve":
        port = None
        i = 1
        while i < len(args):
            if args[i] == "--port" and i + 1 < len(args):
                port = int(args[i + 1])
                i += 2
            else:
                i += 1
        if port is None:
            return 1
        return _serve(port)
    return 1


if __name__ == "__main__":
    sys.exit(main())
"""


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _run_check_only(tmp_path: Path, env_overrides: dict[str, str] | None = None) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake_proxy = _write_executable(tmp_path / "fake-claude-code-proxy", FAKE_PROXY_SOURCE)
    claude_gpt_home = tmp_path / "claude-gpt-home"

    env = os.environ.copy()
    # `CLAUDE_GPT_PROXY_BIN` makes `claude_gpt_resolve_proxy_bin` (lib.sh)
    # pick this fake stub instead of doing a `command -v claude-code-proxy`
    # lookup against the host -- the actual, host-dependent root cause of the
    # CI failure this test file was fixed for (see module docstring).
    env["CLAUDE_GPT_PROXY_BIN"] = str(fake_proxy)
    # Each invocation gets its own isolated CLAUDE_GPT_HOME (instead of the
    # default `$HOME/.claude-gpt` shared by every concurrently-running
    # process on the host), so two `--check-only` invocations in the same
    # test, or two different tests/workers running concurrently, never
    # observe or race on each other's settings.json / mcp-config / proxy
    # state files under a shared directory.
    env["CLAUDE_GPT_HOME"] = str(claude_gpt_home)
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


def test_check_only_reports_issue_create_bridge_endpoint(tmp_path: Path) -> None:
    result = _run_check_only(tmp_path)
    bridge = result.get("issue_create_bridge")
    assert bridge is not None, "check-only JSON is missing issue_create_bridge"
    assert bridge["socket_path"], "bridge socket_path must be non-empty"
    assert bridge["ledger_path"], "bridge ledger_path must be non-empty"
    # 32 raw bytes hex-encoded -> 64 hex chars.
    assert bridge["run_nonce_len"] == 64


def test_check_only_generates_a_distinct_nonce_and_socket_path_per_invocation(tmp_path: Path) -> None:
    first = _run_check_only(tmp_path / "first")
    second = _run_check_only(tmp_path / "second")
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
