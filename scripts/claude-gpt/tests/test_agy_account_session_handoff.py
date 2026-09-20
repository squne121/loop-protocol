"""scripts/claude-gpt/tests/test_agy_account_session_handoff.py

Issue #2670: `scripts/claude-gpt/launch.sh` is the sole authority that
captures the approved pre-isolation host-root binding
(`$HOME/.gemini/antigravity-cli`) and the exact approved AGY OAuth token
source path (`$HOME/.gemini/antigravity-cli/antigravity-oauth-token`)
together, BEFORE its own `HOME`/`XDG_*` isolation swap
(`export HOME="$CLAUDE_ISOLATED_HOME_TARGET"`), and hands both values to
canonical `agy_permission_policy.py` through exactly two dedicated
non-secret path env vars: `AGY_OAUTH_TOKEN_HANDOFF_ROOT` /
`AGY_OAUTH_TOKEN_HANDOFF_SOURCE`.

Without this handoff, `agy_permission_policy.py`'s pre-existing
`_real_home_agy_oauth_token_file()` derives the approved source from
`os.environ["HOME"]` -- correct for a plain, non-isolated invocation, but
structurally unreachable from any process running INSIDE this launcher's
isolated Claude-GPT session (its ambient `HOME` is the fresh, empty
isolated workspace, which never contains the real host token file
regardless of whether that file genuinely exists on the real host). This is
the exact regression this Issue's Outcome describes and this file's tests
verify launch.sh's own fix for.

This suite runs the real, unmodified `launch.sh` end to end (fake
`claude-code-proxy` + fake `claude` only -- no live LLM, no real
credentials, no real `$HOME`/`.gemini` content) and has the fake `claude`
binary dump the environment it actually received at the exact point
launch.sh execs it as the real child process (recognized by the fixed
`--strict-mcp-config` leading argument launch.sh's own final exec line
always passes -- same seam `test_background_execution_foreground_invariant.py`
uses for its own child-process-boundary regression). This directly proves
what the CHILD Claude-GPT outer process (whose own inner test-runner is
where canonical `agy_permission_policy.py` would actually run) receives,
rather than merely asserting on launch.sh's source text.

Covers:
- AC1 (launcher responsibility): both dedicated handoff env vars are always
  present in the child process environment, computed from the ambient
  PRE-isolation `$HOME` (never the isolated `HOME` the child itself also
  receives), and describe exactly `<pre-isolation $HOME>/.gemini/antigravity-cli`
  / `.../antigravity-oauth-token` -- regardless of whether that source file
  actually exists on the (fake) real host (existence classification is
  Issue #2670 AC2's job, performed independently by
  `agy_permission_policy.py`, not by the launcher).
- Path-only transport / no broad HOME passthrough: the handoff interface
  carries EXACTLY the two dedicated values -- the child's own `HOME` (the
  isolated one) never equals either handoff value, and no other new
  broad-HOME/XDG-shaped env var is introduced by this handoff.
- No logging/persistence: neither the captured root/source path values nor
  the launcher's own stdout/stderr, nor any file launch.sh itself writes
  under its `CLAUDE_GPT_HOME` (e.g. the generated `settings.local.json`),
  ever contains the handoff path strings.
- Integration contract with `agy_permission_policy.resolve_agy_oauth_token_source()`:
  feeding the EXACT two values the child process observed back into that
  function (as its own explicit `handoff_root` / `handoff_source`
  arguments, mirroring what an inner test-runner running inside this
  Claude-GPT session would read from `os.environ`) yields
  `validated_handoff_selected` when the fixture source file exists and
  `source_absent` when it does not -- proving the two files' path
  conventions actually agree end to end.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CLAUDE_GPT_DIR = REPO_ROOT / "scripts" / "claude-gpt"
LAUNCH_SH = CLAUDE_GPT_DIR / "launch.sh"
AGY_PERMISSION_POLICY_PY = (
    REPO_ROOT / ".claude" / "skills" / "gemini-cli-headless-delegation" / "scripts" / "agy_permission_policy.py"
)

ANTIGRAVITY_CLI_DIRNAME = "antigravity-cli"
AGY_OAUTH_TOKEN_FILENAME = "antigravity-oauth-token"


def _load_agy_permission_policy() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "agy_permission_policy_account_session_handoff_test", AGY_PERMISSION_POLICY_PY
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


agy_permission_policy = _load_agy_permission_policy()


# ---------------------------------------------------------------------------
# Fake proxy / fake claude harness (mirrors
# test_background_execution_foreground_invariant.py /
# test_auto_mode_policy.py's `_run_launch` dependency-injection seam --
# `CLAUDE_GPT_PROXY_BIN` / `CLAUDE_GPT_CLAUDE_BIN` / `CLAUDE_GPT_HOME` --
# no new shell parser or regex framework is introduced).
# ---------------------------------------------------------------------------

_FAKE_PROXY_SOURCE = r"""#!/usr/bin/env python3
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

# The fake `claude` binary answers the same `--version` / `auto-mode
# defaults` / `auto-mode config` readback subcommands `preflight.sh
# --auto-mode-check` issues before the real launch. Only when invoked with
# `--strict-mcp-config` as its FIRST argument -- the fixed shape of
# launch.sh's own final `"$CLAUDE_BIN" --strict-mcp-config --mcp-config ...
# --settings ... --permission-mode auto --agents ... "$@"` exec line, i.e.
# the actual `claude` child process, never the separate preflight subprocess
# calls above -- does it dump the environment it was actually started with
# to `FAKE_CLAUDE_ENV_DUMP_PATH`.
_FAKE_CLAUDE_ENV_PROBE_SOURCE = r"""#!/usr/bin/env python3
import json
import os
import sys

argv = sys.argv[1:]

if argv and argv[0] == "--version":
    print("2.1.211 (Claude Code)")
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

if argv and argv[0] == "--strict-mcp-config":
    dump_path = os.environ.get("FAKE_CLAUDE_ENV_DUMP_PATH")
    if dump_path:
        observed = {
            name: (os.environ[name] if name in os.environ else None)
            for name in (
                "HOME",
                "AGY_OAUTH_TOKEN_HANDOFF_ROOT",
                "AGY_OAUTH_TOKEN_HANDOFF_SOURCE",
            )
        }
        with open(dump_path, "w", encoding="utf-8") as fh:
            json.dump(observed, fh)
    sys.exit(0)

sys.exit(1)
"""


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_launch_and_observe_child_env(tmp_path: Path, *, real_home: Path) -> dict:
    """Run the real, unmodified launch.sh end to end (real ambient `$HOME`
    set to *real_home*, simulating the pre-isolation host home the launcher
    itself observes) and return the environment the fake `claude` CHILD
    PROCESS actually observed for `HOME` and the two dedicated handoff env
    vars."""
    env = dict(os.environ)
    for name in ("AGY_OAUTH_TOKEN_HANDOFF_ROOT", "AGY_OAUTH_TOKEN_HANDOFF_SOURCE"):
        env.pop(name, None)
    env["HOME"] = str(real_home)
    env["CLAUDE_GPT_HOME"] = str(tmp_path / "claude-gpt-home")
    fake_proxy = _write_executable(tmp_path / "fake-claude-code-proxy", _FAKE_PROXY_SOURCE)
    fake_claude = _write_executable(tmp_path / "fake-claude", _FAKE_CLAUDE_ENV_PROBE_SOURCE)
    dump_path = tmp_path / "observed-child-env.json"
    env["CLAUDE_GPT_PROXY_BIN"] = str(fake_proxy)
    env["CLAUDE_GPT_CLAUDE_BIN"] = str(fake_claude)
    env["FAKE_CLAUDE_ENV_DUMP_PATH"] = str(dump_path)
    result = subprocess.run(
        [str(LAUNCH_SH), "--", "-p", "hello"],
        cwd=str(CLAUDE_GPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert dump_path.exists(), (
        "fake claude was never exec'd with the real child-process argv shape "
        "(--strict-mcp-config leading argument) -- launch.sh did not reach "
        "its final exec line"
    )
    observed = json.loads(dump_path.read_text(encoding="utf-8"))
    observed["_stdout"] = result.stdout
    observed["_stderr"] = result.stderr
    observed["_claude_gpt_home_dir"] = str(tmp_path / "claude-gpt-home")
    return observed


# ---------------------------------------------------------------------------
# AC1: launcher captures the approved pre-isolation host-root binding and
# exact source path together, before isolation, into exactly two dedicated
# non-secret path env vars.
# ---------------------------------------------------------------------------


def test_launch_sh_hands_off_root_and_source_when_fixture_token_present(tmp_path):
    """GIVEN a (fake) real host `$HOME` whose `.gemini/antigravity-cli/antigravity-oauth-token`
    fixture file exists
    WHEN launch.sh runs to the point of exec'ing the real `claude` child
    process
    THEN the child observes both dedicated handoff env vars set to exactly
    `<real $HOME>/.gemini/antigravity-cli` and
    `<real $HOME>/.gemini/antigravity-cli/antigravity-oauth-token`.
    """
    real_home = tmp_path / "real-home-with-token"
    token_dir = real_home / ".gemini" / ANTIGRAVITY_CLI_DIRNAME
    token_dir.mkdir(parents=True)
    (token_dir / AGY_OAUTH_TOKEN_FILENAME).write_text("dummy-fixture-token-value", encoding="utf-8")

    observed = _run_launch_and_observe_child_env(tmp_path, real_home=real_home)

    expected_root = str(real_home / ".gemini" / ANTIGRAVITY_CLI_DIRNAME)
    expected_source = str(real_home / ".gemini" / ANTIGRAVITY_CLI_DIRNAME / AGY_OAUTH_TOKEN_FILENAME)
    assert observed["AGY_OAUTH_TOKEN_HANDOFF_ROOT"] == expected_root, observed
    assert observed["AGY_OAUTH_TOKEN_HANDOFF_SOURCE"] == expected_source, observed


def test_launch_sh_hands_off_root_and_source_when_fixture_token_absent(tmp_path):
    """GIVEN a (fake) real host `$HOME` with no `.gemini/antigravity-cli`
    content at all
    WHEN launch.sh runs to the point of exec'ing the real `claude` child
    process
    THEN the child still observes both dedicated handoff env vars, computed
    from the same path convention -- the launcher never conditions the
    handoff on the source file's own presence (Issue #2670: that
    determination, `source_absent`, is independently made by
    `agy_permission_policy.py`, not by the launcher).
    """
    real_home = tmp_path / "real-home-no-token"
    real_home.mkdir()

    observed = _run_launch_and_observe_child_env(tmp_path, real_home=real_home)

    expected_root = str(real_home / ".gemini" / ANTIGRAVITY_CLI_DIRNAME)
    expected_source = str(real_home / ".gemini" / ANTIGRAVITY_CLI_DIRNAME / AGY_OAUTH_TOKEN_FILENAME)
    assert observed["AGY_OAUTH_TOKEN_HANDOFF_ROOT"] == expected_root, observed
    assert observed["AGY_OAUTH_TOKEN_HANDOFF_SOURCE"] == expected_source, observed


# ---------------------------------------------------------------------------
# Path-only transport / no broad HOME passthrough: the handoff must reflect
# the PRE-isolation real HOME, never the isolated one the child itself also
# receives as its own `HOME`.
# ---------------------------------------------------------------------------


def test_handoff_reflects_pre_isolation_home_not_the_isolated_child_home(tmp_path):
    """GIVEN the launcher's own HOME/XDG isolation swap
    (`export HOME="$CLAUDE_ISOLATED_HOME_TARGET"`) runs AFTER this Issue's
    capture block
    WHEN the real `claude` child process (which inherits the ISOLATED
    `HOME`) is exec'd
    THEN the child's own `HOME` differs from both handoff values, and the
    handoff values are rooted under the (fake) real pre-isolation `$HOME`,
    never under the isolated `CLAUDE_GPT_HOME`-scoped `claude-home`
    directory -- proving the handoff genuinely carries the PRE-isolation
    value forward, not a broad/derived re-statement of the isolated `HOME`.
    """
    real_home = tmp_path / "real-home-for-isolation-check"
    token_dir = real_home / ".gemini" / ANTIGRAVITY_CLI_DIRNAME
    token_dir.mkdir(parents=True)
    (token_dir / AGY_OAUTH_TOKEN_FILENAME).write_text("dummy-fixture-token-value", encoding="utf-8")

    observed = _run_launch_and_observe_child_env(tmp_path, real_home=real_home)

    assert observed["HOME"] != observed["AGY_OAUTH_TOKEN_HANDOFF_ROOT"], observed
    assert observed["HOME"] != observed["AGY_OAUTH_TOKEN_HANDOFF_SOURCE"], observed
    # The child's own isolated HOME is scoped under CLAUDE_GPT_HOME
    # (`<CLAUDE_GPT_HOME>/claude-home`), never under the fake real host HOME.
    assert observed["HOME"].startswith(observed["_claude_gpt_home_dir"]), observed
    assert observed["AGY_OAUTH_TOKEN_HANDOFF_ROOT"].startswith(str(real_home)), observed
    assert observed["AGY_OAUTH_TOKEN_HANDOFF_SOURCE"].startswith(str(real_home)), observed


def test_handoff_interface_carries_exactly_two_dedicated_values(tmp_path):
    """GIVEN the child process environment dump captured above
    WHEN filtering for any env var name containing `AGY_OAUTH_TOKEN_HANDOFF`
    THEN exactly the two dedicated names exist -- no third value, no
    generic/broader-scoped variant is introduced by this handoff."""
    real_home = tmp_path / "real-home-exactness-check"
    token_dir = real_home / ".gemini" / ANTIGRAVITY_CLI_DIRNAME
    token_dir.mkdir(parents=True)
    (token_dir / AGY_OAUTH_TOKEN_FILENAME).write_text("dummy-fixture-token-value", encoding="utf-8")

    observed = _run_launch_and_observe_child_env(tmp_path, real_home=real_home)
    handoff_keys = {k for k in observed if k.startswith("AGY_OAUTH_TOKEN_HANDOFF") and observed[k] is not None}
    assert handoff_keys == {"AGY_OAUTH_TOKEN_HANDOFF_ROOT", "AGY_OAUTH_TOKEN_HANDOFF_SOURCE"}, observed


# ---------------------------------------------------------------------------
# No logging/persistence: the captured path values never appear in
# launch.sh's own stdout/stderr, nor in any file it writes.
# ---------------------------------------------------------------------------


def test_handoff_values_never_appear_in_launcher_stdout_stderr_or_generated_settings(tmp_path):
    """GIVEN a (fake) real host `$HOME` with a distinctive, unique fixture
    path
    WHEN launch.sh runs end to end
    THEN neither launch.sh's own stdout/stderr, nor the launcher-generated
    `settings.local.json` / `mcp-empty.json` / `auto-mode-check.json` under
    its `CLAUDE_GPT_HOME`, ever contains the captured root/source path
    strings (Issue #2670 Outcome: "Root/path values are ... never logged,
    persisted, displayed ...")."""
    real_home = tmp_path / "distinctive-unique-real-home-marker-2670"
    token_dir = real_home / ".gemini" / ANTIGRAVITY_CLI_DIRNAME
    token_dir.mkdir(parents=True)
    (token_dir / AGY_OAUTH_TOKEN_FILENAME).write_text("dummy-fixture-token-value", encoding="utf-8")

    observed = _run_launch_and_observe_child_env(tmp_path, real_home=real_home)
    handoff_root = observed["AGY_OAUTH_TOKEN_HANDOFF_ROOT"]
    handoff_source = observed["AGY_OAUTH_TOKEN_HANDOFF_SOURCE"]
    assert handoff_root and handoff_source

    assert handoff_root not in observed["_stdout"]
    assert handoff_source not in observed["_stdout"]
    assert handoff_root not in observed["_stderr"]
    assert handoff_source not in observed["_stderr"]

    claude_gpt_home_dir = Path(observed["_claude_gpt_home_dir"])
    if claude_gpt_home_dir.exists():
        for generated_file in claude_gpt_home_dir.rglob("*"):
            if not generated_file.is_file():
                continue
            content = generated_file.read_text(encoding="utf-8", errors="replace")
            assert handoff_root not in content, generated_file
            assert handoff_source not in content, generated_file


# ---------------------------------------------------------------------------
# End-to-end path-convention contract with
# agy_permission_policy.resolve_agy_oauth_token_source().
# ---------------------------------------------------------------------------


def test_observed_handoff_resolves_validated_when_fixture_token_present(tmp_path):
    """GIVEN the exact `AGY_OAUTH_TOKEN_HANDOFF_ROOT` / `_SOURCE` values the
    real `claude` child process observed (a fixture token file exists at
    the real host path)
    WHEN fed back into `agy_permission_policy.resolve_agy_oauth_token_source()`
    (as an inner test-runner running inside this Claude-GPT session would,
    reading them from its own inherited `os.environ`)
    THEN the classification is `validated_handoff_selected` and the
    resolved source path matches the handoff's own source value exactly --
    proving the two Allowed-Path files' path conventions agree end to end.
    """
    real_home = tmp_path / "real-home-e2e-present"
    token_dir = real_home / ".gemini" / ANTIGRAVITY_CLI_DIRNAME
    token_dir.mkdir(parents=True)
    (token_dir / AGY_OAUTH_TOKEN_FILENAME).write_text("dummy-fixture-token-value", encoding="utf-8")

    observed = _run_launch_and_observe_child_env(tmp_path, real_home=real_home)

    result = agy_permission_policy.resolve_agy_oauth_token_source(
        handoff_root=observed["AGY_OAUTH_TOKEN_HANDOFF_ROOT"],
        handoff_source=observed["AGY_OAUTH_TOKEN_HANDOFF_SOURCE"],
    )
    assert result.classification == agy_permission_policy.AGY_HANDOFF_VALIDATED_SELECTED
    assert result.source_path is not None
    assert str(result.source_path) == observed["AGY_OAUTH_TOKEN_HANDOFF_SOURCE"]


def test_observed_handoff_resolves_source_absent_when_fixture_token_absent(tmp_path):
    """The negative counterpart: no fixture token file exists at the real
    host path -- the same real-launcher-observed handoff resolves to
    `source_absent` (fail-closed, no fallback to a different backend),
    never `validated_handoff_selected`."""
    real_home = tmp_path / "real-home-e2e-absent"
    real_home.mkdir()

    observed = _run_launch_and_observe_child_env(tmp_path, real_home=real_home)

    result = agy_permission_policy.resolve_agy_oauth_token_source(
        handoff_root=observed["AGY_OAUTH_TOKEN_HANDOFF_ROOT"],
        handoff_source=observed["AGY_OAUTH_TOKEN_HANDOFF_SOURCE"],
    )
    assert result.classification == agy_permission_policy.AGY_HANDOFF_SOURCE_ABSENT
    assert result.source_path is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
