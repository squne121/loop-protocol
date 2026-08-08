#!/usr/bin/env python3
"""Deterministic validator for Codex custom-agent runtime contracts."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTATION_PATH = REPO_ROOT / "tests/fixtures/codex-agent-config/expected-runtime-contract.json"
CONFIG_PATH = REPO_ROOT / ".codex/config.toml"
HOOKS_PATH = REPO_ROOT / ".codex/hooks.json"
ROOT_SKILL_DIRECTORY = Path(".agents/skills")
ROOT_SKILL_DIRECTORY_TARGET = "../.claude/skills"
CODEX_ONLY_ALLOWED_AGENTS = {"spark-skim", "spark-worker", "spark-deep"}
CODEX_ONLY_PARITY_REASON = "manual_codex_spark_agent"
CODEX_ONLY_MODEL = "gpt-5.3-codex-spark"
EXPECTED_HOOK_KEYS = ["command", "statusMessage", "timeout", "type"]
SCOPE_ROLLUP_PROFILE = "loop-protocol-scope-rollup"
SCOPE_ROLLUP_MARKER_TOKENS = (
    "marker_schema_version: 3",
    "query_schema_version: 4",
    "issues_completeness",
    "pull_requests_completeness",
    "transaction_budget",
    "result_sha256",
    "verify_status: verified",
    "payload: {schema_version: 2}",
)
CHECK_CODEX_AGENTS_BASE = 'rtk pnpm exec node "$(git rev-parse --show-toplevel)/scripts/check-codex-agents.mjs"'
COMPOSITE_BASE = 'rtk pnpm exec node "$(git rev-parse --show-toplevel)/.codex/hooks/session-recording-composite.mjs"'
EXPECTED_PRETOOL_HOOKS = {
    "^Bash$": [
        {
            "type": "command",
            "command": 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/local_main_branch_guard.sh"',
            "timeout": 10,
            "statusMessage": "Checking local root branch policy",
        },
        {
            "type": "command",
            "command": 'python3 "$(git rev-parse --show-toplevel)/scripts/agent-guards/worktree_scope_guard.py"',
            "timeout": 20,
            "statusMessage": "Checking worktree cleanup scope policy (shared core)",
        },
        {
            "type": "command",
            "command": f"{CHECK_CODEX_AGENTS_BASE} --hook-pretool",
            "timeout": 30,
            "statusMessage": "Checking LOOP_PROTOCOL Bash guardrail",
        },
        {
            "type": "command",
            "command": f"{COMPOSITE_BASE} --event PreToolUse",
            "timeout": 30,
            "statusMessage": "Checking Codex session-recording PreToolUse guard",
        },
        {
            "type": "command",
            "command": 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/ci_test_performance_advisory.sh"',
            "timeout": 10,
            "statusMessage": "Checking CI/test-lane path advisory",
        },
        {
            "type": "command",
            "command": 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/root_temporary_residue_advisory.sh"',
            "timeout": 10,
            "statusMessage": "Checking root temporary residue advisory",
        },
    ],
    "^(apply_patch|Edit|Write)$": [
        {
            "type": "command",
            "command": 'python3 "$(git rev-parse --show-toplevel)/scripts/agent-guards/codex_apply_patch_adapter.py"',
            "timeout": 20,
            "statusMessage": "Checking worktree containment for apply_patch/Edit/Write (shared core)",
        },
        {
            "type": "command",
            "command": f"{CHECK_CODEX_AGENTS_BASE} --hook-pretool",
            "timeout": 30,
            "statusMessage": "Checking LOOP_PROTOCOL patch guardrail",
        },
        {
            "type": "command",
            "command": f"{COMPOSITE_BASE} --event PreToolUse",
            "timeout": 30,
            "statusMessage": "Checking Codex session-recording patch guard",
        },
        {
            "type": "command",
            "command": 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/ci_test_performance_advisory.sh"',
            "timeout": 10,
            "statusMessage": "Checking CI/test-lane path advisory",
        },
        {
            "type": "command",
            "command": 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/root_temporary_residue_advisory.sh"',
            "timeout": 10,
            "statusMessage": "Checking root temporary residue advisory",
        },
    ],
}


def route_tokens_to_skill_surfaces(route: str) -> list[str]:
    if route in {"", "none"}:
        return []
    return [f".agents/skills/{token}/SKILL.md" for token in route.split("|") if token]


def load_expectations() -> dict:
    return json.loads(EXPECTATION_PATH.read_text(encoding="utf-8"))


def read_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def read_project_config() -> tuple[dict | None, str | None]:
    """Read .codex/config.toml with deterministic diagnostics for the CLI."""
    try:
        return read_toml(CONFIG_PATH), None
    except FileNotFoundError:
        return None, ".codex/config.toml: TOML file not found"
    except PermissionError:
        return None, ".codex/config.toml: TOML file is not readable"
    except OSError:
        return None, ".codex/config.toml: TOML I/O error"
    except tomllib.TOMLDecodeError:
        return None, ".codex/config.toml: malformed TOML"


# Issue #1859 (re-revision): OpenAI-defined built-in permission profile
# names. ":danger-full-access" and legacy `sandbox_mode` must never become
# the root default (see Out of Scope / Stop Conditions). The root default
# itself is the repository-defined custom profile below, which `extends`
# the built-in `:workspace` profile and layers on an explicit development
# network allowlist (owner `HUMAN_PERMISSION_DECISION_V1`, Issue #1859).
BUILTIN_PERMISSION_PROFILES = frozenset({":read-only", ":workspace", ":danger-full-access"})
CUSTOM_ROOT_DEFAULT_PROFILE = "loop-protocol-personal-dev"
REQUIRED_ROOT_DEFAULT_PERMISSIONS = CUSTOM_ROOT_DEFAULT_PROFILE
REQUIRED_ROOT_DEFAULT_EXTENDS = ":workspace"
REQUIRED_ROOT_DEFAULT_NETWORK_MODE = "full"


def _find_misplaced_default_permissions(config: dict) -> list[str]:
    """Structural (not regex) scan for `default_permissions` in any table
    other than the TOML root scope: `[features]`, `[agents]`, or any
    `[permissions.*]` profile table. Misplacement makes the key inert."""
    locations: list[str] = []
    features = config.get("features", {})
    if isinstance(features, dict) and "default_permissions" in features:
        locations.append("[features]")
    agents_table = config.get("agents", {})
    if isinstance(agents_table, dict) and "default_permissions" in agents_table:
        locations.append("[agents]")
    permissions = config.get("permissions", {})
    if isinstance(permissions, dict):
        for profile_name, profile in permissions.items():
            if isinstance(profile, dict) and "default_permissions" in profile:
                locations.append(f"[permissions.{profile_name}]")
    return locations


def _assert_root_default_profile_definition(permissions: dict) -> list[str]:
    """AC1/AC3: `loop-protocol-personal-dev` extends `:workspace` and layers
    on an explicit, non-global, non-local-binding development network
    allowlist."""
    failures: list[str] = []
    profile = permissions.get(CUSTOM_ROOT_DEFAULT_PROFILE)
    if not isinstance(profile, dict):
        return [f".codex/config.toml: [permissions.{CUSTOM_ROOT_DEFAULT_PROFILE}] must be defined"]

    extends = profile.get("extends")
    if extends != REQUIRED_ROOT_DEFAULT_EXTENDS:
        failures.append(
            f".codex/config.toml: [permissions.{CUSTOM_ROOT_DEFAULT_PROFILE}] must declare "
            f"extends = {REQUIRED_ROOT_DEFAULT_EXTENDS!r}, got {extends!r}"
        )

    network = profile.get("network")
    if not isinstance(network, dict):
        return failures + [
            f".codex/config.toml: [permissions.{CUSTOM_ROOT_DEFAULT_PROFILE}.network] must be defined"
        ]

    if network.get("enabled") is not True:
        failures.append(
            f".codex/config.toml: [permissions.{CUSTOM_ROOT_DEFAULT_PROFILE}.network] enabled must be true"
        )
    if network.get("mode") != REQUIRED_ROOT_DEFAULT_NETWORK_MODE:
        failures.append(
            f".codex/config.toml: [permissions.{CUSTOM_ROOT_DEFAULT_PROFILE}.network] mode must be "
            f"{REQUIRED_ROOT_DEFAULT_NETWORK_MODE!r}, got {network.get('mode')!r}"
        )
    if network.get("allow_local_binding") is not False:
        failures.append(
            f".codex/config.toml: [permissions.{CUSTOM_ROOT_DEFAULT_PROFILE}.network] "
            "allow_local_binding must be false"
        )

    domains = network.get("domains")
    if not isinstance(domains, dict) or not domains:
        failures.append(
            f".codex/config.toml: [permissions.{CUSTOM_ROOT_DEFAULT_PROFILE}.network.domains] must "
            "declare at least one explicit development allowlist domain"
        )
    elif "*" in domains:
        failures.append(
            f".codex/config.toml: [permissions.{CUSTOM_ROOT_DEFAULT_PROFILE}.network.domains] must not "
            'use a global "*" allowlist'
        )

    return failures


def assert_root_default_permissions(config: dict, config_text: str) -> list[str]:
    """AC1/AC3/AC5/AC6: root `default_permissions` semantic contract.

    - non-empty `[permissions]` requires a root-scope `default_permissions`.
    - the value must be the repository-defined `loop-protocol-personal-dev`
      profile, which must `extends = ":workspace"` and declare an explicit,
      bounded development network allowlist; any other value (including a
      reference to an undefined custom profile) is rejected.
    - `default_permissions` must live in the root TOML scope, not inside
      `[features]`, `[agents]`, or any `[permissions.*]` profile table
      (structural check against the parsed TOML object, not raw-text regex).
    - legacy `sandbox_mode` must not coexist with permission profiles.
    """
    del config_text  # retained for call-site compatibility; unused (AC5: structural, not regex)
    failures: list[str] = []
    permissions = config.get("permissions", {})
    root_default = config.get("default_permissions")

    failures.extend(
        f".codex/config.toml: default_permissions must not be placed inside {location} "
        "(misplacement makes it inert); it must live in the root TOML scope"
        for location in _find_misplaced_default_permissions(config)
    )

    if permissions:
        if root_default is None:
            failures.append(
                ".codex/config.toml: [permissions] profiles are defined but root "
                "default_permissions is missing (Codex loader rejects this combination)"
            )
        else:
            if root_default != REQUIRED_ROOT_DEFAULT_PERMISSIONS:
                failures.append(
                    f".codex/config.toml: root default_permissions must be "
                    f"{REQUIRED_ROOT_DEFAULT_PERMISSIONS!r}, got {root_default!r}"
                )
            if root_default not in BUILTIN_PERMISSION_PROFILES and root_default not in permissions:
                failures.append(
                    f".codex/config.toml: root default_permissions references an "
                    f"undefined custom profile: {root_default!r}"
                )
            if root_default == CUSTOM_ROOT_DEFAULT_PROFILE:
                failures.extend(_assert_root_default_profile_definition(permissions))

    if "sandbox_mode" in config:
        failures.append(
            ".codex/config.toml: legacy sandbox_mode must not coexist with permission profiles"
        )

    return failures


def extract_runtime_field(instructions: str, field: str) -> str | None:
    match = re.search(rf"{re.escape(field)}:\s*([a-zA-Z0-9._|-]+)", instructions)
    return match.group(1) if match else None


def extract_skill_surface_paths(instructions: str) -> list[str]:
    match = re.search(r"repo_local_skill_surface:\s*(.+)", instructions)
    if not match:
        return []
    return [part for part in re.split(r"\s*,\s*|\s*\|\s*", match.group(1).strip()) if part]


def extract_builder_invocation_block(instructions: str) -> str:
    """Issue #1886: extract the BUILDER_INVOCATION prose block, if present."""
    match = re.search(
        r"BUILDER_INVOCATION\n(.*?)(?:\n\n|\nFAIL_CLOSED|\nNETWORK_LIMITATION|\nKnown limitation|\Z)",
        instructions,
        re.DOTALL,
    )
    return match.group(1) if match else ""


def extract_builder_invocation_provider(instructions: str) -> str | None:
    block = extract_builder_invocation_block(instructions)
    match = re.search(r"- provider:\s*([a-z_]+)", block)
    return match.group(1) if match else None


def extract_builder_invocation_profiles(instructions: str) -> list[str]:
    block = extract_builder_invocation_block(instructions)
    match = re.search(r"- profiles?:\s*(.+)", block)
    if not match:
        return []
    return [part.strip() for part in match.group(1).split(",") if part.strip()]


def validate_agy_builder_invocation(expectations: dict) -> list[str]:
    """Issue #1886 AC4: codebase-investigator / web-researcher must declare an
    AGY-only canonical builder invocation (provider=agy, expected profiles),
    must not hand-write a provider-specific request JSON literal, and must
    not claim Gemini-mandatory delegation (Gemini is disabled_by_operator)."""
    failures: list[str] = []
    for agent_name, expected in expectations["required_agents"].items():
        expected_profiles = expected.get("agy_builder_profiles")
        if not expected_profiles:
            continue
        expected_provider = expected.get("agy_builder_provider", "agy")
        path = REPO_ROOT / expected["path"]
        if not path.exists():
            continue
        agent = load_agent(path)
        instructions = str(agent.get("developer_instructions", ""))
        provider = extract_builder_invocation_provider(instructions)
        profiles = extract_builder_invocation_profiles(instructions)
        if provider != expected_provider:
            failures.append(
                f"{expected['path']}: BUILDER_INVOCATION provider must be {expected_provider!r}, got {provider!r}"
            )
        if profiles != expected_profiles:
            failures.append(
                f"{expected['path']}: BUILDER_INVOCATION profiles must be {expected_profiles!r}, got {profiles!r}"
            )
        if re.search(r'"provider"\s*:', instructions):
            failures.append(f"{expected['path']}: must not hand-write a provider JSON literal")
        if "disabled_by_operator" not in instructions:
            failures.append(f"{expected['path']}: must declare Gemini disabled_by_operator policy")
        failures.extend(
            _forbid_gemini_and_legacy_route_tokens(expected["path"], instructions)
        )
        if agent_name in {"codebase-investigator", "web-researcher"}:
            claude_path = REPO_ROOT / expected["claude_agent_path"]
            if claude_path.exists():
                claude_text = claude_path.read_text(encoding="utf-8")
                if "disabled_by_operator" not in claude_text:
                    failures.append(
                        f"{expected['claude_agent_path']}: must declare Gemini disabled_by_operator policy"
                    )
                failures.extend(
                    _forbid_gemini_and_legacy_route_tokens(
                        expected["claude_agent_path"], claude_text
                    )
                )
    return failures


# Issue #1886 P0-5/P0-6 fix_delta (PR #2005 adversarial review): the prior
# static checker only inspected the BUILDER_INVOCATION prose block for
# provider/profile tokens, so an executable Gemini command elsewhere in the
# same agent definition (e.g. a Serena-triage step calling
# ``setup_check.py`` without ``--provider agy``, which defaults to and
# executes real Gemini OAuth/setup smoke) or a stale
# ``grounded_research_or_direct_web`` legacy route token went undetected.
# This scans the FULL agent instructions text (not just BUILDER_INVOCATION)
# for forbidden executable Gemini invocation tokens and the retired legacy
# route token.
_FORBIDDEN_GEMINI_INVOCATION_SUBSTRINGS = (
    "preflight_gemini_headless.py",
    "provider=auto",
)
_LEGACY_ROUTE_TOKEN = "grounded_research_or_direct_web"
_SETUP_CHECK_LINE_RE = re.compile(r"^.*setup_check\.py.*$", re.MULTILINE)
_BARE_GEMINI_INVOCATION_RE = re.compile(r"(?<![\w-])gemini\s+(--|['\"])")
_CODE_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_-]*)\n(.*?)```", re.DOTALL)


def _extract_code_fences(text: str) -> str:
    """Join all fenced (```...```) command blocks. Executable-invocation
    checks (P0-5) are scoped to fenced blocks only -- prose that documents a
    *prohibition* (e.g. "preflight_gemini_headless.py は使わない") legitimately
    names the forbidden token without invoking it, and must not be treated
    as an executable invocation."""
    return "\n".join(_CODE_FENCE_RE.findall(text))


def _forbid_gemini_and_legacy_route_tokens(path_label: str, text: str) -> list[str]:
    failures: list[str] = []
    fenced = _extract_code_fences(text)
    for line in _SETUP_CHECK_LINE_RE.findall(fenced):
        if "--provider agy" not in line:
            failures.append(
                f"{path_label}: setup_check.py invocation must pass --provider agy"
                f" (defaults to Gemini otherwise): {line.strip()!r}"
            )
    if _BARE_GEMINI_INVOCATION_RE.search(fenced):
        failures.append(
            f"{path_label}: must not invoke a binary literally named `gemini`"
        )
    for token in _FORBIDDEN_GEMINI_INVOCATION_SUBSTRINGS:
        if token in fenced:
            failures.append(f"{path_label}: forbidden Gemini invocation token {token!r} present")
    if _LEGACY_ROUTE_TOKEN in text:
        failures.append(
            f"{path_label}: legacy runtime_followup_route token"
            f" {_LEGACY_ROUTE_TOKEN!r} is retired and must not be present"
        )
    return failures


def is_codex_only_parity(expected: dict) -> bool:
    return expected.get("parity_mode") == "codex_only"


def validate_codex_only_expectation(agent_name: str, expected: dict) -> list[str]:
    failures: list[str] = []
    if agent_name not in CODEX_ONLY_ALLOWED_AGENTS:
        failures.append(
            f"{expected['path']}: codex_only parity is restricted to {sorted(CODEX_ONLY_ALLOWED_AGENTS)!r}"
        )
    if not expected["path"].startswith(".codex/agents/spark-"):
        failures.append(f"{expected['path']}: codex_only parity path must stay under .codex/agents/spark-*")
    if expected.get("claude_agent_path", "__missing__") is not None:
        failures.append(f"{expected['path']}: codex_only parity must use claude_agent_path: null")
    if expected.get("parity_exception_reason") != CODEX_ONLY_PARITY_REASON:
        failures.append(
            f"{expected['path']}: codex_only parity must use parity_exception_reason {CODEX_ONLY_PARITY_REASON!r}"
        )
    if expected.get("model") != CODEX_ONLY_MODEL:
        failures.append(f"{expected['path']}: codex_only parity must use model {CODEX_ONLY_MODEL!r}")
    if expected.get("runtime_followup_route") != "none":
        failures.append(f"{expected['path']}: codex_only parity must use runtime_followup_route 'none'")
    if expected.get("runtime_dependency_status") != "codex_native":
        failures.append(f"{expected['path']}: codex_only parity must use runtime_dependency_status 'codex_native'")
    if expected.get("protected_lane") is not True:
        failures.append(f"{expected['path']}: codex_only parity must set protected_lane true")
    if expected.get("repo_local_skill_surfaces", []) != []:
        failures.append(f"{expected['path']}: codex_only parity must not declare repo_local_skill_surfaces")
    return failures


def load_agent(path: Path) -> dict:
    data = read_toml(path)
    data["_raw_text"] = path.read_text(encoding="utf-8")
    return data


def validate_scope_rollup_runtime_contract(expectations: dict) -> list[str]:
    """Validate the isolated temp-write exception without widening readonly.

    This is a declaration validator, not proof that an unmanaged parent
    runtime honored the profile.  Live evidence is separately availability
    gated by the runtime probe.
    """
    failures: list[str] = []
    expected = expectations["required_agents"].get("scope-rollup-runner")
    if not isinstance(expected, dict):
        return ["scope-rollup-runner: missing expectation"]
    exclusion = expected.get("permission_exclusion")
    if not isinstance(exclusion, dict):
        return ["scope-rollup-runner: permission exclusion must be structured"]
    required_exclusion = {
        "allowlisted_agent": "scope-rollup-runner",
        "reason": "claude_auto_permission_is_not_comparable_to_codex_ephemeral_write_profile",
        "follow_up_issue": "#1686",
        "expires_on": "2026-12-31",
    }
    if exclusion != required_exclusion:
        failures.append("scope-rollup-runner: permission exclusion allowlist/reason/expiry/follow-up mismatch")

    config, config_error = read_project_config()
    if config_error:
        return failures + [config_error]
    assert config is not None
    permissions = config.get("permissions", {})
    profile = permissions.get(SCOPE_ROLLUP_PROFILE) if isinstance(permissions, dict) else None
    if not isinstance(profile, dict):
        return failures + [f".codex/config.toml: missing {SCOPE_ROLLUP_PROFILE} profile"]
    filesystem = profile.get("filesystem", {})
    roots = filesystem.get(":workspace_roots", {}) if isinstance(filesystem, dict) else {}
    if (
        not isinstance(filesystem, dict)
        or filesystem.get(":tmpdir") != "write"
        or filesystem.get(":slash_tmp") != "write"
    ):
        failures.append(f".codex/config.toml: {SCOPE_ROLLUP_PROFILE} must write only :tmpdir and :slash_tmp")
    if not isinstance(roots, dict) or roots.get(".") != "read":
        failures.append(f".codex/config.toml: {SCOPE_ROLLUP_PROFILE} workspace must remain read-only")

    agent = load_agent(REPO_ROOT / expected["path"])
    instructions = str(agent.get("developer_instructions", ""))
    if agent.get("default_permissions") != "loop-protocol-readonly":
        failures.append("scope-rollup-runner: default_permissions must remain loop-protocol-readonly")
    for token in SCOPE_ROLLUP_MARKER_TOKENS:
        if token not in instructions:
            failures.append(f"scope-rollup-runner: missing exact marker contract token {token!r}")
    for token in (
        "required_effective_permission_profile: loop-protocol-scope-rollup",
        "DECLARED_PERMISSION: loop-protocol-readonly",
        "MUTATION_BOUNDARY: repo_remote_readonly_with_ephemeral_local_write",
        "uv sync",
        "session feature set",
        "release_pin: codex-0.145.0",
    ):
        if token not in instructions:
            failures.append(f"scope-rollup-runner: missing runtime contract token {token!r}")
    return failures


def validate_root_skill_directory_symlink(repo_root: Path = REPO_ROOT) -> list[str]:
    """Reject every skill-surface topology except the tracked root directory link."""
    failures: list[str] = []
    repo_root = repo_root.resolve()
    surface = repo_root / ROOT_SKILL_DIRECTORY
    try:
        mode = surface.lstat().st_mode
    except FileNotFoundError:
        return [".agents/skills: root skill-directory symlink is missing"]

    if not os.path.islink(surface):
        return [".agents/skills: must be a root skill-directory symlink, not a regular directory"]
    if not mode:
        return [".agents/skills: root skill-directory symlink metadata is unreadable"]

    target = os.readlink(surface)
    if target != ROOT_SKILL_DIRECTORY_TARGET:
        failures.append(
            ".agents/skills: root skill-directory symlink target must be "
            f"{ROOT_SKILL_DIRECTORY_TARGET!r}, got {target!r}"
        )
    if Path(target).is_absolute():
        failures.append(".agents/skills: absolute symlink targets are prohibited")

    try:
        resolved = surface.resolve(strict=True)
    except (FileNotFoundError, OSError):
        failures.append(".agents/skills: root skill-directory symlink target is broken")
        resolved = None
    expected = (repo_root / ".claude/skills").resolve()
    if resolved is not None:
        if not resolved.is_dir():
            failures.append(".agents/skills: root skill-directory symlink must resolve to a directory")
        if resolved != expected:
            failures.append(".agents/skills: root skill-directory symlink must resolve inside this repository")

    index = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-s", "--", str(ROOT_SKILL_DIRECTORY)],
        text=True,
        capture_output=True,
        check=False,
    )
    index_fields = index.stdout.split(maxsplit=1)
    if index.returncode != 0 or not index_fields or index_fields[0] != "120000":
        failures.append(".agents/skills: Git index must track the root skill-directory symlink with mode 120000")
    return failures


def assert_required_fields(expectations: dict) -> list[str]:
    failures = validate_root_skill_directory_symlink()
    required_tokens = expectations["required_instruction_tokens"]
    for agent_name, expected in expectations["required_agents"].items():
        path = REPO_ROOT / expected["path"]
        codex_only = is_codex_only_parity(expected)
        if not path.exists():
            failures.append(f"missing agent file: {expected['path']}")
            continue
        agent = load_agent(path)
        for field in (
            "name", "description", "model",
            "model_reasoning_effort", "default_permissions", "developer_instructions"
        ):
            if not agent.get(field):
                failures.append(f"{expected['path']}: missing required field '{field}'")
        if agent.get("name") != agent_name:
            failures.append(f"{expected['path']}: name must be {agent_name}")
        instructions = agent.get("developer_instructions", "")
        for token in required_tokens:
            if token not in instructions:
                failures.append(f"{expected['path']}: developer_instructions missing token '{token}'")
        for runtime_field in ("runtime_dependency_status", "runtime_followup_route"):
            if extract_runtime_field(instructions, runtime_field) is None:
                failures.append(
                    f"{expected['path']}: developer_instructions missing {runtime_field}"
                )
        expected_skill_surfaces = expected.get("repo_local_skill_surfaces", [])
        actual_skill_surfaces = extract_skill_surface_paths(instructions)
        if expected_skill_surfaces and not actual_skill_surfaces:
            failures.append(
                f"{expected['path']}: developer_instructions missing repo_local_skill_surface"
            )
        expected_route_surfaces = route_tokens_to_skill_surfaces(expected.get("runtime_followup_route", ""))
        if expected_skill_surfaces and expected_skill_surfaces != expected_route_surfaces:
            failures.append(
                f"{expected['path']}: expected fixture route/surface mismatch"
                f" {expected_route_surfaces!r} vs {expected_skill_surfaces!r}"
            )
        if codex_only:
            failures.extend(validate_codex_only_expectation(agent_name, expected))
    return failures + validate_scope_rollup_runtime_contract(expectations)


def assert_runtime_contract(expectations: dict) -> list[str]:
    failures = validate_root_skill_directory_symlink()
    config, config_error = read_project_config()
    if config_error:
        return [config_error]
    assert config is not None
    hooks = json.loads(HOOKS_PATH.read_text(encoding="utf-8"))
    for agent_name, expected in expectations["required_agents"].items():
        agent = load_agent(REPO_ROOT / expected["path"])
        instructions = agent["developer_instructions"]
        codex_only = is_codex_only_parity(expected)
        for field in ("model", "model_reasoning_effort", "default_permissions"):
            if agent.get(field) != expected[field]:
                failures.append(
                    f"{expected['path']}: {field} expected {expected[field]!r} got {agent.get(field)!r}"
                )
        for runtime_field in ("runtime_dependency_status", "runtime_followup_route"):
            actual = extract_runtime_field(instructions, runtime_field)
            if actual != expected[runtime_field]:
                failures.append(
                    f"{expected['path']}: {runtime_field} expected {expected[runtime_field]!r} got {actual!r}"
                )
        actual_skill_surfaces = extract_skill_surface_paths(instructions)
        expected_skill_surfaces = expected.get("repo_local_skill_surfaces", [])
        if actual_skill_surfaces != expected_skill_surfaces:
            failures.append(
                f"{expected['path']}: repo_local_skill_surfaces expected"
                f" {expected_skill_surfaces!r} got {actual_skill_surfaces!r}"
            )
        route_surface_paths = route_tokens_to_skill_surfaces(expected["runtime_followup_route"])
        if expected_skill_surfaces and actual_skill_surfaces != route_surface_paths:
            failures.append(
                f"{expected['path']}: runtime_followup_route"
                f" {expected['runtime_followup_route']!r} must map to"
                f" {route_surface_paths!r}, got {actual_skill_surfaces!r}"
            )
        for surface in actual_skill_surfaces:
            surface_path = REPO_ROOT / surface
            if not surface.startswith(".agents/skills/"):
                failures.append(f"{expected['path']}: repo_local_skill_surface must stay under .agents/skills/")
            if not surface_path.exists():
                failures.append(f"{expected['path']}: missing repo-local skill surface {surface}")
                continue
            content = surface_path.read_text(encoding="utf-8")
            if "name:" not in content or "description:" not in content:
                failures.append(
                    f"{expected['path']}: skill surface {surface} must declare name and description frontmatter"
                )
        if codex_only:
            failures.extend(validate_codex_only_expectation(agent_name, expected))
        else:
            claude_agent_path = REPO_ROOT / expected["claude_agent_path"]
            if not claude_agent_path.exists():
                failures.append(f"missing parity file: {expected['claude_agent_path']}")

    features = config.get("features", {})
    if not isinstance(features, dict):
        failures.append(".codex/config.toml: [features] must be a table")
        multi_agent_v2 = None
    else:
        multi_agent_v2 = features.get("multi_agent_v2")
    if not isinstance(multi_agent_v2, dict):
        failures.append(".codex/config.toml: [features.multi_agent_v2] must be declared")
    else:
        if type(multi_agent_v2.get("enabled")) is not bool or multi_agent_v2["enabled"] is not True:
            failures.append(".codex/config.toml: [features.multi_agent_v2].enabled must be strict boolean true")
        if (
            type(multi_agent_v2.get("max_concurrent_threads_per_session")) is not int
            or multi_agent_v2["max_concurrent_threads_per_session"] != 4
        ):
            failures.append(
                ".codex/config.toml: [features.multi_agent_v2].max_concurrent_threads_per_session "
                "must be strict integer 4"
            )
    agents = config.get("agents", {})
    if not isinstance(agents, dict):
        failures.append(".codex/config.toml: [agents] must be a table")
    elif "max_depth" in agents:
        failures.append(".codex/config.toml: [agents].max_depth must be absent")
    failures.extend(assert_root_default_permissions(config, CONFIG_PATH.read_text(encoding="utf-8")))

    if sorted(hooks.keys()) != ["hooks"]:
        failures.append(f".codex/hooks.json: root keys must be exactly ['hooks'], got {sorted(hooks.keys())!r}")
    failures.extend(assert_local_main_branch_guard_preflight(hooks))
    hooks_root = hooks.get("hooks", {})
    for event_name, subject in (("SessionEnd", "session"), ("SubagentStop", "subagent")):
        expected_entries = [
            {
                "matcher": ".*",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            "node .codex/hooks/session-recording-composite.mjs"
                            f" --event {event_name}"
                        ),
                        "timeout": 3,
                        "statusMessage": f"Recording advisory Codex {subject} metadata",
                    }
                ],
            }
        ]
        if hooks_root.get(event_name) != expected_entries:
            failures.append(
                f".codex/hooks.json: {event_name} must exactly match the passive advisory handler"
            )
    if (REPO_ROOT / ".codex/skills").exists():
        failures.append(".codex/skills: must not exist as a repo-shared skill surface")

    parity_script = REPO_ROOT / "scripts/check_claude_codex_agent_parity.py"
    namespace: dict[str, object] = {"__file__": str(parity_script), "__name__": "__parity__"}
    exec(parity_script.read_text(encoding="utf-8"), namespace)
    parity_main = namespace["main"]
    if parity_main() != 0:
        failures.append("scripts/check_claude_codex_agent_parity.py: parity validation failed")

    return failures + validate_scope_rollup_runtime_contract(expectations)



CODEX_RULES_DEFAULT_PATH = REPO_ROOT / ".codex" / "rules" / "default.rules"
# B5: The startup preflight command that must be documented in the rules file
REQUIRED_PREFLIGHT_GATE_CMD = "uv run python3 scripts/check_local_main_branch_state.py --json"


def assert_local_main_branch_guard_preflight(hooks: dict) -> list[str]:
    """
    Quarantine 後の Codex guardrail を検証する。

    local_main_branch_guard は active hook ではない。標準 sandbox / approval
    policy を authority とし、repo hook は passive allowlist のみに限定する。
    """
    failures: list[str] = []
    hooks_root = hooks.get("hooks", {})
    if set(hooks_root) != {"SessionEnd", "SubagentStop"}:
        failures.append(
            ".codex/hooks.json: active hooks must be the passive SessionEnd/SubagentStop allowlist"
        )
    commands = [
        hook.get("command", "")
        for entries in hooks_root.values()
        for entry in entries
        for hook in entry.get("hooks", [])
    ]
    if any("local_main_branch_guard" in command for command in commands):
        failures.append(
            ".codex/hooks.json: quarantined local_main_branch_guard must not be active"
        )
    if any(event in hooks_root for event in ("PreToolUse", "PermissionRequest")):
        failures.append(
            ".codex/hooks.json: command enforcement must use standard sandbox/approval, not repo hooks"
        )

    return failures

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assert-required-fields", action="store_true")
    parser.add_argument("--assert-runtime-contract", action="store_true")
    parser.add_argument("--assert-local-main-branch-guard", action="store_true",
                        help="Validate post-quarantine Codex sandbox/approval and passive hook boundary")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    expectations = load_expectations()
    failures: list[str] = []
    no_flag_given = (
        not args.assert_required_fields
        and not args.assert_runtime_contract
        and not args.assert_local_main_branch_guard
    )
    # Issue #1886 AC4: the bare invocation (`check_codex_agent_config.py`
    # with no flags) is a documented Verification Command. Default to
    # running the full assertion suite instead of erroring, so the VC is a
    # deterministic pass/fail rather than an argparse usage error.
    run_required_fields = args.assert_required_fields or no_flag_given
    run_runtime_contract = args.assert_runtime_contract or no_flag_given
    run_local_main_branch_guard = args.assert_local_main_branch_guard or no_flag_given
    if run_required_fields:
        failures.extend(assert_required_fields(expectations))
    if run_runtime_contract:
        failures.extend(assert_runtime_contract(expectations))
    if run_local_main_branch_guard:
        hooks = json.loads(HOOKS_PATH.read_text(encoding="utf-8"))
        failures.extend(assert_local_main_branch_guard_preflight(hooks))
    if no_flag_given or run_required_fields or run_runtime_contract:
        failures.extend(validate_agy_builder_invocation(expectations))
    if failures:
        for failure in failures:
            print(f"[FAIL] {failure}")
        return 1
    print("OK: Codex agent contract validation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
