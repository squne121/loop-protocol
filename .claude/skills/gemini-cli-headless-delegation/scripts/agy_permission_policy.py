#!/usr/bin/env python3
"""AGY profile-scoped isolated permission policy and no-tools negative evidence.

Issue #1705 (parent: #1265, review origin: #1494 Blocker 4).

`run_gemini_headless.py`'s `_run_agy()` previously ran `agy -p <prompt>` with
only an environment allowlist (`_minimal_agy_env()`), which still propagates
the caller's real `$HOME`. Because AGY (Antigravity CLI) resolves its own
permission/sandbox/auto-execution configuration from
`$HOME/.antigravity/settings.json`, any pre-existing *global* settings on the
host (e.g. a developer's own permissive Antigravity config) silently apply to
every profile, regardless of what `ALLOWED_TOOL_PROFILES` in
`run_gemini_headless.py` intends.

This module is the single source of truth for:

- What each `tool_profile` is allowed to do at the AGY *direct* tool-call
  layer (`PROFILE_ALLOWED_TOOLS`).
- How to materialize a fresh, isolated, workspace-scoped permission
  configuration (`materialize_isolated_agy_workspace()`) whose `HOME`/`XDG_*`
  redirection means a hostile pre-existing global settings file can never be
  consulted at all -- there is no code path back to the real `$HOME`.
- How to classify *observed* tool-call attempts (from AGY's own transcript /
  hook events, once available) into `expected_tool_calls` /
  `denied_tool_calls` / `unexpected_tool_calls`
  (`classify_tool_call_events()`), so that a single execution which happens
  not to call any tool is never mistaken for proof that the profile *denies*
  tools.
- How to record a denied attempt as a secret-safe hook event
  (`record_denied_tool_attempt()`), reusing the same credential-like /
  absolute-path redaction posture as `run_gemini_headless.py`'s
  `_redact_text()` / `_scan_redaction_violations()` (duplicated here in
  minimal form to avoid importing the wrapper's evidence-schema module, per
  Issue #1705 Stop Conditions -- `_extract_recognized_tool_calls()` and the
  `grounded_evidence` schema are explicitly out of scope for this Issue).

Design references:
- https://github.com/squne121/loop-protocol/issues/1494#issuecomment-5071397001
- `.claude/skills/gemini-cli-headless-delegation/SKILL.md`
- `.claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py`
  (`_run_agy()` / `_minimal_agy_env()` / `ALLOWED_TOOL_PROFILES`)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

SCHEMA_WORKSPACE_POLICY = "agy_workspace_permission_policy/v1"
SCHEMA_GATE_RESULT = "agy_profile_gate_result/v1"
SCHEMA_DENIED_EVENT = "agy_denied_tool_attempt/v1"
SCHEMA_GLOBAL_SETTINGS_FIXTURE = "agy_global_settings/v1"

# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

NO_TOOLS_PROFILE = "no_tools"
LOCAL_ASSET_RESEARCH_PROFILE = "local_asset_research"
GROUNDED_RESEARCH_PROFILE = "grounded_research"
PROPOSAL_ONLY_PROFILE = "proposal_only"

ALLOWED_PROFILES: frozenset[str] = frozenset(
    {
        NO_TOOLS_PROFILE,
        LOCAL_ASSET_RESEARCH_PROFILE,
        GROUNDED_RESEARCH_PROFILE,
        PROPOSAL_ONLY_PROFILE,
    }
)

# ---------------------------------------------------------------------------
# Auth surface profiles (Issue #1779)
# ---------------------------------------------------------------------------
#
# Distinct axis from `tool_profile` (NO_TOOLS_PROFILE / ... / PROPOSAL_ONLY_PROFILE
# above): `tool_profile` governs which AGY *tool calls* are allowed
# (`PROFILE_ALLOWED_TOOLS`); `auth_profile` governs which *auth-reachability
# env vars / symlinks* `materialize_isolated_agy_workspace()` grants the
# isolated `agy` subprocess a path back to the real host's credential state.
# Deliberately named to avoid the `no_tools` / `local_asset_research` /
# `grounded_research` / `proposal_only` vocabulary (Issue #1779 Notes for
# Reviewer).
#
# `AGY_AUTH_ABLATION_V1` (recorded in Issue #1779's Source section) proved
# that only `agy_oauth_token_path` is necessary and sufficient for AGY auth
# to succeed -- `DBUS_SESSION_BUS_ADDRESS` / `XDG_RUNTIME_DIR` /
# `GOOGLE_APPLICATION_CREDENTIALS` / `gcloud_adc_path` (added defensively by
# #1726 / #1730 while diagnosing #1494's `agy_auth_required` failures) are
# not required. `AGY_AUTH_PROFILE_MINIMAL` is therefore the default and
# excludes all four; `AGY_AUTH_PROFILE_EXTENDED` is an explicit opt-in for
# environments that may need them in the future (kept, not deleted, per
# Issue #1779 In Scope item 1).
AGY_AUTH_PROFILE_MINIMAL = "auth_minimal"
AGY_AUTH_PROFILE_EXTENDED = "auth_extended"

ALLOWED_AUTH_PROFILES: frozenset[str] = frozenset({AGY_AUTH_PROFILE_MINIMAL, AGY_AUTH_PROFILE_EXTENDED})

# Profiles for which materialize_isolated_agy_workspace() fail-closes
# (refuses to create a workspace at all) when the real agy OAuth token file
# exists but `bwrap` is unavailable -- i.e. when only
# `degraded_symlink_reachability` (not `kernel_enforced_ro_bind`) could be
# offered for the one credential-bearing file this module intentionally
# exposes. `grounded_research` / `proposal_only` are excluded: they already
# invoke real AGY tool calls (`grounded_research`) or have no local
# filesystem/tool-call attack surface materially widened by a readable
# (but not kernel-enforced-read-only) token symlink, so degraded-mode
# continuation is judged acceptable for them (Issue #1779 In Scope item 2).
_AUTH_READONLY_FAIL_CLOSED_PROFILES: frozenset[str] = frozenset({NO_TOOLS_PROFILE, LOCAL_ASSET_RESEARCH_PROFILE})


class AgyReadOnlyBoundaryError(RuntimeError):
    """Raised by materialize_isolated_agy_workspace() when a security-sensitive
    profile (`no_tools` / `local_asset_research`) would only be able to offer
    `degraded_symlink_reachability` (not `kernel_enforced_ro_bind`) for the
    real agy OAuth token file, because `bwrap` is unavailable on this host.

    Fail-closed by design (Issue #1779 AC7): no workspace is materialized in
    this case, rather than silently downgrading a security-sensitive
    profile's read-only guarantee.
    """


class AgyPermissionSettingsError(RuntimeError):
    """The primary official permission settings could not be materialized.

    A restrictive profile must never launch AGY after this error: absent
    official deny settings can restore AGY's workspace-default permissions.
    """


# Legacy expectation taxonomy of the AGY *direct* tool surface.  It remains
# for older hermetic wrapper tests only; AGY does not consume it as a runtime
# permission source.
# shell/command execution, filesystem, MCP, GitHub, browser, and web tools.
# This is used to populate the explicit `deny` list in the generated policy
# document (informational / auditable) -- the actual enforcement is via
# allowlist membership (`resolve_tool_permission()`), so an AGY tool not
# present in this taxonomy is still denied by default for every profile
# whose allowlist does not name it.
AGY_DIRECT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "shell",
        "run_command",
        "execute_code",
        "read_file",
        "write_file",
        "edit_file",
        "list_directory",
        "delete_file",
        "mcp_call",
        "mcp_list_tools",
        "github_api",
        "gh_command",
        "browser_navigate",
        "browser_click",
        "search_web",
        "read_url_content",
    }
)

# Official AGY permission resources are deliberately distinct from the native
# hook tool names above.  The runtime settings format consumes
# ``action(target)`` rules (for example ``command(*)``), whereas a PreToolUse
# event reports a native dispatcher name such as ``run_command``.  Keeping
# the two vocabularies separate prevents a native tool name from being
# accidentally serialized as an official permission rule.
CANONICAL_PERMISSION_RESOURCES: frozenset[str] = frozenset(
    {
        "command",
        "read_file",
        "write_file",
        "read_url",
        "execute_url",
        "unsandboxed",
        "mcp",
    }
)

PROFILE_ALLOWED_PERMISSION_RESOURCES: dict[str, frozenset[str]] = {
    NO_TOOLS_PROFILE: frozenset(),
    LOCAL_ASSET_RESEARCH_PROFILE: frozenset(),
    GROUNDED_RESEARCH_PROFILE: frozenset({"read_url"}),
    PROPOSAL_ONLY_PROFILE: frozenset(),
}

GROUNDED_RESEARCH_ALLOWLIST: frozenset[str] = frozenset({"search_web", "read_url_content"})

PROFILE_ALLOWED_TOOLS: dict[str, frozenset[str]] = {
    NO_TOOLS_PROFILE: frozenset(),
    LOCAL_ASSET_RESEARCH_PROFILE: frozenset(),
    GROUNDED_RESEARCH_PROFILE: GROUNDED_RESEARCH_ALLOWLIST,
    PROPOSAL_ONLY_PROFILE: frozenset(),
}

# Fixed policy invariants (Issue #1705 AC11): AGY never receives direct MCP
# tool access under any profile in this Issue's scope. All retrieval that
# feeds AGY's analysis is funneled through the wrapper-side Serena MCP
# client (`_call_serena_mcp_live()` family in run_gemini_headless.py), which
# runs as a separate process the wrapper itself controls -- not an AGY
# direct tool call.
RETRIEVAL_ACTOR_WRAPPER_SERENA_MCP = "wrapper_serena_mcp"
ANALYSIS_ACTOR_ANTIGRAVITY_CLI = "antigravity_cli"
AGY_DIRECT_MCP_ACCESS = False

AGY_DIRECT_SOURCE = "agy_direct"
WRAPPER_SERENA_SOURCE = "wrapper_serena_mcp"


def validate_profile(profile: str) -> None:
    if profile not in ALLOWED_PROFILES:
        raise ValueError(f"unknown AGY tool_profile: {profile!r}; expected one of {sorted(ALLOWED_PROFILES)}")


def profile_allowed_tools(profile: str) -> frozenset[str]:
    """Return the exact set of AGY direct tool names *profile* may call."""
    validate_profile(profile)
    return PROFILE_ALLOWED_TOOLS[profile]


# ---------------------------------------------------------------------------
# Policy document generation
# ---------------------------------------------------------------------------


def build_workspace_permission_policy(profile: str) -> dict[str, Any]:
    """Build a legacy, wrapper-side expectation document for *profile*.

    This is not an official AGY settings file and does not enforce an AGY
    runtime decision.  Issue #1814 uses the isolated HOME's official
    settings and an independent PreToolUse hook for that purpose.
    """
    validate_profile(profile)
    allow = sorted(PROFILE_ALLOWED_TOOLS[profile])
    deny = sorted(AGY_DIRECT_TOOL_NAMES - PROFILE_ALLOWED_TOOLS[profile])
    return {
        "schema": SCHEMA_WORKSPACE_POLICY,
        "profile": profile,
        "permissions": {
            "default": "deny",
            "allow": allow,
            "deny": deny,
        },
        "hooks": {
            "PreToolCall": [
                {
                    "matcher": "*",
                    "action": "workspace_deny_gate",
                    # Workspace-scoped settings always win over any
                    # pre-existing global $HOME/.antigravity/settings.json
                    # allow rules (Issue #1705 AC5/AC6 config precedence).
                    "precedence": "workspace_overrides_global",
                }
            ]
        },
    }


def hostile_global_settings_fixture() -> dict[str, Any]:
    """Return a hostile global settings fixture that allows every AGY tool.

    Used by adversarial tests (Issue #1705 AC5/AC6) to prove that
    `resolve_tool_permission()` / the isolated workspace never consult this
    document to widen a profile's allowlist.
    """
    return {
        "schema": SCHEMA_GLOBAL_SETTINGS_FIXTURE,
        "source": "hostile_fixture",
        "permissions": {
            "default": "allow",
            "allow": sorted(AGY_DIRECT_TOOL_NAMES),
            "deny": [],
        },
    }


def resolve_tool_permission(
    profile: str,
    tool_name: str,
    global_settings: Mapping[str, Any] | None = None,
) -> str:
    """Return `"allow"` or `"deny"` for *tool_name* under *profile*.

    `global_settings` is accepted only to make the config-precedence
    guarantee explicit and testable: it is intentionally **never** consulted
    to widen this legacy expectation model.  It is not an official AGY
    permission decision.
    """
    validate_profile(profile)
    del global_settings  # intentionally unused: workspace policy is authoritative
    allowed = PROFILE_ALLOWED_TOOLS[profile]
    return "allow" if tool_name in allowed else "deny"


def _permission_action(resource: str, target: str = "*") -> str:
    """Return the official settings spelling for one permission resource."""
    if resource not in CANONICAL_PERMISSION_RESOURCES:
        raise ValueError(f"unknown official AGY permission resource: {resource!r}")
    return f"{resource}({target})"


def build_official_agy_settings(profile: str) -> dict[str, Any]:
    """Build the isolated HOME's official AGY settings document.

    The CLI consumes this document from
    ``~/.gemini/antigravity-cli/settings.json``.  ``permissions.deny`` is a
    list of official ``action(target)`` rules and is the primary expectation
    model for restrictive profiles.  ``toolPermission`` only suppresses an
    interactive confirmation prompt; it is never treated as a deny boundary.
    """
    validate_profile(profile)
    allowed = PROFILE_ALLOWED_PERMISSION_RESOURCES[profile]
    denied = CANONICAL_PERMISSION_RESOURCES - allowed
    return {
        "toolPermission": AGY_TOOL_PERMISSION_ALWAYS_PROCEED,
        "permissions": {
            "deny": [_permission_action(resource) for resource in sorted(denied)],
            "ask": [],
            "allow": [_permission_action(resource) for resource in sorted(allowed)],
        },
    }


def resolve_official_permission_action(settings: Mapping[str, Any], resource: str, target: str = "*") -> str:
    """Evaluate the settings expectation model with explicit deny precedence.

    This helper is intentionally an expectation/test model, not a substitute
    for the real AGY runtime.  It makes hostile ``ask``/``allow`` fixtures
    deterministic: a matching official deny is always returned as ``deny``.
    Unknown or malformed values also fail closed to ``deny``.
    """
    try:
        action = _permission_action(resource, target)
    except ValueError:
        return "deny"
    permissions = settings.get("permissions")
    if not isinstance(permissions, Mapping):
        return "deny"
    for decision in ("deny", "ask", "allow"):
        rules = permissions.get(decision)
        if not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules):
            continue
        if action in rules or _permission_action(resource) in rules:
            return decision
    return "deny"


# ---------------------------------------------------------------------------
# Isolated workspace materialization
# ---------------------------------------------------------------------------

# Issue #1779 AC8: no-op placeholder. Issue #1705 originally generated this
# file assuming AGY would execute a `PreToolCall`-style hook against it (the
# same `hooks.PreToolCall` shape `build_workspace_permission_policy()`
# writes into `settings.json`). Re-investigation for #1779 (re-reviewing
# https://antigravity.google/docs/cli/reference and
# https://antigravity.google/docs/cli/using, the same sources #1758 used to
# confirm the *separate* `toolPermission` setting) found no documented AGY
# hook schema that would ever invoke this file as executable code -- AGY has
# no confirmed `PreToolCall`/`PreToolUse`-style hook mechanism at all, only
# the static `permissions.allow`/`permissions.deny` list in `settings.json`
# (already enforced independently, see below) and the unrelated
# `toolPermission` confirmation-prompt setting (#1758). This file is
# therefore an inert **no-op placeholder**: it is written for forward
# compatibility (if AGY later documents a working hook schema, a stable path
# already exists to populate) but nothing in AGY or this module ever
# executes it. The only actually-effective tool-call deny mechanism is the
# static allowlist -- `PROFILE_ALLOWED_TOOLS` via `resolve_tool_permission()`
# / `build_workspace_permission_policy()`'s `permissions.allow`/`.deny` lists
# -- which this file's docstring-only content does not alter and does not
# need to.
_WORKSPACE_DENY_GATE_HOOK_SOURCE = '''"""Workspace-scoped PreToolCall deny gate -- no-op placeholder (Issue #1779).

Generated by agy_permission_policy.py (Issue #1705; re-confirmed no-op by
Issue #1779). No documented AGY `PreToolCall`/`PreToolUse`-style hook schema
was found that would ever execute this file -- it is written for forward
compatibility only. The actually-effective tool-call deny mechanism is the
static `permissions.allow`/`permissions.deny` allowlist in this workspace's
own settings.json (`PROFILE_ALLOWED_TOOLS` / `resolve_tool_permission()`),
not this file.
"""
'''

# Basenames that must never appear inside a materialized isolated workspace.
# materialize_isolated_agy_workspace() only ever creates settings.json, the
# hook script, and empty XDG_* directories -- it never copies files from the
# caller's real $HOME (Issue #1705 AC12).
CREDENTIAL_FILE_BASENAMES: frozenset[str] = frozenset(
    {
        "credentials.json",
        "credentials",
        "token.json",
        "oauth_token.json",
        ".netrc",
        "id_rsa",
        "id_ed25519",
        ".git-credentials",
    }
)

# ---------------------------------------------------------------------------
# agy OAuth token read-only enforcement mode (Issue #1779)
# ---------------------------------------------------------------------------
#
# `AGY_READONLY_BOUNDARY_V1` (Issue #1779 Source section) proved that a bare
# `Path.symlink_to()` exposure (the pre-#1779 `_expose_agy_oauth_token_read_only()`
# behavior, unchanged by this Issue -- see below) is NOT kernel-enforced
# read-only: a process can open the symlink and write through it to mutate
# the real host token file. These three values name what
# `materialize_isolated_agy_workspace()` actually delivers for a given call,
# replacing the prior unqualified "read_only" claim with an explicit,
# truthful mode:
AGY_OAUTH_TOKEN_READONLY_KERNEL_ENFORCED = "kernel_enforced_ro_bind"
AGY_OAUTH_TOKEN_READONLY_DEGRADED = "degraded_symlink_reachability"
AGY_OAUTH_TOKEN_READONLY_ABSENT = "absent"


@dataclass(frozen=True)
class IsolatedAgyWorkspace:
    profile: str
    workspace_dir: Path
    settings_path: Path
    hook_path: Path
    env: dict[str, str]
    # Issue #1730: path to the read-only-exposed gcloud ADC config dir under
    # this workspace's isolated XDG_CONFIG_HOME (`<workspace>/xdg-config/gcloud`),
    # or None when the real `$HOME/.config/gcloud` did not exist. Never points
    # outside the isolated workspace tree even though its *target* (via
    # symlink) is the real `$HOME/.config/gcloud` directory.
    gcloud_adc_path: "Path | None" = None
    # Issue #1740/#1743: path to the read-only-exposed agy OAuth token file
    # under this workspace's isolated HOME
    # (`<workspace>/.gemini/antigravity-cli/antigravity-oauth-token`), or
    # None when the real
    # `$HOME/.gemini/antigravity-cli/antigravity-oauth-token` did not exist.
    # Never points outside the isolated workspace tree even though its
    # *target* (via symlink) is the real token file. This is the actual auth
    # channel `agy` uses -- neither dbus secret-service (#1726) nor gcloud
    # ADC (#1730) resolved `agy_auth_required`; see Issue #1740 Source
    # section for the confirmed diagnosis. Issue #1743: the symlink was
    # originally (incorrectly) placed under `XDG_CONFIG_HOME` -- `agy` reads
    # this file from `$HOME/.gemini/antigravity-cli/`, not from
    # `$XDG_CONFIG_HOME`, so the isolated HOME is the required placement.
    agy_oauth_token_path: "Path | None" = None
    # Issue #1758: path to the explicitly-generated real AGY settings.json
    # (`<workspace>/.gemini/antigravity-cli/settings.json`) that sets
    # `toolPermission: "always-proceed"`. Always non-None -- unlike the
    # gcloud ADC / OAuth token exposures above (which are conditional on a
    # real host file existing), this file is generated unconditionally
    # because it never reads or reuses any real-host settings value; see
    # `_write_agy_tool_permission_settings()` docstring for the live
    # evidence this addresses.
    agy_tool_permission_settings_path: "Path | None" = None
    # Issue #1779: whether the exposure above is actually kernel-enforced
    # read-only (`AGY_OAUTH_TOKEN_READONLY_KERNEL_ENFORCED`, only when
    # `bwrap` is available), merely reachable via a writable symlink with no
    # OS-level enforcement (`AGY_OAUTH_TOKEN_READONLY_DEGRADED`), or the real
    # token file did not exist at all (`AGY_OAUTH_TOKEN_READONLY_ABSENT`).
    # Replaces the prior unqualified "read only" naming that
    # `AGY_READONLY_BOUNDARY_V1` proved was not actually enforced.
    agy_oauth_token_readonly_mode: str = AGY_OAUTH_TOKEN_READONLY_ABSENT
    # Issue #1779: `bwrap` argv prefix that, when prepended to the actual
    # `agy` subprocess command, kernel-enforces read-only access to
    # `agy_oauth_token_path` (and the `agy_tool_permission_settings_path`
    # sitting alongside it). Non-None only when
    # `agy_oauth_token_readonly_mode == AGY_OAUTH_TOKEN_READONLY_KERNEL_ENFORCED`.
    # Consumed by `run_gemini_headless.py::_run_agy()` at the single
    # `subprocess.run(command, ...)` call site that actually launches `agy`
    # (Issue #1779 Allowed Paths); this module never invokes it itself.
    agy_oauth_token_bwrap_prefix: "list[str] | None" = None


# Issue #1730: gcloud Application Default Credentials (ADC) are cached
# file-based under `$HOME/.config/gcloud` (`application_default_credentials.json`,
# `access_tokens.db`) -- not via a D-Bus secret-service session. This is the
# XDG_CONFIG_HOME-relative directory name `materialize_isolated_agy_workspace()`
# exposes the real gcloud config dir under, matching gcloud's own
# `$XDG_CONFIG_HOME/gcloud` lookup convention.
GCLOUD_CONFIG_DIRNAME = "gcloud"

# Issue #1730 AC2: when the real environment already has this env var set,
# its *path string* (never file content) is propagated through unchanged --
# a path string is not credential material in itself (same reasoning as the
# DBUS_SESSION_BUS_ADDRESS / XDG_RUNTIME_DIR endpoint pointers in Issue #1726).
GOOGLE_APPLICATION_CREDENTIALS_ENV = "GOOGLE_APPLICATION_CREDENTIALS"


def _real_home_gcloud_config_dir() -> "Path | None":
    """Return the real `$HOME/.config/gcloud` directory if it exists.

    Existence-check only (`Path.is_dir()`) -- this function never opens or
    reads any file inside the directory it returns (Issue #1730 AC1/AC3/AC5:
    presence and path operations only, no content access).
    """
    real_home = os.environ.get("HOME")
    if not real_home:
        return None
    candidate = Path(real_home) / ".config" / "gcloud"
    try:
        if candidate.is_dir():
            return candidate
    except OSError:
        return None
    return None


def _expose_gcloud_adc_read_only(xdg_config_home: Path) -> "Path | None":
    """Expose the real gcloud ADC config dir under *xdg_config_home*/gcloud.

    Issue #1730: because `materialize_isolated_agy_workspace()` fully
    redirects `HOME`/`XDG_*` into a brand-new isolated tmp workspace, the
    host's real gcloud ADC cache is otherwise structurally unreachable by the
    isolated `agy` subprocess -- causing `agy_auth_required` failures even
    when the host already has a valid, existing gcloud ADC session.

    This creates a *symlink* (never a copy) from
    `<isolated>/xdg-config/gcloud` to the real `$HOME/.config/gcloud`
    directory. `Path.symlink_to()` only writes a path string into a new
    filesystem entry; it never opens or reads a single byte of the target's
    file contents (AC1/AC5). Only this one subpath of the real `$HOME` is
    exposed this way -- the isolated `HOME` itself and every other real
    `$HOME` subdirectory (`.ssh`, `.netrc`, other `.config/*` apps, etc.)
    remain fully isolated and unreachable (AC3).

    Returns `None` (a no-op) when the real `$HOME/.config/gcloud` directory
    does not exist, or if symlink creation fails for any reason -- gcloud ADC
    exposure is an additive reachability improvement, never a hard
    requirement for workspace materialization to succeed.
    """
    gcloud_dir = _real_home_gcloud_config_dir()
    if gcloud_dir is None:
        return None
    link_path = xdg_config_home / GCLOUD_CONFIG_DIRNAME
    try:
        link_path.symlink_to(gcloud_dir, target_is_directory=True)
    except OSError:
        return None
    return link_path


# Issue #1740: `agy` (Antigravity CLI) does not authenticate via dbus
# secret-service (#1726) or gcloud ADC (#1730) -- diagnosis during #1494's
# third live fan-out attempt confirmed it uses its own OAuth token file,
# `$HOME/.gemini/antigravity-cli/antigravity-oauth-token` (mode 600).
# Exposing *only* this file read-only inside the isolated workspace was
# confirmed sufficient for `agy -p "..."` to exit 0 (see Issue #1740 Source
# section). `$HOME/.gemini/antigravity-cli/` also contains other mode-600
# files (`jetski_state.pbtxt`, `history.jsonl`, `settings.json`) that were
# investigated but are not exposed here: the OAuth token file alone was
# already confirmed sufficient for auth reachability, and exposing only the
# minimal necessary subpath keeps the #1705 secret-hygiene design intact.
ANTIGRAVITY_CLI_DIRNAME = "antigravity-cli"
AGY_OAUTH_TOKEN_FILENAME = "antigravity-oauth-token"


def _real_home_agy_oauth_token_file() -> "Path | None":
    """Return the real `$HOME/.gemini/antigravity-cli/antigravity-oauth-token`
    file if it exists.

    Existence-check only (`Path.is_file()`) -- this function never opens or
    reads the file's content (Issue #1740 AC3: presence and path operations
    only, no content access).
    """
    real_home = os.environ.get("HOME")
    if not real_home:
        return None
    candidate = Path(real_home) / ".gemini" / ANTIGRAVITY_CLI_DIRNAME / AGY_OAUTH_TOKEN_FILENAME
    try:
        if candidate.is_file():
            return candidate
    except OSError:
        return None
    return None


def _expose_agy_oauth_token_read_only(isolated_home: Path) -> "Path | None":
    """Expose the real agy OAuth token file under
    *isolated_home*/.gemini/antigravity-cli/antigravity-oauth-token.

    Issue #1740: because `materialize_isolated_agy_workspace()` fully
    redirects `HOME`/`XDG_*` into a brand-new isolated tmp workspace, the
    host's real agy OAuth token file is otherwise structurally unreachable by
    the isolated `agy` subprocess -- causing `agy_auth_required` failures
    even when the host already has a valid, existing agy auth session (the
    same failure class #1726's dbus reachability and #1730's gcloud ADC
    reachability additions did not resolve).

    Issue #1743: `agy` reads this token file from
    `$HOME/.gemini/antigravity-cli/antigravity-oauth-token` -- the same
    state-directory layout its own auth flow writes to on a non-isolated
    host -- not from `$XDG_CONFIG_HOME`. #1740's original implementation
    placed the symlink under `xdg_config_home` (`<isolated>/xdg-config/...`),
    which `agy` never looks at, so `agy -p` inside the isolated workspace
    still failed with `agy_auth_required` even though the symlink itself was
    created successfully. Live diagnosis during #1494's fourth fan-out
    attempt confirmed that placing the symlink under the isolated `HOME`
    (this function's *isolated_home* argument, i.e. `workspace.env["HOME"]`
    / `workspace_dir`) instead of `XDG_CONFIG_HOME` allows `agy -p` to
    succeed -- see the parent Issue #1743 Source section for the confirmed
    diagnosis.

    This creates a *symlink* (never a copy) from
    `<isolated home>/.gemini/antigravity-cli/antigravity-oauth-token` to the
    real `$HOME/.gemini/antigravity-cli/antigravity-oauth-token` file.
    `Path.symlink_to()` only writes a path string into a new filesystem
    entry; it never opens or reads a single byte of the target's file
    contents (AC1/AC3). Only this one file of the real `$HOME` is exposed
    this way -- the isolated `HOME` itself and every other real `$HOME`
    subdirectory (`.ssh`, `.netrc`, other `.gemini/*` state, etc.) remain
    fully isolated and unreachable (AC4).

    Returns `None` (a no-op) when the real
    `$HOME/.gemini/antigravity-cli/antigravity-oauth-token` file does not
    exist, or if symlink creation fails for any reason -- agy OAuth token
    exposure is an additive reachability improvement, never a hard
    requirement for workspace materialization to succeed (AC2).
    """
    token_file = _real_home_agy_oauth_token_file()
    if token_file is None:
        return None
    link_dir = isolated_home / ".gemini" / ANTIGRAVITY_CLI_DIRNAME
    try:
        link_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    link_path = link_dir / AGY_OAUTH_TOKEN_FILENAME
    try:
        link_path.symlink_to(token_file)
    except OSError:
        return None
    return link_path


# ---------------------------------------------------------------------------
# bwrap-based kernel-enforced read-only (Issue #1779)
# ---------------------------------------------------------------------------


def _bwrap_available() -> bool:
    """Return True if the `bwrap` (bubblewrap) binary is on `PATH`.

    Issue #1779 AC4/AC6: `_expose_agy_oauth_token_read_only()` above only
    ever creates a plain symlink -- reachable, but not kernel-enforced
    read-only (`AGY_READONLY_BOUNDARY_V1` proved a process can write through
    it). When `bwrap` is available, `materialize_isolated_agy_workspace()`
    additionally builds a `bwrap` argv prefix (`_build_bwrap_ro_bind_prefix()`)
    that *is* kernel-enforced; when it is not, the workspace is annotated
    `AGY_OAUTH_TOKEN_READONLY_DEGRADED` instead of silently claiming
    read-only.
    """
    return shutil.which("bwrap") is not None


def _build_bwrap_ro_bind_prefix(scratch_dir: Path, ro_bind_pairs: Sequence[tuple[Path, Path]]) -> list[str]:
    """Build a `bwrap` argv prefix that kernel-enforces read-only access to
    each `dest` in *ro_bind_pairs*, prepended to the real `agy` subprocess
    argv by `run_gemini_headless.py::_run_agy()`.

    Issue #1779: `bwrap --dev-bind / /` first binds the real filesystem onto
    itself unchanged (the sandboxed subprocess sees exactly the same tree as
    without this prefix). `--tmpfs <scratch_dir>` then replaces *scratch_dir*
    (the directory containing the pre-created reachability symlink(s) from
    `_expose_agy_oauth_token_read_only()` / the agy tool-permission settings
    file) with a fresh, empty, in-namespace-only view -- required because
    `bwrap --ro-bind` refuses to bind onto a destination that already exists
    as a symlink (confirmed empirically: `bwrap: Can't create file at
    <path>: No such file or directory`), and `--tmpfs` is scoped to a single
    directory so sibling paths under `--dev-bind / /` remain unaffected.
    Each `(source, dest)` pair is then remounted read-only from the real,
    unmodified host file at `source` (resolved from the original, pre-tmpfs
    filesystem view, not the fresh `scratch_dir` overlay) onto `dest`. Any
    write attempt through `dest` inside the sandboxed subprocess fails with
    a kernel-level `EROFS` ("Read-only file system") error; reads succeed
    with the real content. This function only builds an argv list -- it
    never itself opens, reads, or mounts anything (the real `bwrap` process
    that does so is spawned by the caller's own `subprocess.run(...)`).
    """
    argv: list[str] = ["bwrap", "--dev-bind", "/", "/", "--tmpfs", str(scratch_dir)]
    for source, dest in ro_bind_pairs:
        argv.extend(["--ro-bind", str(source), str(dest)])
    argv.append("--")
    return argv


# AGY's official runtime settings live under the isolated HOME, not under the
# legacy expectation-only `.antigravity/` directory.  ``toolPermission`` is a
# confirmation policy and the official ``permissions`` object contains the
# primary restrictive rules.
#
# Issue #1758: AGY's *own* built-in confirmation preset is controlled by a
# `toolPermission` field inside the real AGY settings file
# (`~/.gemini/antigravity-cli/settings.json`, confirmed via live WebFetch of
# `https://antigravity.google/docs/cli/reference` /
# `https://antigravity.google/docs/cli/using`; see
# `references/grounded-research-isolated-workspace-investigation.md` Live
# Verification section). Its default (when the file/key is absent, which is
# always true for `materialize_isolated_agy_workspace()` prior to this fix --
# it never wrote to this path at all) is `"request-review"`: write/bash/web
# tool calls require interactive confirmation. In headless print mode
# (`agy -p`) there is nobody to answer that prompt, so the tool call is
# silently never attempted and the model instead returns a hallucinated
# "searched" answer -- reproduced live for this Issue with
# `web_tool_call_count: 0` in the isolated workspace baseline (before this
# fix). `"always-proceed"` ("never prompts") is the only enum value that
# removes this confirmation gate entirely; it is safe to use here precisely
# *because* the deny-by-default `.antigravity/settings.json` policy above
# (`build_workspace_permission_policy()`) and its `workspace_deny_gate`
# PreToolCall hook remain the actual tool allowlist authority -- this fix
# only removes AGY's own redundant confirmation gate for whatever the
# workspace policy already allows, it does not widen what may be called
# (Issue #1705 AC5/AC6 config precedence is unaffected; see
# `test_isolated_workspace_injects_tool_permission_for_grounded_research`
# and the existing `test_hostile_global_settings_do_not_override_workspace_deny`
# / `test_workspace_hook_deny_precedence_over_global_allow` regression tests).
AGY_TOOL_PERMISSION_ALWAYS_PROCEED = "always-proceed"
AGY_SETTINGS_FILENAME = "settings.json"


def _write_agy_tool_permission_settings(isolated_home: Path, profile: str) -> Path:
    """Write official restrictive settings under an isolated AGY HOME.

    Unlike `_expose_gcloud_adc_read_only()` / `_expose_agy_oauth_token_read_only()`
    above, this function never reads or reuses any value from the real host's
    `$HOME/.gemini/antigravity-cli/settings.json` -- it always generates a
    brand-new, isolated-workspace-only file with a fixed value. This follows
    the `references/grounded-research-isolated-workspace-investigation.md`
    `## Next Action` recommendation to keep true isolation (the isolated
    workspace's `toolPermission` must not depend on whatever a developer
    happens to have configured on their real host) rather than symlinking the
    real settings file the way the OAuth token / gcloud ADC exposures do.

    The operation is atomic and fail-closed: content is written to a private
    temporary file, JSON-read back, mode-checked, atomically renamed, then
    read back again.  Any failure raises ``AgyPermissionSettingsError``;
    callers must stop before starting AGY.
    """
    settings_dir = isolated_home / ".gemini" / ANTIGRAVITY_CLI_DIRNAME
    try:
        settings_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AgyPermissionSettingsError("settings_directory_create_failed") from exc
    settings_path = settings_dir / AGY_SETTINGS_FILENAME
    expected = build_official_agy_settings(profile)
    encoded = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode("utf-8")
    temporary = settings_dir / f".{AGY_SETTINGS_FILENAME}.{next(tempfile._get_candidate_names())}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        if temporary.read_bytes() != encoded or stat.S_IMODE(temporary.stat().st_mode) != 0o600:
            raise AgyPermissionSettingsError("settings_temporary_readback_failed")
        os.replace(temporary, settings_path)
        if settings_path.read_bytes() != encoded or stat.S_IMODE(settings_path.stat().st_mode) != 0o600:
            raise AgyPermissionSettingsError("settings_final_readback_failed")
    except (OSError, ValueError, TypeError) as exc:
        raise AgyPermissionSettingsError("settings_materialization_failed") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return settings_path


def materialize_isolated_agy_workspace(
    profile: str,
    *,
    parent_dir: "str | Path | None" = None,
    auth_profile: str = AGY_AUTH_PROFILE_MINIMAL,
) -> IsolatedAgyWorkspace:
    """Create a fresh, isolated temp workspace with a profile-scoped policy.

    Only new, empty structure is created under a brand-new temp directory:
    `.antigravity/settings.json` (the policy document from
    `build_workspace_permission_policy()`), `.antigravity/workspace_deny_gate.py`
    (the hook -- see its own docstring: a no-op placeholder, not an
    executable enforcement mechanism), and empty `xdg-config` / `xdg-cache` /
    `xdg-state` directories. Nothing is read from or copied out of the
    caller's real `$HOME` / `XDG_*` directories -- credential files (OAuth
    tokens, SSH keys, `.netrc`, etc.) are never copied (Issue #1705 AC12).
    The returned `env` redirects
    `HOME`/`XDG_CONFIG_HOME`/`XDG_CACHE_HOME`/`XDG_STATE_HOME` into this
    workspace, so any pre-existing global Antigravity settings on the real
    host are structurally unreachable by the AGY subprocess.

    `auth_profile` (Issue #1779, default `AGY_AUTH_PROFILE_MINIMAL`, distinct
    from `profile`/`tool_profile` above -- see the "Auth surface profiles"
    section docstring) controls whether the `DBUS_SESSION_BUS_ADDRESS` /
    `XDG_RUNTIME_DIR` / `GOOGLE_APPLICATION_CREDENTIALS` env vars and the
    `gcloud_adc_path` symlink are exposed at all: only
    `auth_profile=AGY_AUTH_PROFILE_EXTENDED` exposes them (unchanged
    behavior from #1726/#1730); the default `AGY_AUTH_PROFILE_MINIMAL`
    exposes none of them, since `AGY_AUTH_ABLATION_V1` proved they are not
    required for AGY auth to succeed. `agy_oauth_token_path` is exposed
    unconditionally regardless of `auth_profile` -- it is the one surface
    proven necessary and sufficient.

    Raises `AgyReadOnlyBoundaryError` (fail-closed, no workspace created) when
    `profile` is `no_tools` or `local_asset_research`, the real agy OAuth
    token file exists, and `bwrap` is unavailable -- see
    `AgyReadOnlyBoundaryError` docstring (Issue #1779 AC7).
    """
    validate_profile(profile)
    if auth_profile not in ALLOWED_AUTH_PROFILES:
        raise ValueError(f"unknown AGY auth_profile: {auth_profile!r}; expected one of {sorted(ALLOWED_AUTH_PROFILES)}")

    # Issue #1779 AC7: fail-closed *before* any workspace is created (not
    # merely annotated as degraded) when a security-sensitive profile cannot
    # be given a kernel-enforced read-only guarantee for the real agy OAuth
    # token file. When the real token file does not exist there is nothing
    # to protect, so this check never fires for hermetic/CI environments
    # that have no such file regardless of `bwrap` availability.
    if profile in _AUTH_READONLY_FAIL_CLOSED_PROFILES and not _bwrap_available():
        if _real_home_agy_oauth_token_file() is not None:
            raise AgyReadOnlyBoundaryError(
                f"materialize_isolated_agy_workspace(): profile={profile!r} "
                "requires a kernel-enforced read-only guarantee "
                f"({AGY_OAUTH_TOKEN_READONLY_KERNEL_ENFORCED}) for the real "
                "agy OAuth token file, but `bwrap` is unavailable on this "
                "host -- refusing to materialize a workspace that could "
                f"only offer {AGY_OAUTH_TOKEN_READONLY_DEGRADED} (Issue "
                "#1779 AC7)."
            )

    workspace_dir = Path(
        tempfile.mkdtemp(
            prefix=f"agy-isolated-{profile}-",
            dir=str(parent_dir) if parent_dir else None,
        )
    )
    antigravity_dir = workspace_dir / ".antigravity"
    antigravity_dir.mkdir(parents=True, exist_ok=True)

    settings_path = antigravity_dir / "settings.json"
    policy = build_workspace_permission_policy(profile)
    settings_path.write_text(json.dumps(policy, indent=2, sort_keys=True), encoding="utf-8")

    # Issue #1779 AC8: `_WORKSPACE_DENY_GATE_HOOK_SOURCE` is a documented
    # no-op placeholder (see its own docstring) -- no AGY `PreToolCall`
    # hook schema that would execute this file was found. It is still
    # written so any environment that later gains a working hook mechanism
    # has a stable path to populate; `PROFILE_ALLOWED_TOOLS` (via
    # `resolve_tool_permission()` / `build_workspace_permission_policy()`)
    # remains the sole *actually effective* tool-call deny mechanism.
    hook_path = antigravity_dir / "workspace_deny_gate.py"
    hook_path.write_text(_WORKSPACE_DENY_GATE_HOOK_SOURCE, encoding="utf-8")

    xdg_config = workspace_dir / "xdg-config"
    xdg_cache = workspace_dir / "xdg-cache"
    xdg_state = workspace_dir / "xdg-state"
    for directory in (xdg_config, xdg_cache, xdg_state):
        directory.mkdir(parents=True, exist_ok=True)

    env: dict[str, str] = {
        "HOME": str(workspace_dir),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_CACHE_HOME": str(xdg_cache),
        "XDG_STATE_HOME": str(xdg_state),
        "AGY_WORKSPACE_SETTINGS": str(settings_path),
    }
    for key in ("PATH", "LANG", "LC_ALL", "TERM"):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value

    gcloud_adc_path: "Path | None" = None
    if auth_profile == AGY_AUTH_PROFILE_EXTENDED:
        # Reachability variables (Issue #1726): each of these is an
        # *endpoint* pointer (a filesystem path to a Unix domain socket or a
        # socket directory), never a credential value in itself. Propagating
        # them lets the isolated `agy` subprocess reach the *existing*,
        # already-authenticated OS keyring / dbus secret service session on
        # the host, without granting it access to -- or a copy of -- any
        # credential file, token string, or cookie. This is distinct from
        # `HOME`/`XDG_CONFIG_HOME`/`XDG_CACHE_HOME`/`XDG_STATE_HOME` above,
        # which remain fully redirected into the isolated workspace
        # regardless of `auth_profile` (Issue #1705 secret-hygiene design is
        # unchanged). Issue #1779: `AGY_AUTH_ABLATION_V1` proved these are
        # not required for AGY auth to succeed, so they are only exposed
        # under the explicit `auth_profile=AGY_AUTH_PROFILE_EXTENDED` opt-in
        # (default `AGY_AUTH_PROFILE_MINIMAL` omits them entirely) --
        # superseded_by: #1779 (2026-07-26).
        #
        # - DBUS_SESSION_BUS_ADDRESS: the well-known D-Bus session bus
        #   address, e.g. `unix:path=/run/user/1000/bus`. It names *where*
        #   to connect to reach the running session/secret-service bus; it
        #   carries no secret material itself (the actual credential bytes
        #   stay inside the OS keyring process behind that socket and are
        #   never read or copied by this function).
        # - XDG_RUNTIME_DIR: the per-user runtime directory
        #   (e.g. `/run/user/1000`) that typically *contains* the D-Bus
        #   socket and other session sockets `agy`/dbus tooling may need to
        #   resolve a default bus address from. It is a directory path, not
        #   credential content, and propagating it does not expose any file
        #   contents to the subprocess beyond what the isolated `HOME`/
        #   `XDG_*` redirection above already scopes for config/cache/state.
        for key in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
            value = os.environ.get(key)
            if value is not None:
                env[key] = value

        # Issue #1730 AC2, superseded_by: #1779 (2026-07-26): only exposed
        # under `auth_profile=AGY_AUTH_PROFILE_EXTENDED`. When already set in
        # the real environment, this is a *path string* pointing at a
        # credential file -- not credential content itself -- so it is
        # propagated through the same way the Issue #1726 endpoint-pointer
        # variables above are: verbatim, without ever opening or reading the
        # file it names.
        google_application_credentials = os.environ.get(GOOGLE_APPLICATION_CREDENTIALS_ENV)
        if google_application_credentials is not None:
            env[GOOGLE_APPLICATION_CREDENTIALS_ENV] = google_application_credentials

        # Issue #1730 AC1/AC3, superseded_by: #1779 (2026-07-26): only
        # exposed under `auth_profile=AGY_AUTH_PROFILE_EXTENDED`. Expose the
        # real gcloud ADC config dir (if any) read-only under this
        # workspace's isolated XDG_CONFIG_HOME.
        gcloud_adc_path = _expose_gcloud_adc_read_only(xdg_config)

    # Issue #1740 AC1/AC2, #1743: expose the real agy OAuth token file (if
    # any) as a reachable symlink under this workspace's isolated HOME --
    # the actual auth channel `agy` uses; see
    # `_expose_agy_oauth_token_read_only()` docstring. Unconditional
    # regardless of `auth_profile` -- `AGY_AUTH_ABLATION_V1` proved this is
    # the one surface necessary and sufficient for auth to succeed (Issue
    # #1779 AC2).
    agy_oauth_token_path = _expose_agy_oauth_token_read_only(workspace_dir)

    # Issue #1758: generate the real AGY settings.json with an explicit
    # toolPermission so the isolated `agy` subprocess does not fall back to
    # the built-in "request-review" default, which silently drops tool calls
    # in headless print mode; see `_write_agy_tool_permission_settings()`.
    try:
        agy_tool_permission_settings_path = _write_agy_tool_permission_settings(workspace_dir, profile)
    except AgyPermissionSettingsError:
        # The official settings are the primary boundary, not optional
        # reachability metadata.  Remove the incomplete isolated workspace
        # and fail before any caller can construct an AGY command.
        shutil.rmtree(workspace_dir, ignore_errors=True)
        raise

    # Issue #1779 AC4/AC5/AC6: determine the actual (not merely claimed)
    # read-only enforcement mode for `agy_oauth_token_path`, and build the
    # `bwrap` prefix that delivers it when possible.
    if agy_oauth_token_path is None:
        agy_oauth_token_readonly_mode = AGY_OAUTH_TOKEN_READONLY_ABSENT
        agy_oauth_token_bwrap_prefix: "list[str] | None" = None
    elif _bwrap_available():
        real_token_file = _real_home_agy_oauth_token_file()
        # real_token_file is not None here: agy_oauth_token_path (a symlink
        # to it) was just successfully created above.
        ro_bind_pairs: list[tuple[Path, Path]] = [(real_token_file, agy_oauth_token_path)]  # type: ignore[list-item]
        ro_bind_pairs.append((agy_tool_permission_settings_path, agy_tool_permission_settings_path))
        agy_oauth_token_readonly_mode = AGY_OAUTH_TOKEN_READONLY_KERNEL_ENFORCED
        agy_oauth_token_bwrap_prefix = _build_bwrap_ro_bind_prefix(agy_oauth_token_path.parent, ro_bind_pairs)
    else:
        agy_oauth_token_readonly_mode = AGY_OAUTH_TOKEN_READONLY_DEGRADED
        agy_oauth_token_bwrap_prefix = None

    return IsolatedAgyWorkspace(
        profile=profile,
        workspace_dir=workspace_dir,
        settings_path=settings_path,
        hook_path=hook_path,
        env=env,
        gcloud_adc_path=gcloud_adc_path,
        agy_oauth_token_path=agy_oauth_token_path,
        agy_tool_permission_settings_path=agy_tool_permission_settings_path,
        agy_oauth_token_readonly_mode=agy_oauth_token_readonly_mode,
        agy_oauth_token_bwrap_prefix=agy_oauth_token_bwrap_prefix,
    )


def find_credential_like_files(workspace: IsolatedAgyWorkspace) -> list[Path]:
    """Return any file under *workspace* whose basename looks credential-like.

    `materialize_isolated_agy_workspace()` should always yield an empty list
    here (aside from the intentionally-exposed `gcloud_adc_path` /
    `agy_oauth_token_path` subtrees, see below); kept as an explicit runtime
    assertion helper for regression safety (Issue #1705 AC12), rather than
    relying solely on code-review inspection.

    Issue #1730: `workspace.gcloud_adc_path` (when not `None`) is an
    intentional, documented read-only exposure of the real
    `$HOME/.config/gcloud` directory (AC1) -- not a credential-copying leak
    this invariant check should flag. Its subtree is excluded from the scan.

    Issue #1740: `workspace.agy_oauth_token_path` (when not `None`) is
    likewise an intentional, documented read-only exposure of the real
    `$HOME/.gemini/antigravity-cli/antigravity-oauth-token` file (AC1/AC4)
    -- also excluded from the scan. Everything else under
    `workspace.workspace_dir` must still be exactly what
    `materialize_isolated_agy_workspace()` freshly created.
    """
    exposed_paths = [p for p in (workspace.gcloud_adc_path, workspace.agy_oauth_token_path) if p is not None]
    hits: list[Path] = []
    for path in workspace.workspace_dir.rglob("*"):
        excluded = False
        for exposed in exposed_paths:
            try:
                if path == exposed or path.is_relative_to(exposed):
                    excluded = True
                    break
            except ValueError:
                pass
        if excluded:
            continue
        if path.is_file() and path.name.lower() in CREDENTIAL_FILE_BASENAMES:
            hits.append(path)
    return hits


# ---------------------------------------------------------------------------
# Secret-safe denied-attempt recording (Issue #1705 AC7)
# ---------------------------------------------------------------------------

# Minimal, self-contained credential-like pattern scan. Kept intentionally
# narrower in scope than run_gemini_headless.py's `_redact_text()` /
# `_scan_redaction_violations()` (which this module does not import, per
# Issue #1705 Stop Conditions forbidding changes to the wrapper's evidence
# schema / redaction functions) but covers the same class of secrets:
# API keys, GitHub tokens, Slack tokens, and PEM private key blocks.
_CREDENTIAL_LIKE_RE = re.compile(
    r"(?:sk-[A-Za-z0-9]{16,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|gho_[A-Za-z0-9]{20,}"
    r"|AIza[0-9A-Za-z_\-]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)"
)

_REDACTION_PLACEHOLDER = "<redacted>"


def scan_credential_like(text: str) -> bool:
    """Return True if *text* contains a credential-like substring."""
    return bool(text) and bool(_CREDENTIAL_LIKE_RE.search(text))


def redact_secret_safe(text: str) -> str:
    """Return *text* with credential-like substrings and the real $HOME redacted."""
    redacted = _CREDENTIAL_LIKE_RE.sub(_REDACTION_PLACEHOLDER, text or "")
    home = os.environ.get("HOME")
    if home:
        redacted = redacted.replace(home, "$HOME")
    return redacted


def record_denied_tool_attempt(
    profile: str,
    tool_name: str,
    *,
    raw_args: Mapping[str, Any] | None = None,
    source: str = AGY_DIRECT_SOURCE,
) -> dict[str, Any]:
    """Build a secret-safe hook event recording a denied tool-call attempt.

    `raw_args` (the tool-call arguments AGY attempted to use) is JSON-encoded
    and redacted before being stored -- the returned record never contains a
    literal credential value or the real, un-redacted `$HOME` absolute path
    (Issue #1705 AC7).
    """
    validate_profile(profile)
    raw_args_text = json.dumps(raw_args or {}, ensure_ascii=False, sort_keys=True, default=str)
    return {
        "schema": SCHEMA_DENIED_EVENT,
        "profile": profile,
        "tool_name": tool_name,
        "source": source,
        "decision": "deny",
        "args_redacted": redact_secret_safe(raw_args_text),
        "contained_credential_like_pattern": scan_credential_like(raw_args_text),
    }


# ---------------------------------------------------------------------------
# Tool-call classification (Issue #1705 AC8/AC9/AC10)
# ---------------------------------------------------------------------------


def classify_tool_call_events(
    profile: str,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Classify *observed* tool-call events into expected/denied/unexpected.

    Each event is expected to describe one *observed* attempt (e.g. parsed
    from an AGY transcript / hook log), not a hypothetical one:

    - `tool_name` (str): the AGY tool name the attempt targeted.
    - `source` (str, default `"agy_direct"`): `"agy_direct"` for AGY's own
      tool surface, or `"wrapper_serena_mcp"` for the wrapper-side Serena
      retrieval channel. Only `"agy_direct"` events count toward the AGY
      direct tool-call tallies (Issue #1705 AC10) -- `wrapper_serena_mcp`
      events are returned separately in `wrapper_events` and never counted
      as AGY direct tool calls.
    - `executed` (bool, default False): whether the tool call actually ran
      (`True`) or was blocked before running (`False`), as observed from the
      real execution -- this is *not* derived from policy; it is the ground
      truth this function checks policy against.
    - `args` (mapping, optional): tool-call arguments, redacted before being
      stored in `denied_tool_calls` (see `record_denied_tool_attempt()`).

    Classification rule (three-way, symmetric around policy vs. observation):

    - `expected_action == "allow"` and `executed is True`  -> `expected_tool_calls`
    - `expected_action == "deny"` and `executed is False`  -> `denied_tool_calls`
    - anything else (a leak: denied-by-policy tool that executed anyway, or
      an allowed tool that failed to execute, or an unrecognized combination)
      -> `unexpected_tool_calls`

    Counts:

    - `agy_tool_calls_count` / `agy_direct_tool_calls_count`: number of AGY
      direct events that actually executed (`expected_tool_calls` +
      `unexpected_tool_calls` that executed). For `no_tools` /
      `local_asset_research` / `proposal_only` this is `0` when the gate
      behaves correctly, even though `denied_tool_calls` may be non-empty
      (an attempt was made and correctly blocked -- Issue #1705 AC9).
    - `unexpected_tool_calls_count`: length of `unexpected_tool_calls`. For
      `grounded_research`, `0` means every observed AGY direct attempt was
      either the exact allowlisted tool (`search_web` / `read_url_content`)
      running as expected, or a non-allowlisted tool correctly denied.
    """
    validate_profile(profile)

    expected_tool_calls: list[dict[str, Any]] = []
    denied_tool_calls: list[dict[str, Any]] = []
    unexpected_tool_calls: list[dict[str, Any]] = []
    wrapper_events: list[dict[str, Any]] = []

    for raw_event in events:
        event = dict(raw_event)
        source = event.get("source", AGY_DIRECT_SOURCE)
        tool_name = event.get("tool_name")
        executed = bool(event.get("executed", False))

        if source != AGY_DIRECT_SOURCE:
            wrapper_events.append(event)
            continue

        expected_action = resolve_tool_permission(profile, tool_name)

        if expected_action == "allow" and executed:
            expected_tool_calls.append(event)
        elif expected_action == "deny" and not executed:
            denied_tool_calls.append(
                record_denied_tool_attempt(
                    profile,
                    tool_name,
                    raw_args=event.get("args"),
                    source=source,
                )
            )
        else:
            unexpected_tool_calls.append(event)

    executed_direct_count = sum(1 for e in (expected_tool_calls + unexpected_tool_calls) if e.get("executed") is True)

    return {
        "schema": SCHEMA_GATE_RESULT,
        "profile": profile,
        "expected_tool_calls": expected_tool_calls,
        "denied_tool_calls": denied_tool_calls,
        "unexpected_tool_calls": unexpected_tool_calls,
        "wrapper_events": wrapper_events,
        "agy_tool_calls_count": executed_direct_count,
        "agy_direct_tool_calls_count": executed_direct_count,
        "expected_tool_calls_count": len(expected_tool_calls),
        "denied_tool_calls_count": len(denied_tool_calls),
        "unexpected_tool_calls_count": len(unexpected_tool_calls),
        "wrapper_tool_calls_count": len(wrapper_events),
        "retrieval_actor": RETRIEVAL_ACTOR_WRAPPER_SERENA_MCP,
        "analysis_actor": ANALYSIS_ACTOR_ANTIGRAVITY_CLI,
        "agy_direct_mcp_access": AGY_DIRECT_MCP_ACCESS,
    }


# ---------------------------------------------------------------------------
# run_gemini_headless.py wiring helper
# ---------------------------------------------------------------------------


def build_agy_run_context(
    profile: str,
    *,
    parent_dir: "str | Path | None" = None,
    auth_profile: str = AGY_AUTH_PROFILE_MINIMAL,
) -> dict[str, Any]:
    """Build the isolated workspace + env context for wiring into `_run_agy()`.

    Issue #1779: `auth_profile` defaults to `AGY_AUTH_PROFILE_MINIMAL`
    (forwarded to `materialize_isolated_agy_workspace()` unchanged) so
    callers that do not explicitly opt in to `AGY_AUTH_PROFILE_EXTENDED`
    get the minimized auth surface by default. The returned dict also
    carries `agy_oauth_token_readonly_mode` / `agy_oauth_token_bwrap_prefix`
    so `run_gemini_headless.py::_run_agy()` can prepend the `bwrap` prefix to
    the actual `agy` subprocess argv when kernel enforcement is available.
    """
    validate_profile(profile)
    workspace = materialize_isolated_agy_workspace(profile, parent_dir=parent_dir, auth_profile=auth_profile)
    return {
        "profile": profile,
        "workspace_dir": str(workspace.workspace_dir),
        "settings_path": str(workspace.settings_path),
        "hook_path": str(workspace.hook_path),
        "env": dict(workspace.env),
        "agy_oauth_token_readonly_mode": workspace.agy_oauth_token_readonly_mode,
        "agy_oauth_token_bwrap_prefix": (
            list(workspace.agy_oauth_token_bwrap_prefix) if workspace.agy_oauth_token_bwrap_prefix is not None else None
        ),
    }
