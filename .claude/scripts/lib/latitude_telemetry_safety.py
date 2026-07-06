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

Execution profiles:
  host:    Inspects real host surfaces (Path.home(), /proc, systemd, etc.)
           Rejects all SRRS_LAT_* overrides (AC19).
  fixture: Inspects only surfaces rooted at fixture_root / home_root.
           Requires explicit home_root; raises ValueError if not provided.

Environment overrides (fixture mode only):
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

  #1261 distribution evidence / argv exposure / remote cleanup gate overrides:
  SRRS_LAT_RESOLUTION_SOURCE       'local_lockfile' | 'project_local_install' | 'npm_cache'
                                   | 'global_install' | 'npx_only' | 'unknown'
  SRRS_LAT_RESOLVED_REGISTRY_ORIGIN  '<url>' | 'unknown'
  SRRS_LAT_LOCKFILE_DIGEST         'sha256:<64hex>' | '' (empty = not set)
  SRRS_LAT_TARBALL_SHA256         'sha256:<64hex>' | '' (empty = not set)
  SRRS_LAT_ENTRYPOINT_SHA256       'sha256:<64hex>' | '' (empty = not set)
  SRRS_LAT_PRELOAD_SHA256          'sha256:<64hex>' | '' (empty = not set)
  SRRS_LAT_HOOK_COMMAND_SHA256     'sha256:<64hex>' | '' (empty = not set)
  SRRS_LAT_ARGV_EXPOSURE_STATE     'absent_verified' | 'possible' | 'unknown'
  SRRS_LAT_REMOTE_CLEANUP_STATE    'machine_verified' | 'human_attested' | 'unknown'
"""
from __future__ import annotations

import os
import re
import stat as stat_module
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
# #1261: distribution / argv exposure / remote cleanup evidence gate reason codes
RC_NPX_ONLY_UNPINNED = "latitude_npx_only_without_exact_version"
RC_RESOLUTION_SOURCE_UNKNOWN = "latitude_resolution_source_unknown"
RC_ARGV_EXPOSURE_STATE_UNKNOWN = "latitude_argv_exposure_state_unknown"
RC_REMOTE_CLEANUP_NOT_MACHINE_VERIFIED = "latitude_remote_cleanup_state_not_machine_verified"
RC_HOOK_COMMAND_DIGEST_MISSING = "latitude_hook_command_digest_missing"
# PR #1352 REQUEST_CHANGES follow-up: per-field reason codes so missing/malformed
# evidence is never silently absorbed into a single generic code.
RC_NPX_INVOCATION_FLOATING = "latitude_npx_invocation_floating"
RC_REGISTRY_ORIGIN_MISSING_OR_MISMATCH = "latitude_registry_origin_missing_or_mismatch"
RC_LOCKFILE_DIGEST_MISSING_OR_MALFORMED = "latitude_lockfile_digest_missing_or_malformed"
RC_TARBALL_DIGEST_MISSING_OR_MALFORMED = "latitude_tarball_digest_missing_or_malformed"
RC_ENTRYPOINT_DIGEST_MISSING_OR_MALFORMED = "latitude_entrypoint_digest_missing_or_malformed"
RC_PRELOAD_DIGEST_MISSING_OR_MALFORMED = "latitude_preload_digest_missing_or_malformed"
RC_PACKAGE_SPEC_NOT_EXACT = "latitude_package_spec_not_exact"
RC_DIST_INTEGRITY_MALFORMED = "latitude_dist_integrity_malformed"

# #1261: closed enums for the Latitude distribution / argv exposure / remote
# cleanup evidence gate (LATITUDE_DISTRIBUTION_GATE_V1, docs/dev/secret-policy.md).
RESOLUTION_SOURCE_ENUM = {
    "local_lockfile",
    "project_local_install",
    "npm_cache",
    "global_install",
    "npx_only",
    "unknown",
}
ARGV_EXPOSURE_STATE_ENUM = {"absent_verified", "possible", "unknown"}
REMOTE_CLEANUP_STATE_ENUM = {"machine_verified", "human_attested", "unknown"}
# PR #1352 REQUEST_CHANGES follow-up: npx_invocation is independent of
# resolution_source so a floating `npx -y <pkg>` invocation (no exact version
# pin) can be blocked even when other evidence looks complete.
NPX_INVOCATION_ENUM = {"exact_version", "floating", "absent", "unknown"}
_EVIDENCE_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_APPROVED_REGISTRY_ORIGINS = {"https://registry.npmjs.org"}
_NPX_PREFIX_RE = re.compile(r"^npx\s+(?:-y|--yes)?\s*", re.IGNORECASE)
_EXACT_SEMVER_SPEC_RE = re.compile(
    r"^(@[\w.\-]+/[\w.\-]+|[\w.\-]+)@\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$"
)
_SRI_DIGEST_RE = re.compile(r"^sha(256|384|512)-[A-Za-z0-9+/]+={0,2}$")


def _is_evidence_sha256(value: Any) -> bool:
    """#1261 follow-up: True iff value is a well-formed `sha256:<64hex>` digest."""
    return isinstance(value, str) and _EVIDENCE_SHA256_RE.fullmatch(value) is not None


def _is_exact_semver_spec(value: Any) -> bool:
    """#1261 follow-up: True iff value normalizes to `<name>@x.y.z` exact semver.

    Strips a leading `npx`/`npx -y` prefix (the distribution checker stores the
    raw hook/dist spec, which may include the npx invocation prefix) before
    validating the exact-semver suffix.
    """
    if not isinstance(value, str):
        return False
    normalized = _NPX_PREFIX_RE.sub("", value).strip()
    return _EXACT_SEMVER_SPEC_RE.fullmatch(normalized) is not None


def _is_sri_digest(value: Any) -> bool:
    """#1261 follow-up: True iff value looks like a Subresource Integrity digest."""
    return isinstance(value, str) and _SRI_DIGEST_RE.fullmatch(value) is not None


def _is_approved_registry_origin(value: Any) -> bool:
    """#1261 follow-up: resolved_registry_origin must be an approved npm registry."""
    return isinstance(value, str) and value in _APPROVED_REGISTRY_ORIGINS

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

# Latitude state/spool paths (relative to home) — legacy paths
_LATITUDE_STATE_PATHS_LEGACY = [
    ".latitude",
    ".config/latitude",
    ".local/share/latitude",
    ".latitude-state",
    ".latitude/state",
    ".latitude/config.json",
]

# Latitude state/spool paths from upstream implementation (~/.claude/state/latitude/)
_LATITUDE_STATE_PATHS_UPSTREAM = [
    ".claude/state/latitude",
    ".claude/state/latitude/intercept.js",
    ".claude/state/latitude/requests",
    ".claude/state/latitude/state.json",
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

# Approved origins (exact scheme + host, no path prefix match)
# B5 fix: use exact origin (scheme+host) matching, not startswith
_APPROVED_ORIGINS_EXACT = {
    "https://telemetry.latitude.so",
    "https://latitude.so",
    "https://app.latitude.so",
    "https://gateway.latitude.so",
    "https://ingest.latitude.so",  # B5: upstream production ingest origin
}


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


def _is_approved_origin(url: str) -> bool:
    """B5 fix: exact (scheme, hostname, effective_port) matching for approved origins."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    # Reconstruct canonical origin: scheme + "://" + host (strip default port)
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname or ""
    port = parsed.port
    # Default ports: https=443, http=80
    default_ports = {"https": 443, "http": 80}
    if port and port != default_ports.get(scheme):
        origin = f"{scheme}://{hostname}:{port}"
    else:
        origin = f"{scheme}://{hostname}"
    return origin in _APPROVED_ORIGINS_EXACT


def _parse_npm_spec(spec: str) -> tuple[str | None, str | None]:
    """
    Parse npm package spec into (name, version).
    Handles scoped packages: @scope/name@version -> name=@scope/name, version=version
    Unscoped: name@version -> name=name, version=version
    B4 fix: scoped packages have @ in name, version pin is after the last @
    """
    if not spec:
        return None, None
    spec = spec.strip()
    # Remove leading 'npx' or 'npx -y'
    spec = re.sub(r"^npx\s+(-y\s+)?", "", spec).strip()
    if not spec:
        return None, None

    if spec.startswith("@"):
        # Scoped package: @scope/name[@version]
        # Find version separator: last @ that is NOT the leading @
        at_idx = spec.rfind("@", 1)  # search from index 1 to skip leading @
        if at_idx > 0:
            name = spec[:at_idx]
            version = spec[at_idx + 1:]
            return name, version if version else None
        else:
            return spec, None
    else:
        # Unscoped: name[@version]
        at_idx = spec.find("@")
        if at_idx > 0:
            name = spec[:at_idx]
            version = spec[at_idx + 1:]
            return name, version if version else None
        else:
            return spec, None


def _is_version_pinned(spec: str) -> bool:
    """
    B4 fix: Check if spec has exact version pin.
    npx @latitude-data/claude-code-telemetry (no version) -> unpinned
    npx @latitude-data/claude-code-telemetry@1.2.3 -> pinned
    """
    if not spec:
        return False
    if spec in ("unpinned", "unknown"):
        return False

    name, version = _parse_npm_spec(spec)
    if version is None:
        return False
    # #1220: exact semver only. latest/next/canary/tag, '*', x-ranges, and
    # caret (^x.y.z) / tilde (~x.y.z) / comparator ranges (>=, <=, >, <, =) are
    # all treated as UNPINNED so real pilot activation cannot use a floating spec.
    if version in ("latest", "next", "canary", "*", ""):
        return False
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?", version):
        return False
    return True


def check_execution_profile_override_rejection(execution_profile: str) -> str | None:
    """AC19: In host mode, reject SRRS_LAT_* overrides."""
    if execution_profile != "host":
        return None
    lat_overrides = [k for k in os.environ if k.startswith("SRRS_LAT_")]
    if lat_overrides:
        return RC_SRRS_OVERRIDE_REJECTED
    return None


def check_credential_state(
    repo_root: Path,
    execution_profile: str,
    home_root: Path | None = None,
) -> dict[str, Any]:
    """AC11: Detect LATITUDE_API_KEY field presence (not value)."""
    override = _get_env_override("SRRS_LAT_CREDENTIAL_STATE")
    if override is not None:
        state = override.strip()
        rcs = [RC_CREDENTIAL_PRESENT] if state == "present" else []
        return {"state": state, "reason_codes": rcs, "checked_surfaces": ["claude_user_settings"]}

    state = "absent"
    reason_codes: list[str] = []
    checked: list[str] = []

    # B1 fix: use home_root for fixture, Path.home() only for host
    if execution_profile == "host":
        home = Path.home()
    else:
        home = home_root if home_root is not None else repo_root

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


def check_hook_state(
    repo_root: Path,
    execution_profile: str,
    home_root: Path | None = None,
) -> dict[str, Any]:
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
    inspection_gaps: list[str] = []

    # B1 fix: use home_root for fixture, Path.home() only for host
    if execution_profile == "host":
        home = Path.home()
    else:
        home = home_root if home_root is not None else repo_root

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
                # B3 fix: permission error -> unknown, not silent pass
                if state == "absent":
                    state = "unknown"
                reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)
                inspection_gaps.append(surface_name)

    # Managed settings surfaces — mark as gaps unless host mode can inspect them
    # B3 fix: Only add to checked_surfaces if actually inspected
    managed_surfaces = ["enabled_plugin_hooks", "active_skill_agent_hooks", "managed_claude_settings"]

    if execution_profile == "host":
        # Try to inspect managed settings paths
        managed_paths = [
            (Path("/etc/claude-code/managed-settings.json"), "managed_claude_settings"),
        ]
        for mp, mname in managed_paths:
            if mp.is_file():
                try:
                    text = mp.read_text(encoding="utf-8", errors="replace")
                    if _text_contains_latitude(text):
                        state = "present"
                        reason_codes.append(RC_STOP_HOOK_PRESENT)
                    checked.append(mname)
                except OSError:
                    if state == "absent":
                        state = "unknown"
                    reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)
                    inspection_gaps.append(mname)
            else:
                # File doesn't exist — surface inspected, nothing found
                checked.append(mname)
    else:
        # In fixture mode, managed policy is not reachable — mark as gaps
        for ms in managed_surfaces:
            if ms not in inspection_gaps:
                inspection_gaps.append(ms)

    # Scan repo skill/agent markdown files for hook commands
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
                        # B3 fix: permission error -> unknown
                        if state == "absent":
                            state = "unknown"
                        reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)
            except (OSError, PermissionError):
                if state == "absent":
                    state = "unknown"
                reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)
                inspection_gaps.append(str(base_dir))

    return {
        "state": state,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "checked_surfaces": checked,
        "inspection_gaps": inspection_gaps,
    }


def check_preload_state(
    repo_root: Path,
    execution_profile: str,
    home_root: Path | None = None,
) -> dict[str, Any]:
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
            "inspection_gaps": [],
        }

    state = "absent"
    reason_codes: list[str] = []
    checked: list[str] = []
    inspection_gaps: list[str] = []

    # B1 fix: use home_root for fixture, Path.home() only for host
    if execution_profile == "host":
        home = Path.home()
    else:
        home = home_root if home_root is not None else repo_root

    # Current shell BUN_OPTIONS (only in host mode — fixture env is isolated)
    if execution_profile == "host":
        checked.append("current_shell_environment")
        bun_options = os.environ.get("BUN_OPTIONS")
        if bun_options is not None and _text_contains_preload(bun_options):
            state = "present"
            reason_codes.append(RC_PRELOAD_CONFIGURED)

    # Claude settings BUN_OPTIONS
    checked.append("settings_bun_options")
    for sp in [home / ".claude" / "settings.json", repo_root / ".claude" / "settings.json"]:
        if sp.is_file():
            try:
                text = sp.read_text(encoding="utf-8", errors="replace")
                if "BUN_OPTIONS" in text and _text_contains_preload(text):
                    state = "present"
                    reason_codes.append(RC_PRELOAD_CONFIGURED)
            except OSError:
                # B3 fix: permission error -> unknown
                if state == "absent":
                    state = "unknown"
                reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)
                inspection_gaps.append("settings_bun_options")

    # systemd/environment.d
    checked.extend([
        "systemd_user_environment",
        "environment_d_dropin",
        "systemd_environment_generators",
    ])

    # B3 fix: In host mode, also query systemd manager environment directly
    if execution_profile == "host":
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show-environment"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                if "BUN_OPTIONS" in result.stdout and _text_contains_preload(result.stdout):
                    state = "present"
                    reason_codes.append(RC_PRELOAD_CONFIGURED)
            else:
                # systemctl not available or failed — mark as unknown gap
                if state == "absent":
                    state = "unknown"
                reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)
                inspection_gaps.append("systemd_manager_environment")
        except (OSError, FileNotFoundError):
            # systemctl not available
            if state == "absent":
                state = "unknown"
            reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)
            inspection_gaps.append("systemd_manager_environment")
        except subprocess.TimeoutExpired:
            if state == "absent":
                state = "unknown"
            reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)
            inspection_gaps.append("systemd_manager_environment")

    for env_dir in [
        home / ".config" / "environment.d",
        Path("/etc/environment.d") if execution_profile == "host" else None,
        home / ".config" / "systemd" / "user" / "environment",
    ]:
        if env_dir is None:
            continue
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
                            if state == "absent":
                                state = "unknown"
                            reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)
            except OSError:
                if state == "absent":
                    state = "unknown"
                reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)

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
                if state == "absent":
                    state = "unknown"
                reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)

    return {
        "state": state,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "checked_surfaces": checked,
        "inspection_gaps": inspection_gaps,
    }


def check_active_process_preload(
    execution_profile: str,
    proc_root: Path | None = None,
) -> dict[str, Any]:
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
            "checked_surfaces": ["active_claude_processes"],
            "inspection_gaps": [],
        }

    state = "preload_absent"
    reason_codes: list[str] = []
    inspection_gaps: list[str] = []

    # B1 fix: fixture mode does not inspect real /proc or run pgrep
    if execution_profile != "host":
        # In fixture mode, process inspection is not available
        state = "unknown"
        reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)
        inspection_gaps.append("active_claude_processes")
        inspection_gaps.append("process_parent_environment")
        return {
            "state": state,
            "reason_codes": reason_codes,
            "checked_surfaces": [],
            "inspection_gaps": inspection_gaps,
        }

    # Host mode: inspect real processes
    # B3 fix: pgrep failure is unknown, not "no PIDs"
    actual_proc_root = proc_root if proc_root is not None else Path("/proc")
    pids: list[str] = []
    pgrep_ok = False
    try:
        result = subprocess.run(
            ["pgrep", "-f", "claude"],
            capture_output=True, text=True, timeout=10,
        )
        pgrep_ok = True
        pids = [p.strip() for p in result.stdout.splitlines() if p.strip().isdigit()]
    except FileNotFoundError:
        # pgrep not available
        state = "unknown"
        reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)
        inspection_gaps.append("active_claude_processes")
    except Exception:
        # B3 fix: other pgrep failures -> unknown
        state = "unknown"
        reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)
        inspection_gaps.append("active_claude_processes")

    if pgrep_ok:
        checked_count = 0
        for pid in pids[:50]:  # inspect up to 50 PIDs
            env_file = actual_proc_root / pid / "environ"
            try:
                if env_file.is_file():
                    raw = env_file.read_bytes().decode("utf-8", errors="replace")
                    bun_env = [kv for kv in raw.split("\0") if kv.startswith("BUN_OPTIONS=")]
                    if bun_env and _text_contains_preload(bun_env[0]):
                        state = "preload_present"
                        reason_codes.append(RC_PRELOAD_ACTIVE_PROCESS)
                    checked_count += 1
            except PermissionError:
                # B3 fix: permission error on /proc/<pid>/environ -> unknown, not silent pass
                if state == "preload_absent":
                    state = "unknown"
                reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)
                inspection_gaps.append(f"proc_environ_{pid}")
            except OSError:
                if state == "preload_absent":
                    state = "unknown"
                reason_codes.append(RC_RUNTIME_STATE_UNKNOWN)

    checked_surfaces = ["active_claude_processes"] if pgrep_ok else []

    return {
        "state": state,
        "reason_codes": reason_codes,
        "checked_surfaces": checked_surfaces,
        "inspection_gaps": inspection_gaps,
    }


def check_export_state(
    repo_root: Path,
    execution_profile: str,
    home_root: Path | None = None,
) -> dict[str, Any]:
    """Check LATITUDE_CLAUDE_CODE_ENABLED state."""
    override = _get_env_override("SRRS_LAT_EXPORT_STATE")
    if override is not None:
        return {"state": override.strip(), "checked_surfaces": ["current_shell_environment"]}

    # B1 fix: only read real env in host mode
    if execution_profile != "host":
        return {"state": "unknown", "checked_surfaces": []}

    val = os.environ.get("LATITUDE_CLAUDE_CODE_ENABLED")
    if val is None:
        return {"state": "unknown", "checked_surfaces": ["current_shell_environment"]}
    if val == "0":
        return {"state": "disabled", "checked_surfaces": ["current_shell_environment"]}
    return {"state": "enabled", "checked_surfaces": ["current_shell_environment"]}


def check_local_storage(
    repo_root: Path,
    execution_profile: str,
    home_root: Path | None = None,
) -> dict[str, Any]:
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
            "checked_surfaces": ["latitude_request_spool", "latitude_state", "claude_state_latitude"],
        }

    state = "absent"
    reason_codes: list[str] = []

    # B1 fix: use home_root for fixture, Path.home() only for host
    if execution_profile == "host":
        home = Path.home()
    else:
        home = home_root if home_root is not None else repo_root

    # B2 fix: include upstream ~/.claude/state/latitude/ paths
    all_paths = (
        [home / p for p in _LATITUDE_STATE_PATHS_LEGACY]
        + [home / p for p in _LATITUDE_SPOOL_PATHS]
        + [home / p for p in _LATITUDE_STATE_PATHS_UPSTREAM]
    )

    for path in all_paths:
        try:
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
        "checked_surfaces": ["latitude_request_spool", "latitude_state", "claude_state_latitude"],
    }


def check_settings_backup(
    repo_root: Path,
    execution_profile: str,
    home_root: Path | None = None,
) -> dict[str, Any]:
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

    # B1 fix: use home_root for fixture, Path.home() only for host
    if execution_profile == "host":
        home = Path.home()
    else:
        home = home_root if home_root is not None else repo_root

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
    """AC9, AC26, #1261: Check Latitude package distribution integrity and evidence.

    #1261: adds resolution_source (closed enum), resolved_registry_origin,
    lockfile_digest, tarball_sha256, installed_entrypoint_sha256, preload_sha256
    and hook_command_sha256 to the distribution evidence, matching the
    LATITUDE_DISTRIBUTION_GATE_V1 shape documented in docs/dev/secret-policy.md.
    """
    dist_spec_override = _get_env_override("SRRS_LAT_DIST_SPEC")
    integrity_override = _get_env_override("SRRS_LAT_DIST_INTEGRITY")
    provenance_override = _get_env_override("SRRS_LAT_DIST_PROVENANCE")
    resolution_source_override = _get_env_override("SRRS_LAT_RESOLUTION_SOURCE")
    registry_origin_override = _get_env_override("SRRS_LAT_RESOLVED_REGISTRY_ORIGIN")
    lockfile_digest_override = _get_env_override("SRRS_LAT_LOCKFILE_DIGEST")
    tarball_override = _get_env_override("SRRS_LAT_TARBALL_SHA256")
    entrypoint_override = _get_env_override("SRRS_LAT_ENTRYPOINT_SHA256")
    preload_override = _get_env_override("SRRS_LAT_PRELOAD_SHA256")
    hook_command_override = _get_env_override("SRRS_LAT_HOOK_COMMAND_SHA256")
    npx_invocation_override = _get_env_override("SRRS_LAT_NPX_INVOCATION")

    # #1261: the new evidence overrides are only asserted when a caller opts in
    # by setting at least one of them, so pre-existing callers that only use the
    # legacy SRRS_LAT_DIST_SPEC/INTEGRITY/PROVENANCE overrides keep their prior
    # reason_codes (no unrelated regression).
    new_evidence_overrides = [
        resolution_source_override, registry_origin_override, lockfile_digest_override,
        tarball_override, entrypoint_override, preload_override, hook_command_override,
    ]
    new_evidence_requested = any(v is not None for v in new_evidence_overrides)

    if (dist_spec_override is not None
            or integrity_override is not None
            or provenance_override is not None
            or new_evidence_requested):
        spec = (dist_spec_override or "unknown").strip()
        integrity = (integrity_override or "unknown").strip()
        provenance = (provenance_override or "unknown").strip()
        resolution_source = (resolution_source_override or "unknown").strip()
        if resolution_source not in RESOLUTION_SOURCE_ENUM:
            resolution_source = "unknown"
        registry_origin = (registry_origin_override or "unknown").strip()
        lockfile_digest = lockfile_digest_override.strip() if lockfile_digest_override else None
        tarball_sha256 = tarball_override.strip() if tarball_override else None
        installed_entrypoint_sha256 = entrypoint_override.strip() if entrypoint_override else None
        preload_sha256 = preload_override.strip() if preload_override else None
        hook_command_sha256 = hook_command_override.strip() if hook_command_override else None

        # B4 fix: use _is_version_pinned for correct scoped package handling
        is_unpinned = spec in ("unpinned", "unknown") or not _is_version_pinned(spec)
        provenance_unknown = provenance == "unknown"
        is_npx_only = resolution_source == "npx_only"

        # #1261 follow-up (PR #1352 REQUEST_CHANGES): npx_invocation is a
        # closed-enum field independent of resolution_source. It can be set
        # explicitly for tests; otherwise it is derived from is_npx_only /
        # is_unpinned so a floating `npx -y <pkg>` (no exact version) is
        # always classified as `floating`, never silently `absent`/`unknown`.
        if npx_invocation_override is not None:
            npx_invocation = npx_invocation_override.strip()
            if npx_invocation not in NPX_INVOCATION_ENUM:
                npx_invocation = "unknown"
        elif is_npx_only:
            npx_invocation = "floating" if is_unpinned else "exact_version"
        elif resolution_source == "unknown":
            npx_invocation = "unknown"
        else:
            npx_invocation = "absent"

        reason_codes = []
        if is_unpinned:
            reason_codes.append(RC_DISTRIBUTION_UNPINNED)
        if provenance_unknown:
            reason_codes.append(RC_DISTRIBUTION_PROVENANCE_UNKNOWN)
        if new_evidence_requested:
            if is_npx_only and is_unpinned:
                reason_codes.append(RC_NPX_ONLY_UNPINNED)
            if npx_invocation == "floating":
                reason_codes.append(RC_NPX_INVOCATION_FLOATING)
            if resolution_source_override is not None and resolution_source == "unknown":
                reason_codes.append(RC_RESOLUTION_SOURCE_UNKNOWN)
            if not hook_command_sha256:
                reason_codes.append(RC_HOOK_COMMAND_DIGEST_MISSING)
            # #1261 follow-up: per-field reason codes for missing/malformed
            # evidence (PR #1352 REQUEST_CHANGES P1).
            if not _is_approved_registry_origin(registry_origin if registry_origin != "unknown" else None):
                reason_codes.append(RC_REGISTRY_ORIGIN_MISSING_OR_MISMATCH)
            if not _is_evidence_sha256(lockfile_digest):
                reason_codes.append(RC_LOCKFILE_DIGEST_MISSING_OR_MALFORMED)
            if not _is_evidence_sha256(tarball_sha256):
                reason_codes.append(RC_TARBALL_DIGEST_MISSING_OR_MALFORMED)
            if not _is_evidence_sha256(installed_entrypoint_sha256):
                reason_codes.append(RC_ENTRYPOINT_DIGEST_MISSING_OR_MALFORMED)
            if not _is_evidence_sha256(preload_sha256):
                reason_codes.append(RC_PRELOAD_DIGEST_MISSING_OR_MALFORMED)
            if spec not in ("unpinned", "unknown") and not _is_exact_semver_spec(spec):
                reason_codes.append(RC_PACKAGE_SPEC_NOT_EXACT)
            if integrity != "unknown" and not _is_sri_digest(integrity):
                reason_codes.append(RC_DIST_INTEGRITY_MALFORMED)

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
            "resolved_registry_origin": registry_origin if registry_origin != "unknown" else None,
            "resolution_source": resolution_source,
            "npx_invocation": npx_invocation,
            "lockfile_digest": lockfile_digest,
            "tarball_sha256": tarball_sha256,
            "installed_entrypoint_sha256": installed_entrypoint_sha256,
            "preload_sha256": preload_sha256,
            "hook_command_sha256": hook_command_sha256,
            "reason_codes": reason_codes,
            "checked_surfaces": ["package_distribution"],
        }

    # B4 fix: In host mode, attempt to check npm/npx for actual Latitude package
    if execution_profile == "host":
        return _check_distribution_host(repo_root)

    return {
        "state": "not_installed",
        "package_spec": None,
        "dist_integrity": None,
        "registry_signature_verified": None,
        "provenance_verified": None,
        "provenance_source_ref": None,
        "resolved_registry_origin": None,
        "resolution_source": "unknown",
        "npx_invocation": "unknown",
        "lockfile_digest": None,
        "tarball_sha256": None,
        "installed_entrypoint_sha256": None,
        "preload_sha256": None,
        "hook_command_sha256": None,
        "reason_codes": [],
        "checked_surfaces": ["package_distribution"],
    }


_LAT_PACKAGES = (
    "@latitude-data/claude-code-telemetry",
    "@latitude-so/claude-code",
    "latitude-claude",
)


def _read_claude_settings_text_host(repo_root: Path) -> list[str]:
    """#1261 follow-up: read-only text of Claude settings surfaces (host mode).

    Returns the raw text of any settings.json files that exist, so the caller
    can pattern-match hook commands. Never reads shell history / terminal
    scrollback; only the same settings.json surfaces `check_hook_state`
    already inspects (repo-project settings first, then real user home).
    """
    texts: list[str] = []
    candidates = [repo_root / ".claude" / "settings.json"]
    try:
        candidates.append(Path.home() / ".claude" / "settings.json")
    except RuntimeError:
        pass
    for settings_path in candidates:
        if settings_path.is_file():
            try:
                texts.append(settings_path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    return texts


def _find_npx_invocation_for_package(text: str, pkg: str) -> str | None:
    """#1261 follow-up: return 'floating' or 'exact_version' for an
    `npx -y <pkg>[@version]` invocation of `pkg` found in `text`, else None.

    A floating invocation (no version pin at all, e.g. the upstream
    `npx -y @latitude-data/claude-code-telemetry` install/Stop-hook command)
    is classified `floating` even when other repo-local evidence (lockfile,
    node_modules, npm cache) looks like a pinned install, because npx always
    re-resolves against the registry at invocation time.
    """
    pattern = re.compile(
        r"npx\s+(?:-y|--yes)\s+" + re.escape(pkg) + r"(@(\S+))?",
        re.IGNORECASE,
    )
    m = pattern.search(text)
    if m is None:
        return None
    version = m.group(2)
    if version and _is_version_pinned(f"{pkg}@{version}"):
        return "exact_version"
    return "floating"


def _classify_resolution_source_host(repo_root: Path, pkg: str) -> tuple[str, str]:
    """#1261: best-effort, presence-only classification of npm resolution source.

    Returns (resolution_source, npx_invocation). Never reads shell history /
    terminal scrollback. Hook-command parsing (Claude settings.json, read-only)
    takes priority over repo-local lockfiles / node_modules / npm's own
    cache/global-install inventory commands (subprocess, stdout not forwarded
    to caller): a floating `npx -y <pkg>` Stop hook re-resolves the package
    from the registry on every invocation regardless of what happens to be
    cached or lockfile-pinned in the repo, so it must be surfaced as
    `npx_only` / `floating` and not hidden behind a `local_lockfile` or
    `npm_cache` classification (PR #1352 REQUEST_CHANGES #2).
    """
    for text in _read_claude_settings_text_host(repo_root):
        invocation = _find_npx_invocation_for_package(text, pkg)
        if invocation is not None:
            return "npx_only", invocation

    for lockfile_name in ("package-lock.json", "npm-shrinkwrap.json"):
        lockfile = repo_root / lockfile_name
        if lockfile.is_file():
            try:
                lock_text = lockfile.read_text(encoding="utf-8", errors="replace")
                if pkg in lock_text:
                    return "local_lockfile", "absent"
            except OSError:
                pass

    if (repo_root / "node_modules" / pkg).is_dir():
        return "project_local_install", "absent"

    # Auxiliary evidence only: `npm cache ls` inspects npm's general package
    # cache, not the npx-specific cache (`npm cache npx ls/info`), so it is
    # only consulted when no hook-based npx invocation was found above.
    try:
        result = subprocess.run(
            ["npm", "cache", "ls", pkg],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and pkg in (result.stdout or ""):
            return "npm_cache", "absent"
    except (OSError, FileNotFoundError, subprocess.TimeoutExpired):
        pass

    try:
        result = subprocess.run(
            ["npm", "list", "-g", "--depth=0", "--json", pkg],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and pkg in (result.stdout or ""):
            return "global_install", "absent"
    except (OSError, FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return "unknown", "unknown"


def _check_distribution_host(repo_root: Path | None = None) -> dict[str, Any]:
    """B4, #1261: Check Latitude distribution in host mode.

    Classifies resolution_source from repo-local evidence (Claude settings
    hook command / lockfile / node_modules / npm cache / global install).
    Cryptographic evidence (registry signature, provenance attestation,
    tarball/entrypoint/preload/hook-command digests) is not computed here
    (requires real registry network access and an isolated install tree, out
    of scope for this checker) and stays None/fail-closed, matching the
    fail-closed contract: unknown evidence must never be treated as verified.
    """
    root = repo_root if repo_root is not None else Path.cwd()
    inspection_gaps = ["registry_signature", "provenance_attestation",
                        "tarball_sha256", "installed_entrypoint_sha256",
                        "preload_sha256", "hook_command_sha256"]

    found_spec = None
    resolution_source = "unknown"
    npx_invocation = "unknown"
    for pkg in _LAT_PACKAGES:
        source, invocation = _classify_resolution_source_host(root, pkg)
        if source != "unknown":
            found_spec = pkg
            resolution_source = source
            npx_invocation = invocation
            break

    base = {
        "state": "unknown",
        "package_spec": found_spec,
        "dist_integrity": None,
        "registry_signature_verified": None,
        "provenance_verified": None,
        "provenance_source_ref": None,
        "resolved_registry_origin": None,
        "resolution_source": resolution_source,
        "npx_invocation": npx_invocation,
        "lockfile_digest": None,
        "tarball_sha256": None,
        "installed_entrypoint_sha256": None,
        "preload_sha256": None,
        "hook_command_sha256": None,
        "checked_surfaces": ["package_distribution"],
        "inspection_gaps": inspection_gaps,
    }

    reason_codes: list[str] = []
    if found_spec is None:
        reason_codes = [RC_RUNTIME_STATE_UNKNOWN, RC_RESOLUTION_SOURCE_UNKNOWN]
        base["inspection_gaps"] = ["package_distribution"] + inspection_gaps
    else:
        reason_codes = [RC_RUNTIME_STATE_UNKNOWN]
        if npx_invocation == "floating":
            reason_codes.append(RC_NPX_ONLY_UNPINNED)
            reason_codes.append(RC_NPX_INVOCATION_FLOATING)
    base["reason_codes"] = reason_codes

    return base


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
        elif _is_approved_origin(url):
            destination_state = "approved_cloud"
        else:
            destination_state = "unapproved"
    elif execution_profile == "host":
        # B1 fix: only read real env in host mode
        base_url = os.environ.get("LATITUDE_BASE_URL")
        if base_url is None:
            destination_state = "unknown"
        elif _is_approved_origin(base_url):
            destination_state = "approved_cloud"
        else:
            destination_state = "unapproved"
    else:
        destination_state = "unknown"

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
    elif execution_profile == "host":
        base_url = os.environ.get("LATITUDE_BASE_URL")
        if base_url is None:
            transport_state = "unknown"
        elif base_url.startswith("https://"):
            transport_state = "https"
        elif base_url.startswith("http://"):
            transport_state = "plaintext"
        else:
            transport_state = "unknown"
    else:
        transport_state = "unknown"

    # Diagnostic logging
    if diag_override is not None:
        diagnostic_state = diag_override.strip()
    elif debug_override is not None:
        diagnostic_state = "enabled" if debug_override.strip() == "1" else "disabled"
    elif execution_profile == "host":
        debug_val = os.environ.get("LATITUDE_DEBUG")
        diagnostic_state = "enabled" if debug_val == "1" else "disabled"
    else:
        diagnostic_state = "unknown"

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


_ARGV_CREDENTIAL_PATTERNS = [
    re.compile(r"--api[-_]?key", re.IGNORECASE),
    re.compile(r"--latitude[-_]?key", re.IGNORECASE),
]

# #1261 follow-up (PR #1352 REQUEST_CHANGES #3): only inspect processes whose
# comm/cmdline plausibly belongs to Claude Code / the Latitude telemetry
# toolchain. Never widen this to an unrelated process (or shell history).
_ARGV_RELEVANT_PROCESS_PATTERNS = [
    re.compile(r"\bclaude\b", re.IGNORECASE),
    re.compile(r"\bnode\b", re.IGNORECASE),
    re.compile(r"\bbun\b", re.IGNORECASE),
    re.compile(r"\bnpx\b", re.IGNORECASE),
    re.compile(r"\bnpm\b", re.IGNORECASE),
    re.compile(r"latitude", re.IGNORECASE),
]


def _is_argv_relevant_process(cmdline_args: list[str]) -> bool:
    joined = " ".join(cmdline_args)
    return any(pat.search(joined) for pat in _ARGV_RELEVANT_PROCESS_PATTERNS)


def _cmdline_has_credential_flag(cmdline_args: list[str]) -> bool:
    for arg in cmdline_args:
        for pat in _ARGV_CREDENTIAL_PATTERNS:
            if pat.match(arg):
                return True
    return False


def _check_argv_credential_host(proc_root: Path | None = None) -> dict[str, Any]:
    """#1261 follow-up: presence-only argv credential scan over relevant
    processes (Claude Code / node / bun / npx / npm / Latitude), never the
    checker's own argv (`/proc/self/cmdline`) and never shell history /
    terminal scrollback (PR #1352 REQUEST_CHANGES #3).

    Returns `absent_verified` only when the scan actually completed over
    every discoverable relevant process without a permission gap. Any
    enumeration failure, permission error, or inability to isolate relevant
    processes falls back to `unknown` (fail-closed) rather than
    `absent_verified`.
    """
    actual_proc_root = proc_root if proc_root is not None else Path("/proc")
    try:
        pid_dirs = [p for p in actual_proc_root.iterdir() if p.name.isdigit()]
    except OSError:
        return {
            "state": "unknown",
            "argv_exposure_state": "unknown",
            "reason_codes": [RC_ARGV_EXPOSURE_STATE_UNKNOWN],
        }

    inspected_any_relevant = False
    inspection_gap = False
    for pid_dir in pid_dirs:
        cmdline_file = pid_dir / "cmdline"
        try:
            raw = cmdline_file.read_bytes().decode("utf-8", errors="replace")
        except PermissionError:
            # Cannot tell if this PID was relevant -> counts as a gap.
            inspection_gap = True
            continue
        except OSError:
            continue
        cmdline_args = [a for a in raw.split("\0") if a]
        if not cmdline_args:
            continue
        if not _is_argv_relevant_process(cmdline_args):
            continue
        inspected_any_relevant = True
        if _cmdline_has_credential_flag(cmdline_args):
            return {
                "state": "possible",
                "argv_exposure_state": "possible",
                "reason_codes": [RC_EXPOSURE_POSSIBLE],
            }

    if inspection_gap:
        return {
            "state": "unknown",
            "argv_exposure_state": "unknown",
            "reason_codes": [RC_ARGV_EXPOSURE_STATE_UNKNOWN],
        }

    # No credential flag found. Only claim `absent_verified` (a positive
    # safety claim) when the scan actually inspected at least one relevant
    # process without gaps; otherwise the absence is unverified.
    if inspected_any_relevant:
        return {"state": "absent_verified", "argv_exposure_state": "absent_verified", "reason_codes": []}
    return {
        "state": "unknown",
        "argv_exposure_state": "unknown",
        "reason_codes": [RC_ARGV_EXPOSURE_STATE_UNKNOWN],
    }


def check_argv_credential(execution_profile: str) -> dict[str, Any]:
    """AC28, #1261 AC5: Detect if credential was passed via argv.

    #1261: exposes `argv_exposure_state` as its own closed-enum field
    (absent_verified | possible | unknown), independent of the legacy
    `state` key kept for backward compatibility with existing callers.

    #1261 follow-up (PR #1352 REQUEST_CHANGES #3): host-mode inspection is
    presence-only over relevant processes (Claude Code / node / bun / npx /
    npm / Latitude), never the checker's own argv and never shell history /
    terminal scrollback. `absent_verified` is only ever returned when that
    scan actually completed; otherwise the state is `unknown` (fail-closed).
    """
    exposure_override = _get_env_override("SRRS_LAT_ARGV_EXPOSURE_STATE")
    if exposure_override is not None:
        state = exposure_override.strip()
        if state not in ARGV_EXPOSURE_STATE_ENUM:
            state = "unknown"
        rcs = [RC_EXPOSURE_POSSIBLE] if state == "possible" else (
            [RC_ARGV_EXPOSURE_STATE_UNKNOWN] if state == "unknown" else []
        )
        return {"state": state, "argv_exposure_state": state, "reason_codes": rcs}

    override = _get_env_override("SRRS_LAT_ARGV_CREDENTIAL")
    if override is not None:
        # #1261 follow-up: `present` is the only value that can positively
        # assert exposure. Anything else (including the legacy `absent`
        # value) is NOT sufficient to claim a positive `absent_verified`
        # safety claim from a single boolean override, so it maps to
        # `unknown` (fail-closed) rather than `absent_verified`
        # (PR #1352 REQUEST_CHANGES #3).
        legacy_state = override.strip()
        if legacy_state == "present":
            return {
                "state": "possible",
                "argv_exposure_state": "possible",
                "reason_codes": [RC_EXPOSURE_POSSIBLE],
            }
        return {
            "state": "unknown",
            "argv_exposure_state": "unknown",
            "reason_codes": [RC_ARGV_EXPOSURE_STATE_UNKNOWN],
        }

    # B1 fix: only read real /proc in host mode
    if execution_profile != "host":
        return {
            "state": "unknown",
            "argv_exposure_state": "unknown",
            "reason_codes": [RC_RUNTIME_STATE_UNKNOWN],
        }

    return _check_argv_credential_host()


def check_containment_and_exposure(execution_profile: str) -> dict[str, Any]:
    """AC27: Check containment_state and exposure_state."""
    containment_override = _get_env_override("SRRS_LAT_CONTAINMENT_STATE")
    exposure_override = _get_env_override("SRRS_LAT_EXPOSURE_STATE")

    containment_state = containment_override.strip() if containment_override else "unknown"
    exposure_state = exposure_override.strip() if exposure_override else "unknown"

    reason_codes: list[str] = []
    if exposure_state in ("possible", "confirmed"):
        reason_codes.append(RC_EXPOSURE_POSSIBLE)

    return {
        "containment_state": containment_state,
        "exposure_state": exposure_state,
        "reason_codes": reason_codes,
    }


def check_uninstall_state(
    repo_root: Path,
    execution_profile: str,
    home_root: Path | None = None,
) -> dict[str, Any]:
    """AC8: Detect uninstall state via quiescent verification."""
    override = _get_env_override("SRRS_LAT_UNINSTALL_STATE")
    if override is not None:
        state = override.strip()
        rcs = [RC_UNINSTALL_INCOMPLETE] if state == "incomplete" else (
            [RC_RUNTIME_STATE_UNKNOWN] if state == "unknown" else []
        )
        return {"state": state, "reason_codes": rcs}

    # B1 fix: fixture mode cannot run real uninstall checks
    if execution_profile != "host":
        return {"state": "not_attempted", "reason_codes": []}

    # B8 fix: quiescent verification via snapshot diff
    # First snapshot: take state snapshot
    first_snapshot = _take_uninstall_snapshot(home_root)
    # Second snapshot: after a brief filesystem re-check (no sleep needed for static check)
    second_snapshot = _take_uninstall_snapshot(home_root)

    # Compare snapshots for stability
    if first_snapshot != second_snapshot:
        return {
            "state": "unknown",
            "reason_codes": [RC_UNINSTALL_INCOMPLETE],
            "snapshot_diff": True,
        }

    # Check if any Latitude artifacts remain in the snapshot
    remaining = first_snapshot.get("present_paths", [])
    if remaining:
        return {
            "state": "incomplete",
            "reason_codes": [RC_UNINSTALL_INCOMPLETE],
            "remaining_artifacts": remaining,
        }

    return {"state": "not_attempted", "reason_codes": []}


def _take_uninstall_snapshot(home_root: Path | None) -> dict[str, Any]:
    """B8: Take a point-in-time snapshot of Latitude artifact presence."""
    home = home_root if home_root is not None else Path.home()
    present_paths: list[str] = []

    all_paths = (
        [home / p for p in _LATITUDE_STATE_PATHS_LEGACY]
        + [home / p for p in _LATITUDE_SPOOL_PATHS]
        + [home / p for p in _LATITUDE_STATE_PATHS_UPSTREAM]
    )

    for path in all_paths:
        try:
            if path.exists():
                present_paths.append(path.name)
        except OSError:
            present_paths.append(f"error:{path.name}")

    return {"present_paths": sorted(present_paths)}


def check_remote_trace(execution_profile: str) -> dict[str, Any]:
    """AC16: Check remote trace state (requires human attestation)."""
    override = _get_env_override("SRRS_LAT_REMOTE_TRACE")
    if override is not None:
        state = override.strip()
        rcs = [RC_REMOTE_TRACE_UNKNOWN] if state == "unknown" else []
        return {"state": state, "reason_codes": rcs}
    # Default: unknown (requires human attestation)
    return {"state": "unknown", "reason_codes": [RC_REMOTE_TRACE_UNKNOWN]}


def check_remote_cleanup_state(execution_profile: str) -> dict[str, Any]:
    """#1261 AC7: provider-side retention/delete verification.

    Independent of `remote_trace_state` (which answers "is there a trace"):
    `remote_cleanup_state` answers "has provider-side retention/delete been
    machine-verified". `human_attested` is NOT a substitute for
    `machine_verified` (docs/dev/secret-policy.md). No provider API
    integration exists yet, so the unoverridden default is `unknown`
    (fail-closed) and carries no reason_code by itself — the strict
    real-pilot preflight predicate is what enforces the block for
    Cloud pilot close readiness.
    """
    del execution_profile  # no execution-profile-specific real check exists yet
    override = _get_env_override("SRRS_LAT_REMOTE_CLEANUP_STATE")
    if override is not None:
        state = override.strip()
        if state not in REMOTE_CLEANUP_STATE_ENUM:
            state = "unknown"
        rcs = [] if state == "machine_verified" else [RC_REMOTE_CLEANUP_NOT_MACHINE_VERIFIED]
        return {"state": state, "reason_codes": rcs}
    return {"state": "unknown", "reason_codes": []}


def check_secrets_mode_policy(
    repo_root: Path,
    credential_state: str,
) -> dict[str, Any]:
    """B6: Check SRRS_SECRETS_MODE policy consistency with runtime credential state.

    SRRS_SECRETS_MODE unset -> secrets_mode: unknown -> check policy_mode_mismatch.
    """
    raw = os.environ.get("SRRS_SECRETS_MODE")
    if raw is None:
        secrets_mode = "unknown"
    else:
        secrets_mode = raw.strip()

    reason_codes: list[str] = []

    # B6 fix: if credential present but secrets_mode is 'none' or 'unknown', flag mismatch
    if credential_state == "present":
        if secrets_mode in ("none", "unknown"):
            reason_codes.append(RC_POLICY_MODE_MISMATCH)

    return {
        "secrets_mode": secrets_mode,
        "reason_codes": reason_codes,
    }


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
    inspection_gaps: list[str],
    argv_exposure_state: str = "unknown",
    remote_cleanup_state: str = "unknown",
) -> tuple[str, bool]:
    """Compute latitude component verdict per Issue contract Verdict Rules.

    Returns (verdict, inspection_complete).
    B7 fix: verdict and inspection_complete are computed independently.
    Gap presence makes inspection_complete=False regardless of verdict.

    #1261 follow-up (PR #1352 REQUEST_CHANGES #5): argv_exposure_state and
    remote_cleanup_state are independent top-level #1261 fields, kept in
    sync with the `unknown` fail-closed detection here so
    `components.latitude.verdict` cannot read `safe` while either field (or
    the distribution summary state) is unresolved.
    """
    # B7 fix: inspection_complete is independent of verdict
    inspection_complete = len(inspection_gaps) == 0

    # not_applicable: no indicators, no unknowns, no reason codes
    lat_indicators = [credential_state, hook_state, preload_state]
    any_present = any(s in ("present", "active") for s in lat_indicators)
    any_rc = bool(all_reason_codes)
    any_unknown = any(
        s == "unknown"
        for s in [
            credential_state, hook_state, preload_state,
            active_process_state, local_storage_state,
            uninstall_state, remote_trace_state,
            containment_state, exposure_state,
            argv_exposure_state,
        ]
    ) or remote_cleanup_state == "unknown" or dist_state == "unknown"

    if not any_present and not any_unknown and not any_rc:
        return "not_applicable", inspection_complete

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
        or RC_POLICY_MODE_MISMATCH in all_reason_codes
    ):
        # B7 fix: blocked verdict does NOT force inspection_complete=True
        return "blocked", inspection_complete

    # AC27: containment_state != never_observed -> not safe
    if containment_state not in ("never_observed", "not_applicable", "unknown"):
        return "blocked", inspection_complete

    # fail_closed: unknown states
    if any_unknown or active_process_state == "unknown" or transport_state == "unknown":
        return "fail_closed", inspection_complete

    # AC5: export disabled capture active -> blocked (handled via reason codes)
    if RC_EXPORT_DISABLED_CAPTURE_ACTIVE in all_reason_codes:
        return "blocked", inspection_complete

    return "safe", inspection_complete


def check_latitude_component(
    repo_root: Path,
    execution_profile: str,
    home_root: Path | None = None,
) -> dict[str, Any]:
    """
    Run all Latitude telemetry safety checks.
    Returns a dict matching the `latitude` field of session_recording_runtime_safety/v2.

    Args:
        repo_root: Repository root path.
        execution_profile: 'host' or 'fixture'.
        home_root: In fixture mode, the home directory root to use instead of Path.home().
                   Must be provided for fixture mode if real home inspection is not desired.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # AC19: reject SRRS_LAT_* overrides in host mode
    if execution_profile == "host":
        rejection = check_execution_profile_override_rejection(execution_profile)
        if rejection is not None:
            return {
                "applicability": "applicable",
                "verdict": "fail_closed",
                "inspection_complete": False,
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
                # #1261 follow-up (PR #1352 REQUEST_CHANGES P1): the
                # override-rejection fail_closed path must expose the same
                # #1261 top-level fields (with null/unknown values) as the
                # normal path, so consumers never see a shape drift between
                # the two fail_closed producers.
                "argv_exposure_state": "unknown",
                "remote_cleanup_state": "unknown",
                "distribution": {
                    "state": "unknown",
                    "package_spec": None,
                    "dist_integrity": None,
                    "registry_signature_verified": None,
                    "provenance_verified": None,
                    "provenance_source_ref": None,
                    "resolved_registry_origin": None,
                    "resolution_source": "unknown",
                    "npx_invocation": "unknown",
                    "lockfile_digest": None,
                    "tarball_sha256": None,
                    "installed_entrypoint_sha256": None,
                    "preload_sha256": None,
                    "hook_command_sha256": None,
                },
                "reason_codes": [RC_SRRS_OVERRIDE_REJECTED],
                "checked_surfaces": [],
                "inspection_gaps": ["all_surfaces"],
                "raw_values_emitted": False,
                "checked_at": now_iso,
            }

    cred = check_credential_state(repo_root, execution_profile, home_root)
    hook = check_hook_state(repo_root, execution_profile, home_root)
    preload = check_preload_state(repo_root, execution_profile, home_root)
    proc = check_active_process_preload(execution_profile)
    export = check_export_state(repo_root, execution_profile, home_root)
    local_storage = check_local_storage(repo_root, execution_profile, home_root)
    backup = check_settings_backup(repo_root, execution_profile, home_root)
    dist = check_distribution(repo_root, execution_profile)
    dest_transport = check_destination_transport(execution_profile)
    argv = check_argv_credential(execution_profile)
    containment_exposure = check_containment_and_exposure(execution_profile)
    uninstall = check_uninstall_state(repo_root, execution_profile, home_root)
    remote_trace = check_remote_trace(execution_profile)
    remote_cleanup = check_remote_cleanup_state(execution_profile)

    # B6: policy mode check
    policy_check = check_secrets_mode_policy(repo_root, cred["state"])

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
        elif proc["state"] == "preload_present":
            # B3 fix: active process preload -> capture active even if config removed
            capture_state = "active"
        else:
            capture_state = "inactive"

    # Collect all reason codes
    all_rcs: list[str] = []
    for sub in [cred, hook, preload, proc, local_storage, backup, argv,
                containment_exposure, uninstall, remote_trace, remote_cleanup,
                policy_check]:
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

    # B7 fix: collect all inspection gaps from all sub-checks
    all_gaps: list[str] = []
    for sub in [hook, preload, proc, dist]:
        all_gaps.extend(sub.get("inspection_gaps", []))

    all_rcs = list(dict.fromkeys(all_rcs))
    all_surfaces = list(dict.fromkeys(all_surfaces))
    all_gaps = list(dict.fromkeys(all_gaps))

    argv_exposure_state_value = argv.get("argv_exposure_state", argv.get("state", "unknown"))
    remote_cleanup_state_value = remote_cleanup["state"]

    verdict, inspection_complete = _compute_latitude_verdict(
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
        inspection_gaps=all_gaps,
        argv_exposure_state=argv_exposure_state_value,
        remote_cleanup_state=remote_cleanup_state_value,
    )

    return {
        "applicability": "applicable",
        "verdict": verdict,
        "inspection_complete": inspection_complete,
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
        # #1261: argv_exposure_state / remote_cleanup_state are independent,
        # closed-enum top-level fields consumed by the strict real-pilot
        # preflight predicate (direct field assertion, not summary-only).
        "argv_exposure_state": argv.get("argv_exposure_state", argv.get("state", "unknown")),
        "remote_cleanup_state": remote_cleanup["state"],
        "distribution": {
            "state": dist.get("state"),
            "package_spec": dist.get("package_spec"),
            "dist_integrity": dist.get("dist_integrity"),
            "registry_signature_verified": dist.get("registry_signature_verified"),
            "provenance_verified": dist.get("provenance_verified"),
            "provenance_source_ref": dist.get("provenance_source_ref"),
            "resolved_registry_origin": dist.get("resolved_registry_origin"),
            "resolution_source": dist.get("resolution_source", "unknown"),
            "npx_invocation": dist.get("npx_invocation", "unknown"),
            "lockfile_digest": dist.get("lockfile_digest"),
            "tarball_sha256": dist.get("tarball_sha256"),
            "installed_entrypoint_sha256": dist.get("installed_entrypoint_sha256"),
            "preload_sha256": dist.get("preload_sha256"),
            "hook_command_sha256": dist.get("hook_command_sha256"),
        },
        "reason_codes": all_rcs,
        "checked_surfaces": all_surfaces,
        "inspection_gaps": all_gaps,
        "raw_values_emitted": False,
        "checked_at": now_iso,
    }
