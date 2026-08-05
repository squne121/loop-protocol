#!/usr/bin/env python3
"""Preflight agy CLI headless support: detect agy --help / agy -p contract."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

EXPECTED_SMOKE = "LOOP_AGY_SMOKE_OK"
SMOKE_PROMPT = f"Return exactly: {EXPECTED_SMOKE}"
SMOKE_TIMEOUT_SECONDS = 20
# Issue #1941 fix_delta P1-3: the `leading_slash_is_literal` runtime probe is a
# real, model-backed `agy -p` call — there is no mechanical way to prove it is
# free. Rather than silently spending quota/cost on every capability
# computation, the probe is skipped by default unless this env var is
# explicitly set (never inferred, never on by default).
AGY_RUNTIME_PROBE_COST_CONFIRM_ENV_VAR = "AGY_PREFLIGHT_CONFIRM_RUNTIME_PROBE_COST"
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


def _detect_gcloud_adc(env_home: str | None = None) -> dict[str, Any]:
    """Detect gcloud Application Default Credentials (ADC) file-based presence.

    Existence-check only (Issue #1730 AC7) -- never opens or reads the ADC
    file / token DB content, only whether the well-known gcloud config paths
    exist under `$HOME/.config/gcloud`. This is a distinct auth channel from
    `_detect_keyring()`'s D-Bus secret-service inference: gcloud ADC is a
    file-based auth cache that works even when no D-Bus session bus is
    present at all (the exact scenario Issue #1730's Source section
    describes: WSL2, `DBUS_SESSION_BUS_ADDRESS`/`XDG_RUNTIME_DIR` propagated,
    but auth still resolves via `$HOME/.config/gcloud`, not a keyring).
    """
    real_home = env_home if env_home is not None else os.environ.get("HOME")
    if not real_home:
        return {
            "config_dir_present": False,
            "adc_file_present": False,
            "access_tokens_db_present": False,
        }
    gcloud_dir = Path(real_home) / ".config" / "gcloud"
    adc_file = gcloud_dir / "application_default_credentials.json"
    tokens_db = gcloud_dir / "access_tokens.db"
    try:
        config_dir_present = gcloud_dir.is_dir()
    except OSError:
        config_dir_present = False
    try:
        adc_file_present = adc_file.is_file()
    except OSError:
        adc_file_present = False
    try:
        access_tokens_db_present = tokens_db.is_file()
    except OSError:
        access_tokens_db_present = False
    return {
        "config_dir_present": config_dir_present,
        "adc_file_present": adc_file_present,
        "access_tokens_db_present": access_tokens_db_present,
    }


# Issue #1740: `agy` (Antigravity CLI) does not authenticate via dbus
# secret-service (#1726) or gcloud ADC (#1730). Diagnosis during #1494's
# third live fan-out attempt confirmed it uses its own OAuth token file,
# `$HOME/.gemini/antigravity-cli/antigravity-oauth-token` (mode 600) -- see
# Issue #1740 Source section.
ANTIGRAVITY_CLI_DIRNAME = "antigravity-cli"
AGY_OAUTH_TOKEN_FILENAME = "antigravity-oauth-token"


def _detect_agy_oauth_token(env_home: str | None = None) -> dict[str, Any]:
    """Detect agy's own OAuth token file-based auth cache presence.

    Existence-check only (Issue #1740 AC3) -- never opens or reads the token
    file content, only whether
    `$HOME/.gemini/antigravity-cli/antigravity-oauth-token` exists. This is
    the actual auth channel `agy` uses; it is distinct from both
    `_detect_keyring()`'s D-Bus secret-service inference (#1726) and
    `_detect_gcloud_adc()`'s gcloud ADC file-based cache (#1730), neither of
    which `agy` consults for auth (confirmed during #1740's diagnosis).
    """
    real_home = env_home if env_home is not None else os.environ.get("HOME")
    if not real_home:
        return {"token_file_present": False}
    token_file = Path(real_home) / ".gemini" / ANTIGRAVITY_CLI_DIRNAME / AGY_OAUTH_TOKEN_FILENAME
    try:
        token_file_present = token_file.is_file()
    except OSError:
        token_file_present = False
    return {"token_file_present": token_file_present}


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
    gcloud_adc_info = _detect_gcloud_adc()
    agy_oauth_token_info = _detect_agy_oauth_token()
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
    elif agy_oauth_token_info.get("token_file_present"):
        # Issue #1740: agy's own OAuth token file is the actual auth channel
        # `agy` uses -- confirmed during #1494's live fan-out diagnosis that
        # neither dbus secret-service (#1726) nor gcloud ADC (#1730) resolve
        # `agy_auth_required` on their own. Checked ahead of the gcloud ADC /
        # keyring-failure_class fallbacks below so a real, existing agy OAuth
        # token session is not misreported as "unauthenticated" or
        # "gcloud_adc_file_based" purely because those other signals are also
        # present or absent.
        auth_mode, auth_mode_confidence = "agy_oauth_token_file_based", "inferred"
    elif gcloud_adc_info.get("adc_file_present") or gcloud_adc_info.get("access_tokens_db_present"):
        # Issue #1730: gcloud ADC is a file-based auth cache that does not
        # require a D-Bus session bus at all. Checked ahead of the
        # keyring-failure_class fallback below so that a real, existing
        # gcloud ADC session is not misreported as "unauthenticated" purely
        # because no D-Bus session bus is present (Issue #1730 Source: this
        # is exactly the WSL2 fan-out scenario that motivated this Issue).
        # Issue #1740 diagnosis established gcloud ADC is not actually
        # consulted by `agy` for auth, so this branch is now reached only
        # when the agy OAuth token file is absent -- see the
        # `agy_oauth_token_file_based` branch above, which takes priority.
        auth_mode, auth_mode_confidence = "gcloud_adc_file_based", "inferred"
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
        # Issue #1730 AC7: gcloud ADC file-based auth cache presence
        # (existence-check only -- never the file content).
        "gcloud_adc": gcloud_adc_info,
        # Issue #1740 AC9: agy's own OAuth token file presence
        # (existence-check only -- never the file content). This is the
        # actual auth channel `agy` uses.
        "agy_oauth_token": agy_oauth_token_info,
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
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """subprocess.run wrapper — shell=False is enforced.

    *env* defaults to `_minimal_agy_env()` (pass-through of the real user's
    allowlisted env). Callers that need an isolated environment (e.g. the
    model-backed runtime probe — Issue #1941 fix_delta P1-3) pass an explicit
    override so global `~/.gemini/config/` hooks/permissions/skills/plugins
    are never implicitly loaded by that probe.
    """
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=env if env is not None else _minimal_agy_env(),
        shell=False,
    )


def _isolated_probe_env(isolated_home: Path) -> dict[str, str]:
    """Return an env for a model-backed probe subprocess with HOME/XDG_*
    overridden to a temp root (Issue #1941 fix_delta P1-3).

    Starts from the same allowlisted `_minimal_agy_env()` base (never leaks
    unrelated env values) but rebinds HOME/XDG_CONFIG_HOME/XDG_CACHE_HOME/
    XDG_STATE_HOME to *isolated_home* so the real user's global
    `~/.gemini/config/` hooks/permissions/skills/plugins cannot be discovered
    or loaded by this probe — only the specific read-only credential channel
    (an isolated, empty HOME) is bound.
    """
    env = _minimal_agy_env()
    isolated_home_str = str(isolated_home)
    env["HOME"] = isolated_home_str
    env["XDG_CONFIG_HOME"] = str(isolated_home / ".config")
    env["XDG_CACHE_HOME"] = str(isolated_home / ".cache")
    env["XDG_STATE_HOME"] = str(isolated_home / ".local" / "state")
    return env


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


def _early_exit_capability_matrix(result: dict[str, Any], reason_code: str) -> dict[str, Any]:
    """Build a full-shaped, fail-closed capability matrix for a controlled
    early-exit path (Issue #1941 fix_delta P2-1).

    Controlled early exits (binary missing, help failure, smoke/auth
    failure, grounded-research failure, local-asset-contract failure)
    previously returned before ever reaching the capability-computation
    block, so `--require-capability` mode silently evaluated an effectively
    empty matrix with no record of *why* each predicate was unavailable.
    This builds the real matrix shape using whatever version evidence is
    already available, with every predicate that has no probe evidence
    recorded as `unavailable` and *reason_code* explaining which early-exit
    condition prevented the probe from running.
    """
    version_result = result.get("version_evidence") or {
        "status": "version_evidence_invalid",
        "version": None,
        "core": None,
        "raw": None,
    }
    matrix = build_capability_matrix(
        version_result=version_result,
        disable_slash_probe=None,
        leading_slash_probe=None,
    )
    for predicates in matrix.values():
        for entry in predicates.values():
            if entry.get("reason_code") == "probe_not_run":
                entry["reason_code"] = reason_code
    return matrix


def _finalize_early_exit(result: dict[str, Any], compute_capabilities: bool, reason_code: str) -> dict[str, Any]:
    """`_finalize()` wrapper for controlled early-exit return paths that also
    requested `compute_capabilities=True` (Issue #1941 fix_delta P2-1)."""
    if compute_capabilities:
        result.setdefault("capabilities", _early_exit_capability_matrix(result, reason_code))
        result.setdefault("capability_probes", {})
        result.setdefault("capability_schema", CAPABILITY_MATRIX_SCHEMA_VERSION)
    return _finalize(result)


def run_preflight(
    *,
    validate_local_asset_contract: bool = False,
    live_serena: bool = False,
    mcp_config_path: Path | None = None,
    grounded_research: bool = False,
    compute_capabilities: bool = False,
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

    # Issue #1941 fix_delta P1-1: resolve the executable exactly once, at the
    # very start, and bind every subsequent probe invocation (version / help /
    # smoke / capability probes) to this single resolved absolute path. The
    # binary-identity fingerprint (`binary_identity` / `binary_identity_after`)
    # is measured against exactly what was executed — never re-resolved by
    # name mid-run, which previously left a TOCTOU/PATH-swap gap where a probe
    # could silently execute a different binary than the one fingerprinted.
    _resolved_realpath: str | None = None
    try:
        which_result = shutil.which(agy_bin)
        if which_result:
            _resolved_realpath = os.path.realpath(which_result)
    except Exception:
        pass
    exec_bin = _resolved_realpath if _resolved_realpath else agy_bin
    result["agy"]["resolved_path"] = _mask_resolved_path(_resolved_realpath)

    binary_identity_before = compute_binary_identity(_resolved_realpath)
    result["binary_identity"] = binary_identity_before

    # Step 1: version check
    try:
        version_proc = _run_version(exec_bin)
    except FileNotFoundError:
        result["failure_reason"] = f"{agy_bin}: command not found"
        result["failure_class"] = "cli_missing"
        result["recovery_action"] = "install agy or set AGY_BIN to a valid path"
        result["warnings"].append(result["failure_reason"])
        return _finalize_early_exit(result, compute_capabilities, "cli_missing")

    if version_proc.returncode != 0:
        result["failure_reason"] = f"agy --version failed (exit {version_proc.returncode})"
        result["failure_class"] = "cli_missing"
        result["warnings"].append(result["failure_reason"])
        return _finalize_early_exit(result, compute_capabilities, "cli_missing")

    version_str = version_proc.stdout.strip() or None
    result["agy"]["version"] = version_str
    combined_version_text = "\n".join(
        part for part in [version_proc.stdout, version_proc.stderr] if part
    )
    result["version_evidence"] = parse_agy_version_string(combined_version_text)

    # Step 2: help check
    try:
        help_proc = _run_help(exec_bin)
    except FileNotFoundError:
        result["failure_reason"] = f"{agy_bin}: command not found"
        result["failure_class"] = "cli_missing"
        result["warnings"].append(result["failure_reason"])
        return _finalize_early_exit(result, compute_capabilities, "cli_missing")

    if help_proc.returncode != 0:
        result["failure_reason"] = "agy --help failed"
        result["failure_class"] = "cli_incompatible"
        result["warnings"].append(result["failure_reason"])
        return _finalize_early_exit(result, compute_capabilities, "help_unavailable")

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
        # Issue #1941 In Scope: help non-listing is supporting evidence only.
        # It is recorded as a warning, but the actual fixed-argv runtime smoke
        # below is still the authority on whether -p/--print/--prompt work.
        result["warnings"].append(
            "agy --help does not list -p/--print/--prompt; deferring to runtime smoke (PR #1976 design)"
        )

    # Step 3: smoke check
    try:
        smoke = _run_smoke(exec_bin)
    except subprocess.TimeoutExpired:
        smoke = {
            "ok": False,
            "argv": [exec_bin, "-p", SMOKE_PROMPT],
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
        return _finalize_early_exit(result, compute_capabilities, "smoke_timed_out")

    if not smoke["ok"]:
        result["failure_reason"] = smoke.get("failure_reason") or "agy smoke check failed"
        result["failure_class"] = smoke.get("failure_class") or "agy_output_missing"
        result["recovery_action"] = (
            result["auth"].get("recovery_action") or "check agy configuration and rerun preflight"
        )
        _auth_failure_classes = {
            "system_keyring_locked",
            "system_keyring_access_denied",
            "system_keyring_backend_missing",
            "system_keyring_unavailable",
            "google_sign_in_required",
            "noninteractive_auth_prompt_required",
            "agy_auth_unknown",
        }
        if result["failure_class"] in _auth_failure_classes:
            early_exit_reason = "auth_blocked_probe"
        else:
            early_exit_reason = "smoke_check_failed"
        return _finalize_early_exit(result, compute_capabilities, early_exit_reason)

    if grounded_research:
        grounded_result = _run_grounded_research_smoke(exec_bin)
        result["grounded_research"]["check"] = grounded_result
        if not grounded_result["ok"]:
            result["failure_reason"] = grounded_result.get("failure_reason") or "agy grounded_research probe failed"
            result["failure_class"] = grounded_result.get("failure_class") or "agy_grounded_research_failed"
            result["recovery_action"] = "check AGY WebSearch/WebGrounding connectivity and rerun preflight"
            return _finalize_early_exit(result, compute_capabilities, "grounded_research_failed")
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
        return _finalize_early_exit(result, compute_capabilities, "local_asset_contract_invalid")

    if compute_capabilities:
        capability_probes: dict[str, Any] = {}
        try:
            capability_probes["disable_slash_commands"] = _run_disable_slash_commands_probe(exec_bin)
        except Exception as exc:  # pragma: no cover - defensive fail-closed path
            result["warnings"].append(f"capability_probe_failed: {exc}")

        # Issue #1941 fix_delta P1-3 / P1-7: the memoization cache lookup
        # happens BEFORE the potentially costly, model-backed
        # leading_slash_literal runtime probe runs -- a cache hit skips that
        # probe entirely rather than always paying for it and discarding the
        # result. The cache key binds BOTH the pre-run and
        # pre-runtime-probe binary identities (not a single snapshot), so any
        # drift between them always causes a cache miss/bypass instead of
        # silently reusing a matrix computed against a different binary.
        binary_identity_pre_runtime_probe = compute_binary_identity(_resolved_realpath)
        config_digest = compute_config_digest()

        def _compute_bundle() -> dict[str, Any]:
            try:
                if _runtime_probe_cost_confirmed():
                    leading_slash_probe = _run_leading_slash_literal_probe(exec_bin)
                else:
                    # Issue #1941 fix_delta P1-3: there is no mechanical way to
                    # prove `agy -p <prompt>` is free -- skip the real
                    # model-backed call rather than silently spending
                    # quota/cost, and record `unavailable` instead.
                    leading_slash_probe = {"skipped": True, "skip_reason": "runtime_probe_cost_unconfirmed"}
            except Exception as exc:  # pragma: no cover - defensive fail-closed path
                result["warnings"].append(f"capability_probe_failed: {exc}")
                leading_slash_probe = None
            capability_probes["leading_slash_literal"] = leading_slash_probe
            binary_identity_after = compute_binary_identity(_resolved_realpath)
            matrix = build_capability_matrix(
                version_result=result["version_evidence"],
                disable_slash_probe=capability_probes.get("disable_slash_commands"),
                leading_slash_probe=leading_slash_probe,
                binary_identity_before=binary_identity_before,
                binary_identity_after=binary_identity_after,
            )
            return {
                "matrix": matrix,
                "capability_probes": dict(capability_probes),
                "binary_identity_after": binary_identity_after,
            }

        bundle = get_or_compute_capability_matrix(
            binary_identity_before, binary_identity_pre_runtime_probe, config_digest, _compute_bundle
        )
        result["capabilities"] = bundle["matrix"]
        result["capability_probes"] = bundle["capability_probes"]
        result["binary_identity_after"] = bundle["binary_identity_after"]
        result["capability_schema"] = CAPABILITY_MATRIX_SCHEMA_VERSION

    result["ok"] = True
    return _finalize(result)


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



# ---------------------------------------------------------------------------
# Capability gate (Issue #1941 — agy_capability_matrix/v1)
#
# preflight_agy.py is the single implementation SSOT for capability detection.
# Consumers (setup_check.py, run_gemini_headless.py) MUST consume this result
# and MUST NOT re-implement a version parser or help-text parser of their own
# (Issue #1941 AC3).
#
# Evidence priority (highest authority first): runtime_semantic_observation >
# parser_acceptance > changelog > help > version. `help` and `version` are
# supporting evidence only and never independently confirm `supported`
# (Issue #1941 Outcome / AC2 / AC6).
# ---------------------------------------------------------------------------

CAPABILITY_MATRIX_SCHEMA_VERSION = "agy_capability_matrix/v1"

CAPABILITY_STATUSES = frozenset(
    {"supported", "unsupported", "unavailable", "inconclusive", "evidence_invalid"}
)

CAPABILITY_PREDICATES: dict[str, list[str]] = {
    "headless_permission_policy": [
        "persisted_settings_loaded",
        "deny_precedence_enforced",
        "ask_is_soft_denied_noninteractive",
    ],
    "hooks": [
        "workspace_hooks_config_loaded",
        "pre_invocation_hook_dispatch",
        "pre_invocation_ephemeral_message_injection",
        "pre_invocation_injected_tool_call",
        "pre_tool_use_verdict",
        "post_tool_use_dispatch",
        "post_tool_use_matcher_semantics",
    ],
    "disable_slash_commands": [
        "parser_accepts_flag",
        "leading_slash_is_literal",
    ],
}

CAPABILITY_PREDICATE_KINDS = frozenset({"bootstrap_prerequisite", "claim_under_test"})

# Issue #1979 AC1: classify each predicate as a bootstrap_prerequisite (must
# be confirmed supported before a live run is attempted at all -- e.g. the
# base injection mechanism the live runner depends on) or a claim_under_test
# (the actual behavior a permission-boundary live run exists to prove; a live
# run is never gated on these being pre-confirmed supported, since the live
# run itself is how they get confirmed).
CAPABILITY_PREDICATE_CLASSIFICATION: dict[str, dict[str, str]] = {
    "headless_permission_policy": {
        "persisted_settings_loaded": "bootstrap_prerequisite",
        "deny_precedence_enforced": "claim_under_test",
        "ask_is_soft_denied_noninteractive": "claim_under_test",
    },
    "hooks": {
        "workspace_hooks_config_loaded": "bootstrap_prerequisite",
        "pre_invocation_hook_dispatch": "bootstrap_prerequisite",
        # Issue #1979: this is the live runner's actual bootstrap predicate
        # (`run_agy_permission_boundary_e2e.py::BOOTSTRAP_PREDICATE_NAME`) --
        # it gates whether ephemeralMessage-based PreInvocation injection is
        # attempted at all, replacing the toolCall-only
        # `pre_invocation_injected_tool_call` predicate below in that role
        # (upstream google-antigravity/antigravity-cli#728 only breaks
        # `toolCall`; ephemeralMessage injectSteps are independently
        # confirmed accepted -- see references/failure-class-taxonomy.md).
        "pre_invocation_ephemeral_message_injection": "bootstrap_prerequisite",
        # Retained (unrenamed) for the hermetic hook-dispatch harness's own
        # toolCall-based injection contract (#1814/PR #1957 hermetic harness
        # reimplementation is explicitly Out of Scope for #1979) and for
        # `test_setup_check.py`'s existing `--require-capability` assertion,
        # which is outside this Issue's Allowed Paths.
        "pre_invocation_injected_tool_call": "bootstrap_prerequisite",
        "pre_tool_use_verdict": "claim_under_test",
        "post_tool_use_dispatch": "claim_under_test",
        "post_tool_use_matcher_semantics": "claim_under_test",
    },
    "disable_slash_commands": {
        "parser_accepts_flag": "bootstrap_prerequisite",
        "leading_slash_is_literal": "bootstrap_prerequisite",
    },
}


def classify_predicate_kind(group: str, predicate: str) -> str:
    """Return "bootstrap_prerequisite" or "claim_under_test" for *predicate*.

    Unknown (group, predicate) pairs are never silently treated as either
    kind -- ValueError is raised so a typo or a newly added predicate that
    was not classified cannot fall through unnoticed (Issue #1979 AC1).
    """
    group_map = CAPABILITY_PREDICATE_CLASSIFICATION.get(group)
    if group_map is None or predicate not in group_map:
        raise ValueError(f"unclassified capability predicate: {group}.{predicate}")
    return group_map[predicate]


EVIDENCE_PRIORITY = (
    "runtime_semantic_observation",
    "parser_acceptance",
    "changelog",
    "help",
    "version",
)

# google-antigravity/antigravity-cli#728 ("unknown injected step type: <nil>"
# on agy 1.1.9) is open as of 2026-08-03. Until resolved, this predicate is
# fixed `unsupported` regardless of version/help/runtime evidence (Issue #1941
# In Scope).
UPSTREAM_ANTIGRAVITY_CLI_728_OPEN = True

# Minimum agy version at which the official CHANGELOG.md confirms a given
# capability was introduced. Used only as supporting (changelog) evidence —
# never sufficient on its own to claim `supported` for a predicate that
# requires runtime/parser proof (Issue #1941 In Scope, AC6).
_CHANGELOG_MIN_VERSION: dict[str, tuple[int, int, int]] = {
    "persisted_settings_loaded": (1, 1, 4),
    "parser_accepts_flag": (1, 1, 9),
    "leading_slash_is_literal": (1, 1, 9),
}

_UNKNOWN_OPTION_RE = re.compile(
    r"unknown (?:option|flag)|unrecognized (?:option|flag|arguments?)|invalid option",
    re.IGNORECASE,
)

# Issue #1941 fix_delta P1-5: a narrow parser-rejection line shape requiring
# BOTH a generic unknown-option/flag phrase AND the specific
# `--disable-slash-commands` flag name on the same line -- a bare "invalid
# option" string appearing alongside an unrelated warning must never be
# treated as evidence that this specific flag was rejected.
_PARSER_REJECTION_LINE_RE = re.compile(
    r"(?:unknown|unrecognized|invalid)\s+(?:option|flag|arguments?)[^\n]*--disable-slash-commands"
    r"|--disable-slash-commands[^\n]*(?:unknown|unrecognized|invalid)\s+(?:option|flag|arguments?)",
    re.IGNORECASE,
)

# Matches `agy 1.1.9`, bare `1.1.9`, and prerelease/build metadata suffixes
# such as `1.1.9-beta.1+build.5`. Anchored on a preceding start-of-line,
# whitespace, or colon so it does not match version-looking substrings mid
# word.
_VERSION_LINE_RE = re.compile(
    r"(?:^|[\s:])v?(?P<ver>\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]*)?)(?:\s|$)"
)


def parse_agy_version_string(raw_text: str | None) -> dict[str, Any]:
    """Parse `agy --version` (or combined stdout+stderr) output, fail-closed.

    Returns ``{"status": "parsed" | "version_evidence_invalid", "version":
    str | None, "core": (major, minor, patch) | None, "raw": raw_text}``.

    Handles: ``agy 1.1.9``-shape, bare ``1.1.9``, prerelease/build metadata,
    empty-stdout-with-version-in-stderr (caller passes combined text),
    multi-line warning-prefixed output, malformed/locale-dependent text, and
    exit-0-but-unparsable output. A parse failure is classified as
    ``version_evidence_invalid`` — never as ``unsupported`` (Issue #1941 AC8).

    Issue #1941 fix_delta P2-2: when the output contains more than one
    version-shaped token (e.g. an unrelated dependency-version warning line
    preceding the real ``agy <version>`` line), only a candidate anchored to
    an explicit ``agy`` program-name context line is accepted; if zero or
    more than one candidate line matches that anchor, the evidence is
    ambiguous and rejected (``version_evidence_invalid``) rather than
    guessing at the first match found.
    """
    text = (raw_text or "").strip()
    if not text:
        return {"status": "version_evidence_invalid", "version": None, "core": None, "raw": raw_text}

    candidates: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _VERSION_LINE_RE.search(line)
        if match:
            candidates.append((line, match.group("ver")))

    if not candidates:
        return {"status": "version_evidence_invalid", "version": None, "core": None, "raw": raw_text}

    if len(candidates) == 1:
        ver = candidates[0][1]
    else:
        agy_anchored = [(line, ver) for line, ver in candidates if re.search(r"\bagy\b", line, re.IGNORECASE)]
        if len(agy_anchored) != 1:
            return {"status": "version_evidence_invalid", "version": None, "core": None, "raw": raw_text}
        ver = agy_anchored[0][1]

    core_match = re.match(r"(\d+)\.(\d+)\.(\d+)", ver)
    core = tuple(int(part) for part in core_match.groups()) if core_match else None
    return {"status": "parsed", "version": ver, "core": core, "raw": raw_text}


def compute_binary_identity(resolved_path: str | None) -> dict[str, Any]:
    """Compute an identity fingerprint for the resolved agy binary.

    Returns ``realpath_class``/``realpath_digest``/``sha256``/``size``/
    ``mtime_ns``/``platform``/``arch``.  A binary that cannot be
    resolved/read returns an all-``None`` identity (except platform/arch) so
    drift detection can distinguish "no binary" from "binary changed" (Issue
    #1941 AC6).

    Issue #1979 fix_delta (P1-3): this identity is embedded verbatim in the
    distributed, secret-safe ``agy_permission_boundary_e2e`` artifact.  A raw
    absolute realpath (e.g. ``/home/<username>/.local/bin/agy``) would leak
    the host username/home layout into that artifact -- sanitizing that at
    the *scan* layer only (blanking a deep-copy before the forbidden-
    substring check) still leaves the raw path in the real, on-disk/returned
    artifact.  Sanitizing at the source instead: ``realpath_class`` is a
    generalized/classified form of the path (``$HOME``-relative when under
    the current user's home, otherwise just the basename -- see
    ``_mask_resolved_path``) and ``realpath_digest`` is a hash of the actual
    real path, so exact-match / drift comparison (``binary_identity_matches``)
    remains possible without disclosing the raw path anywhere in the
    artifact.
    """
    identity: dict[str, Any] = {
        "realpath_class": None,
        "realpath_digest": None,
        "sha256": None,
        "size": None,
        "mtime_ns": None,
        "platform": platform.system(),
        "arch": platform.machine(),
    }
    if not resolved_path:
        return identity
    try:
        real = str(Path(resolved_path).resolve())
        stat_result = os.stat(real)
        digest = hashlib.sha256()
        with open(real, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        identity["realpath_class"] = _mask_resolved_path(real)
        identity["realpath_digest"] = "sha256:" + hashlib.sha256(real.encode("utf-8")).hexdigest()
        identity["sha256"] = digest.hexdigest()
        identity["size"] = stat_result.st_size
        identity["mtime_ns"] = stat_result.st_mtime_ns
    except OSError:
        pass
    return identity


def binary_identity_matches(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Return True iff two identity fingerprints denote the same binary."""
    keys = ("realpath_digest", "sha256", "size", "mtime_ns", "platform", "arch")
    return all(before.get(key) == after.get(key) for key in keys)


def _predicate_result(
    status: str,
    *,
    reason_code: str,
    evidence_source: str,
    detail: str | None = None,
) -> dict[str, Any]:
    if status not in CAPABILITY_STATUSES:
        raise ValueError(f"invalid capability status: {status!r}")
    if evidence_source not in EVIDENCE_PRIORITY:
        raise ValueError(f"invalid evidence_source: {evidence_source!r}")
    return {
        "status": status,
        "reason_code": reason_code,
        "evidence_source": evidence_source,
        "detail": detail,
    }


def classify_parser_acceptance(
    exit_code: int | None,
    stdout: str | None,
    stderr: str | None,
) -> dict[str, Any]:
    """Classify parser acceptance/rejection of a fixed-argv flag probe.

    Issue #1941 fix_delta P1-5: auth/quota/runtime evidence is checked FIRST
    and always takes priority over a generic parser-error string match (an
    unrelated config warning containing "invalid option" alongside an auth
    error must never be misclassified as a parser rejection). A parser
    rejection is only recognized when a non-zero exit code is combined with a
    narrow parser-error line shape that names the specific
    ``--disable-slash-commands`` flag -- a bare "unknown option"/"invalid
    option" string anywhere in the output is never sufficient on its own
    (Issue #1941 AC2 / fix_delta P1-5).
    """
    stdout_text = stdout or ""
    stderr_text = stderr or ""
    combined = "\n".join([stdout_text, stderr_text])
    auth_signal = _classify_auth_signal(combined)

    if auth_signal:
        return {"accepted": None, "evidence_source": "auth_signal", "auth_signal": auth_signal}

    parser_rejection_line = bool(_PARSER_REJECTION_LINE_RE.search(stderr_text)) or bool(
        _PARSER_REJECTION_LINE_RE.search(stdout_text)
    )

    if exit_code not in (0, None) and parser_rejection_line:
        return {"accepted": False, "evidence_source": "parser_rejection_line", "auth_signal": auth_signal}

    if exit_code == 0:
        return {"accepted": True, "evidence_source": "exit_zero", "auth_signal": auth_signal}

    return {"accepted": None, "evidence_source": "exit_nonzero_unclassified", "auth_signal": auth_signal}


def derive_parser_accepts_flag_status(parser_result: dict[str, Any]) -> dict[str, Any]:
    """Derive the ``disable_slash_commands.parser_accepts_flag`` status.

    ``--help`` flag visibility is intentionally NOT a parameter here: help
    absence never blocks `supported`, and help presence never overrides a
    parser rejection (Issue #1941 In Scope / PR #1976 design).
    """
    if parser_result["accepted"] is True:
        return _predicate_result(
            "supported", reason_code="parser_accepted_fixed_argv", evidence_source="parser_acceptance"
        )
    if parser_result["accepted"] is False:
        return _predicate_result(
            "unsupported", reason_code="parser_rejected_fixed_argv", evidence_source="parser_acceptance"
        )
    if parser_result.get("auth_signal"):
        return _predicate_result(
            "inconclusive", reason_code="auth_blocked_probe", evidence_source="parser_acceptance"
        )
    return _predicate_result(
        "inconclusive",
        reason_code="ambiguous_exit_without_rejection_evidence",
        evidence_source="parser_acceptance",
    )


def _resolve_predicate(
    group: str,
    predicate: str,
    *,
    version_result: dict[str, Any],
    disable_slash_probe: dict[str, Any] | None,
    leading_slash_probe: dict[str, Any] | None,
) -> dict[str, Any]:
    if predicate == "pre_invocation_injected_tool_call" and UPSTREAM_ANTIGRAVITY_CLI_728_OPEN:
        return _predicate_result(
            "unsupported",
            reason_code="upstream_known_runtime_rejection",
            evidence_source="runtime_semantic_observation",
            detail="google-antigravity/antigravity-cli#728 open as of 2026-08-03",
        )

    if predicate == "parser_accepts_flag":
        if disable_slash_probe is None:
            return _predicate_result(
                "unavailable", reason_code="probe_not_run", evidence_source="parser_acceptance"
            )
        parser_result = classify_parser_acceptance(
            disable_slash_probe.get("exit_code"),
            disable_slash_probe.get("stdout", ""),
            disable_slash_probe.get("stderr", ""),
        )
        return derive_parser_accepts_flag_status(parser_result)

    if predicate == "leading_slash_is_literal":
        if leading_slash_probe is None:
            return _predicate_result(
                "unavailable", reason_code="probe_not_run", evidence_source="runtime_semantic_observation"
            )
        if leading_slash_probe.get("skipped"):
            # Issue #1941 fix_delta P1-3: the real model-backed probe was
            # skipped because its no-cost contract could not be mechanically
            # confirmed. `unavailable` here is never a success/adoption
            # signal on its own.
            return _predicate_result(
                "unavailable",
                reason_code=leading_slash_probe.get("skip_reason") or "probe_cost_unconfirmed",
                evidence_source="runtime_semantic_observation",
            )
        if leading_slash_probe.get("timed_out"):
            return _predicate_result(
                "inconclusive",
                reason_code="client_subprocess_timeout",
                evidence_source="runtime_semantic_observation",
            )
        if leading_slash_probe.get("literal_confirmed"):
            return _predicate_result(
                "supported",
                reason_code="runtime_sentinel_echoed_literally",
                evidence_source="runtime_semantic_observation",
            )
        if leading_slash_probe.get("expansion_detected"):
            return _predicate_result(
                "unsupported",
                reason_code="slash_command_expansion_detected",
                evidence_source="runtime_semantic_observation",
            )
        return _predicate_result(
            "inconclusive", reason_code="ambiguous_runtime_output", evidence_source="runtime_semantic_observation"
        )

    if predicate == "persisted_settings_loaded":
        if version_result.get("status") != "parsed":
            return _predicate_result(
                "evidence_invalid", reason_code="version_evidence_invalid", evidence_source="version"
            )
        version_core = version_result.get("core")
        min_version = _CHANGELOG_MIN_VERSION["persisted_settings_loaded"]
        if version_core and version_core >= min_version:
            return _predicate_result(
                "inconclusive",
                reason_code="changelog_supported_pending_runtime_verification",
                evidence_source="changelog",
            )
        return _predicate_result(
            "unsupported", reason_code="below_changelog_min_version", evidence_source="changelog"
        )

    # Remaining hooks / headless_permission_policy predicates: actual live
    # enforcement (does persisted settings.json really force deny? does the
    # hook actually dispatch/inject/verdict/match?) is Issue #1979's
    # responsibility, explicitly Out of Scope for #1941. We never claim
    # `supported` without a runtime semantic observation this Issue does not
    # perform for these predicates.
    return _predicate_result(
        "inconclusive",
        reason_code="runtime_semantic_observation_deferred_to_1979",
        evidence_source="changelog",
        detail="live enforcement verification is Issue #1979's responsibility per #1941 Out of Scope",
    )


def get_capability_status(matrix: dict[str, Any], group: str, predicate: str) -> dict[str, Any]:
    """Look up a single predicate result. Unknown capability names are never
    `supported` — they resolve to `unavailable` (Issue #1941 AC2).
    """
    group_matrix = matrix.get(group) if isinstance(matrix, dict) else None
    if not isinstance(group_matrix, dict) or predicate not in group_matrix:
        return _predicate_result(
            "unavailable", reason_code="unknown_capability", evidence_source="runtime_semantic_observation"
        )
    return group_matrix[predicate]


def build_capability_matrix(
    *,
    version_result: dict[str, Any],
    disable_slash_probe: dict[str, Any] | None = None,
    leading_slash_probe: dict[str, Any] | None = None,
    binary_identity_before: dict[str, Any] | None = None,
    binary_identity_after: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the full `agy_capability_matrix/v1` predicate matrix.

    If both binary identities are supplied and they diverge, every predicate
    is forced to `evidence_invalid` (binary identity drift — Issue #1941 AC6);
    no partial trust is given to probes gathered against a different binary.
    """
    drift = False
    if binary_identity_before is not None and binary_identity_after is not None:
        drift = not binary_identity_matches(binary_identity_before, binary_identity_after)

    matrix: dict[str, Any] = {}
    for group, predicates in CAPABILITY_PREDICATES.items():
        matrix[group] = {}
        for predicate in predicates:
            if drift:
                matrix[group][predicate] = _predicate_result(
                    "evidence_invalid",
                    reason_code="binary_identity_drift",
                    evidence_source="runtime_semantic_observation",
                )
                continue
            matrix[group][predicate] = _resolve_predicate(
                group,
                predicate,
                version_result=version_result,
                disable_slash_probe=disable_slash_probe,
                leading_slash_probe=leading_slash_probe,
            )
    return matrix


# Issue #1979 AC5 / fix_delta major_7: MCP is unsupported_by_design, not
# merely unavailable/untested. The reason cites the actual dispatch
# mechanism -- not merely the (true, but non-exhaustive) fact that
# `agy_permission_enforcement_hook.py` never imports
# `agy_permission_policy.py` for ANY tool. The real deny mechanism is that
# `NATIVE_TO_RESOURCE` (the hook's tool-name -> resource dispatch table) has
# no entry mapping any `mcp_*` tool name to a resource, so unknown-native-tool
# calls are denied by default (`unknown_native_tool`); additionally
# `agy_permission_policy.AGY_DIRECT_MCP_ACCESS` is `False` and no profile's
# `PROFILE_ALLOWED_PERMISSION_RESOURCES` includes `"mcp"`. There is no code
# path a runtime probe could ever observe as "supported". This is therefore
# excluded from completion blockers rather than left as an
# inconclusive/unavailable predicate.
MCP_UNSUPPORTED_BY_DESIGN_REASON = (
    "agy_permission_enforcement_hook.py's NATIVE_TO_RESOURCE dispatch table has no entry mapping "
    "any mcp_* tool name to a resource (unknown-native-tool calls are denied by default); "
    "agy_permission_policy.AGY_DIRECT_MCP_ACCESS is False; no profile's PROFILE_ALLOWED_PERMISSION_RESOURCES "
    "includes \"mcp\"; and agy_permission_enforcement_hook.py never imports agy_permission_policy.py for any tool"
)


def mcp_capability_status() -> dict[str, Any]:
    """Return the fixed `unsupported_by_design` capability record for MCP.

    Never derived from a runtime probe -- MCP access is disabled by
    construction (see `MCP_UNSUPPORTED_BY_DESIGN_REASON`), so there is no
    probe outcome that could ever change this value (Issue #1979 AC5).
    """
    return {
        "status": "unsupported_by_design",
        "completion_blocker": False,
        "reason": MCP_UNSUPPORTED_BY_DESIGN_REASON,
    }


_CAPABILITY_MEMO_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


def compute_config_digest(repo_root: Path | None = None) -> str:
    """Digest of the config files relevant to hooks/permission capability
    memoization. Never itself part of persistent cache — only used as an
    in-process memoization key component (Issue #1941 AC9).
    """
    root = repo_root or _repo_root()
    relevant_paths = [
        root / ".claude" / "settings.json",
        root / ".agents" / "mcp_config.json",
        # Issue #1941 fix_delta P1-7: the digest previously omitted the real
        # hooks config file location(s) entirely, so a hooks config change
        # never invalidated a cached capability matrix. Repo-root workspace
        # hooks config (may not exist for a normal run -- missing is digested
        # deterministically the same as the other optional inputs above).
        root / ".agents" / "hooks.json",
    ]
    home = os.environ.get("HOME")
    if home:
        # Global hooks location AGY actually consults (see
        # agy_tool_provenance.py's canonical_hooks_dir /
        # `<HOME>/.gemini/config/hooks.json`).
        relevant_paths.append(Path(home) / ".gemini" / "config" / "hooks.json")
    digest = hashlib.sha256()
    for path in relevant_paths:
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"\x00missing\x00")
    return digest.hexdigest()


def _identity_key_tuple(binary_identity: dict[str, Any]) -> tuple[Any, ...]:
    return (
        binary_identity.get("realpath_digest"),
        binary_identity.get("sha256"),
        binary_identity.get("size"),
        binary_identity.get("mtime_ns"),
        binary_identity.get("platform"),
        binary_identity.get("arch"),
    )


def _capability_cache_key(
    binary_identity_before: dict[str, Any],
    binary_identity_check: dict[str, Any],
    config_digest: str,
) -> tuple[Any, ...]:
    """Cache key bound to BOTH identity snapshots (Issue #1941 fix_delta P1-7).

    Binding to a single identity snapshot allowed a cache entry computed for
    binary B (from an earlier, unrelated run) to be silently reused for a run
    in which the binary drifted mid-run from A to B, since `compute_fn()`
    would never be called at all in that case -- bypassing the
    binary-identity-drift check entirely. Requiring BOTH the pre-run and
    pre-runtime-probe identities to match means any drift between them
    produces a key that cannot collide with a legitimate (non-drifted) prior
    run's key, guaranteeing `compute_fn()` always runs when drift occurred.
    """
    return (
        _identity_key_tuple(binary_identity_before),
        _identity_key_tuple(binary_identity_check),
        config_digest,
    )


def get_or_compute_capability_matrix(
    binary_identity_before: dict[str, Any],
    binary_identity_check: dict[str, Any],
    config_digest: str,
    compute_fn: Any,
) -> dict[str, Any]:
    """In-process-only memoization for capability probe evidence bundles.

    There is intentionally no persistent (cross-process) cache — the module
    dict lives only for the current process lifetime, so a fresh process
    always re-probes (Issue #1941 AC9 / Out of Scope: no persistent cache).

    *compute_fn* is expected to return the full evidence bundle for the run
    (matrix + capability_probes + binary_identity_after), not just the
    matrix, so a cache hit can never leave `capabilities` and
    `capability_probes` mutually inconsistent (Issue #1941 fix_delta P1-7).
    """
    key = _capability_cache_key(binary_identity_before, binary_identity_check, config_digest)
    if key in _CAPABILITY_MEMO_CACHE:
        return _CAPABILITY_MEMO_CACHE[key]
    result = compute_fn()
    _CAPABILITY_MEMO_CACHE[key] = result
    return result


def _run_disable_slash_commands_probe(agy_bin: str) -> dict[str, Any]:
    """Fixed-argv parser acceptance probe for ``--disable-slash-commands``.

    Deliberately combined with ``--help`` so the probe never triggers a real
    (possibly costly) generation call — only parser-level accept/reject
    evidence is needed for this predicate.
    """
    argv = [agy_bin, "--disable-slash-commands", "--help"]
    probe: dict[str, Any] = {
        "argv": argv,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
    }
    try:
        proc = _run(argv, timeout=SMOKE_TIMEOUT_SECONDS)
        probe["exit_code"] = proc.returncode
        probe["stdout"] = proc.stdout or ""
        probe["stderr"] = proc.stderr or ""
    except subprocess.TimeoutExpired:
        probe["timed_out"] = True
    except FileNotFoundError:
        probe["exit_code"] = None
    return probe


_SLASH_LITERAL_PROMPT_SUFFIX = "/nonexistent-loop-agy-capability-probe"

_SLASH_EXPANSION_ERROR_RE = re.compile(
    r"unknown command|no such command|unrecognized command", re.IGNORECASE
)


def _runtime_probe_cost_confirmed() -> bool:
    """Return True iff the caller explicitly confirmed acceptance of the real
    (model-backed) cost incurred by the leading-slash-literal runtime probe.

    There is no mechanical way to prove `agy -p <prompt>` is free -- it is a
    real model-backed call. Issue #1941 fix_delta P1-3: rather than silently
    spending quota/cost on every capability computation, this probe is
    skipped by default unless the caller sets
    ``AGY_PREFLIGHT_CONFIRM_RUNTIME_PROBE_COST=1`` (never inferred).
    """
    return os.environ.get(AGY_RUNTIME_PROBE_COST_CONFIRM_ENV_VAR, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _run_leading_slash_literal_probe(agy_bin: str) -> dict[str, Any]:
    """Isolated runtime smoke probing whether a leading ``/`` in a print-mode
    prompt is treated as a literal character (expected) rather than expanded
    as a slash-command (the known 1.1.9 print-mode expansion issue).

    Issue #1941 fix_delta:
      P1-2: runs the exact production argv
      ``agy --disable-slash-commands -p <prompt>`` (not bare ``-p``), and
      classifies expansion-error evidence ahead of sentinel-presence when
      both appear in the combined output -- a "did you mean" style
      diagnostic can legitimately echo the sentinel text alongside an
      unknown-command rejection.
      P1-3: isolates HOME/XDG_* (not just cwd) so this model-backed probe
      never discovers or loads the real user's global
      ``~/.gemini/config/`` hooks/permissions/skills/plugins. Caller is
      responsible for the cost-confirmation gate (this function always
      actually invokes the subprocess when called).
    """
    prompt = f"{_SLASH_LITERAL_PROMPT_SUFFIX} {SMOKE_PROMPT}"
    argv = [agy_bin, "--disable-slash-commands", "-p", prompt]
    probe: dict[str, Any] = {
        "argv": argv,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "literal_confirmed": False,
        "expansion_detected": False,
    }
    with tempfile.TemporaryDirectory(prefix="agy-preflight-slash-") as temp_dir:
        temp_root = Path(temp_dir)
        isolated_home = temp_root / "isolated-home"
        isolated_home.mkdir(parents=True, exist_ok=True)
        isolated_cwd = temp_root / "cwd"
        isolated_cwd.mkdir(parents=True, exist_ok=True)
        try:
            proc = _run(
                argv,
                cwd=isolated_cwd,
                timeout=SMOKE_TIMEOUT_SECONDS,
                env=_isolated_probe_env(isolated_home),
            )
            probe["exit_code"] = proc.returncode
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            probe["stdout"] = stdout
            probe["stderr"] = stderr
            combined = stdout + "\n" + stderr
            # P1-2: expansion-error evidence takes priority over sentinel
            # presence -- a combined output containing both must never be
            # misclassified as `literal_confirmed`.
            if _SLASH_EXPANSION_ERROR_RE.search(combined):
                probe["expansion_detected"] = True
            elif proc.returncode == 0 and EXPECTED_SMOKE in stdout:
                probe["literal_confirmed"] = True
        except subprocess.TimeoutExpired:
            probe["timed_out"] = True
        except FileNotFoundError:
            probe["exit_code"] = None
    return probe



def _argv_shape(argv: list[str]) -> dict[str, Any]:
    return {"flags": [tok for tok in argv if tok.startswith("-")], "arg_count": len(argv)}


def _prompt_digest(prompt: str | None) -> dict[str, Any]:
    if not prompt:
        return {"kind": "none", "sha256": None, "byte_length": 0}
    encoded = prompt.encode("utf-8")
    return {
        "kind": "fixed_sentinel",
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "byte_length": len(encoded),
    }


def _sanitize_probe_in_place(probe: dict[str, Any] | None, agy_bin: str) -> None:
    if not isinstance(probe, dict):
        return
    argv = probe.pop("argv", None)
    if not isinstance(argv, list):
        return
    probe["argv_shape"] = _argv_shape(argv)
    prompt_text = next(
        (tok for tok in argv if tok != agy_bin and not tok.startswith("-")),
        None,
    )
    probe["prompt"] = _prompt_digest(prompt_text)
    # never persist raw stdout/stderr beyond the already-redacted samples.
    probe.pop("stdout", None)
    probe.pop("stderr", None)


def _sanitize_for_artifact(result: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe deep copy of *result* safe to persist to disk AND
    safe to return to the caller / print via ``--json`` / write via
    ``--output-file`` (Issue #1941 fix_delta P1-4: this is the single
    sanitizer every external-facing output surface must route through —
    return value, ``--json``, ``--output-file``, and the artifact file — so
    none of them can independently leak evidence the others redact).

    Never persists prompt text, raw credential paths, absolute HOME,
    un-redacted stderr, or an unredacted combined-output ``version_evidence
    .raw`` / ``agy.version`` sample — only ``argv_shape.flags`` and
    ``prompt.sha256``/``prompt.byte_length`` for probes, and a normalized
    version plus a redacted bounded sample for the version fields
    (Issue #1941 AC7 / fix_delta P1-4).
    """
    sanitized = json.loads(json.dumps(result, default=str))
    agy_bin = (sanitized.get("agy") or {}).get("bin") or "agy"

    _sanitize_probe_in_place(sanitized.get("smoke"), agy_bin)
    grounded_check = (sanitized.get("grounded_research") or {}).get("check")
    _sanitize_probe_in_place(grounded_check, agy_bin)
    capability_probes = sanitized.get("capability_probes")
    if isinstance(capability_probes, dict):
        for probe in capability_probes.values():
            _sanitize_probe_in_place(probe, agy_bin)

    # P1-4: `version_evidence.raw` previously stored the full, unredacted
    # combined `agy --version` stdout+stderr verbatim -- a warning line in
    # that output could contain an auth URL or credential path. Replace it
    # with the same bounded/redacted sample used everywhere else.
    version_evidence = sanitized.get("version_evidence")
    if isinstance(version_evidence, dict) and version_evidence.get("raw") is not None:
        version_evidence["raw"] = _redact_output_sample(str(version_evidence["raw"]))

    # P1-4: `agy.version` previously stored the raw first-line stdout
    # verbatim (same leakage risk as version_evidence.raw). Limit it to the
    # normalized, already-validated value from `version_evidence.version`
    # plus a separate redacted bounded sample of the raw text -- never the
    # raw value itself.
    agy_info = sanitized.get("agy")
    if isinstance(agy_info, dict) and agy_info.get("version") is not None:
        raw_version_value = str(agy_info["version"])
        normalized_version = (
            version_evidence.get("version") if isinstance(version_evidence, dict) else None
        )
        agy_info["version"] = normalized_version
        agy_info["version_raw_sample"] = _redact_output_sample(raw_version_value)

    home = os.environ.get("HOME")
    if home:
        def _scrub_home(value: Any) -> Any:
            if isinstance(value, str):
                return value.replace(home, "$HOME")
            if isinstance(value, dict):
                return {k: _scrub_home(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_scrub_home(v) for v in value]
            return value

        sanitized = _scrub_home(sanitized)

    return sanitized


def _artifact_output_dir(repo_root: Path | None = None) -> Path:
    override = os.environ.get("AGY_PREFLIGHT_ARTIFACT_DIR")
    if override:
        return Path(override)
    root = repo_root or _repo_root()
    return root / ".claude" / "tmp"


def write_sanitized_artifact(
    result: dict[str, Any],
    *,
    repo_root: Path | None = None,
    sanitized: dict[str, Any] | None = None,
) -> Path:
    """Write a sanitized JSON artifact for a controlled-exit `run_preflight`
    result. This is the single finalizer path used by every controlled exit
    (binary missing, help failure, auth unavailable, cost unconfirmed,
    timeout, cleanup failure) — Issue #1941 AC7.

    *sanitized* lets callers that already computed the sanitized payload
    (e.g. `_finalize`) reuse it instead of re-running `_sanitize_for_artifact`
    a second time.
    """
    out_dir = _artifact_output_dir(repo_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out_path = out_dir / f"agy_preflight_result_{timestamp}_{os.getpid()}.json"
    payload = sanitized if sanitized is not None else _sanitize_for_artifact(result)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out_path


def _finalize(result: dict[str, Any]) -> dict[str, Any]:
    """Single finalizer invoked on every `run_preflight` return path.

    Issue #1941 fix_delta P1-4: sanitizes *result* FIRST and returns the
    sanitized dict as `run_preflight()`'s actual return value -- previously
    only the artifact file was sanitized, while the in-memory return value
    (and therefore `--json` / `--output-file`, which both serialize that same
    return value in `main()`) still carried raw capability-probe
    argv/stdout/stderr and an unredacted `version_evidence.raw`.

    Always attempts to write the sanitized artifact; a write failure is
    recorded as a warning but never raises (cleanup failure is itself a
    controlled-exit path per Issue #1941 In Scope).
    """
    sanitized = _sanitize_for_artifact(result)
    try:
        artifact_path = write_sanitized_artifact(result, sanitized=sanitized)
        sanitized["artifact_path"] = str(artifact_path)
    except OSError as exc:
        sanitized.setdefault("warnings", []).append(f"artifact_write_failed: {exc}")
        sanitized["artifact_path"] = None
    return sanitized


def compute_require_capability_exit_code(matrix: dict[str, Any], requested: list[str]) -> int:
    """Derive the `--require-capability` exit code from the capability matrix.

    0 = every requested predicate is `supported`.
    1 = any requested predicate is `unsupported` / `inconclusive` / `evidence_invalid`.
    77 = no `1`-triggering predicate remains, but at least one requested
         predicate is still `unavailable` (Issue #1941 AC4). 77 is never a
         success/close/merge signal on its own — callers must not treat it as
         PASS.
    """
    statuses: list[str] = []
    for dotted in requested:
        group, _, predicate = dotted.partition(".")
        status_obj = get_capability_status(matrix, group, predicate)
        statuses.append(status_obj.get("status", "unavailable"))

    if statuses and all(status == "supported" for status in statuses):
        return 0
    if any(status in {"unsupported", "inconclusive", "evidence_invalid"} for status in statuses):
        return 1
    return 77


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
    parser.add_argument(
        "--require-capability",
        required=False,
        default=None,
        help=(
            "Comma-separated capability.predicate list (e.g. "
            "disable_slash_commands.parser_accepts_flag). When given, exit code is "
            "0=all supported / 1=any unsupported|inconclusive|evidence_invalid / "
            "77=only unavailable remains (never a success signal). Default invocation "
            "(flag omitted) is unaffected and keeps the existing ok-boolean exit 0/1."
        ),
    )
    args = parser.parse_args(argv)

    requested_capabilities = (
        [item.strip() for item in args.require_capability.split(",") if item.strip()]
        if args.require_capability
        else None
    )

    result = run_preflight(
        validate_local_asset_contract=args.local_asset_research,
        live_serena=args.live_serena,
        mcp_config_path=args.mcp_config,
        grounded_research=args.grounded_research,
        compute_capabilities=bool(requested_capabilities),
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

    if requested_capabilities:
        # Issue #1941 AC4: --require-capability mode derives its own exit
        # code taxonomy from the capability matrix; it never reuses the
        # default ok-boolean exit 0/1 semantics.
        return compute_require_capability_exit_code(
            result.get("capabilities") or {}, requested_capabilities
        )

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
