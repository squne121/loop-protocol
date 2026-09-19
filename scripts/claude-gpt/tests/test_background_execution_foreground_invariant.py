"""scripts/claude-gpt/tests/test_background_execution_foreground_invariant.py

Issue #2274 AC13: `CLAUDE_CODE_FORK_SUBAGENT` / `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`
production invariant (`CLAUDE_CODE_FORK_SUBAGENT`: unset/0,
`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`: 1). Any deviation from the invariant
-- across all 5 distinguishable states (both unset, fork-only enabled,
disable-background-only disabled i.e. unset, both enabled/unset, or a
settings-layer re-injection) -- denies the Spark Agent launch BEFORE it
happens (fail-closed), never a post-hoc detection.

These tests exercise the *exact* python source embedded in
``scripts/claude-gpt/launch.sh`` between the ``SPARK_GATE_WRITER_PY_BEGIN``/
``_END`` markers (extracted via
``run_worktree_agent_runtime_smoke.extract_spark_gate_writer_source``), same
mechanism as ``test_delegation_directive.py`` /
``test_model_precedence_negative_controls.py``, so there is a single source
of truth between what actually runs in a live claude-gpt session and what
this suite verifies.

Covers (see Issue #2274 Verification Commands, AC13):
- state 1: `CLAUDE_CODE_FORK_SUBAGENT` unset, `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`
  unset -> deny (invariant requires disable-background explicitly == "1").
- state 2: fork enabled (`1`), disable-background compliant (`1`) -> deny
  (fork must never be enabled for spark-codex).
- state 3: fork unset/compliant, disable-background NOT `1` (explicit `0`)
  -> deny.
- state 4: fork enabled AND disable-background not `1` -> deny.
- state 5 (positive control): fork unset, disable-background == `1` ->
  allow.

NOTE (scope boundary): the `PostToolUse.status == completed` foreground-
completion authority (asserting `async_launched` is FAIL, never a silent
pass) belongs to the `run_worktree_agent_runtime_smoke.py` live-smoke
evidence layer, not this pre-launch gate hook -- this suite covers only the
gate hook's pre-launch invariant deny. The PostToolUse-status classification
helper itself is tracked as remaining work for this Issue and is
intentionally NOT asserted here (no SKIP/placeholder substitute for it).

Issue #2652 update: the gate hook described above (and therefore the AC13
`test_invariant_*` behavioral tests exercising it) was retired in Issue
#2651/#2662 -- see the `gate_script_source` fixture below, which now skips
every test depending on it. Separately, the regression check on `launch.sh`'s
own unconditional pre-launch exports for the real `claude` child process
(formerly named
`test_launch_sh_exports_production_invariant_before_child_process_launch`,
then briefly a *static* text-presence check named
`test_launch_sh_restores_effective_background_capability_before_child_process_launch`)
is UPDATED (not left unaffected) by Issue #2652: it used to pin
`export CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`
as a positive invariant. That session-global background-task disable had
no remaining non-Spark justification once the Spark gate above was retired,
and its blast radius (deleting `Bash.run_in_background` from the schema for
the entire Claude-GPT session) broke `issue-refinement-loop`'s canonical
Step 2 background+join contract (Issue #2610) for every Claude-GPT session,
not just Spark invocations. Issue #2652 retires that disable (`unset`
instead of `export ...=1`) while keeping `CLAUDE_CODE_FORK_SUBAGENT` unset
as an independent, unretired contract.

PR #2667 OWNER REQUEST_CHANGES (Blocker A, 2026-09-19): the text-presence
check above (`"unset ..." in launch_sh_text`) cannot distinguish a real,
early, permanent `unset` from a commented-out `unset`, an `unset` placed
after the `claude` child process is already spawned, or an `unset` that is
immediately re-exported afterward -- all of those source shapes contain the
same substring and would false-PASS. `test_launch_sh_restores_effective_
background_capability_before_child_process_launch` below is now a
*behavioral* test: it runs the real, unmodified `launch.sh` end to end
(fake `claude-code-proxy` + fake `claude` only -- no live LLM, no real
credentials) and has the fake `claude` binary dump the environment it
actually received, at the exact point `launch.sh` execs it as the real
child process (recognized by the fixed `--strict-mcp-config` leading
argument the launcher's own final exec line always passes -- see
`launch.sh`'s `"$CLAUDE_BIN" --strict-mcp-config ...` line). This directly
exercises the production child-process boundary the launcher promises, for
each of the parent-shell states named in the Owner's review comment
(`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` unset / `"0"` / `"1"`,
`CLAUDE_CODE_FORK_SUBAGENT="1"`). The previous text-presence assertions are
kept as a secondary/auxiliary guard (cheap, still useful for a quick diff
signal) but are no longer the sole evidence.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
CLAUDE_GPT_DIR = REPO_ROOT / "scripts" / "claude-gpt"
LAUNCH_SH = CLAUDE_GPT_DIR / "launch.sh"

LAUNCH_NONCE_PLACEHOLDER = "__CLAUDE_GPT_SPARK_LAUNCH_NONCE__"

_DEFAULT_COMPLIANT_EFFECTIVE_ENV = {
    "CLAUDE_CODE_FORK_SUBAGENT": "",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "CLAUDE_CODE_SUBAGENT_MODEL": "",
}


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "run_worktree_agent_runtime_smoke_background_execution_foreground_invariant", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate_script_source() -> str:
    """Issue #2651: the Spark explicit-only authorization gate (Issue #2186)
    that used to embed this suite's AC13 fork/background invariant
    detection between ``SPARK_GATE_WRITER_PY_BEGIN``/``_END`` markers in
    ``launch.sh`` has been retired. ``extract_spark_gate_writer_source()``
    now always returns ``None`` (no marker region exists to extract any
    more) -- this fixture asserts that negative/retired outcome instead of
    the prior positive-source contract, then skips every test that depends
    on rendering and executing that (now nonexistent) gate script, since
    there is no gate source left to render. This does NOT touch or rewrite
    the invariant-behavior test bodies below (`test_invariant_*`) beyond
    the skip itself. `test_launch_sh_exports_production_invariant_before_
    child_process_launch` does NOT use this fixture at all (it is a
    separate, non-Spark, static regression check on launch.sh's own
    unconditional pre-launch exports) and Issue #2652 DOES update that
    test's body directly (see module docstring above) -- this fixture's
    skip has no bearing on it either way."""
    module = _load_module()
    launch_sh_text = LAUNCH_SH.read_text(encoding="utf-8")
    source = module.extract_spark_gate_writer_source(launch_sh_text)
    assert source is None, (
        "extract_spark_gate_writer_source() must return None now that the "
        "Spark authorization gate (SPARK_GATE_WRITER_PY_BEGIN/_END marker "
        "region) has been retired from launch.sh (Issue #2651); a non-None "
        "return here would mean the gate was reintroduced without updating "
        "this negative-test contract."
    )
    pytest.skip(
        "GPT-5.3-Codex-Spark authorization gate retired (Issue #2651): no "
        "gate script source remains to render/execute, so this fixture's "
        "dependent behavioral tests no longer apply."
    )


def _render_gate_script(directory: Path, source: str, launch_nonce: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    gate_path = directory / f"spark_gate_{uuid.uuid4().hex}.py"
    gate_path.write_text(source.replace(LAUNCH_NONCE_PLACEHOLDER, launch_nonce), encoding="utf-8")
    return gate_path


def _run_gate(
    gate_script_source: str,
    event: str,
    payload: dict,
    *,
    auth_dir: Path,
    launch_nonce: str = "nonce-fixture",
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    gate_script_path = _render_gate_script(auth_dir.parent / "gate-scripts", gate_script_source, launch_nonce)
    env = {**os.environ, **_DEFAULT_COMPLIANT_EFFECTIVE_ENV, "CLAUDE_GPT_SPARK_AUTH_DIR": str(auth_dir)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(gate_script_path), event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def _required_directive(prompt_prefix: str = "please use spark for this:\n") -> str:
    return prompt_prefix + (
        "schema: DELEGATION_REQUEST_V1\n"
        "agent_id: spark-codex\n"
        "model: gpt-5.3-codex-spark\n"
        "mode: required\n"
        "fallback: forbidden\n"
        "wait: true\n"
        "authorization_source: explicit_directive\n"
    )


def _user_prompt_submit(gate_script_source, auth_dir, session_id, prompt, *, launch_nonce="nonce-fixture"):
    return _run_gate(
        gate_script_source,
        "user-prompt-submit",
        {"session_id": session_id, "prompt": prompt},
        auth_dir=auth_dir,
        launch_nonce=launch_nonce,
    )


def _pre_tool_use_agent(gate_script_source, auth_dir, session_id, *, extra_env=None):
    return _run_gate(
        gate_script_source,
        "pre-tool-use-agent",
        {"session_id": session_id, "tool_input": {"subagent_type": "spark-codex"}},
        auth_dir=auth_dir,
        extra_env=extra_env,
    )


def _output(result: subprocess.CompletedProcess[str]) -> dict | None:
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def _decision(result: subprocess.CompletedProcess[str]) -> str | None:
    payload = _output(result)
    if payload is None:
        return None
    return payload["hookSpecificOutput"].get("permissionDecision")


def _reason(result: subprocess.CompletedProcess[str]) -> str | None:
    payload = _output(result)
    if payload is None:
        return None
    return payload["hookSpecificOutput"].get("permissionDecisionReason")


def _authorize_and_invoke(gate_script_source, tmp_path, name, *, extra_env=None):
    auth_dir = tmp_path / f"auth-{name}"
    session_id = f"sess-{name}"
    _user_prompt_submit(gate_script_source, auth_dir, session_id, _required_directive())
    return _pre_tool_use_agent(gate_script_source, auth_dir, session_id, extra_env=extra_env)


STATES = [
    pytest.param({"CLAUDE_CODE_FORK_SUBAGENT": "", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": ""}, id="both-unset"),
    pytest.param(
        {"CLAUDE_CODE_FORK_SUBAGENT": "1", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}, id="fork-only-enabled"
    ),
    pytest.param(
        {"CLAUDE_CODE_FORK_SUBAGENT": "", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0"},
        id="disable-background-only-explicit-0",
    ),
    pytest.param(
        {"CLAUDE_CODE_FORK_SUBAGENT": "1", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": ""}, id="both-violating"
    ),
]


@pytest.mark.parametrize("extra_env", STATES)
def test_invariant_violation_states_are_denied_before_launch(gate_script_source, tmp_path, extra_env, request):
    name = request.node.callspec.id
    result = _authorize_and_invoke(gate_script_source, tmp_path, name, extra_env=extra_env)
    assert _decision(result) == "deny"
    assert _reason(result) == "background_execution_invariant_violation"
    payload = _output(result)
    assert "updatedInput" not in payload["hookSpecificOutput"]


def test_invariant_compliant_state_is_allowed(gate_script_source, tmp_path):
    result = _authorize_and_invoke(
        gate_script_source,
        tmp_path,
        "compliant",
        extra_env={"CLAUDE_CODE_FORK_SUBAGENT": "", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"},
    )
    assert _decision(result) == "allow"


def test_invariant_settings_layer_reinjection_surfaces_identically_to_shell_export(gate_script_source, tmp_path):
    # A settings-layer (managed/user/project/local) `env` re-injection of
    # CLAUDE_CODE_FORK_SUBAGENT surfaces in os.environ identically to an
    # ambient shell export by the time this hook runs -- the same check
    # covers both sources without needing to distinguish provenance.
    result = _authorize_and_invoke(
        gate_script_source,
        tmp_path,
        "settings-reinjected",
        extra_env={"CLAUDE_CODE_FORK_SUBAGENT": "1", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"},
    )
    assert _decision(result) == "deny"
    assert _reason(result) == "background_execution_invariant_violation"


def test_launch_sh_static_unset_text_present_and_no_reintroduced_session_global_disable():
    # Auxiliary guard only (Issue #2652 PR #2667 Blocker A): a cheap diff
    # signal, kept alongside the behavioral test below, but never the sole
    # evidence -- see module docstring.
    launch_sh_text = LAUNCH_SH.read_text(encoding="utf-8")
    assert "unset CLAUDE_CODE_FORK_SUBAGENT" in launch_sh_text
    assert "unset CLAUDE_CODE_DISABLE_BACKGROUND_TASKS" in launch_sh_text
    # Guard against reintroducing the retired session-global disable as an
    # actual executable statement (Verification Guidance: "launcher が不要な
    # session-global background disable を再導入しない"). Checked line-by-line
    # (not a raw substring-of-the-whole-file check) so that this guard
    # cannot be defeated by wrapping the executable line in a comment, and
    # so that comments elsewhere in this file discussing the retired
    # `export ...=1`/`=0` forms (for historical/rationale context) never
    # false-positive this regression check.
    executable_lines = [
        line.split("#", 1)[0].strip()
        for line in launch_sh_text.splitlines()
        if not line.lstrip().startswith("#")
    ]
    assert "export CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1" not in executable_lines
    assert "export CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=0" not in executable_lines


# --- Behavioral child-process-boundary regression (Issue #2652 PR #2667
#     Blocker A). Reuses the same fake-proxy/fake-claude dependency
#     injection surface (`CLAUDE_GPT_PROXY_BIN` / `CLAUDE_GPT_CLAUDE_BIN` /
#     `CLAUDE_GPT_HOME`) that `test_auto_mode_policy.py`'s `_run_launch`
#     already establishes as this launcher's supported test seam -- no new
#     shell parser or regex framework is introduced. -----------------------

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
# --auto-mode-check` issues before the real launch (so the launcher's own
# fail-closed readback gate does not block the run before ever reaching the
# real child-process exec). Only when it is invoked with `--strict-mcp-
# config` as its FIRST argument -- the fixed shape of launch.sh's own final
# `"$CLAUDE_BIN" --strict-mcp-config --mcp-config ... --settings ...
# --permission-mode auto --agents ... "$@"` exec line, i.e. the actual
# `claude` child process, never the separate preflight subprocess calls
# above -- does it dump the environment it was actually started with to
# `FAKE_CLAUDE_ENV_DUMP_PATH`. This is the "entry point the launcher starts
# fake Claude at" the Owner's review comment asks for.
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
            for name in ("CLAUDE_CODE_FORK_SUBAGENT", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS")
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


def _run_launch_and_observe_child_effective_env(tmp_path: Path, parent_env: dict[str, str]) -> dict:
    """Run the real, unmodified launch.sh end to end and return the
    environment the fake `claude` CHILD PROCESS actually observed for the
    two variables under test."""
    env = dict(os.environ)
    for name in ("CLAUDE_CODE_FORK_SUBAGENT", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"):
        env.pop(name, None)
    env.update(parent_env)
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
    return json.loads(dump_path.read_text(encoding="utf-8"))


PARENT_SHELL_STATES = [
    pytest.param({}, id="parent-both-unset"),
    pytest.param({"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0"}, id="parent-disable-background-explicit-0"),
    pytest.param({"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}, id="parent-disable-background-explicit-1"),
    pytest.param({"CLAUDE_CODE_FORK_SUBAGENT": "1"}, id="parent-fork-subagent-explicit-1"),
    pytest.param(
        {"CLAUDE_CODE_FORK_SUBAGENT": "1", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"},
        id="parent-both-explicit-1",
    ),
]


@pytest.mark.parametrize("parent_env", PARENT_SHELL_STATES)
def test_launch_sh_restores_effective_background_capability_before_child_process_launch(
    tmp_path, parent_env
):
    """Issue #2652 PR #2667 OWNER Blocker A: regardless of the launching
    parent shell's own value for `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`
    (unset / `0` / `1`) or `CLAUDE_CODE_FORK_SUBAGENT` (`1`), the real
    `claude` child process launch.sh execs must observe BOTH variables as
    unset -- never a leaked/forged/re-injected value -- because the
    launcher's own `unset` statements run unconditionally, before the child
    process is spawned, and are not followed by a re-export. This is
    observed at the actual child-process environment boundary (a fake
    `claude` binary dumping `os.environ` at its own entry point), not by
    inspecting launch.sh's source text.
    """
    observed = _run_launch_and_observe_child_effective_env(tmp_path, dict(parent_env))
    assert observed["CLAUDE_CODE_FORK_SUBAGENT"] is None, observed
    assert observed["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] is None, observed
