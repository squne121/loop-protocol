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

# Canonical taxonomy of the AGY *direct* tool surface this policy governs:
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
        raise ValueError(
            f"unknown AGY tool_profile: {profile!r}; expected one of {sorted(ALLOWED_PROFILES)}"
        )


def profile_allowed_tools(profile: str) -> frozenset[str]:
    """Return the exact set of AGY direct tool names *profile* may call."""
    validate_profile(profile)
    return PROFILE_ALLOWED_TOOLS[profile]


# ---------------------------------------------------------------------------
# Policy document generation
# ---------------------------------------------------------------------------


def build_workspace_permission_policy(profile: str) -> dict[str, Any]:
    """Build the workspace-scoped permission policy document for *profile*.

    This is the document written into the isolated workspace's
    `.antigravity/settings.json`. `permissions.default` is always `"deny"`;
    only tool names in `permissions.allow` may execute. For every profile in
    this Issue's scope (`no_tools` / `local_asset_research` / `proposal_only`)
    `permissions.allow` is empty -- AGY direct tools are fully denied.
    `grounded_research` allows exactly `search_web` and `read_url_content`.
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
    to widen the workspace allowlist. Workspace-scoped deny always wins over
    a global allow (Issue #1705 AC5/AC6), including when `global_settings`
    is a `hostile_global_settings_fixture()` that allows everything.
    """
    validate_profile(profile)
    del global_settings  # intentionally unused: workspace policy is authoritative
    allowed = PROFILE_ALLOWED_TOOLS[profile]
    return "allow" if tool_name in allowed else "deny"


# ---------------------------------------------------------------------------
# Isolated workspace materialization
# ---------------------------------------------------------------------------

_WORKSPACE_DENY_GATE_HOOK_SOURCE = '''"""Workspace-scoped PreToolCall deny gate.

Generated by agy_permission_policy.py (Issue #1705). Denies any tool call
whose name is not present in this workspace's settings.json
`permissions.allow`, taking precedence over any global
$HOME/.antigravity/settings.json allow rules -- because this workspace *is*
the isolated $HOME for the AGY subprocess, there is no global settings file
to fall back to at all.
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


@dataclass(frozen=True)
class IsolatedAgyWorkspace:
    profile: str
    workspace_dir: Path
    settings_path: Path
    hook_path: Path
    env: dict[str, str]


def materialize_isolated_agy_workspace(
    profile: str,
    *,
    parent_dir: "str | Path | None" = None,
) -> IsolatedAgyWorkspace:
    """Create a fresh, isolated temp workspace with a profile-scoped policy.

    Only new, empty structure is created under a brand-new temp directory:
    `.antigravity/settings.json` (the policy document from
    `build_workspace_permission_policy()`), `.antigravity/workspace_deny_gate.py`
    (the hook), and empty `xdg-config` / `xdg-cache` / `xdg-state`
    directories. Nothing is read from or copied out of the caller's real
    `$HOME` / `XDG_*` directories -- credential files (OAuth tokens, SSH
    keys, `.netrc`, etc.) are never copied (Issue #1705 AC12). The returned
    `env` redirects `HOME`/`XDG_CONFIG_HOME`/`XDG_CACHE_HOME`/`XDG_STATE_HOME`
    into this workspace, so any pre-existing global Antigravity settings on
    the real host are structurally unreachable by the AGY subprocess.
    """
    validate_profile(profile)
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

    return IsolatedAgyWorkspace(
        profile=profile,
        workspace_dir=workspace_dir,
        settings_path=settings_path,
        hook_path=hook_path,
        env=env,
    )


def find_credential_like_files(workspace: IsolatedAgyWorkspace) -> list[Path]:
    """Return any file under *workspace* whose basename looks credential-like.

    `materialize_isolated_agy_workspace()` should always yield an empty list
    here; kept as an explicit runtime assertion helper for regression safety
    (Issue #1705 AC12), rather than relying solely on code-review inspection.
    """
    hits: list[Path] = []
    for path in workspace.workspace_dir.rglob("*"):
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

    executed_direct_count = sum(
        1 for e in (expected_tool_calls + unexpected_tool_calls) if e.get("executed") is True
    )

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
) -> dict[str, Any]:
    """Build the isolated workspace + env context for wiring into `_run_agy()`."""
    validate_profile(profile)
    workspace = materialize_isolated_agy_workspace(profile, parent_dir=parent_dir)
    return {
        "profile": profile,
        "workspace_dir": str(workspace.workspace_dir),
        "settings_path": str(workspace.settings_path),
        "hook_path": str(workspace.hook_path),
        "env": dict(workspace.env),
    }
