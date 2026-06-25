"""
latitude_telemetry_safety.py

Latitude telemetry containment checker for session_recording_runtime_safety/v2.

This module provides the `check_latitude_component` function that inspects
Latitude API key presence, hook configuration, BUN_OPTIONS preload,
systemd/environment.d, process environment, local storage, distribution
integrity, and uninstall state.

Sensitive value handling contract:
  - credential values are NOT read, only presence is detected
  - full environment is NOT dumped, only specific key presence
  - local absolute paths are NOT emitted in output
  - subprocess raw stderr is NOT forwarded
  - stdout is suppressed for subprocesses

Environment overrides (for fixture/test mode):
  SRRS_LAT_CREDENTIAL_STATE     'present' | 'absent' | 'unknown'
  SRRS_LAT_EXPORT_STATE         'enabled' | 'disabled' | 'unknown'
  SRRS_LAT_CAPTURE_STATE        'active' | 'inactive' | 'unknown'
  SRRS_LAT_HOOK_STATE           'present' | 'absent' | 'unknown'
  SRRS_LAT_PRELOAD_SETTINGS     'present' | 'absent' | 'unknown'
  SRRS_LAT_PRELOAD_SYSTEMD      'present' | 'absent' | 'unknown'
  SRRS_LAT_PRELOAD_SHELL        'present' | 'absent' | 'unknown'
  SRRS_LAT_ACTIVE_PROCESS       'preload_present' | 'preload_absent' | 'unknown'
  SRRS_LAT_LOCAL_STORAGE        'absent' | 'present' | 'unsafe_metadata' | 'unknown'
  SRRS_LAT_UNINSTALL_STATE      'complete' | 'incomplete' | 'not_attempted' | 'unknown'
  SRRS_LAT_DIST_SPEC            '<spec_string>' | 'unpinned' | 'unknown'
  SRRS_LAT_DIST_INTEGRITY       'verified' | 'unknown'
  SRRS_LAT_DIST_PROVENANCE      'verified' | 'unknown'
  SRRS_LAT_REMOTE_TRACE         'absent_human_attested' | 'present' | 'unknown'
  SRRS_LAT_BASE_URL             '<url>' | '' (empty = not set)
  SRRS_LAT_DEBUG                '1' | '0' | '' (empty = not set)
  SRRS_LAT_ARGV_CREDENTIAL      'present' | 'absent'  (for AC28 test)
  SRRS_LAT_CONTAINMENT_STATE    'never_observed' | 'active' | 'contained' | 'unknown'
  SRRS_LAT_DESTINATION_STATE    'approved_cloud' | 'approved_self_host' | 'unapproved' | 'unknown'
  SRRS_LAT_TRANSPORT_STATE      'https' | 'plaintext' | 'unknown'
  SRRS_LAT_DIAGNOSTIC_LOG       'disabled' | 'enabled' | 'unknown'
  SRRS_LAT_EXPOSURE_STATE       'none_observed' | 'possible' | 'confirmed' | 'unknown'
  SRRS_LAT_MANAGED_HOOK         'present' | 'absent' | 'unknown'
  SRRS_LAT_BACKUP_CREDENTIAL    'present' | 'absent' | 'unknown'
  SRRS_LAT_LSTAT_STATE          'normal' | 'unsafe_metadata' | 'unknown'
"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Reason codes (matching Issue contract)
# ---------------------------------------------------------------------------
RC_CREDENTIAL_PRESENT = "latitude_credential_present"
RC_POLICY_MODE_MISMATCH = "latitude_policy_mode_mismatch"
RC_STOP_HOOK_PRESENT = "latitude_stop_hook_present"
RC_PRELOAD_CONFIGURED = "latitude_preload_configured"
RC_PRELOAD_ACTIVE_PROCESS = "latitude_preload_active_process"
RC_EXPORT_DISABLED_CAPTURE_ACTIVE = "latitude_export_disabled_capture_active"
RC_LOCAL_STORAGE_PRESENT = "latitude_local_storage_present"
RC_LOCAL_STORAGE_UNSAFE_METADATA = "latitude_local_storage_unsafe_metadata"
RC_SETTINGS_BACKUP_CREDENTIAL = "latitude_settings_backup_contains_credential_field"
RC_UNINSTALL_INCOMPLETE = "latitude_uninstall_incomplete"
RC_RUNTIME_STATE_UNKNOWN = "latitude_runtime_state_unknown"
RC_DISTRIBUTION_UNPINNED = "latitude_distribution_unpinned"
RC_DISTRIBUTION_PROVENANCE_UNKNOWN = "latitude_distribution_provenance_unknown"
RC_REMOTE_TRACE_UNKNOWN = "latitude_remote_trace_state_unknown"
RC_DESTINATION_UNAPPROVED = "latitude_destination_unapproved"
RC_TRANSPORT_PLAINTEXT = "latitude_transport_plaintext_or_unknown"
RC_DIAGNOSTIC_LOGGING = "latitude_diagnostic_logging_enabled"
RC_EXPOSURE_POSSIBLE = "latitude_exposure_possible_or_confirmed"
RC_WINDOWS_HOST = "latitude_windows_host_not_inspected"
RC_SRRS_OVERRIDE_REJECTED = "latitude_srrs_override_rejected"

# Known Latitude hook command patterns (for presence detection)
_LATITUDE_HOOK_PATTERNS = [
    re.compile(r"latitude", re.IGNORECASE),
    re.compile(r"@latitude-so", re.IGNORECASE),
    re.compile(r"latitude[-_]claude", re.IGNORECASE),
    re.compile(r"LATITUDE_CLAUDE_CODE_ENABLED", re.IGNORECASE),
]

# Latitude preload patterns
_LATITUDE_PRELOAD_PATTERNS = [
    re.compile(r"latitude.*preload", re.IGNORECASE),
    re.compile(r"@latitude-so.*intercept", re.IGNORECASE),
    re.compile(r"latitude.*intercept", re.IGNORECASE),
    re.compile(r"latitude_intercept", re.IGNORECASE),
    re.compile(r"--require.*latitude", re.IGNORECASE),
    re.compile(r"--loader.*latitude", re.IGNORECASE),
]

# Latitude state/spool paths (relative to home)
_LATITUDE_STATE_PATHS = [
    ".latitude",
    ".config/latitude",
    ".local/share/latitude",
    ".latitude-state",
    ".latitude/state",
    ".latitude/config.json",
]

_LATITUDE_SPOOL_PATHS = [
    ".latitude/spool",
    ".latitude/requests",
    ".latitude/queue",
]

# Backup patterns
_BACKUP_PATTERNS = [
    re.compile(r"settings.*backup", re.IGNORECASE),
    re.compile(r"settings.*bak", re.IGNORECASE),
    re.compile(r"claude.*backup", re.IGNORECASE),
]

# Approved origins
_APPROVED_ORIGINS = [
    "https://telemetry.latitude.so",
    "https://latitude.so",
    "https://app.latitude.so",
    "https://gateway.latitude.so",
]


def _get_env_override(key: str) -> str | None:
    return os.environ.get(key)


def _text_contains_latitude(text: str) -> bool:
    for pat in _LATITUDE_HOOK_PATTERNS:
        if pat.search(text):
            return True
    return False


def _text_contains_preload(text: str) -> bool:
    for pat in _LATITUDE_PRELOAD_PATTERNS:
        if pat.search(text):
            return True
    return False


def _text_contains_credential_field(text: str) -> bool:
    """Check if text contains Latitude API key field name (not value)."""
    patterns = [
        re.compile(r"LATITUDE_API_KEY", re.IGNORECASE),
        re.compile(r'"latitude[_-]?api[_-]?key"', re.IGNORECASE),
        re.compile(r"latitude.*apiKey", re.IGNORECASE),
    ]
    for pat in patterns:
        if pat.search(text):
            return True
    return False


def check_execution_profile_override_rejection(execution_profile: str) -> str | None:
    """AC19: In host mode, reject SRRS_LAT_* overrides."""
    if execution_profile != "host":
        return None
    lat_overrides = [k for k in os.environ if k.startswith("SRRS_LAT_")]
    if lat_overrides:
        return RC_SRRS_OVERRIDE_REJECTED
    return None


def check_credential_state(repo_root: Path, execution_profile: str) -> dict[str, Any]:
    """AC11: Detect LATITUDE_API_KEY field presence (not value)."""
    override = _get_env_override("SRRS_LAT_CREDENTIAL_STATE")
    if override is not None:
        state = override.strip()
        rcs = [RC_CREDENTIAL_PRESENT] if state == "present" else []
        return {"state": state, "reason_codes": rcs, "checked_surfaces": ["claude_user_settings"]}

    state = "absent"
    reason_codes: list[str] = []
    checked: list[str] = []

    home = Path.home()
    settings_paths = [
        (home / ".claude" / "settings.json", "claude_user_settings"),
        (repo_root / ".claude" / "settings.json", "claude_project_settings"),
    ]

    for settings_path, surface_name in settings_paths:
        checked.append(surface_name)
        if settings_path.is_file():
            try:
                text = settings_path.read_text(encoding="utf-8", errors="replace")
                if _text_contains_credential_field(text):
                    state = "present"
                    reason_codes.append(RC_CREDENTIAL_PRESENT)
            except OSError:
                state = "unknown"
                reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)

    return {"state": state, "reason_codes": reason_codes, "checked_surfaces": checked}


def check_hook_state(repo_root: Path, execution_profile: str) -> dict[str, Any]:
    """AC4, AC21: Detect Latitude hook in settings and managed files."""
    override = _get_env_override("SRRS_LAT_HOOK_STATE")
    if override is not None:
        state = override.strip()
        rcs = [RC_STOP_HOOK_PRESENT] if state == "present" else (
            [RC_RUNTIME_STATE_UNKNOWN] if state == "unknown" else []
        )
        return {"state": state, "reason_codes": rcs, "checked_surfaces": ["latitude_stop_hook"]}

    managed_override = _get_env_override("SRRS_LAT_MANAGED_HOOK")
    if managed_override is not None:
        state = managed_override.strip()
        rcs = [RC_STOP_HOOK_PRESENT] if state == "present" else (
            [] if state == "absent" else [RC_RUNTIME_STATE_UNKNOWN]
        )
        return {
            "state": state,
            "reason_codes": rcs,
            "checked_surfaces": ["enabled_plugin_hooks", "active_skill_agent_hooks"],
        }

    state = "absent"
    reason_codes: list[str] = []
    checked = ["latitude_stop_hook"]

    home = Path.home()
    for settings_path, surface_name in [
        (home / ".claude" / "settings.json", "claude_user_settings"),
        (repo_root / ".claude" / "settings.json", "claude_project_settings"),
    ]:
        if surface_name not in checked:
            checked.append(surface_name)
        if settings_path.is_file():
            try:
                text = settings_path.read_text(encoding="utf-8", errors="replace")
                if _text_contains_latitude(text):
                    state = "present"
                    reason_codes.append(RC_STOP_HOOK_PRESENT)
            except OSError:
                state = "unknown"
                reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)

    checked.extend(["enabled_plugin_hooks", "active_skill_agent_hooks", "managed_claude_settings"])
    for base_dir in [repo_root / ".claude" / "agents", repo_root / ".claude" / "skills"]:
        if base_dir.is_dir():
            try:
                for md_file in base_dir.rglob("*.md"):
                    try:
                        text = md_file.read_text(encoding="utf-8", errors="replace")
                        if _text_contains_latitude(text):
                            state = "present"
                            reason_codes.append(RC_STOP_HOOK_PRESENT)
                    except OSError:
                        pass
            except (OSError, PermissionError):
                state = "unknown" if state == "absent" else state
                reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)

    return {
        "state": state,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "checked_surfaces": checked,
    }


def check_preload_state(repo_root: Path, execution_profile: str) -> dict[str, Any]:
    """AC4, AC5, AC6: Check BUN_OPTIONS preload configuration."""
    override = _get_env_override("SRRS_LAT_PRELOAD_SETTINGS")
    systemd_override = _get_env_override("SRRS_LAT_PRELOAD_SYSTEMD")
    shell_override = _get_env_override("SRRS_LAT_PRELOAD_SHELL")

    if override is not None or systemd_override is not None or shell_override is not None:
        settings_state = (override or "absent").strip()
        systemd_state = (systemd_override or "absent").strip()
        shell_state = (shell_override or "absent").strip()
        any_present = any(s == "present" for s in [settings_state, systemd_state, shell_state])
        any_unknown = any(s == "unknown" for s in [settings_state, systemd_state, shell_state])
        state = "present" if any_present else ("unknown" if any_unknown else "absent")
        rcs = [RC_PRELOAD_CONFIGURED] if any_present else (
            [RC_RUNTIME_STATE_UNKNOWN] if any_unknown else []
        )
        return {
            "state": state,
            "reason_codes": rcs,
            "checked_surfaces": [
                "settings_bun_options",
                "systemd_user_environment",
                "environment_d_dropin",
                "shell_startup_environment",
            ],
        }

    state = "absent"
    reason_codes: list[str] = []
    checked: list[str] = []

    # Current shell BUN_OPTIONS (key presence only)
    checked.append("current_shell_environment")
    bun_options = os.environ.get("BUN_OPTIONS")
    if bun_options is not None and _text_contains_preload(bun_options):
        state = "present"
        reason_codes.append(RC_PRELOAD_CONFIGURED)

    # Claude settings BUN_OPTIONS
    checked.append("settings_bun_options")
    home = Path.home()
    for sp in [home / ".claude" / "settings.json", repo_root / ".claude" / "settings.json"]:
        if sp.is_file():
            try:
                text = sp.read_text(encoding="utf-8", errors="replace")
                if "BUN_OPTIONS" in text and _text_contains_preload(text):
                    state = "present"
                    reason_codes.append(RC_PRELOAD_CONFIGURED)
            except OSError:
                pass

    # systemd/environment.d
    checked.extend([
        "systemd_user_environment",
        "environment_d_dropin",
        "systemd_environment_generators",
    ])
    for env_dir in [
        home / ".config" / "environment.d",
        Path("/etc/environment.d"),
        home / ".config" / "systemd" / "user" / "environment",
    ]:
        if env_dir.is_dir():
            try:
                for env_file in env_dir.iterdir():
                    if env_file.is_file():
                        try:
                            text = env_file.read_text(encoding="utf-8", errors="replace")
                            if "BUN_OPTIONS" in text and _text_contains_preload(text):
                                state = "present"
                                reason_codes.append(RC_PRELOAD_CONFIGURED)
                        except OSError:
                            pass
            except OSError:
                pass

    # Shell startup files
    checked.append("shell_startup_environment")
    for shell_file in [
        home / ".bashrc",
        home / ".bash_profile",
        home / ".profile",
        home / ".zshrc",
        home / ".zprofile",
    ]:
        if shell_file.is_file():
            try:
                text = shell_file.read_text(encoding="utf-8", errors="replace")
                if "BUN_OPTIONS" in text and _text_contains_preload(text):
                    state = "present"
                    reason_codes.append(RC_PRELOAD_CONFIGURED)
            except OSError:
                pass

    return {
        "state": state,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "checked_surfaces": checked,
    }


def check_active_process_preload(execution_profile: str) -> dict[str, Any]:
    """AC4, AC6: Check active Claude processes for BUN_OPTIONS preload."""
    override = _get_env_override("SRRS_LAT_ACTIVE_PROCESS")
    if override is not None:
        state = override.strip()
        rcs = [RC_PRELOAD_ACTIVE_PROCESS] if state == "preload_present" else (
            [RC_RUNTIME_STATE_UNKNOWN] if state == "unknown" else []
        )
        return {
            "state": state,
            "reason_codes": rcs,
            "checked_surfaces": ["active_claude_processes", "process_parent_environment"],
        }

    state = "preload_absent"
    reason_codes: list[str] = []

    try:
        result = subprocess.run(
            ["pgrep", "-f", "claude"],
            capture_output=True, text=True, timeout=10,
        )
        pids = [p.strip() for p in result.stdout.splitlines() if p.strip().isdigit()]
    except Exception:
        pids = []

    for pid in pids[:10]:
        try:
            env_file = Path(f"/proc/{pid}/environ")
            if env_file.is_file():
                raw = env_file.read_bytes().decode("utf-8", errors="replace")
                bun_env = [kv for kv in raw.split("\0") if kv.startswith("BUN_OPTIONS=")]
                if bun_env and _text_contains_preload(bun_env[0]):
                    state = "preload_present"
                    reason_codes.append(RC_PRELOAD_ACTIVE_PROCESS)
        except (OSError, PermissionError):
            pass

    return {
        "state": state,
        "reason_codes": reason_codes,
        "checked_surfaces": ["active_claude_processes", "process_parent_environment"],
    }


def check_export_state(repo_root: Path, execution_profile: str) -> dict[str, Any]:
    """Check LATITUDE_CLAUDE_CODE_ENABLED state."""
    override = _get_env_override("SRRS_LAT_EXPORT_STATE")
    if override is not None:
        return {"state": override.strip(), "checked_surfaces": ["current_shell_environment"]}

    val = os.environ.get("LATITUDE_CLAUDE_CODE_ENABLED")
    if val is None:
        return {"state": "unknown", "checked_surfaces": ["current_shell_environment"]}
    if val == "0":
        return {"state": "disabled", "checked_surfaces": ["current_shell_environment"]}
    return {"state": "enabled", "checked_surfaces": ["current_shell_environment"]}


def check_local_storage(repo_root: Path, execution_profile: str) -> dict[str, Any]:
    """AC4, AC7, AC22: Check Latitude local storage with lstat for unsafe metadata."""
    override = _get_env_override("SRRS_LAT_LOCAL_STORAGE")
    lstat_override = _get_env_override("SRRS_LAT_LSTAT_STATE")

    if override is not None:
        state = override.strip()
        rcs = []
        if state == "present":
            rcs = [RC_LOCAL_STORAGE_PRESENT]
        elif state == "unsafe_metadata":
            rcs = [RC_LOCAL_STORAGE_UNSAFE_METADATA]
        elif state == "unknown":
            rcs = [RC_RUNTIME_STATE_UNKNOWN]
        return {
            "state": state,
            "reason_codes": rcs,
            "checked_surfaces": ["latitude_request_spool", "latitude_state"],
        }

    state = "absent"
    reason_codes: list[str] = []
    home = Path.home()

    for path in [home / p for p in _LATITUDE_STATE_PATHS + _LATITUDE_SPOOL_PATHS]:
        try:
            import stat as stat_module
            st = path.lstat()
            mode = st.st_mode
            if stat_module.S_ISLNK(mode):
                state = "unsafe_metadata"
                reason_codes.append(RC_LOCAL_STORAGE_UNSAFE_METADATA)
            elif not stat_module.S_ISREG(mode) and not stat_module.S_ISDIR(mode):
                state = "unsafe_metadata"
                reason_codes.append(RC_LOCAL_STORAGE_UNSAFE_METADATA)
            else:
                if state == "absent":
                    state = "present"
                reason_codes.append(RC_LOCAL_STORAGE_PRESENT)
        except FileNotFoundError:
            pass
        except OSError:
            state = "unknown"
            reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)

    if lstat_override is not None and lstat_override.strip() == "unsafe_metadata":
        state = "unsafe_metadata"
        reason_codes = [RC_LOCAL_STORAGE_UNSAFE_METADATA]

    return {
        "state": state,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "checked_surfaces": ["latitude_request_spool", "latitude_state"],
    }


def check_settings_backup(repo_root: Path, execution_profile: str) -> dict[str, Any]:
    """AC4, AC7: Check backup files for Latitude credential field."""
    override = _get_env_override("SRRS_LAT_BACKUP_CREDENTIAL")
    if override is not None:
        state = override.strip()
        rcs = [RC_SETTINGS_BACKUP_CREDENTIAL] if state == "present" else []
        return {
            "state": state,
            "reason_codes": rcs,
            "checked_surfaces": ["latitude_settings_backup"],
        }

    state = "absent"
    reason_codes: list[str] = []
    home = Path.home()

    for backup_dir in [home / ".claude", home / ".claude" / "backups"]:
        if backup_dir.is_dir():
            try:
                for backup_file in backup_dir.iterdir():
                    if backup_file.is_file() and any(
                        pat.search(backup_file.name) for pat in _BACKUP_PATTERNS
                    ):
                        try:
                            text = backup_file.read_text(encoding="utf-8", errors="replace")
                            if _text_contains_credential_field(text):
                                state = "present"
                                reason_codes.append(RC_SETTINGS_BACKUP_CREDENTIAL)
                        except OSError:
                            pass
            except OSError:
                pass

    return {
        "state": state,
        "reason_codes": reason_codes,
        "checked_surfaces": ["latitude_settings_backup"],
    }


def check_distribution(repo_root: Path, execution_profile: str) -> dict[str, Any]:
    """AC9, AC26: Check Latitude package distribution integrity."""
    dist_spec_override = _get_env_override("SRRS_LAT_DIST_SPEC")
    integrity_override = _get_env_override("SRRS_LAT_DIST_INTEGRITY")
    provenance_override = _get_env_override("SRRS_LAT_DIST_PROVENANCE")

    if (dist_spec_override is not None
            or integrity_override is not None
            or provenance_override is not None):
        spec = (dist_spec_override or "unknown").strip()
        integrity = (integrity_override or "unknown").strip()
        provenance = (provenance_override or "unknown").strip()

        is_unpinned = spec in ("unpinned", "unknown") or (
            spec.startswith("npx ") and "@" not in spec
        )
        provenance_unknown = provenance == "unknown"

        reason_codes = []
        if is_unpinned:
            reason_codes.append(RC_DISTRIBUTION_UNPINNED)
        if provenance_unknown:
            reason_codes.append(RC_DISTRIBUTION_PROVENANCE_UNKNOWN)

        result_state = "unpinned" if is_unpinned else (
            "unverified" if provenance_unknown else "verified"
        )

        return {
            "state": result_state,
            "package_spec": spec if spec not in ("unpinned", "unknown") else None,
            "dist_integrity": integrity if integrity != "unknown" else None,
            "registry_signature_verified": (integrity == "verified"),
            "provenance_verified": (provenance == "verified"),
            "provenance_source_ref": None,
            "reason_codes": reason_codes,
            "checked_surfaces": ["package_distribution"],
        }

    return {
        "state": "not_installed",
        "package_spec": None,
        "dist_integrity": None,
        "registry_signature_verified": None,
        "provenance_verified": None,
        "provenance_source_ref": None,
        "reason_codes": [],
        "checked_surfaces": ["package_distribution"],
    }


def check_destination_transport(execution_profile: str) -> dict[str, Any]:
    """AC23, AC24: Check LATITUDE_BASE_URL and LATITUDE_DEBUG."""
    base_url_override = _get_env_override("SRRS_LAT_BASE_URL")
    debug_override = _get_env_override("SRRS_LAT_DEBUG")
    dest_override = _get_env_override("SRRS_LAT_DESTINATION_STATE")
    transport_override = _get_env_override("SRRS_LAT_TRANSPORT_STATE")
    diag_override = _get_env_override("SRRS_LAT_DIAGNOSTIC_LOG")

    # Destination state
    if dest_override is not None:
        destination_state = dest_override.strip()
    elif base_url_override is not None:
        url = base_url_override.strip()
        if not url:
            destination_state = "unknown"
        elif any(url.startswith(approved) for approved in _APPROVED_ORIGINS):
            destination_state = "approved_cloud"
        else:
            destination_state = "unapproved"
    else:
        base_url = os.environ.get("LATITUDE_BASE_URL")
        if base_url is None:
            destination_state = "unknown"
        elif any(base_url.startswith(approved) for approved in _APPROVED_ORIGINS):
            destination_state = "approved_cloud"
        else:
            destination_state = "unapproved"

    # Transport state
    if transport_override is not None:
        transport_state = transport_override.strip()
    elif base_url_override is not None:
        url = base_url_override.strip()
        if url.startswith("https://"):
            transport_state = "https"
        elif url.startswith("http://"):
            transport_state = "plaintext"
        else:
            transport_state = "unknown"
    else:
        base_url = os.environ.get("LATITUDE_BASE_URL")
        if base_url is None:
            transport_state = "unknown"
        elif base_url.startswith("https://"):
            transport_state = "https"
        elif base_url.startswith("http://"):
            transport_state = "plaintext"
        else:
            transport_state = "unknown"

    # Diagnostic logging
    if diag_override is not None:
        diagnostic_state = diag_override.strip()
    elif debug_override is not None:
        diagnostic_state = "enabled" if debug_override.strip() == "1" else "disabled"
    else:
        debug_val = os.environ.get("LATITUDE_DEBUG")
        diagnostic_state = "enabled" if debug_val == "1" else "disabled"

    reason_codes: list[str] = []
    if destination_state == "unapproved":
        reason_codes.append(RC_DESTINATION_UNAPPROVED)
    if transport_state in ("plaintext", "unknown"):
        reason_codes.append(RC_TRANSPORT_PLAINTEXT)
    if diagnostic_state == "enabled":
        reason_codes.append(RC_DIAGNOSTIC_LOGGING)

    return {
        "destination_state": destination_state,
        "transport_state": transport_state,
        "diagnostic_logging_state": diagnostic_state,
        "reason_codes": reason_codes,
        "checked_surfaces": ["current_shell_environment"],
    }


def check_argv_credential(execution_profile: str) -> dict[str, Any]:
    """AC28: Detect if credential was passed via argv."""
    override = _get_env_override("SRRS_LAT_ARGV_CREDENTIAL")
    if override is not None:
        state = override.strip()
        rcs = [RC_EXPOSURE_POSSIBLE] if state == "present" else []
        return {"state": state, "reason_codes": rcs}

    try:
        cmdline = Path("/proc/self/cmdline").read_bytes().decode("utf-8", errors="replace")
        for arg in cmdline.split("\0"):
            if re.match(r"--api[-_]?key", arg, re.IGNORECASE):
                return {"state": "possible", "reason_codes": [RC_EXPOSURE_POSSIBLE]}
            if re.match(r"--latitude[-_]?key", arg, re.IGNORECASE):
                return {"state": "possible", "reason_codes": [RC_EXPOSURE_POSSIBLE]}
    except OSError:
        pass

    return {"state": "absent", "reason_codes": []}


def check_containment_and_exposure(execution_profile: str) -> dict[str, Any]:
    """AC27: Check containment_state and exposure_state."""
    containment_override = _get_env_override("SRRS_LAT_CONTAINMENT_STATE")
    exposure_override = _get_env_override("SRRS_LAT_EXPOSURE_STATE")

    containment_state = containment_override.strip() if containment_override else "never_observed"
    exposure_state = exposure_override.strip() if exposure_override else "none_observed"

    reason_codes: list[str] = []
    if exposure_state in ("possible", "confirmed"):
        reason_codes.append(RC_EXPOSURE_POSSIBLE)

    return {
        "containment_state": containment_state,
        "exposure_state": exposure_state,
        "reason_codes": reason_codes,
    }


def check_uninstall_state(repo_root: Path, execution_profile: str) -> dict[str, Any]:
    """AC8: Detect uninstall state."""
    override = _get_env_override("SRRS_LAT_UNINSTALL_STATE")
    if override is not None:
        state = override.strip()
        rcs = [RC_UNINSTALL_INCOMPLETE] if state == "incomplete" else (
            [RC_RUNTIME_STATE_UNKNOWN] if state == "unknown" else []
        )
        return {"state": state, "reason_codes": rcs}
    return {"state": "not_attempted", "reason_codes": []}


def check_remote_trace(execution_profile: str) -> dict[str, Any]:
    """AC16: Check remote trace state (requires human attestation)."""
    override = _get_env_override("SRRS_LAT_REMOTE_TRACE")
    if override is not None:
        state = override.strip()
        rcs = [RC_REMOTE_TRACE_UNKNOWN] if state == "unknown" else []
        return {"state": state, "reason_codes": rcs}
    # Default: unknown (requires human attestation)
    return {"state": "unknown", "reason_codes": [RC_REMOTE_TRACE_UNKNOWN]}


def _compute_latitude_verdict(
    credential_state: str,
    hook_state: str,
    preload_state: str,
    active_process_state: str,
    export_state: str,
    local_storage_state: str,
    uninstall_state: str,
    dist_state: str,
    destination_state: str,
    transport_state: str,
    diagnostic_state: str,
    exposure_state: str,
    containment_state: str,
    backup_credential_state: str,
    remote_trace_state: str,
    all_reason_codes: list[str],
) -> str:
    """Compute latitude component verdict per Issue contract Verdict Rules."""
    # not_applicable: no indicators, no unknowns
    lat_indicators = [credential_state, hook_state, preload_state]
    any_present = any(s in ("present", "active") for s in lat_indicators)
    any_rc = bool(all_reason_codes)
    any_unknown = any(
        s == "unknown"
        for s in [
            credential_state, hook_state, preload_state,
            active_process_state, local_storage_state,
            uninstall_state, remote_trace_state,
        ]
    )

    if not any_present and not any_unknown and not any_rc:
        return "not_applicable"

    # blocked conditions
    if (
        credential_state == "present"
        or hook_state == "present"
        or preload_state == "present"
        or active_process_state == "preload_present"
        or local_storage_state in ("present", "unsafe_metadata")
        or uninstall_state == "incomplete"
        or dist_state in ("unpinned", "unverified")
        or destination_state == "unapproved"
        or transport_state == "plaintext"
        or diagnostic_state == "enabled"
        or exposure_state in ("possible", "confirmed")
        or containment_state == "active"
        or backup_credential_state == "present"
        or RC_EXPOSURE_POSSIBLE in all_reason_codes
    ):
        return "blocked"

    # AC27: containment_state != never_observed -> not safe
    if containment_state not in ("never_observed", "not_applicable"):
        return "blocked"

    # fail_closed: unknown states
    if any_unknown or active_process_state == "unknown" or transport_state == "unknown":
        return "fail_closed"

    # AC5: export disabled capture active -> blocked (handled via reason codes)
    if RC_EXPORT_DISABLED_CAPTURE_ACTIVE in all_reason_codes:
        return "blocked"

    return "safe"


def check_latitude_component(
    repo_root: Path,
    execution_profile: str,
) -> dict[str, Any]:
    """
    Run all Latitude telemetry safety checks.
    Returns a dict matching the `latitude` field of session_recording_runtime_safety/v2.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # AC19: reject SRRS_LAT_* overrides in host mode
    if execution_profile == "host":
        rejection = check_execution_profile_override_rejection(execution_profile)
        if rejection is not None:
            return {
                "applicability": "applicable",
                "verdict": "fail_closed",
                "containment_state": "unknown",
                "credential_state": "unknown",
                "export_state": "unknown",
                "capture_state": "unknown",
                "local_storage_state": "unknown",
                "active_process_state": "unknown",
                "destination_state": "unknown",
                "transport_state": "unknown",
                "diagnostic_logging_state": "unknown",
                "exposure_state": "unknown",
                "uninstall_state": "unknown",
                "remote_trace_state": "unknown",
                "distribution": {
                    "package_spec": None,
                    "dist_integrity": None,
                    "registry_signature_verified": None,
                    "provenance_verified": None,
                    "provenance_source_ref": None,
                    "tarball_sha256": None,
                    "installed_entrypoint_sha256": None,
                    "preload_sha256": None,
                    "resolution_source": "unknown",
                },
                "reason_codes": [RC_SRRS_OVERRIDE_REJECTED],
                "checked_surfaces": [],
                "raw_values_emitted": False,
                "checked_at": now_iso,
            }

    cred = check_credential_state(repo_root, execution_profile)
    hook = check_hook_state(repo_root, execution_profile)
    preload = check_preload_state(repo_root, execution_profile)
    proc = check_active_process_preload(execution_profile)
    export = check_export_state(repo_root, execution_profile)
    local_storage = check_local_storage(repo_root, execution_profile)
    backup = check_settings_backup(repo_root, execution_profile)
    dist = check_distribution(repo_root, execution_profile)
    dest_transport = check_destination_transport(execution_profile)
    argv = check_argv_credential(execution_profile)
    containment_exposure = check_containment_and_exposure(execution_profile)
    uninstall = check_uninstall_state(repo_root, execution_profile)
    remote_trace = check_remote_trace(execution_profile)

    # Capture state (AC5)
    export_state = export["state"]
    capture_state_override = _get_env_override("SRRS_LAT_CAPTURE_STATE")
    if capture_state_override is not None:
        capture_state = capture_state_override.strip()
    else:
        if export_state == "disabled" and preload["state"] == "present":
            capture_state = "active"
        elif preload["state"] == "present":
            capture_state = "active"
        else:
            capture_state = "inactive"

    # Collect all reason codes
    all_rcs: list[str] = []
    for sub in [cred, hook, preload, proc, local_storage, backup, argv,
                containment_exposure, uninstall, remote_trace]:
        all_rcs.extend(sub.get("reason_codes", []))
    all_rcs.extend(dist.get("reason_codes", []))
    all_rcs.extend(dest_transport.get("reason_codes", []))

    # AC5: export disabled but capture active
    if export_state == "disabled" and capture_state == "active":
        all_rcs.append(RC_EXPORT_DISABLED_CAPTURE_ACTIVE)

    # Collect surfaces
    all_surfaces: list[str] = []
    for sub in [cred, hook, preload, proc, local_storage, backup, dist]:
        all_surfaces.extend(sub.get("checked_surfaces", []))
    all_surfaces.extend(dest_transport.get("checked_surfaces", []))

    all_rcs = list(dict.fromkeys(all_rcs))
    all_surfaces = list(dict.fromkeys(all_surfaces))

    verdict = _compute_latitude_verdict(
        credential_state=cred["state"],
        hook_state=hook["state"],
        preload_state=preload["state"],
        active_process_state=proc["state"],
        export_state=export_state,
        local_storage_state=local_storage["state"],
        uninstall_state=uninstall["state"],
        dist_state=dist["state"],
        destination_state=dest_transport["destination_state"],
        transport_state=dest_transport["transport_state"],
        diagnostic_state=dest_transport["diagnostic_logging_state"],
        exposure_state=containment_exposure["exposure_state"],
        containment_state=containment_exposure["containment_state"],
        backup_credential_state=backup["state"],
        remote_trace_state=remote_trace["state"],
        all_reason_codes=all_rcs,
    )

    return {
        "applicability": "applicable",
        "verdict": verdict,
        "containment_state": containment_exposure["containment_state"],
        "credential_state": cred["state"],
        "export_state": export_state,
        "capture_state": capture_state,
        "local_storage_state": local_storage["state"],
        "active_process_state": proc["state"],
        "destination_state": dest_transport["destination_state"],
        "transport_state": dest_transport["transport_state"],
        "diagnostic_logging_state": dest_transport["diagnostic_logging_state"],
        "exposure_state": containment_exposure["exposure_state"],
        "uninstall_state": uninstall["state"],
        "remote_trace_state": remote_trace["state"],
        "distribution": {
            "package_spec": dist.get("package_spec"),
            "dist_integrity": dist.get("dist_integrity"),
            "registry_signature_verified": dist.get("registry_signature_verified"),
            "provenance_verified": dist.get("provenance_verified"),
            "provenance_source_ref": dist.get("provenance_source_ref"),
            "tarball_sha256": None,
            "installed_entrypoint_sha256": None,
            "preload_sha256": None,
            "resolution_source": "unknown",
        },
        "reason_codes": all_rcs,
        "checked_surfaces": all_surfaces,
        "raw_values_emitted": False,
        "checked_at": now_iso,
    }
