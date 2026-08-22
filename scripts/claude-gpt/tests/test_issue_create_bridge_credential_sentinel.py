"""scripts/claude-gpt/tests/test_issue_create_bridge_credential_sentinel.py

Issue #2299 AC7: a sentinel credential-like value injected into the isolated
Claude-GPT child's ambient environment must never appear in `launch.sh`'s own
stdout/stderr or in any generated evidence/settings artifact under
`CLAUDE_GPT_HOME` -- regardless of whether the value is a GitHub auth
credential that Issue #2299 now shares native-equivalent (`GH_TOKEN` /
`GH_CONFIG_DIR`系) or an unrelated secret that remains isolated
(`SSH_AUTH_SOCK` / `GIT_ASKPASS` / `SSH_ASKPASS` / `GIT_CREDENTIAL_HELPER`).

This file keeps its historical name (`test_issue_create_bridge_credential_
sentinel.py`) because the Issue #2299 Verification Commands hard-code this
exact path; the parent-mediated bridge design it originally targeted
(Issue #2259, PR #2286) was replaced by the single native path this Issue
implements (see `## Background` / AC3 in Issue #2299).

The sentinel is treated as an opaque string (no fixed-length regex
assumption). Each test asserts the *negative* (sentinel absent from log/
artifact surfaces) as well as the relevant *positive* env-passthrough/
isolation invariant, so a hollow (vacuously-true) assertion cannot pass.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
LAUNCH_SH = SCRIPT_DIR / "launch.sh"

REAL_CLAUDE_BIN = shutil.which("claude")

FAKE_PROXY_SOURCE = r"""#!/usr/bin/env python3
import json
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

# Fake `claude` binary: answers the `--version` / `auto-mode defaults` /
# `auto-mode config` readback subcommands launch.sh always invokes before the
# real launch (same contract as test_auto_mode_policy.py's fixture), and --
# the part this test actually cares about -- dumps its own full `os.environ`
# as JSON to FAKE_CLAUDE_ENV_DUMP_FILE. The dump is written to a private
# tmp_path file, never to stdout/stderr, so a sentinel value appearing in the
# dump does not by itself violate AC7 (that's the *expected* place to look
# for native GH auth passthrough); the assertions below check stdout/stderr/
# CLAUDE_GPT_HOME artifacts, not this dump file.
FAKE_CLAUDE_SOURCE = r"""#!/usr/bin/env python3
import json
import os
import sys

argv = sys.argv[1:]

if argv and argv[0] == "--version":
    print(os.environ.get("FAKE_CLAUDE_VERSION") or "2.1.211 (Claude Code)")
    sys.exit(0)
if "auto-mode" in argv:
    auto_mode_idx = argv.index("auto-mode")
    subcommand = argv[auto_mode_idx + 1] if auto_mode_idx + 1 < len(argv) else ""
    baseline = {
        "environment": ["defaults-env-baseline"],
        "allow": ["defaults-allow-baseline"],
        "hard_deny": ["defaults-hard-deny-baseline"],
        "soft_deny": ["defaults-soft-deny-baseline"],
        "classifyAllShell": False,
    }
    if subcommand == "defaults":
        print(json.dumps(baseline))
        sys.exit(0)
    if subcommand == "config":
        config = dict(baseline)
        settings_path = None
        for i, tok in enumerate(argv):
            if tok == "--settings" and i + 1 < len(argv):
                settings_path = argv[i + 1]
        if settings_path and os.path.exists(settings_path):
            with open(settings_path, encoding="utf-8") as fh:
                settings = json.load(fh)
            auto_mode = settings.get("autoMode", {})

            def _merge(key):
                entries = auto_mode.get(key)
                if entries is None:
                    return
                merged = []
                for entry in entries:
                    if entry == "$defaults":
                        merged.extend(baseline[key])
                    else:
                        merged.append(entry)
                config[key] = merged

            _merge("environment")
            _merge("allow")
            _merge("hard_deny")
            if auto_mode.get("classifyAllShell"):
                config["classifyAllShell"] = True
        print(json.dumps(config))
        sys.exit(0)

dump_file = os.environ.get("FAKE_CLAUDE_ENV_DUMP_FILE")
if dump_file:
    with open(dump_file, "w", encoding="utf-8") as fh:
        json.dump(dict(os.environ), fh)

sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT_CODE", "0")))
"""


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_launch_with_sentinels(
    tmp_path: Path, extra_env: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], Path]:
    env = dict(os.environ)
    env["CLAUDE_GPT_HOME"] = str(tmp_path / "claude-gpt-home")
    fake_proxy = _write_executable(tmp_path / "fake-claude-code-proxy", FAKE_PROXY_SOURCE)
    fake_claude = _write_executable(tmp_path / "fake-claude", FAKE_CLAUDE_SOURCE)
    env_dump_file = tmp_path / "child-env-dump.json"
    env["CLAUDE_GPT_PROXY_BIN"] = str(fake_proxy)
    env["CLAUDE_GPT_CLAUDE_BIN"] = str(fake_claude)
    env["FAKE_CLAUDE_ENV_DUMP_FILE"] = str(env_dump_file)
    env.update(extra_env)
    result = subprocess.run(
        [str(LAUNCH_SH), "--", "-p", "hello"],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    return result, env_dump_file


def _scan_tree_for_sentinel(root: Path, sentinel: str) -> list[str]:
    """Return the relative paths (under root) of any file whose content contains
    the given sentinel string. Binary-unreadable files are skipped (best-effort;
    text-based evidence/settings artifacts are what AC7 targets)."""
    hits: list[str] = []
    if not root.exists():
        return hits
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if sentinel in content:
            hits.append(str(path.relative_to(root)))
    return hits


def test_gh_auth_sentinel_shared_native_but_never_printed(tmp_path):
    """GIVEN an ambient GH_TOKEN-like sentinel credential
    WHEN launch.sh launches the isolated Claude-GPT child (Issue #2299 AC1
    native GH auth passthrough)
    THEN the sentinel value reaches the child's env (native equivalent
    sharing) but never appears in launch.sh's own stdout/stderr or in any
    generated CLAUDE_GPT_HOME artifact (settings/evidence/audit files).
    """
    sentinel = f"SENTINEL-GH-TOKEN-{uuid.uuid4().hex}-DO-NOT-LEAK"
    result, env_dump_file = _run_launch_with_sentinels(tmp_path, {"GH_TOKEN": sentinel})

    assert result.returncode == 0, result.stderr
    assert sentinel not in result.stdout
    assert sentinel not in result.stderr

    claude_gpt_home = tmp_path / "claude-gpt-home"
    hits = _scan_tree_for_sentinel(claude_gpt_home, sentinel)
    assert hits == [], f"sentinel leaked into CLAUDE_GPT_HOME artifacts: {hits}"

    # Positive check: the sentinel *did* reach the isolated child's env
    # (AC1 native GH auth passthrough) -- otherwise the negative assertions
    # above would be vacuously true (the credential was never handled at all).
    assert env_dump_file.exists(), result.stderr
    child_env = json.loads(env_dump_file.read_text(encoding="utf-8"))
    assert child_env.get("GH_TOKEN") == sentinel


def test_ssh_and_credential_helper_sentinels_remain_isolated_and_never_printed(tmp_path):
    """GIVEN ambient SSH_AUTH_SOCK/GIT_ASKPASS/SSH_ASKPASS/GIT_CREDENTIAL_HELPER
    sentinel-like values (unrelated to GitHub auth)
    WHEN launch.sh launches the isolated Claude-GPT child
    THEN these remain scrubbed from the child's env (isolation unchanged by
    Issue #2299) and never appear in stdout/stderr/CLAUDE_GPT_HOME artifacts.
    """
    sentinels = {
        "SSH_AUTH_SOCK": f"SENTINEL-SSH-SOCK-{uuid.uuid4().hex}-DO-NOT-LEAK",
        "GIT_ASKPASS": f"SENTINEL-GIT-ASKPASS-{uuid.uuid4().hex}-DO-NOT-LEAK",
        "SSH_ASKPASS": f"SENTINEL-SSH-ASKPASS-{uuid.uuid4().hex}-DO-NOT-LEAK",
        "GIT_CREDENTIAL_HELPER": f"SENTINEL-CRED-HELPER-{uuid.uuid4().hex}-DO-NOT-LEAK",
    }
    result, env_dump_file = _run_launch_with_sentinels(tmp_path, sentinels)

    assert result.returncode == 0, result.stderr
    claude_gpt_home = tmp_path / "claude-gpt-home"
    for name, sentinel in sentinels.items():
        assert sentinel not in result.stdout, name
        assert sentinel not in result.stderr, name
        hits = _scan_tree_for_sentinel(claude_gpt_home, sentinel)
        assert hits == [], f"{name} sentinel leaked into CLAUDE_GPT_HOME artifacts: {hits}"

    assert env_dump_file.exists(), result.stderr
    child_env = json.loads(env_dump_file.read_text(encoding="utf-8"))
    for name in sentinels:
        # Positive isolation check: the scrubbed vars must be entirely absent
        # from the child's env (not merely non-matching -- absent), otherwise
        # a partial-scrub regression would slip past a substring-only check.
        assert name not in child_env, f"{name} unexpectedly present in isolated child env"


def test_gh_config_dir_native_passthrough_points_at_ambient_value(tmp_path):
    """GIVEN an explicit ambient GH_CONFIG_DIR
    WHEN launch.sh launches the isolated Claude-GPT child
    THEN the child's GH_CONFIG_DIR is the ambient value itself (native
    equivalent), not the old isolated empty `claude-gh-config` placeholder
    directory under CLAUDE_GPT_HOME (Issue #2299 AC1).
    """
    ambient_gh_config_dir = tmp_path / "ambient-gh-config"
    ambient_gh_config_dir.mkdir()
    result, env_dump_file = _run_launch_with_sentinels(
        tmp_path, {"GH_CONFIG_DIR": str(ambient_gh_config_dir)}
    )
    assert result.returncode == 0, result.stderr
    assert env_dump_file.exists(), result.stderr
    child_env = json.loads(env_dump_file.read_text(encoding="utf-8"))
    assert child_env.get("GH_CONFIG_DIR") == str(ambient_gh_config_dir)
    assert "claude-gh-config" not in child_env.get("GH_CONFIG_DIR", "")
