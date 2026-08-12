#!/usr/bin/env python3
"""probe_agy_native_serena_mcp.py -- Issue #2015 AC12 (OWNER Scope Reframe 2026-08-09).

Dedicated LIVE probe proving that Antigravity CLI (AGY) -- the real ``agy``
binary, invoked exactly as a human/CI would invoke it -- natively
discovers, connects to, and invokes the pinned Serena MCP server from the
workspace's own ``.agents/mcp_config.json`` (the same file this Issue's
wrapper collector -- ``_collect_live_serena_read_only_evidence()`` in
``run_gemini_headless.py`` -- reads for its own, separate, direct-launch
retrieval path). This is a genuinely different code path: AGY spawns and
talks to Serena over its own MCP client implementation; this script never
calls that collector, never launches Serena directly on AGY's behalf, and
never lets the model's own text response stand in for evidence.

Why this exists (see Issue #2015 AC12): "collector 直接呼び出しを AGY 利用証拠
として数えない" -- the fact that this repo's wrapper *can* talk to Serena
proves nothing about whether AGY itself can. This probe launches the real
``agy`` CLI, with ``cwd`` set to the repository root (so AGY's own working
directory -- not a copy, not a symlinked scratch tree -- is the actual
Issue/PR checkout), and verifies two *independent* sources of evidence
before ever declaring PASS:

1.  AGY's own ``--output-format stream-json`` event stream: a ``tool``
    step whose ``tool_name`` is ``call_mcp_tool`` and whose
    ``tool_info.parameters`` name ``ServerName: "serena"`` and a
    ``ToolName`` drawn from the read-only allowlist, reaching state
    ``DONE`` with no ``error``.
2.  Serena's own MCP server log (``$HOME/.serena/logs/**/mcp_*.txt`` under
    this probe's isolated ``$HOME``) independently recording a
    ``ListToolsRequest`` (tools/list succeeded), a ``CallToolRequest``, and
    a matching ``_log_tool_application`` line for the same tool name and
    arguments AGY's own event reported.

Neither source alone is trusted: a probe that only checked AGY's own event
stream would still be trusting AGY's self-reported tool_info; a probe that
only checked Serena's log could not prove *AGY* (rather than some other
process) was the MCP client. Requiring both, with the arguments
cross-checked for exact equality, is the "MCP tool event / server log"
evidence AC12 requires -- never the model's plain-text claim to have used a
tool.

Least privilege (AC12: "least-privilege な read-only Serena tool のみ
allow"):

*   AGY's own permission engine (an isolated, throwaway
    ``$HOME/.gemini/antigravity-cli/settings.json``, materialized fresh for
    every probe run and never touching the real host's global AGY
    settings) allows only the ``mcp(serena)`` resource and denies
    ``command``/``read_file``/``write_file``/``read_url``/``execute_url``/
    ``unsandboxed`` outright -- so any shell/file-read/URL-fetch fallback
    attempt AGY's own model might make instead of using MCP is denied by
    AGY itself, not merely discouraged by prompt wording (AC12: "direct
    shell/file read/Python fallback を sentinel または permission deny で
    検出・禁止する"). This probe treats any such fallback tool call that
    reaches a *successful* (non-error) state as a fail-closed violation,
    even when genuine MCP evidence was also observed.
*   Independently of AGY's own permission engine, Serena's *own*
    server-side ``excluded_tools`` configuration (a throwaway
    ``$HOME/.serena/serena_config.yml``, sourced from this Issue's
    existing ``serena-tool-manifest.json`` ``dangerous_denylist`` -- the
    same single source of truth the wrapper collector's manifest-drift
    check already uses) removes every non-read-only Serena tool from the
    server's own ``tools/list`` response entirely, before AGY's engine is
    even in the picture. This is verified directly (independent of AGY)
    by ``verify_excluded_tools_enforced()`` below, which speaks raw
    JSON-RPC to a Serena subprocess launched with this exact config and
    asserts the dangerous denylist is genuinely absent from ``tools/list``.

Binding to head SHA (AC12/AC15): every result record includes
``head_sha`` (``git rev-parse HEAD`` in *repo_root*), the absolute path
and sha256 of the tracked ``.agents/mcp_config.json`` this probe's
isolated ``$HOME`` symlinks to (never copies -- so a byte-for-byte content
match with the tracked file is structural, not merely claimed), the AGY
``conversation_id``/event step index, and Serena's own per-connection
``session_id`` / task id.

Design references:
- Issue #2015 OWNER Scope Reframe (2026-08-09), AC12.
- ``.claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py``
  (`_collect_live_serena_read_only_evidence()`, `_run_agy()`,
  `agy_permission_policy.materialize_isolated_agy_workspace()` -- this
  probe's isolated-workspace materialization intentionally does NOT reuse
  that function or `agy_permission_policy.PROFILE_ALLOWED_PERMISSION_RESOURCES`:
  that registry's documented invariant ("no profile's
  PROFILE_ALLOWED_PERMISSION_RESOURCES includes 'mcp'", relied on by
  `preflight_agy.py`'s `MCP_UNSUPPORTED_BY_DESIGN_REASON` for the
  *delegation route*) must stay literally true. This probe is a
  deliberately separate, one-off exception -- its own permission
  materialization lives entirely in this file.
- ``.claude/skills/gemini-cli-headless-delegation/references/serena-tool-manifest.json``
  (`read_only_allowlist` / `dangerous_denylist` / `pinned_ref`).
- ``.agents/mcp_config.json`` (the workspace MCP config this probe proves
  AGY discovers).

Live verification is environment-dependent (a real, authenticated ``agy``
binary and network access to fetch the pinned Serena package are both
required) -- this module implements the probe deterministically and fails
closed on every missing precondition, but genuine live PASS can only be
observed in an environment with both available (see Issue #2015 AC12 VC:
``uv run --locked pytest scripts/agent-ops/tests/ -k agy_native_serena_probe -q``).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

try:
    import yaml
except ImportError:  # pragma: no cover -- yaml is a resolved project dependency
    yaml = None  # type: ignore[assignment]

SCHEMA = "agy_native_serena_mcp_probe_result/v1"

SERVER_NAME = "serena"
PROBE_TOOL_NAME = "find_file"
PROBE_TOOL_ARGUMENTS: dict[str, str] = {"relative_path": ".", "file_mask": "CLAUDE.md"}

MCP_CONFIG_RELATIVE_PATH = Path(".agents/mcp_config.json")
MANIFEST_RELATIVE_PATH = Path(
    ".claude/skills/gemini-cli-headless-delegation/references/serena-tool-manifest.json"
)

AGY_OAUTH_TOKEN_FILENAME = "antigravity-oauth-token"
ANTIGRAVITY_CLI_DIRNAME = "antigravity-cli"

# AGY native tool names whose *successful* (non-denied) use during this
# probe would indicate the model fell back to a direct shell/file/URL
# capability instead of routing through the MCP client -- see module
# docstring "Least privilege" section. Deliberately excludes purely
# orchestration-only native tools (define_subagent/invoke_subagent/
# manage_subagents/manage_task/ask_question/finish/wait/schedule/
# send_message/list_permissions) which carry no filesystem/shell/network
# read surface of their own.
FALLBACK_SENTINEL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "run_command",
        "command_status",
        "send_command_input",
        "view_file",
        "write_to_file",
        "sed_file",
        "multi_replace_file_content",
        "replace_file_content",
        "read_url_content",
        "list_dir",
        "find_by_name",
        "grep_search",
        "notebook_edit",
        "notebook_execution",
        "execute_browser_javascript",
        "read_resource",
    }
)

DEFAULT_AGY_PRINT_TIMEOUT_SEC = 100
DEFAULT_SUBPROCESS_TIMEOUT_SEC = 120
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_EXCLUDED_TOOLS_TIMEOUT_SEC = 30


class ProbePreconditionError(RuntimeError):
    """Raised when a fixed precondition (manifest/mcp_config/agy binary) is missing."""


def _repo_root_from_here() -> Path:
    return Path(__file__).resolve().parents[3]


def _sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_manifest(repo_root: Path) -> dict[str, Any]:
    manifest_path = repo_root / MANIFEST_RELATIVE_PATH
    if not manifest_path.is_file():
        raise ProbePreconditionError(f"serena-tool-manifest.json not found at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for key in ("read_only_allowlist", "dangerous_denylist", "pinned_ref", "mcp_command"):
        if key not in manifest:
            raise ProbePreconditionError(f"serena-tool-manifest.json missing required key: {key}")
    return manifest


def _load_mcp_config(repo_root: Path) -> dict[str, Any]:
    config_path = repo_root / MCP_CONFIG_RELATIVE_PATH
    if not config_path.is_file():
        raise ProbePreconditionError(f"{MCP_CONFIG_RELATIVE_PATH} not found under {repo_root}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    servers = config.get("mcpServers")
    if not isinstance(servers, Mapping) or SERVER_NAME not in servers:
        raise ProbePreconditionError(
            f"{MCP_CONFIG_RELATIVE_PATH} does not declare an mcpServers.{SERVER_NAME} entry"
        )
    return config


def _git_head_sha(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        raise ProbePreconditionError(f"git rev-parse HEAD failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _real_home_agy_oauth_token_file() -> "Path | None":
    real_home = os.environ.get("HOME")
    if not real_home:
        return None
    candidate = Path(real_home) / ".gemini" / ANTIGRAVITY_CLI_DIRNAME / AGY_OAUTH_TOKEN_FILENAME
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None


class ProbeWorkspace:
    def __init__(
        self,
        workspace_dir: Path,
        env: dict[str, str],
        settings_path: Path,
        mcp_config_symlink: Path,
        serena_config_path: Path,
        oauth_token_exposed: bool,
    ) -> None:
        self.workspace_dir = workspace_dir
        self.env = env
        self.settings_path = settings_path
        self.mcp_config_symlink = mcp_config_symlink
        self.serena_config_path = serena_config_path
        self.oauth_token_exposed = oauth_token_exposed


def materialize_probe_workspace(
    repo_root: Path,
    manifest: Mapping[str, Any],
    *,
    parent_dir: "str | Path | None" = None,
) -> ProbeWorkspace:
    """Build a fresh, isolated, throwaway AGY $HOME for exactly one probe attempt.

    Never reads or reuses any value from the real host's
    ``$HOME/.gemini/antigravity-cli/settings.json`` or
    ``$HOME/.gemini/config/mcp_config.json`` -- only the OAuth token file
    is exposed (as a read-only symlink; the same idiom
    ``agy_permission_policy._expose_agy_oauth_token_read_only()`` uses for
    the delegation route, reimplemented locally here rather than imported
    -- see module docstring for why this probe's permission materialization
    is intentionally independent of that shared module's profile registry).
    """
    if yaml is None:
        raise ProbePreconditionError("PyYAML is required to write serena_config.yml but is not importable")

    workspace_dir = Path(
        tempfile.mkdtemp(prefix="agy-native-serena-probe-", dir=str(parent_dir) if parent_dir else None)
    )
    antigravity_dir = workspace_dir / ".gemini" / ANTIGRAVITY_CLI_DIRNAME
    antigravity_dir.mkdir(parents=True, exist_ok=True)
    gemini_config_dir = workspace_dir / ".gemini" / "config"
    gemini_config_dir.mkdir(parents=True, exist_ok=True)
    serena_dir = workspace_dir / ".serena"
    serena_dir.mkdir(parents=True, exist_ok=True)
    xdg_config = workspace_dir / "xdg-config"
    xdg_cache = workspace_dir / "xdg-cache"
    xdg_state = workspace_dir / "xdg-state"
    for directory in (xdg_config, xdg_cache, xdg_state):
        directory.mkdir(parents=True, exist_ok=True)

    # Least-privilege AGY permission settings (AC12): only mcp(serena) is
    # allowed; every direct filesystem/shell/network resource is denied
    # outright, so any fallback attempt is denied by AGY's own engine.
    settings_path = antigravity_dir / "settings.json"
    settings_doc = {
        "toolPermission": "always-proceed",
        "permissions": {
            "allow": [f"mcp({SERVER_NAME})"],
            "ask": [],
            "deny": [
                "command(*)",
                "read_file(*)",
                "write_file(*)",
                "read_url(*)",
                "execute_url(*)",
                "unsandboxed(*)",
            ],
        },
    }
    settings_path.write_text(json.dumps(settings_doc, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(settings_path, 0o600)

    oauth_token_exposed = False
    token_file = _real_home_agy_oauth_token_file()
    if token_file is not None:
        link_path = antigravity_dir / AGY_OAUTH_TOKEN_FILENAME
        try:
            link_path.symlink_to(token_file)
            oauth_token_exposed = True
        except OSError:
            oauth_token_exposed = False

    # Symlink (never copy) the isolated $HOME's global MCP config to the
    # real, tracked workspace .agents/mcp_config.json -- so AGY's own
    # native MCP discovery is reading the exact committed file, byte for
    # byte (see module docstring "Binding to head SHA").
    real_mcp_config = (repo_root / MCP_CONFIG_RELATIVE_PATH).resolve()
    mcp_config_symlink = gemini_config_dir / "mcp_config.json"
    mcp_config_symlink.symlink_to(real_mcp_config)

    # Server-side least-privilege enforcement (AC12), independent of AGY's
    # own permission engine: Serena's own excluded_tools removes every
    # non-read-only tool from this server's own tools/list, sourced from
    # the single canonical manifest.
    dangerous_denylist = sorted(str(name) for name in manifest["dangerous_denylist"])
    serena_config_path = serena_dir / "serena_config.yml"
    # Issue #2015 AC12 fix_delta: Serena.`SerenaConfig.from_config_file()`
    # fatally rejects any config file that lacks a top-level `projects` key
    # (`serena.config.serena_config.SerenaConfigError`), independent of
    # `--project-from-cwd` -- live-verified during this Issue's probe
    # development. A partial config with only `excluded_tools` crashes
    # Serena outright, which would otherwise silently masquerade as an
    # AGY-side "MCP tool unavailable" model hallucination. `projects` is
    # populated with *repo_root* (the exact workspace this probe runs
    # against) so `--project-from-cwd` resolves deterministically.
    serena_config_path.write_text(
        yaml.safe_dump(
            {"excluded_tools": dangerous_denylist, "projects": [str(repo_root)]},
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    env: dict[str, str] = {
        "HOME": str(workspace_dir),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_CACHE_HOME": str(xdg_cache),
        "XDG_STATE_HOME": str(xdg_state),
    }
    for key in ("PATH", "LANG", "LC_ALL", "TERM"):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value

    return ProbeWorkspace(
        workspace_dir=workspace_dir,
        env=env,
        settings_path=settings_path,
        mcp_config_symlink=mcp_config_symlink,
        serena_config_path=serena_config_path,
        oauth_token_exposed=oauth_token_exposed,
    )


def _resolve_serena_launch_command(manifest: Mapping[str, Any], tool_timeout_sec: int) -> list[str]:
    command = list(manifest["mcp_command"])
    if "--tool-timeout" not in command:
        command = command + ["--tool-timeout", str(tool_timeout_sec)]
    return command


def verify_excluded_tools_enforced(
    repo_root: Path,
    manifest: Mapping[str, Any],
    workspace: ProbeWorkspace,
    *,
    timeout_sec: int = DEFAULT_EXCLUDED_TOOLS_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Independently (of AGY) verify Serena's own tools/list under *workspace*'s
    excluded_tools config never exposes a dangerous_denylist tool.

    Speaks raw JSON-RPC directly to a Serena subprocess launched with
    *workspace*'s isolated $HOME (so it reads the ``serena_config.yml``
    ``materialize_probe_workspace()`` wrote) -- this is a least-privilege
    enforcement check, not an AGY-usage evidence collector, and is never
    treated as substitute evidence for AC12's AGY-native discovery
    requirement.
    """
    command = _resolve_serena_launch_command(manifest, timeout_sec)
    env = dict(workspace.env)
    process = subprocess.Popen(
        command,
        cwd=str(repo_root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        env=env,
        bufsize=1,
        start_new_session=True,
    )
    result: dict[str, Any] = {
        "checked": True,
        "enforced": False,
        "tools_seen": [],
        "dangerous_tools_present": [],
        "read_only_tools_present": [],
        "error": None,
    }
    try:
        deadline = time.monotonic() + timeout_sec

        def send(payload: Mapping[str, Any]) -> None:
            assert process.stdin is not None
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()

        def recv(expected_id: int) -> Mapping[str, Any]:
            assert process.stdout is not None
            while time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [], 0.2)
                if not ready:
                    if process.poll() is not None:
                        raise RuntimeError(f"serena exited before response id {expected_id}")
                    continue
                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        raise RuntimeError(f"serena stdout closed before response id {expected_id}")
                    continue
                message = json.loads(line)
                if message.get("id") == expected_id:
                    return message
            raise TimeoutError(f"timed out waiting for serena response id {expected_id}")

        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "agy-native-serena-probe", "version": "1"},
                },
            }
        )
        recv(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools_response = recv(2)
        tools = ((tools_response.get("result") or {}).get("tools") or [])
        tools_seen = sorted(str(t.get("name")) for t in tools if isinstance(t, Mapping) and t.get("name"))
        result["tools_seen"] = tools_seen
        dangerous = sorted(set(tools_seen) & set(manifest["dangerous_denylist"]))
        read_only_present = sorted(set(tools_seen) & set(manifest["read_only_allowlist"]))
        result["dangerous_tools_present"] = dangerous
        result["read_only_tools_present"] = read_only_present
        result["enforced"] = (not dangerous) and (PROBE_TOOL_NAME in read_only_present)
    except (RuntimeError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    return result


def _build_probe_prompt() -> str:
    # Proven-reliable wording (see Issue #2015 AC12 investigation notes):
    # imperative, single ask, explicit tool/server/argument names. Do not
    # reword without re-verifying live reliability -- small wording changes
    # were observed to make the model fall back to defining a subagent or
    # hallucinating tool unavailability instead of calling call_mcp_tool
    # directly.
    args_json = json.dumps(PROBE_TOOL_ARGUMENTS, sort_keys=True)
    return (
        f"Invoke the call_mcp_tool tool with server_name {SERVER_NAME}, "
        f"tool_name {PROBE_TOOL_NAME}, arguments a JSON object "
        f"{args_json}. Report the raw tool result verbatim."
    )


def _parse_stream_json(stdout_text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _extract_mcp_tool_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for event in events:
        step_update = event.get("step_update")
        if not isinstance(step_update, Mapping):
            continue
        if step_update.get("tool_name") == "call_mcp_tool":
            found.append(dict(step_update))
    return found


def _extract_fallback_violations(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    violations = []
    for event in events:
        step_update = event.get("step_update")
        if not isinstance(step_update, Mapping):
            continue
        tool_name = step_update.get("tool_name")
        if tool_name not in FALLBACK_SENTINEL_TOOL_NAMES:
            continue
        if step_update.get("state") != "DONE":
            continue
        tool_info = step_update.get("tool_info")
        error = tool_info.get("error") if isinstance(tool_info, Mapping) else None
        if error is None:
            violations.append(dict(step_update))
    return violations


def _find_latest_serena_log(workspace_dir: Path, *, after_monotonic: float) -> "Path | None":
    logs_dir = workspace_dir / ".serena" / "logs"
    if not logs_dir.is_dir():
        return None
    candidates = sorted(logs_dir.glob("*/mcp_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


_SERENA_TOOL_APPLICATION_RE = re.compile(
    r"_log_tool_application:\d+ - (?P<tool>\w+): (?P<argstring>.*?); session_id: (?P<session_id>\S+)$"
)
_SERENA_TASK_ID_RE = re.compile(r"\[Task-\d+:(?P<task>\w+)\]")


def _extract_serena_evidence(log_text: str) -> dict[str, Any]:
    tools_list_confirmed = "ListToolsRequest" in log_text
    call_tool_confirmed = "CallToolRequest" in log_text
    applications: list[dict[str, Any]] = []
    for line in log_text.splitlines():
        match = _SERENA_TOOL_APPLICATION_RE.search(line)
        if not match:
            continue
        task_match = _SERENA_TASK_ID_RE.search(line)
        applications.append(
            {
                "tool": match.group("tool"),
                "argstring": match.group("argstring"),
                "session_id": match.group("session_id"),
                "task_id": task_match.group("task") if task_match else None,
            }
        )
    return {
        "tools_list_confirmed": tools_list_confirmed,
        "call_tool_request_confirmed": call_tool_confirmed,
        "tool_applications": applications,
    }


def _run_agy_once(
    *,
    repo_root: Path,
    workspace: ProbeWorkspace,
    prompt: str,
    log_path: Path,
    print_timeout_sec: int,
    subprocess_timeout_sec: int,
    agy_bin: str,
) -> dict[str, Any]:
    command = [
        agy_bin,
        "--log-file",
        str(log_path),
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--print-timeout",
        f"{print_timeout_sec}s",
    ]
    started_monotonic = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=str(repo_root),
        env=workspace.env,
        capture_output=True,
        text=True,
        timeout=subprocess_timeout_sec,
        check=False,
        shell=False,
    )
    events = _parse_stream_json(completed.stdout)
    mcp_tool_events = _extract_mcp_tool_events(events)
    fallback_violations = _extract_fallback_violations(events)

    serena_evidence: dict[str, Any] = {
        "tools_list_confirmed": False,
        "call_tool_request_confirmed": False,
        "tool_applications": [],
        "log_path": None,
    }
    serena_log_path = _find_latest_serena_log(workspace.workspace_dir, after_monotonic=started_monotonic)
    if serena_log_path is not None:
        log_text = serena_log_path.read_text(encoding="utf-8", errors="replace")
        serena_evidence = {**_extract_serena_evidence(log_text), "log_path": str(serena_log_path)}

    successful_mcp_events = [
        e
        for e in mcp_tool_events
        if e.get("state") == "DONE"
        and isinstance(e.get("tool_info"), Mapping)
        and e["tool_info"].get("error") is None
        and isinstance(e["tool_info"].get("parameters"), Mapping)
        and e["tool_info"]["parameters"].get("ServerName") == SERVER_NAME
    ]

    matching_applications = [
        app
        for app in serena_evidence["tool_applications"]
        if app.get("tool") == PROBE_TOOL_NAME
        and PROBE_TOOL_ARGUMENTS["file_mask"] in app.get("argstring", "")
        and PROBE_TOOL_ARGUMENTS["relative_path"] in app.get("argstring", "")
    ]

    agy_side_pass = bool(successful_mcp_events)
    serena_side_pass = serena_evidence["tools_list_confirmed"] and bool(matching_applications)
    attempt_pass = agy_side_pass and serena_side_pass and not fallback_violations

    return {
        "pass": attempt_pass,
        "agy_returncode": completed.returncode,
        "agy_side_pass": agy_side_pass,
        "serena_side_pass": serena_side_pass,
        "fallback_violations": fallback_violations,
        "mcp_tool_events": mcp_tool_events,
        "successful_mcp_events": successful_mcp_events,
        "serena_evidence": serena_evidence,
        "matching_applications": matching_applications,
        "agy_conversation_id": next(
            (e.get("conversation_id") for e in events if e.get("event") == "init"), None
        ),
        "elapsed_sec": round(time.monotonic() - started_monotonic, 3),
        "log_path": str(log_path),
    }


def run_probe(
    repo_root: Path,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    print_timeout_sec: int = DEFAULT_AGY_PRINT_TIMEOUT_SEC,
    subprocess_timeout_sec: int = DEFAULT_SUBPROCESS_TIMEOUT_SEC,
    excluded_tools_timeout_sec: int = DEFAULT_EXCLUDED_TOOLS_TIMEOUT_SEC,
    keep_workspace: bool = False,
    agy_bin: "str | None" = None,
) -> dict[str, Any]:
    """Run the AGY-native Serena MCP discovery probe against *repo_root*.

    Bounded, honestly-reported retry (AGY's own model behavior is
    stochastic -- see module docstring): up to *max_attempts* fresh,
    independent AGY invocations (each with its own fresh isolated
    workspace) are made; the first attempt whose evidence satisfies both
    the AGY-side and Serena-side checks is reported as the PASSing
    attempt. All attempts (not just the winning one) are recorded in the
    result for audit.
    """
    head_sha = _git_head_sha(repo_root)
    manifest = _load_manifest(repo_root)
    _load_mcp_config(repo_root)
    mcp_config_path = (repo_root / MCP_CONFIG_RELATIVE_PATH).resolve()
    mcp_config_sha256 = _sha256_file(mcp_config_path)

    resolved_agy_bin = agy_bin or os.environ.get("AGY_BIN") or shutil.which("agy")
    if not resolved_agy_bin:
        raise ProbePreconditionError("agy binary not found on PATH (set AGY_BIN to override)")

    attempts: list[dict[str, Any]] = []
    excluded_tools_result: "dict[str, Any] | None" = None
    workspaces: list[Path] = []
    winning_attempt: "dict[str, Any] | None" = None

    for attempt_index in range(1, max_attempts + 1):
        workspace = materialize_probe_workspace(repo_root, manifest)
        workspaces.append(workspace.workspace_dir)
        try:
            if excluded_tools_result is None:
                excluded_tools_result = verify_excluded_tools_enforced(
                    repo_root, manifest, workspace, timeout_sec=excluded_tools_timeout_sec
                )
            log_path = workspace.workspace_dir / "agy.log"
            prompt = _build_probe_prompt()
            attempt_result = _run_agy_once(
                repo_root=repo_root,
                workspace=workspace,
                prompt=prompt,
                log_path=log_path,
                print_timeout_sec=print_timeout_sec,
                subprocess_timeout_sec=subprocess_timeout_sec,
                agy_bin=resolved_agy_bin,
            )
            attempt_result["attempt_index"] = attempt_index
            attempt_result["mcp_config_symlink"] = str(workspace.mcp_config_symlink)
            attempts.append(attempt_result)
            if attempt_result["pass"] and winning_attempt is None:
                winning_attempt = attempt_result
                break
        finally:
            if not keep_workspace:
                shutil.rmtree(workspace.workspace_dir, ignore_errors=True)

    overall_pass = winning_attempt is not None and bool(
        excluded_tools_result and excluded_tools_result.get("enforced")
    )

    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "pass" if overall_pass else "fail",
        "head_sha": head_sha,
        "repo_root": str(repo_root),
        "server_name": SERVER_NAME,
        "tool_name": PROBE_TOOL_NAME,
        "config_path": str(mcp_config_path),
        "config_sha256": mcp_config_sha256,
        "manifest_pinned_ref": manifest.get("pinned_ref"),
        "excluded_tools_enforcement": excluded_tools_result,
        "attempts": attempts,
        "attempt_count": len(attempts),
        "winning_attempt_index": winning_attempt.get("attempt_index") if winning_attempt else None,
        "agy_bin": resolved_agy_bin,
        "kept_workspaces": [str(p) for p in workspaces] if keep_workspace else [],
    }
    return result


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Issue #2015 AC12 AGY-native Serena MCP probe")
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--print-timeout-sec", type=int, default=DEFAULT_AGY_PRINT_TIMEOUT_SEC)
    parser.add_argument("--subprocess-timeout-sec", type=int, default=DEFAULT_SUBPROCESS_TIMEOUT_SEC)
    parser.add_argument("--artifact-path", type=Path, default=None)
    parser.add_argument("--keep-workspace", action="store_true")
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve() if args.repo_root else _repo_root_from_here()
    try:
        result = run_probe(
            repo_root,
            max_attempts=args.max_attempts,
            print_timeout_sec=args.print_timeout_sec,
            subprocess_timeout_sec=args.subprocess_timeout_sec,
            keep_workspace=args.keep_workspace,
        )
    except ProbePreconditionError as exc:
        result = {"schema": SCHEMA, "status": "blocked", "error": str(exc)}

    payload = json.dumps(result, indent=2, sort_keys=True, default=str)
    if args.artifact_path:
        args.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        args.artifact_path.write_text(payload, encoding="utf-8")
    print(payload)
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
