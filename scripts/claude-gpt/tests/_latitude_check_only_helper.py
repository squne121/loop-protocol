"""scripts/claude-gpt/tests/_latitude_check_only_helper.py

Issue #2426: shared hermetic `launch.sh --check-only` harness used by the
static (non-live) Latitude AC1/AC2/AC3/AC4/AC8 test files
(`test_latitude_stop_hook_wiring.py`, `test_latitude_allowlist.py`,
`test_latitude_api_key_hygiene.py`, `test_latitude_same_project_binding.py`,
`test_latitude_ingest_failure_fail_open.py`).

Mirrors the fake-proxy pattern already established in
`scripts/claude-gpt/test_launch_transport_policy.py` (Issue #2204) without
importing that file, so this helper is fully self-contained under Allowed
Paths (`scripts/claude-gpt/tests/**`).

Not a pytest test module itself (leading underscore -- pytest does not
collect it).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # scripts/claude-gpt/
LAUNCH_SH = SCRIPT_DIR / "launch.sh"

FAKE_PROXY_MODELS = ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"]

FAKE_PROXY_SOURCE = r"""#!/usr/bin/env python3
import json
import signal
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS = ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"]


def _serve(port: int) -> int:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
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

        def log_message(self, fmt, *args):
            return

    httpd = HTTPServer(("127.0.0.1", port), Handler)

    def _on_term(signum, frame):
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)
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


def write_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def run_check_only(
    tmp_path: Path,
    *,
    extra_env: dict[str, str] | None = None,
    native_settings: dict | None = None,
    timeout: float = 40.0,
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run `launch.sh --check-only` hermetically and return (result, settings_path).

    `native_settings`, if given, is written to a fixture `~/.claude/settings.json`
    under an isolated `HOME` for the duration of this single launch.sh
    invocation (never the real ambient `~/.claude/settings.json`).
    """
    claude_gpt_home = tmp_path / "claude-gpt-home"
    env = dict(os.environ)
    env["CLAUDE_GPT_HOME"] = str(claude_gpt_home)

    fake_proxy = write_executable(tmp_path / "fake-claude-code-proxy", FAKE_PROXY_SOURCE)
    env["CLAUDE_GPT_PROXY_BIN"] = str(fake_proxy)
    env.pop("CLAUDE_GPT_CLAUDE_BIN", None)

    if native_settings is not None:
        native_home = tmp_path / "native-home"
        claude_dir = native_home / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "settings.json").write_text(
            json.dumps(native_settings), encoding="utf-8"
        )
        env["HOME"] = str(native_home)

    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        [str(LAUNCH_SH), "--check-only"],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    settings_path = claude_gpt_home / "claude" / "settings.local.json"
    return result, settings_path
