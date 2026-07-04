#!/usr/bin/env python3
"""Preflight agy CLI headless support: detect agy --help / agy -p contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

EXPECTED_SMOKE = "LOOP_AGY_SMOKE_OK"
SMOKE_PROMPT = f"Return exactly: {EXPECTED_SMOKE}"
SMOKE_TIMEOUT_SECONDS = 20
GROUNDING_PROBE_PROMPT = "Search for: latest reliable news and return exactly one source URL."
GROUNDING_TIMEOUT_SECONDS = 40
NONINTERACTIVE_FLAGS = ["-p", "--print", "--prompt"]
UNEXPECTED_CAPABILITY_KEYWORDS = ["chat", "--output-format"]
SMOKE_SAMPLE_MAX_CHARS = 500
_QUOTA_EXHAUSTED_RE = re.compile(
    r"RESOURCE_EXHAUSTED|quota[_ ]exhausted|Individual quota reached",
    re.IGNORECASE,
)
_HTTP_429_RE = re.compile(
    r"(?:HTTP\s+|status[:\s]+|code[:\s]+|error[:\s]+)429\b",
    re.IGNORECASE,
)
LOCAL_ASSET_SERENA_TOOL_POLICY = "exact_match"
SERENA_TOOL_MANIFEST_RELATIVE_PATH = Path(
    ".claude/skills/gemini-cli-headless-delegation/references/serena-tool-manifest.json"
)
AGY_MCP_CONFIG_RELATIVE_PATH = Path(".agents/mcp_config.json")
SERENA_READ_ONLY_TOOLS = frozenset({
    "find_file",
    "find_referencing_symbols",
    "find_symbol",
    "get_symbols_overview",
    "list_dir",
    "search_for_pattern",
})
SERENA_DANGEROUS_TOOLS = frozenset({
    "activate_project",
    "create_text_file",
    "execute_shell_command",
    "find_declaration",
    "find_implementations",
    "get_current_config",
    "get_diagnostics_for_file",
    "initial_instructions",
    "insert_after_symbol",
    "insert_before_symbol",
    "list_memories",
    "onboarding",
    "read_file",
    "read_memory",
    "replace_content",
    "replace_in_files",
    "replace_symbol_body",
    "rename_symbol",
    "safe_delete_symbol",
    "delete_memory",
    "edit_memory",
    "rename_memory",
    "write_memory",
})
SERENA_KNOWN_TOOLS = frozenset(SERENA_READ_ONLY_TOOLS | SERENA_DANGEROUS_TOOLS)
SECRET_ENV_KEYS = (
    "AGY_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
    "GITHUB_TOKEN",
)

# Regex patterns for flag detection with word boundaries to prevent false positives
# e.g. --prompting must NOT match -p, --printable must NOT match --print
FLAG_PATTERNS: dict[str, re.Pattern[str]] = {
    "-p": re.compile(r"(?<![\w-])-p(?![\w-])"),
    "--print": re.compile(r"(?<![\w-])--print(?![\w-])"),
    "--prompt": re.compile(r"(?<![\w-])--prompt(?![\w-])"),
}

# ---------------------------------------------------------------------------
# Auth/keyring/TTY diagnostics (Issue #1267 — agy_auth_diagnostics_v1 schema)
#
# SSOT for this schema is this module: setup_check.py surfaces the same object
# unmodified at agy_preflight.auth (no schema drift — see setup_check.py
# `_extract_agy_auth_failure_class`). preflight_gemini_headless.py's OAuth-sunset
# detection has a separate SSOT (setup_check.check_auth()) and does not reuse this
# schema, since it targets the Gemini CLI, not agy.
# ---------------------------------------------------------------------------

# Env vars whose *presence* (never their value) is recorded as a diagnostic
# signal. Distinct from _minimal_agy_env(), which is the allowlisted env used to
# actually execute the agy subprocess — diagnostics never leak env values.
_DIAGNOSTIC_ENV_PRESENCE_KEYS = (
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "WSL_INTEROP",
    "WSL_DISTRO_NAME",
)

_KEYRING_FAILURE_CLASSES = frozenset({
    "system_keyring_unavailable",
    "system_keyring_locked",
    "system_keyring_backend_missing",
    "system_keyring_access_denied",
})

# Recovery action templates keyed by failure class / auth signal (Issue #1267 AC3/AC7).
_AUTH_RECOVERY_ACTIONS: dict[str, str] = {
    "system_keyring_unavailable": (
        "Start a D-Bus session (e.g. `dbus-launch`) or configure a system keyring "
        "backend before running agy. On WSL2 this is a known issue — see "
        "SKILL.md 'AGY 認証診断・既知の環境課題' for the recovery command."
    ),
    "system_keyring_locked": (
        "Unlock the system keyring (e.g. `gnome-keyring-daemon --unlock`) and rerun preflight."
    ),
    "system_keyring_backend_missing": (
        "Install a keyring backend (e.g. gnome-keyring, or a supported python-keyring "
        "backend) and rerun preflight."
    ),
    "system_keyring_access_denied": (
        "Check keyring file/socket permissions for the current user and rerun preflight."
    ),
    "system_keyring_probe_unsupported": (
        "No display/D-Bus session was detected; keyring probing is unsupported in this "
        "environment (headless/CI without a session bus)."
    ),
    "google_sign_in_required": (
        "Run agy's interactive auth login (Google Sign-In) once in a TTY session, then "
        "rerun preflight non-interactively."
    ),
    "noninteractive_auth_prompt_required": (
        "agy requires an interactive browser-based auth prompt; complete auth login in an "
        "interactive TTY session before running agy -p non-interactively (Issue #1267 known "
        "issue: agy -p can silently drop stdout when auth is required in non-TTY mode)."
    ),
    "agy_auth_unknown": (
        "agy output looked auth-related but did not match a known pattern; inspect "
        "smoke.stdout_sample / smoke.stderr_sample directly."
    ),
}


def _diagnostic_env_snapshot() -> dict[str, bool]:
    """Return boolean-only presence flags for diagnostic env vars.

    Never returns the env var *values* — only whether each is present — so this
    snapshot is safe to include directly in agy_auth_diagnostics_v1 output.
    """
    return {
        f"{key}_present": bool(os.environ.get(key))
        for key in _DIAGNOSTIC_ENV_PRESENCE_KEYS
    }


def _detect_platform() -> dict[str, Any]:
    """Detect OS and WSL2 status without leaking env var values."""
    system = platform.system().lower()
    os_name = {"linux": "linux", "windows": "windows", "darwin": "macos"}.get(system, "unknown")
    is_wsl = False
    wsl_hint: str | None = None
    if os_name == "linux":
        if os.environ.get("WSL_DISTRO_NAME"):
            is_wsl = True
            wsl_hint = "env:WSL_DISTRO_NAME"
        elif os.environ.get("WSL_INTEROP"):
            is_wsl = True
            wsl_hint = "env:WSL_INTEROP"
        else:
            try:
                proc_version = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
                if "microsoft" in proc_version or "wsl" in proc_version:
                    is_wsl = True
                    wsl_hint = "proc_version"
            except OSError:
                pass
    return {"os": os_name, "is_wsl": is_wsl, "wsl_hint": wsl_hint}


def _detect_tty() -> dict[str, Any]:
    """Detect isatty() state for stdin/stdout/stderr (non-interactive mode signal)."""
    def _isatty(stream: Any) -> bool:
        try:
            return bool(stream.isatty())
        except (AttributeError, ValueError, OSError):
            return False

    stdin_isatty = _isatty(sys.stdin)
    stdout_isatty = _isatty(sys.stdout)
    stderr_isatty = _isatty(sys.stderr)
    return {
        "stdin_isatty": stdin_isatty,
        "stdout_isatty": stdout_isatty,
        "stderr_isatty": stderr_isatty,
        "noninteractive_mode": not (stdin_isatty and stdout_isatty),
    }


def _detect_keyring(env_snapshot: dict[str, bool], platform_info: dict[str, Any]) -> dict[str, Any]:
    """Infer system keyring availability from boolean env presence + platform hints.

    This is a best-effort *inference*, not a live keyring probe — no keyring backend
    is contacted. `available: null` means "could not be inferred" (Issue #1267 schema).
    """
    dbus_present = env_snapshot.get("DBUS_SESSION_BUS_ADDRESS_present", False)
    display_present = (
        env_snapshot.get("DISPLAY_present", False)
        or env_snapshot.get("WAYLAND_DISPLAY_present", False)
    )
    if dbus_present:
        # Issue #1267 fix_delta Blocker 2: a D-Bus session being present does NOT
        # prove a keyring backend is installed, its daemon is running, or it is
        # unlocked. Treat this as a weak hint only — `available` stays unknown
        # (null) until an actual keyring probe or explicit AGY evidence confirms
        # it either way.
        return {
            "available": None,
            "backend_hint": "secret_service_dbus_session_present",
            "failure_class": None,
        }
    if platform_info.get("is_wsl"):
        # WSL2 known issue: no D-Bus session bus by default → secret-service keyring
        # backends are unreachable (Issue #1267 Notes for Reviewer).
        return {
            "available": False,
            "backend_hint": None,
            "failure_class": "system_keyring_unavailable",
        }
    if not display_present:
        return {"available": None, "backend_hint": None, "failure_class": "system_keyring_probe_unsupported"}
    return {"available": None, "backend_hint": None, "failure_class": None}


def _classify_auth_signal(raw_text: str) -> str | None:
    """Classify agy stdout/stderr text for explicit auth/keyring evidence.

    Returns ``None`` when no explicit evidence is found. Callers MUST NOT reclassify
    an empty-stdout / output-missing failure as an auth failure without this evidence
    (Issue #1267 Required Result Contract: agy_empty_stdout / agy_output_missing stay
    output-surface failures unless this function finds explicit auth/keyring text).
    """
    if not raw_text:
        return None
    text = raw_text.lower()

    if "keyring" in text:
        if "locked" in text:
            return "system_keyring_locked"
        if "permission denied" in text or "access denied" in text:
            return "system_keyring_access_denied"
        if "no recommended backend" in text or (
            "backend" in text and ("missing" in text or "not found" in text or "unavailable" in text)
        ):
            return "system_keyring_backend_missing"
        if "unavailable" in text or "not found" in text or "no such file" in text:
            return "system_keyring_unavailable"

    if ("sign in" in text or "sign-in" in text) and "google" in text:
        return "google_sign_in_required"
    if "google login" in text and ("required" in text or "no longer" in text):
        return "google_sign_in_required"

    if (
        ("please open" in text and "browser" in text)
        or "waiting for authentication" in text
        or "interactive login required" in text
        or ("requires" in text and "browser" in text and "interactive" in text)
    ):
        return "noninteractive_auth_prompt_required"

    if any(
        kw in text
        for kw in ("credential", "unauthorized", "unauthenticated", "not logged in", "login required", "auth")
    ):
        return "agy_auth_unknown"

    return None


def _build_auth_diagnostics(
    *,
    combined_output: str = "",
    smoke_ok: bool | None = None,
) -> dict[str, Any]:
    """Build the agy_auth_diagnostics_v1 object (Issue #1267 Auth Diagnostics Schema).

    Included in every agy_preflight_result/v1 response (success, CLI missing, smoke
    failure, timeout, grounded/local-asset sub-check failure).
    """
    tty_info = _detect_tty()
    platform_info = _detect_platform()
    env_snapshot = _diagnostic_env_snapshot()
    keyring_info = _detect_keyring(env_snapshot, platform_info)
    auth_signal = _classify_auth_signal(combined_output)

    if auth_signal is not None:
        if auth_signal in _KEYRING_FAILURE_CLASSES:
            keyring_info = {
                "available": False,
                "backend_hint": keyring_info.get("backend_hint"),
                "failure_class": auth_signal,
            }
            auth_mode = "unauthenticated"
        elif auth_signal == "agy_auth_unknown":
            auth_mode = "auth_probe_failed"
        elif auth_signal == "google_sign_in_required":
            auth_mode = "google_sign_in_required"
        else:
            auth_mode = "unauthenticated"
        auth_mode_confidence = "observed"
    elif smoke_ok is True:
        if os.environ.get("AGY_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            auth_mode, auth_mode_confidence = "api_key_env_present", "inferred"
        else:
            auth_mode, auth_mode_confidence = "system_keyring_cached", "inferred"
    elif keyring_info.get("failure_class"):
        auth_mode, auth_mode_confidence = "unauthenticated", "inferred"
    else:
        auth_mode, auth_mode_confidence = "unknown", "unknown"

    recovery_action: str | None = None
    if auth_signal is not None:
        recovery_action = _AUTH_RECOVERY_ACTIONS.get(auth_signal)
    elif keyring_info.get("failure_class"):
        recovery_action = _AUTH_RECOVERY_ACTIONS.get(keyring_info["failure_class"])

    return {
        "checked": True,
        "auth_mode": auth_mode,
        "auth_mode_confidence": auth_mode_confidence,
        "keyring": {
            "available": keyring_info.get("available"),
            "backend_hint": keyring_info.get("backend_hint"),
            "failure_class": keyring_info.get("failure_class"),
        },
        "tty": tty_info,
        "platform": platform_info,
        "recovery_action": recovery_action,
    }


def build_auth_diagnostics(
    *,
    combined_output: str = "",
    smoke_ok: bool | None = None,
) -> dict[str, Any]:
    """Public wrapper for `_build_auth_diagnostics` (Issue #1267 fix_delta Blocker 1).

    Exposed so callers that never invoke `run_preflight()` at all (e.g.
    `setup_check.py`'s agy-tools-missing stub, which returns before
    `check_agy_preflight()` would run) can still attach a schema-conformant
    `agy_auth_diagnostics_v1` object instead of a partial hand-authored stub.
    This is the same builder `run_preflight()` uses internally — no duplicated
    auth/keyring/TTY/platform diagnostics logic.
    """
    return _build_auth_diagnostics(combined_output=combined_output, smoke_ok=smoke_ok)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_serena_tool_manifest(repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root or _repo_root()
    manifest = _load_json(root / SERENA_TOOL_MANIFEST_RELATIVE_PATH)
    if not isinstance(manifest, dict):
        raise ValueError("serena manifest must be a JSON object")
    if manifest.get("schema") != "serena_tool_manifest_v1":
        raise ValueError("serena manifest schema must equal serena_tool_manifest_v1")
    for key in ("pinned_ref", "read_only_allowlist", "dangerous_denylist", "known_tools"):
        value = manifest.get(key)
        if key == "pinned_ref":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("serena manifest pinned_ref must be a non-empty string")
            continue
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError(f"serena manifest {key} must be a list of non-empty strings")
    return manifest


def _load_serena_server_config(repo_root: Path, mcp_config_path: Path | None = None) -> dict[str, Any]:
    config_path = mcp_config_path or repo_root / AGY_MCP_CONFIG_RELATIVE_PATH
    config = _load_json(config_path)
    if not isinstance(config, dict):
        raise ValueError(f"{config_path} must contain a JSON object")
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        raise ValueError(f"{config_path} must contain mcpServers")
    serena = servers.get("serena")
    if not isinstance(serena, dict):
        raise ValueError(f"{config_path} must contain mcpServers.serena")
    return serena


def _validate_serena_server_config(
    serena: dict[str, Any],
    manifest: dict[str, Any],
    *,
    source_label: str,
) -> list[str]:
    errors: list[str] = []
    expected_read_only = set(manifest["read_only_allowlist"])
    expected_dangerous = set(manifest["dangerous_denylist"])
    known_tools = set(manifest["known_tools"])
    pinned_ref = str(manifest["pinned_ref"])
    expected_source = f"git+https://github.com/oraios/serena@{pinned_ref}"

    command = serena.get("command")
    args = serena.get("args")
    if command != "uvx" or not isinstance(args, list) or "serena" not in args or "--project-from-cwd" not in args:
        errors.append(
            f"local_asset_research requires {source_label} Serena command: "
            "uvx ... serena ... --project-from-cwd"
        )
    elif expected_source not in args and not any(
        arg == f"serena=={pinned_ref}" for arg in args if isinstance(arg, str)
    ):
        errors.append(
            f"local_asset_research pinned_serena_manifest_mismatch: {source_label} args must match manifest pinned_ref"
        )

    if serena.get("trust", False) is not False:
        errors.append(f"local_asset_research requires {source_label}.trust to be false")

    include_tools = serena.get("includeTools")
    if not isinstance(include_tools, list) or not include_tools:
        errors.append(f"local_asset_research requires {source_label}.includeTools read-only allowlist")
    elif not all(isinstance(tool, str) for tool in include_tools):
        errors.append(f"local_asset_research requires {source_label}.includeTools to contain only strings")
    else:
        include_set = set(include_tools)
        unknown_tools = sorted(include_set - known_tools)
        if unknown_tools:
            errors.append(
                f"local_asset_research unknown_tool_policy({LOCAL_ASSET_SERENA_TOOL_POLICY}) failed: "
                f"unknown tools in {source_label}.includeTools: {', '.join(unknown_tools)}"
            )
        if include_set != expected_read_only:
            missing = sorted(expected_read_only - include_set)
            unexpected = sorted(include_set - expected_read_only)
            if missing:
                errors.append(f"local_asset_research read-only includeTools is incomplete: {', '.join(missing)}")
            if unexpected:
                errors.append(
                    f"local_asset_research has unverified MCP tools in includeTools: {', '.join(unexpected)}"
                )

    exclude_tools = serena.get("excludeTools", [])
    if not isinstance(exclude_tools, list):
        errors.append(f"local_asset_research requires {source_label}.excludeTools to be a list when present")
    elif not expected_dangerous.issubset(set(exclude_tools)):
        missing_excludes = sorted(expected_dangerous - set(exclude_tools))
        errors.append(f"local_asset_research dangerous tool denylist is incomplete: {', '.join(missing_excludes)}")

    return errors


def _validate_local_asset_serena_contract(
    repo_root: Path | None = None,
    mcp_config_path: Path | None = None,
) -> list[str]:
    root = repo_root or _repo_root()
    settings_path = root / ".gemini" / "settings.json"
    errors: list[str] = []
    try:
        manifest = load_serena_tool_manifest(root)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        return [f"local_asset_research serena manifest validation failed: {exc}"]

    try:
        settings = _load_json(settings_path)
    except FileNotFoundError:
        return [f"local_asset_research requires {settings_path}"]
    except json.JSONDecodeError as exc:
        return [f"local_asset_research requires valid JSON in {settings_path}: {exc}"]
    if not isinstance(settings, dict):
        return [f"local_asset_research requires {settings_path} to contain a JSON object"]

    mcp = settings.get("mcp")
    allowed = mcp.get("allowed") if isinstance(mcp, dict) else None
    if allowed != ["serena"]:
        errors.append("local_asset_research requires .gemini/settings.json mcp.allowed to equal ['serena']")

    servers = settings.get("mcpServers")
    if not isinstance(servers, dict):
        errors.append("local_asset_research requires .gemini/settings.json mcpServers")
        return errors

    serena = servers.get("serena")
    if not isinstance(serena, dict):
        errors.append("local_asset_research requires .gemini/settings.json mcpServers.serena")
        return errors

    errors.extend(_validate_serena_server_config(serena, manifest, source_label=".gemini/settings.json"))
    try:
        agy_serena = _load_serena_server_config(root, mcp_config_path)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"local_asset_research requires AGY MCP config .agents/mcp_config.json: {exc}")
        return errors
    errors.extend(_validate_serena_server_config(agy_serena, manifest, source_label=".agents/mcp_config.json"))

    return errors


def _safe_json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _call_serena_mcp_live(
    repo_root: Path,
    manifest: dict[str, Any],
    mcp_config_path: Path | None = None,
    *,
    timeout_sec: float = 180.0,
) -> dict[str, Any]:
    serena = _load_serena_server_config(repo_root, mcp_config_path)
    command = [str(serena["command"]), *[str(arg) for arg in serena["args"]]]
    transcript: list[dict[str, Any]] = []
    called_tools: list[str] = []
    tools_seen: list[str] = []

    def event(payload: dict[str, Any]) -> None:
        transcript.append(payload)

    event({
        "event": "mcp_server_launch",
        "server": "serena",
        "transport": "stdio",
        "command_sha256": hashlib.sha256("\0".join(command).encode("utf-8")).hexdigest(),
        "pinned_ref": manifest["pinned_ref"],
        "cwd_kind": "repo_root",
        "config_path": ".agents/mcp_config.json",
    })

    process = subprocess.Popen(
        command,
        cwd=str(repo_root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        env=_minimal_agy_env(),
        bufsize=1,
    )

    next_id = 1

    def send(payload: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        process.stdin.flush()
        if "id" in payload:
            event({
                "event": "mcp_request",
                "id": payload["id"],
                "method": payload.get("method"),
                "params": payload.get("params", {}),
            })
        else:
            event({"event": "mcp_notification", "method": payload.get("method")})

    def recv(expected_id: int) -> dict[str, Any]:
        assert process.stdout is not None
        import select
        import time
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], 0.2)
            if not ready:
                if process.poll() is not None:
                    raise RuntimeError("serena MCP server exited before response")
                continue
            line = process.stdout.readline()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != expected_id:
                continue
            result = message.get("result")
            event({
                "event": "mcp_response",
                "id": expected_id,
                "result_sha256": hashlib.sha256(json.dumps(result, sort_keys=True).encode("utf-8")).hexdigest(),
                "bounded_result_sample": _redact_output_sample(json.dumps(result, ensure_ascii=False)[:500]),
            })
            return message
        raise TimeoutError(f"timed out waiting for MCP response id {expected_id}")

    try:
        initialize_id = next_id
        next_id += 1
        send({
            "jsonrpc": "2.0",
            "id": initialize_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "loop-protocol-preflight", "version": "1"},
            },
        })
        initialize_response = recv(initialize_id)
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        tools_id = next_id
        next_id += 1
        send({"jsonrpc": "2.0", "id": tools_id, "method": "tools/list", "params": {}})
        tools_response = recv(tools_id)
        tools = ((tools_response.get("result") or {}).get("tools") or [])
        tools_seen = sorted(
            tool.get("name")
            for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        )

        missing = sorted(set(manifest["read_only_allowlist"]) - set(tools_seen))
        if missing:
            raise RuntimeError(f"Serena tools/list missing required tools: {', '.join(missing)}")
        manifest_known = sorted(manifest.get("known_tools") or [])
        if tools_seen != manifest_known:
            missing_from_manifest = sorted(set(tools_seen) - set(manifest_known))
            stale_manifest_tools = sorted(set(manifest_known) - set(tools_seen))
            raise RuntimeError(
                "Serena tools/list manifest drift: "
                f"missing_from_manifest={missing_from_manifest}; "
                f"stale_manifest_tools={stale_manifest_tools}"
            )

        calls = [
            ("find_file", {"relative_path": ".", "file_mask": "run_gemini_headless.py"}),
            (
                "search_for_pattern",
                {
                    "relative_path": ".claude/skills/gemini-cli-headless-delegation/scripts",
                    "substring_pattern": "_validate_agy_local_asset_request",
                },
            ),
            (
                "get_symbols_overview",
                {"relative_path": ".claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py"},
            ),
        ]
        evidence_count = 0
        for tool_name, arguments in calls:
            call_id = next_id
            next_id += 1
            send({
                "jsonrpc": "2.0",
                "id": call_id,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            })
            response = recv(call_id)
            called_tools.append(tool_name)
            result = response.get("result")
            result_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
            event({
                "event": "evidence_envelope_created",
                "source_kind": "serena_mcp_read_only_evidence",
                "tool_name": tool_name,
                "response_sha256": hashlib.sha256(result_text.encode("utf-8")).hexdigest(),
                "repo_relative_path": arguments.get("relative_path", "."),
                "byte_size": _safe_json_size(result),
            })
            evidence_count += 1

        return {
            "ok": True,
            "transport": "stdio",
            "pinned_ref": manifest["pinned_ref"],
            "server_started": True,
            "initialized": bool(initialize_response.get("result")),
            "tools_list_checked": True,
            "tools_seen": tools_seen,
            "called_tools": called_tools,
            "evidence_envelope_count": evidence_count,
            "transcript": transcript,
        }
    finally:
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            process.kill()


def _minimal_agy_env() -> dict[str, str]:
    """Return a minimal allowlisted environment for agy subprocess execution."""
    allowlist = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")
    env: dict[str, str] = {}
    for key in allowlist:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


# Issue #1267 fix_delta Blocker 3: agy prints a full Google OAuth authorization
# URL (accounts.google.com/... with code/state/token query params) when it
# requires interactive re-auth over Remote/SSH. That URL — and any bearer-like
# query parameters — must never appear in stdout_sample/stderr_sample/failure_reason.
_OAUTH_URL_RE = re.compile(
    r"https?://[^\s\"'<>]*(?:accounts\.google\.com|oauth2?|/o/oauth)[^\s\"'<>]*",
    re.IGNORECASE,
)
_OAUTH_QUERY_PARAM_RE = re.compile(
    r"(?i)\b(code|state|token|access_token|refresh_token|id_token|authuser)=[^&\s\"'<>]+"
)


def _redact_auth_url(text: str) -> str:
    """Redact OAuth/authorization URLs and their query parameters.

    Applied to every stdout/stderr sample and to any failure_reason derived from
    raw agy output, so a leaked Google Sign-In URL (or its code/state/token query
    parameters) never reaches result JSON, logs, or PR/issue comments.
    """
    if not text:
        return text
    redacted = _OAUTH_URL_RE.sub("<redacted-oauth-url>", text)
    redacted = _OAUTH_QUERY_PARAM_RE.sub(lambda m: f"{m.group(1)}=<redacted>", redacted)
    return redacted


def _redact_output_sample(text: str) -> str:
    """Return a bounded, redacted sample for stdout/stderr capture."""
    sample = text or ""
    for key in SECRET_ENV_KEYS:
        secret = os.environ.get(key)
        if secret:
            sample = sample.replace(secret, "<redacted>")
            if len(secret) >= 12:
                for width in (64, 48, 32, 24, 16, 12):
                    if len(secret) >= width:
                        sample = sample.replace(secret[:width], "<redacted-prefix>")

    home = os.environ.get("HOME")
    if home:
        sample = sample.replace(home, "$HOME")

    sample = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]{8,}\b", "<redacted>", sample)
    sample = re.sub(r"\bsk-[A-Za-z0-9_-]{20,}\b", "<redacted>", sample)

    # Auth/OAuth URL redaction MUST happen before truncation (Issue #1267 fix_delta
    # Blocker 3), otherwise a truncated-but-unredacted URL/query-param prefix could
    # still leak.
    sample = _redact_auth_url(sample)

    return sample[:SMOKE_SAMPLE_MAX_CHARS]


def _extract_urls(text: str) -> list[str]:
    found: list[str] = []
    for match in re.findall(r"https?://[^\s\]\)\},<>\"']+", text):
        normalized = match.strip().rstrip(")]},.\"'")
        if normalized and normalized not in found:
            found.append(normalized)
    return found


# Recognized structured web tool-call names (mirrors run_gemini_headless.py's
# RECOGNIZED_WEB_TOOL_NAMES — Issue #1266 Blocker 1 reopened: this preflight smoke path
# had not been migrated to the structured tool_calls trace requirement).
RECOGNIZED_WEB_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "web_search",
        "websearch",
        "browser_navigate",
        "browser",
        "url_read",
        "read_url",
        "fetch_url",
        "fetch",
    }
)


def _extract_grounded_research_output(stdout: str) -> dict[str, Any]:
    """Parse best-effort structured AGY native grounded research evidence from stdout.

    Mirrors run_gemini_headless.py's `_extract_grounded_research_output`. Only a
    structured JSON payload (via a recognized marker line or a bare JSON line) is
    considered machine-verifiable; a bare URL string is never treated as structured
    evidence by this function (Issue #1266 Blocker 1).
    """
    markers = (
        "AGY_GROUNDED_RESEARCH:",
        "AGY_WEBSEARCH:",
        "grounded_research:",
        "grounding:",
    )
    for line in stdout.splitlines():
        stripped = line.strip()
        for marker in markers:
            if stripped.startswith(marker):
                candidate = stripped[len(marker):].strip()
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return {"source": marker, "data": parsed}

    for line in stdout.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("{") and stripped.endswith("}")):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and any(
            key in parsed
            for key in ("grounded_research", "grounding", "web_search", "web", "citations", "sources", "tool_calls")
        ):
            return {"source": "json_line", "data": parsed}

    return {}


def _extract_recognized_tool_calls(parsed: dict[str, Any] | None) -> list[dict[str, str]]:
    """Extract machine-verifiable web tool-call trace entries from structured evidence.

    Only structured `tool_calls` entries whose name is in RECOGNIZED_WEB_TOOL_NAMES count
    as machine-verifiable evidence. A bare URL string appearing in stdout without this
    structured trace is NOT a tool-call trace (Issue #1266 Blocker 1).
    """
    if not isinstance(parsed, dict):
        return []
    data = parsed.get("data")
    if not isinstance(data, dict):
        return []
    calls = data.get("tool_calls")
    if not isinstance(calls, list):
        return []
    recognized: list[dict[str, str]] = []
    for call in calls:
        name: Any = None
        if isinstance(call, dict):
            name = call.get("name") or call.get("tool")
        elif isinstance(call, str):
            name = call
        if isinstance(name, str) and name.strip().lower() in RECOGNIZED_WEB_TOOL_NAMES:
            recognized.append({"name": name.strip().lower()})
    return recognized


def _mask_resolved_path(path: str | None) -> str | None:
    """Return a sanitized resolved path suitable for JSON evidence."""
    if not path:
        return None
    home = os.environ.get("HOME")
    if home and path.startswith(home):
        suffix = path[len(home):].lstrip("/")
        return "$HOME" if not suffix else f"$HOME/{suffix}"
    return Path(path).name


def _resolve_binary() -> str:
    """Return agy binary path, overridable via AGY_BIN env var."""
    return os.environ.get("AGY_BIN", "agy")


def _run(
    argv: list[str],
    cwd: Path | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """subprocess.run wrapper — shell=False is enforced."""
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=_minimal_agy_env(),
        shell=False,
    )


def _run_version(agy_bin: str) -> subprocess.CompletedProcess[str]:
    """Run `agy --version` to confirm binary exists."""
    return _run([agy_bin, "--version"])


def _run_help(agy_bin: str) -> subprocess.CompletedProcess[str]:
    """Run `agy --help` to retrieve help text."""
    return _run([agy_bin, "--help"])


def _parse_help_capabilities(help_text: str) -> tuple[dict[str, bool], list[str]]:
    """Detect -p/--print/--prompt flags and unexpected capabilities.

    Returns a tuple of:
      noninteractive_flags: {"-p": bool, "--print": bool, "--prompt": bool}
      unexpected_capabilities: list of capability strings found

    Uses regex word-boundary matching to avoid false positives:
    e.g. --prompting will NOT match -p, --printable will NOT match --print.
    """
    noninteractive_flags: dict[str, bool] = {}
    for flag, pattern in FLAG_PATTERNS.items():
        noninteractive_flags[flag] = bool(pattern.search(help_text))

    unexpected_capabilities: list[str] = []
    for keyword in UNEXPECTED_CAPABILITY_KEYWORDS:
        if keyword in help_text:
            unexpected_capabilities.append(keyword)

    return noninteractive_flags, unexpected_capabilities


def _run_smoke(agy_bin: str) -> dict[str, Any]:
    """Run smoke check: `agy -p <SMOKE_PROMPT>` in isolated temp cwd.

    Returns dict with ok, argv, exit_code, timed_out, stdout_sample, stderr_sample.
    Success requires exit_code == 0 AND exact sentinel stdout.
    """
    argv = [agy_bin, "-p", SMOKE_PROMPT]
    smoke: dict[str, Any] = {
        "ok": False,
        "argv": argv,
        "exit_code": None,
        "timed_out": False,
        "failure_reason": None,
        "failure_class": None,
        "stdout_sample": "",
        "stderr_sample": "",
    }

    with tempfile.TemporaryDirectory(prefix="agy-preflight-") as temp_dir:
        try:
            proc = _run(argv, cwd=Path(temp_dir), timeout=SMOKE_TIMEOUT_SECONDS)
            smoke["exit_code"] = proc.returncode
            smoke["stdout_sample"] = _redact_output_sample(proc.stdout)
            smoke["stderr_sample"] = _redact_output_sample(proc.stderr)
            stdout = proc.stdout or ""

            if proc.returncode != 0:
                smoke["failure_reason"] = f"agy smoke command exited {proc.returncode}"
                smoke["failure_class"] = "agy_smoke_exit_nonzero"
            elif not stdout.strip():
                is_ci = os.environ.get("CI", "").lower() in {"1", "true", "yes", "on"}
                smoke["failure_reason"] = "agy_output_missing"
                smoke["failure_class"] = "agy_output_missing" if is_ci else "agy_empty_stdout"
            elif stdout.strip() != EXPECTED_SMOKE:
                # Issue #1267 fix_delta Blocker 3: never embed raw (unredacted) agy
                # stdout in failure_reason — it may contain an OAuth authorization
                # URL. Reuse the same redaction+truncation path as stdout_sample.
                redacted_mismatch_sample = _redact_output_sample(stdout.strip())
                smoke["failure_reason"] = f"agy_output_mismatch: got {redacted_mismatch_sample!r}"
                smoke["failure_class"] = "agy_output_mismatch"
            else:
                smoke["ok"] = True

            if not smoke["ok"]:
                # Issue #1267 Required Result Contract: only reclassify as an
                # auth/keyring failure when stderr/stdout contains explicit evidence.
                # Empty stdout (agy_empty_stdout / agy_output_missing) MUST remain an
                # output-surface failure otherwise.
                combined = "\n".join(part for part in [proc.stdout, proc.stderr] if part)
                auth_signal = _classify_auth_signal(combined)
                if auth_signal:
                    smoke["failure_class"] = auth_signal
                    smoke["failure_reason"] = (
                        f"{smoke['failure_reason']} (auth evidence detected: {auth_signal})"
                    )
        except subprocess.TimeoutExpired:
            smoke["timed_out"] = True

    return smoke


def _run_grounded_research_smoke(agy_bin: str) -> dict[str, Any]:
    """Run a bounded AGY native WebSearch/grounding probe.

    This smoke intentionally favors a lightweight query and records evidence
    samples so caller can verify that web search output can be produced.

    Success requires a machine-verifiable structured `tool_calls` trace naming a
    recognized web tool (see RECOGNIZED_WEB_TOOL_NAMES). A bare URL string appearing in
    stdout without this structured trace is weak evidence only and is never treated as
    proof of a WebSearch tool-call execution; `web_tool_call_count` is never inferred
    from a URL count alone (Issue #1266 Blocker 1 — this preflight smoke path had not
    been migrated to the same fail-closed contract already applied to
    run_gemini_headless.py).
    """
    argv = [agy_bin, "-p", GROUNDING_PROBE_PROMPT]
    result: dict[str, Any] = {
        "ok": False,
        "argv": argv,
        "exit_code": None,
        "timed_out": False,
        "failure_reason": None,
        "failure_class": None,
        "stdout_sample": "",
        "stderr_sample": "",
        "evidence_urls": [],
        "web_tool_call_count": 0,
        "url_citation_count": 0,
        "stdout_line_count": 0,
        "tool_calls_verified": False,
    }

    with tempfile.TemporaryDirectory(prefix="agy-preflight-grounding-") as temp_dir:
        try:
            proc = _run(argv, cwd=Path(temp_dir), timeout=GROUNDING_TIMEOUT_SECONDS)
            result["exit_code"] = proc.returncode
            result["stdout_sample"] = _redact_output_sample(proc.stdout)
            result["stderr_sample"] = _redact_output_sample(proc.stderr)
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            result["stdout_line_count"] = len([line for line in stdout.splitlines() if line.strip()])
            # Bounded to 1 URL (Issue #1266 Major 1: 1 query / 1 URL quota-bound contract).
            urls = _extract_urls(stdout)[:1]
            result["evidence_urls"] = urls
            result["url_citation_count"] = len(urls)

            parsed = _extract_grounded_research_output(stdout)
            tool_calls = _extract_recognized_tool_calls(parsed)
            result["tool_calls_verified"] = bool(tool_calls)
            result["web_tool_call_count"] = min(len(tool_calls), 1)

            combined_output = "\n".join([stdout, stderr])
            if _QUOTA_EXHAUSTED_RE.search(combined_output) or _HTTP_429_RE.search(combined_output):
                result["failure_reason"] = "agy_grounded_research quota exhausted"
                result["failure_class"] = "agy_grounded_research_quota_exhausted"
            elif proc.returncode != 0:
                result["failure_reason"] = f"agy_grounded_research check failed: exit {proc.returncode}"
                result["failure_class"] = "agy_grounded_research_exit_nonzero"
            elif not urls and not stdout.strip():
                is_ci = os.environ.get("CI", "").lower() in {"1", "true", "yes", "on"}
                result["failure_reason"] = (
                    "agy_grounded_research output empty"
                    + (" in CI" if is_ci else "")
                )
                result["failure_class"] = "agy_output_missing" if is_ci else "agy_empty_stdout"
            elif not urls:
                result["failure_reason"] = "agy_grounded_research no_evidence_urls_found"
                result["failure_class"] = "agy_grounded_research_no_evidence"
            elif not tool_calls:
                # Issue #1266 Blocker 1: a bare URL string is never treated as proof of a
                # WebSearch tool-call execution without a machine-verifiable structured
                # tool_calls trace naming a recognized web tool.
                result["web_tool_call_count"] = 0
                result["failure_reason"] = (
                    "agy_grounded_research no machine-verifiable web tool-call trace found"
                )
                result["failure_class"] = "agy_web_grounding_tool_call_missing"
            else:
                result["ok"] = True
        except subprocess.TimeoutExpired:
            result["timed_out"] = True
            result["failure_reason"] = "agy grounded_research timed out"
            result["failure_class"] = "client_subprocess_timeout"

    return result


def run_preflight(
    *,
    validate_local_asset_contract: bool = False,
    live_serena: bool = False,
    mcp_config_path: Path | None = None,
    grounded_research: bool = False,
) -> dict[str, Any]:
    """Run version → help → smoke checks for agy binary.

    Returns an agy_preflight_result/v1 dict.
    """
    agy_bin = _resolve_binary()

    result: dict[str, Any] = {
        "schema": "agy_preflight_result/v1",
        "ok": False,
        "failure_reason": None,
        "failure_class": None,
        "recovery_action": None,
        "agy": {
            "bin": agy_bin,
            "resolved_path": None,
            "version": None,
        },
        "help": {
            "ok": False,
            "noninteractive_flags": {"-p": False, "--print": False, "--prompt": False},
            "unexpected_capabilities": [],
            "stdout_sample": "",
            "stderr_sample": "",
        },
        "smoke": {
            "ok": False,
            "argv": [],
            "exit_code": None,
            "timed_out": False,
            "stdout_sample": "",
            "stderr_sample": "",
        },
        "grounded_research": {
            "ok": False,
            "requested": grounded_research,
            "check": None,
        },
        "warnings": [],
    }
    # Issue #1267: auth is attached to every return path. It is initialised here
    # (no agy output yet) and refined with smoke output evidence below.
    result["auth"] = _build_auth_diagnostics()

    # Step 1: version check
    try:
        version_proc = _run_version(agy_bin)
    except FileNotFoundError:
        result["failure_reason"] = f"{agy_bin}: command not found"
        result["failure_class"] = "cli_missing"
        result["recovery_action"] = "install agy or set AGY_BIN to a valid path"
        result["warnings"].append(result["failure_reason"])
        return result

    if version_proc.returncode != 0:
        result["failure_reason"] = f"agy --version failed (exit {version_proc.returncode})"
        result["failure_class"] = "cli_missing"
        result["warnings"].append(result["failure_reason"])
        return result

    version_str = version_proc.stdout.strip() or None
    result["agy"]["version"] = version_str
    try:
        import shutil
        resolved = shutil.which(agy_bin)
        result["agy"]["resolved_path"] = _mask_resolved_path(resolved)
    except Exception:
        pass

    # Step 2: help check
    try:
        help_proc = _run_help(agy_bin)
    except FileNotFoundError:
        result["failure_reason"] = f"{agy_bin}: command not found"
        result["failure_class"] = "cli_missing"
        result["warnings"].append(result["failure_reason"])
        return result

    if help_proc.returncode != 0:
        result["failure_reason"] = "agy --help failed"
        result["failure_class"] = "cli_incompatible"
        result["warnings"].append(result["failure_reason"])
        return result

    # Store redacted help output as live probe evidence.
    result["help"]["stdout_sample"] = _redact_output_sample(help_proc.stdout)
    result["help"]["stderr_sample"] = _redact_output_sample(help_proc.stderr)

    help_text = "\n".join(part for part in [help_proc.stdout, help_proc.stderr] if part)
    noninteractive_flags, unexpected_capabilities = _parse_help_capabilities(help_text)
    result["help"]["noninteractive_flags"] = noninteractive_flags
    result["help"]["unexpected_capabilities"] = unexpected_capabilities

    has_noninteractive = any(noninteractive_flags.values())
    result["help"]["ok"] = has_noninteractive

    if not has_noninteractive:
        result["failure_reason"] = "agy --help is missing noninteractive flags (-p / --print / --prompt)"
        result["failure_class"] = "cli_incompatible"
        result["recovery_action"] = "upgrade agy to a version that supports -p / --print / --prompt"
        return result

    # Step 3: smoke check
    try:
        smoke = _run_smoke(agy_bin)
    except subprocess.TimeoutExpired:
        smoke = {
            "ok": False,
            "argv": [agy_bin, "-p", SMOKE_PROMPT],
            "exit_code": None,
            "timed_out": True,
            "failure_reason": "agy smoke timed out",
            "failure_class": "client_subprocess_timeout",
            "stdout_sample": "",
            "stderr_sample": "",
        }

    result["smoke"] = smoke

    combined_smoke_output = "\n".join(
        part for part in [smoke.get("stdout_sample", ""), smoke.get("stderr_sample", "")] if part
    )
    result["auth"] = _build_auth_diagnostics(
        combined_output=combined_smoke_output,
        smoke_ok=smoke.get("ok"),
    )

    if smoke["timed_out"]:
        result["failure_reason"] = "agy smoke check timed out"
        result["failure_class"] = "client_subprocess_timeout"
        result["recovery_action"] = "check agy network connectivity or increase timeout"
        return result

    if not smoke["ok"]:
        result["failure_reason"] = smoke.get("failure_reason") or "agy smoke check failed"
        result["failure_class"] = smoke.get("failure_class") or "agy_output_missing"
        result["recovery_action"] = (
            result["auth"].get("recovery_action") or "check agy configuration and rerun preflight"
        )
        return result

    if grounded_research:
        grounded_result = _run_grounded_research_smoke(agy_bin)
        result["grounded_research"]["check"] = grounded_result
        if not grounded_result["ok"]:
            result["failure_reason"] = grounded_result.get("failure_reason") or "agy grounded_research probe failed"
            result["failure_class"] = grounded_result.get("failure_class") or "agy_grounded_research_failed"
            result["recovery_action"] = "check AGY WebSearch/WebGrounding connectivity and rerun preflight"
            return result
        result["grounded_research"]["ok"] = True

    if validate_local_asset_contract:
        repo_root = _repo_root()
        manifest: dict[str, Any] | None = None
        try:
            manifest = load_serena_tool_manifest(repo_root)
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
            manifest = None
            contract_errors = [f"local_asset_research serena manifest validation failed: {exc}"]
        else:
            contract_errors = _validate_local_asset_serena_contract(repo_root, mcp_config_path)
        local_asset_result = {
            "ok": not contract_errors,
            "errors": contract_errors,
            "unknown_tool_policy": LOCAL_ASSET_SERENA_TOOL_POLICY,
            "config_path": str((mcp_config_path or AGY_MCP_CONFIG_RELATIVE_PATH).as_posix()),
        }
        if live_serena and not contract_errors and manifest is not None:
            try:
                serena_result = _call_serena_mcp_live(repo_root, manifest, mcp_config_path)
                local_asset_result["serena"] = {
                    key: value for key, value in serena_result.items() if key != "transcript"
                }
                local_asset_result["live_transcript"] = serena_result["transcript"]
            except Exception as exc:
                local_asset_result["ok"] = False
                local_asset_result["errors"] = [f"local_asset_research live_serena_probe_failed: {exc}"]
        if local_asset_result["ok"]:
            local_asset_result["status"] = "ok"
        else:
            result["failure_reason"] = local_asset_result["errors"][0]
            result["failure_class"] = "local_asset_contract_invalid"
            result["recovery_action"] = "fix .agents/mcp_config.json Serena contract for local_asset_research"
        result["local_asset_research"] = local_asset_result

    if result.get("local_asset_research") is not None and not result["local_asset_research"]["ok"]:
        result["ok"] = False
        return result

    result["ok"] = True
    return result


def build_evidence_envelope(
    result: dict[str, Any],
    *,
    issue_number: int,
    captured_at: str,
) -> dict[str, Any]:
    """Build the checked-in `agy_web_grounding_evidence_v1` envelope directly from a
    `run_preflight(grounded_research=True)` result.

    This is the single source of truth for checked-in evidence: every field is read from
    *result* (no independent/hand-authored values), so the generated markdown and the PR body
    can never drift from the same underlying preflight run (Issue #1266 Blocker 4).
    """
    check = ((result.get("grounded_research") or {}).get("check")) or {}
    urls = check.get("evidence_urls") or []
    stdout_sample = check.get("stdout_sample") or ""
    return {
        "issue_number": issue_number,
        "captured_at": captured_at,
        "agy_web_grounding_evidence_v1": {
            "grounding_actor": "antigravity_cli",
            "grounding_backend": "agy_native_websearch" if check.get("ok") else "none",
            "prompt_shape": "bounded_websearch_probe",
            "agy_cli_version": result.get("agy", {}).get("version"),
            "command_exit_code": check.get("exit_code"),
            "web_tool_call_count": check.get("web_tool_call_count", 0),
            "search_query_count": 1,
            "url_citation_count": check.get("url_citation_count", 0),
            "search_queries": [GROUNDING_PROBE_PROMPT],
            "citations": [{"url": url, "title": None, "cited_text_snippet": None} for url in urls],
            "transcript_evidence": [
                {
                    "source_kind": "agy_stdout_or_artifact_excerpt",
                    "excerpt": stdout_sample,
                    "sha256": hashlib.sha256(stdout_sample.encode("utf-8")).hexdigest(),
                }
            ],
            "redaction_status": "checked_no_secret_pattern",
            "raw_transcript_included": False,
            "raw_credential_included": False,
            "repo_absolute_path_included": False,
            "failure_class": check.get("failure_class"),
        },
    }


def _yaml_scalar(value: Any) -> str:
    """Render *value* as a bounded single-line YAML scalar (null / quoted string)."""
    if value is None:
        return "null"
    text_value = str(value).strip().replace("\n", " ").replace('"', "'")
    return f'"{text_value}"'


def render_evidence_markdown(envelope: dict[str, Any]) -> str:
    """Render the checked-in evidence markdown document from *envelope*.

    *envelope* must come from `build_evidence_envelope()` so that every value (citations,
    sha256, exit code) is traceable to the exact preflight run that produced it.
    """
    evidence = envelope["agy_web_grounding_evidence_v1"]
    citations_lines = "\n".join(
        f'    - url: {_yaml_scalar(citation["url"])}\n'
        f'      title: {_yaml_scalar(citation["title"])}\n'
        f'      cited_text_snippet: {_yaml_scalar(citation["cited_text_snippet"])}'
        for citation in evidence["citations"]
    ) or "    []"
    transcript = evidence["transcript_evidence"][0]
    lines = [
        "# Live AGY Native WebSearch Evidence",
        "",
        f"Issue: `#{envelope['issue_number']}`（対象 Issue）",
        "Provider/profile: `provider=agy + tool_profile=grounded_research`（プロバイダ / プロファイル）",
        f"Captured at: `{envelope['captured_at']}`（取得日時）",
        "",
        "## Command（実行コマンド）",
        "",
        "```bash",
        "uv run --locked python3 .claude/skills/gemini-cli-headless-delegation/scripts/preflight_agy.py "
        "--grounded-research --json",
        "```",
        "",
        "## Sanitized Result（サニタイズ済み結果）",
        "",
        "```yaml",
        "agy_web_grounding_evidence_v1:",
        f"  grounding_actor: {evidence['grounding_actor']}",
        f"  grounding_backend: {evidence['grounding_backend']}",
        f"  prompt_shape: {evidence['prompt_shape']}",
        f'  agy_cli_version: "{evidence["agy_cli_version"]}"',
        f"  command_exit_code: {evidence['command_exit_code']}",
        f"  web_tool_call_count: {evidence['web_tool_call_count']}",
        f"  search_query_count: {evidence['search_query_count']}",
        f"  url_citation_count: {evidence['url_citation_count']}",
        "  search_queries:",
        *[f'    - "{query}"' for query in evidence["search_queries"]],
        "  citations:",
        citations_lines,
        "  transcript_evidence:",
        "    - source_kind: agy_stdout_or_artifact_excerpt",
        f"      excerpt: {_yaml_scalar(transcript['excerpt'])}",
        f'      sha256: "{transcript["sha256"]}"',
        f"  redaction_status: {evidence['redaction_status']}",
        f"  raw_transcript_included: {str(evidence['raw_transcript_included']).lower()}",
        f"  raw_credential_included: {str(evidence['raw_credential_included']).lower()}",
        f"  repo_absolute_path_included: {str(evidence['repo_absolute_path_included']).lower()}",
        f"  failure_class: {_yaml_scalar(evidence['failure_class'])}",
        "```",
        "",
        "## Boundary Claim（境界主張）",
        "",
        "This evidence was produced by AGY native `agy -p` execution through "
        "`preflight_agy.py --grounded-research`.",
        "It is not Gemini API Google Search grounding, not wrapper-side web retrieval, and not "
        "fixture-only evidence.",
        "この証跡は AGY ネイティブの `agy -p` "
        "実行を通じて取得したものであり、"
        "Gemini API の Google Search grounding でも wrapper 側の Web "
        "取得でもなく、fixture のみの証跡でも"
        "ないことを明示する。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Entry point for CLI invocation.

    --json: print result to stdout as JSON
    --output-file: write result to file
    Success exits 0, failure exits 1.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_stdout",
        default=False,
        help="Print the preflight result JSON to stdout.",
    )
    parser.add_argument(
        "--local-asset-research",
        action="store_true",
        dest="local_asset_research",
        default=False,
        help="Also validate local_asset_research Serena tool contract.",
    )
    parser.add_argument(
        "--mcp-config",
        required=False,
        type=Path,
        default=None,
        help="AGY project MCP config path. Defaults to .agents/mcp_config.json.",
    )
    parser.add_argument(
        "--live-serena",
        action="store_true",
        dest="live_serena",
        default=False,
        help="Launch the pinned Serena MCP server and run live read-only tool calls.",
    )
    parser.add_argument(
        "--grounded-research",
        "--live-websearch",
        "--discover-web-grounding",
        action="store_true",
        dest="grounded_research",
        default=False,
        help="Run a bounded AGY native WebSearch/WebGrounding probe.",
    )
    parser.add_argument(
        "--output-file",
        required=False,
        type=Path,
        default=None,
        help="Path to write the preflight result JSON.",
    )
    parser.add_argument(
        "--render-evidence-doc",
        required=False,
        type=Path,
        default=None,
        help=(
            "Render docs/dev/agy-grounded-research-evidence.md from this run's grounded_research "
            "result and write it to the given path. Requires --grounded-research."
        ),
    )
    parser.add_argument(
        "--evidence-issue-number",
        required=False,
        type=int,
        default=1266,
        help="Issue number to record in the rendered evidence doc.",
    )
    args = parser.parse_args(argv)

    result = run_preflight(
        validate_local_asset_contract=args.local_asset_research,
        live_serena=args.live_serena,
        mcp_config_path=args.mcp_config,
        grounded_research=args.grounded_research,
    )

    if args.json_stdout:
        print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.output_file is not None:
        args.output_file.parent.mkdir(parents=True, exist_ok=True)
        with args.output_file.open("w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    if args.render_evidence_doc is not None:
        import datetime

        if not args.grounded_research:
            raise SystemExit("--render-evidence-doc requires --grounded-research")
        captured_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        envelope = build_evidence_envelope(
            result,
            issue_number=args.evidence_issue_number,
            captured_at=captured_at,
        )
        markdown = render_evidence_markdown(envelope)
        args.render_evidence_doc.parent.mkdir(parents=True, exist_ok=True)
        args.render_evidence_doc.write_text(markdown, encoding="utf-8")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
