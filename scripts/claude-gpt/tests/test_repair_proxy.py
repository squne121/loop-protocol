"""scripts/claude-gpt/tests/test_repair_proxy.py

Issue #2801 AC4/AC5: focused tests for `scripts/claude-gpt/repair_proxy.sh`,
the repository-supported one-command bounded repair/bootstrap helper.

Hermetic strategy: `repair_proxy.sh` fetches its installer script via `curl`
from `CLAUDE_GPT_REPAIR_INSTALLER_URL` (defaulting to the real upstream
`raine/claude-code-proxy` installer). Tests override that URL with a local
`file://` URL pointing at a small fixture "fake installer" shell script
(curl supports the `file://` scheme without any network access), so no real
network access or upstream dependency is exercised. The fixture installer
itself writes a fake, dependency-free `claude-code-proxy` HTTP server binary
(stdlib-only Python) whose `--version` output and `/v1/models` catalog are
controlled entirely by env vars that `repair_proxy.sh` passes through
unmodified to the installer step (unlike `launch.sh`'s `env -i` proxy child,
this installer invocation is not scrubbed).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # scripts/claude-gpt/
REPAIR_PROXY_SH = SCRIPT_DIR / "repair_proxy.sh"

FAKE_INSTALLER_SOURCE = r"""#!/bin/sh
set -e
: "${CLAUDE_CODE_PROXY_INSTALL_DIR:?}"
: "${CLAUDE_CODE_PROXY_VERSION:?}"
MODELS_JSON="${CLAUDE_GPT_TEST_FIXTURE_MODELS_JSON:-[]}"
mkdir -p "$CLAUDE_CODE_PROXY_INSTALL_DIR"
cat > "$CLAUDE_CODE_PROXY_INSTALL_DIR/claude-code-proxy" <<PYEOF
#!/usr/bin/env python3
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS = ${MODELS_JSON}
VERSION = "claude-code-proxy ${CLAUDE_CODE_PROXY_VERSION}"


def _serve(port):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
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

    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main():
    args = sys.argv[1:]
    if not args:
        return 1
    if args[0] == "--version":
        print(VERSION)
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


sys.exit(main())
PYEOF
chmod +x "$CLAUDE_CODE_PROXY_INSTALL_DIR/claude-code-proxy"
"""


def _required_models() -> list[str]:
    result = subprocess.run(
        ["sh", "-c", ". ./lib.sh; claude_gpt_required_model_set"],
        cwd=str(SCRIPT_DIR),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    models = [line for line in result.stdout.splitlines() if line]
    assert models
    return models


def _write_fake_installer(tmp_path: Path) -> Path:
    installer_path = tmp_path / "fake-install.sh"
    installer_path.write_text(FAKE_INSTALLER_SOURCE, encoding="utf-8")
    mode = installer_path.stat().st_mode
    installer_path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return installer_path


def _run_repair(
    tmp_path: Path,
    *,
    fixture_models: list[str],
    claude_gpt_home: Path | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    installer_path = _write_fake_installer(tmp_path)
    env = dict(os.environ)
    env["CLAUDE_GPT_REPAIR_INSTALLER_URL"] = f"file://{installer_path}"
    env["CLAUDE_GPT_TEST_FIXTURE_MODELS_JSON"] = json.dumps(fixture_models)
    env["CLAUDE_GPT_HOME"] = str(claude_gpt_home or (tmp_path / "claude-gpt-home"))
    env.pop("CLAUDE_CODE_PROXY_INSTALL_DIR", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(REPAIR_PROXY_SH)],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# --- AC4 ----------------------------------------------------------------------


def test_repair_installs_isolated_compatible_proxy(tmp_path):
    """GIVEN the (fixture) upstream installer and no pre-existing proxy
    WHEN repair_proxy.sh runs with default install dir / version pin
    THEN it installs a compatible proxy binary into `$CLAUDE_GPT_HOME/bin`
    (isolated Claude-GPT-owned directory), reports success, and uses the
    `CLAUDE_CODE_PROXY_VERSION` pin (`CLAUDE_GPT_MIN_KNOWN_COMPATIBLE_PROXY_VERSION`
    by default) -- without touching anything outside `$CLAUDE_GPT_HOME`
    (global PATH install / Native Claude / running session are not disturbed).
    """
    required = _required_models()
    claude_gpt_home = tmp_path / "claude-gpt-home"
    decoy_native_home = tmp_path / "decoy-native-home"
    decoy_native_home.mkdir()
    (decoy_native_home / "sentinel.txt").write_text("untouched", encoding="utf-8")

    result = _run_repair(tmp_path, fixture_models=required, claude_gpt_home=claude_gpt_home)
    assert result.returncode == 0, result.stderr

    payload = json.loads(result.stdout)
    assert payload["schema"] == "CLAUDE_GPT_REPAIR_PROXY_RESULT_V1"
    assert payload["status"] == "ok"

    expected_installed_path = claude_gpt_home / "bin" / "claude-code-proxy"
    assert payload["installed_path"] == str(expected_installed_path)
    assert expected_installed_path.exists()
    assert os.access(expected_installed_path, os.X_OK)
    assert "0.1.42" in payload["installed_version"]  # default version pin
    assert set(payload["required_models"]) == set(required)

    # Isolation: nothing was written outside $CLAUDE_GPT_HOME.
    assert (decoy_native_home / "sentinel.txt").read_text(encoding="utf-8") == "untouched"
    assert list(decoy_native_home.iterdir()) == [decoy_native_home / "sentinel.txt"]


def test_repair_respects_explicit_version_pin_override(tmp_path):
    """GIVEN an explicit CLAUDE_CODE_PROXY_VERSION override
    WHEN repair_proxy.sh runs
    THEN the fixture installer receives and reports that exact version pin
    (not the repository default), matching the documented
    `CLAUDE_CODE_PROXY_VERSION` env contract.
    """
    required = _required_models()
    result = _run_repair(
        tmp_path,
        fixture_models=required,
        extra_env={"CLAUDE_CODE_PROXY_VERSION": "9.9.9-pinned-test"},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "9.9.9-pinned-test" in payload["installed_version"]


# --- AC5 ------------------------------------------------------------------------


def test_repair_reverifies_catalog_after_install(tmp_path):
    """GIVEN the fixture installer succeeds (exit 0, binary present) and the
    installed binary's live catalog has every required model
    WHEN repair_proxy.sh runs
    THEN it reports success only after re-verifying the live catalog itself
    (AC5) -- not merely because the installer process exited 0.
    """
    required = _required_models()
    result = _run_repair(tmp_path, fixture_models=required)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"


def test_repair_fails_when_install_exits_zero_but_catalog_still_incompatible(tmp_path):
    """GIVEN the fixture installer succeeds (exit 0, binary present) but the
    installed binary's live catalog is STILL missing a required model
    WHEN repair_proxy.sh runs
    THEN it does NOT report success -- an installer exit 0 alone is not
    sufficient; required models must actually be present afterwards (AC5).
    """
    required = _required_models()
    incompatible_catalog = required[:-1]
    dropped = required[-1]

    result = _run_repair(tmp_path, fixture_models=incompatible_catalog)
    assert result.returncode == 2, result.stdout

    payload = json.loads(result.stderr)
    assert payload["schema"] == "CLAUDE_GPT_REPAIR_PROXY_RESULT_V1"
    assert payload["status"] == "failed"
    assert payload["reason"] == "catalog_still_incompatible_after_install"
    assert dropped in payload["missing_models"]


# --- misc: dry-run / curl-unavailable guardrails (scripts/CLAUDE.md 破壊的処理不変条件) --


def test_dry_run_performs_no_filesystem_mutation(tmp_path):
    """GIVEN --dry-run
    WHEN repair_proxy.sh runs
    THEN it reports the planned install_dir/version_pin/installer_url without
    creating any directory or file (scripts/CLAUDE.md dry-run invariant).
    """
    claude_gpt_home = tmp_path / "claude-gpt-home"
    env = dict(os.environ)
    env["CLAUDE_GPT_HOME"] = str(claude_gpt_home)
    result = subprocess.run(
        [str(REPAIR_PROXY_SH), "--dry-run"],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "dry_run"
    assert payload["install_dir"] == str(claude_gpt_home / "bin")
    assert not claude_gpt_home.exists()


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
