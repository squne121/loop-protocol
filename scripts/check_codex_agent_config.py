#!/usr/bin/env python3
"""Deterministic validator for Codex custom-agent runtime contracts."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTATION_PATH = REPO_ROOT / "tests/fixtures/codex-agent-config/expected-runtime-contract.json"
CONFIG_PATH = REPO_ROOT / ".codex/config.toml"
HOOKS_PATH = REPO_ROOT / ".codex/hooks.json"
REQUIRED_DERIVED_MARKER = "derived/non-canonical"
REQUIRED_IMPERATIVE = "Before executing this skill, read the canonical body at"
MAX_BRIDGE_BODY_LINES = 3
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


def extract_canonical_body_target(skill_surface: Path) -> str | None:
    match = re.search(r"`([^`]*\.claude/skills/[^`]+/SKILL\.md)`", skill_surface.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def load_expectations() -> dict:
    return json.loads(EXPECTATION_PATH.read_text(encoding="utf-8"))


def read_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def extract_runtime_field(instructions: str, field: str) -> str | None:
    match = re.search(rf"{re.escape(field)}:\s*([a-zA-Z0-9._|-]+)", instructions)
    return match.group(1) if match else None


def extract_skill_surface_paths(instructions: str) -> list[str]:
    match = re.search(r"repo_local_skill_surface:\s*(.+)", instructions)
    if not match:
        return []
    return [part for part in re.split(r"\s*,\s*|\s*\|\s*", match.group(1).strip()) if part]


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

    config = read_toml(CONFIG_PATH)
    profile = config.get("permissions", {}).get(SCOPE_ROLLUP_PROFILE)
    if not isinstance(profile, dict):
        return failures + [f".codex/config.toml: missing {SCOPE_ROLLUP_PROFILE} profile"]
    filesystem = profile.get("filesystem", {})
    roots = filesystem.get(":workspace_roots", {}) if isinstance(filesystem, dict) else {}
    if not isinstance(filesystem, dict) or filesystem.get(":tmpdir") != "write" or filesystem.get(":slash_tmp") != "write":
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


def expected_canonical_target_for_surface(surface: Path) -> str:
    return f"../../../.claude/skills/{surface.parent.name}/SKILL.md"


def extract_bridge_body_lines(text: str) -> list[str]:
    remainder = text.split("\n---\n", 2)[-1]
    return [line.strip() for line in remainder.splitlines() if line.strip() and not line.strip().startswith("# ")]


def validate_bridge_surface(surface_path: Path) -> list[str]:
    failures: list[str] = []
    text = surface_path.read_text(encoding="utf-8")
    body_lines = extract_bridge_body_lines(text)

    if REQUIRED_DERIVED_MARKER not in text:
        failures.append(f"{surface_path}: derived/non-canonical marker required")
    if REQUIRED_IMPERATIVE not in text:
        failures.append(f"{surface_path}: exact imperative required")

    canonical_target = extract_canonical_body_target(surface_path)
    expected_target = expected_canonical_target_for_surface(surface_path)
    if canonical_target is None:
        failures.append(f"{surface_path}: wrong skill target - canonical target missing")
    elif canonical_target != expected_target:
        failures.append(f"{surface_path}: wrong skill target - expected {expected_target!r} got {canonical_target!r}")
    else:
        canonical_target_path = (surface_path.parent / canonical_target).resolve()
        if not canonical_target_path.exists():
            failures.append(f"{surface_path}: canonical skill body target missing for {canonical_target}")

    if len(body_lines) > MAX_BRIDGE_BODY_LINES:
        failures.append(f"{surface_path}: required thin wrapper - body bloat detected")
    if any(token in text for token in ("```", "## ", "### ", "\n- ", "\n* ")):
        failures.append(f"{surface_path}: required thin wrapper - stale procedure body detected")

    return failures


def find_duplicate_canonical_targets(surface_paths: list[Path]) -> list[str]:
    seen: dict[str, Path] = {}
    failures: list[str] = []
    for surface_path in surface_paths:
        target = extract_canonical_body_target(surface_path)
        if target is None:
            continue
        if target in seen:
            failures.append(f"duplicate canonical target: {target} used by {seen[target]} and {surface_path}")
        else:
            seen[target] = surface_path
    return failures


def assert_required_fields(expectations: dict) -> list[str]:
    failures: list[str] = []
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
    failures: list[str] = []
    config = read_toml(CONFIG_PATH)
    hooks = json.loads(HOOKS_PATH.read_text(encoding="utf-8"))
    hook_command_fragment = expectations["required_hook_command_fragment"]
    all_surface_paths: list[Path] = []
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
            all_surface_paths.append(surface_path)
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
            failures.extend(validate_bridge_surface(surface_path))
        if codex_only:
            failures.extend(validate_codex_only_expectation(agent_name, expected))
        else:
            claude_agent_path = REPO_ROOT / expected["claude_agent_path"]
            if not claude_agent_path.exists():
                failures.append(f"missing parity file: {expected['claude_agent_path']}")

    deduped_surface_paths = list(dict.fromkeys(all_surface_paths))
    failures.extend(find_duplicate_canonical_targets(deduped_surface_paths))
    if config.get("agents", {}).get("max_depth") != 1:
        failures.append(".codex/config.toml: [agents].max_depth must be 1")

    if sorted(hooks.keys()) != ["hooks"]:
        failures.append(f".codex/hooks.json: root keys must be exactly ['hooks'], got {sorted(hooks.keys())!r}")
    hooks_root = hooks.get("hooks", {})
    subagent_entries = hooks_root.get("SubagentStart")
    if not isinstance(subagent_entries, list) or not subagent_entries:
        failures.append(".codex/hooks.json: missing hooks for SubagentStart")
    else:
        if len(subagent_entries) != 1:
            failures.append(".codex/hooks.json: SubagentStart must have exactly one matcher entry")
        else:
            entry = subagent_entries[0]
            if entry.get("matcher") != ".*":
                failures.append(".codex/hooks.json: SubagentStart matcher must be '.*'")
            commands = [hook.get("command") for hook in entry.get("hooks", []) if isinstance(hook.get("command"), str)]
            if len(commands) != 1 or "--hook-subagent-start" not in commands[0]:
                failures.append(
                    ".codex/hooks.json: SubagentStart must route exactly"
                    " one command with --hook-subagent-start"
                )

    pretool_entries = hooks_root.get("PreToolUse")
    if not isinstance(pretool_entries, list) or not pretool_entries:
        failures.append(".codex/hooks.json: missing hooks for PreToolUse")
        pretool_entries = []
    actual_matchers = {entry.get("matcher"): entry for entry in pretool_entries if isinstance(entry, dict)}
    if len(actual_matchers) != len(EXPECTED_PRETOOL_HOOKS):
        failures.append(
            f".codex/hooks.json: PreToolUse must have exactly {len(EXPECTED_PRETOOL_HOOKS)} matcher entries"
        )
    for matcher, expected_hooks in EXPECTED_PRETOOL_HOOKS.items():
        entry = actual_matchers.get(matcher)
        if entry is None:
            failures.append(f".codex/hooks.json: missing PreToolUse matcher {matcher}")
            continue
        hooks_for_matcher = entry.get("hooks", [])
        if not isinstance(hooks_for_matcher, list):
            failures.append(f".codex/hooks.json: matcher {matcher} hooks must be a list")
            continue
        if hooks_for_matcher != expected_hooks:
            failures.append(
                f".codex/hooks.json: matcher {matcher} must exactly match expected PreToolUse handler matrix"
            )
        for index, hook in enumerate(hooks_for_matcher):
            if sorted(hook.keys()) != EXPECTED_HOOK_KEYS:
                failures.append(
                    f".codex/hooks.json: matcher {matcher} hook {index} keys must be exactly {EXPECTED_HOOK_KEYS!r}"
                )

    all_commands: list[str] = []
    for event_name in expectations["required_hook_events"]:
        hooks_for_event = hooks_root.get(event_name, [])
        for entry in hooks_for_event:
            for hook in entry.get("hooks", []):
                command = hook.get("command")
                if isinstance(command, str):
                    all_commands.append(command)

    if not any(hook_command_fragment in command for command in all_commands):
        failures.append(
            ".codex/hooks.json: expected hooks to route through scripts/check-codex-agents.mjs"
        )

    if not any("rtk pnpm exec node" in command for command in all_commands):
        failures.append(".codex/hooks.json: hooks must invoke the validator through rtk pnpm exec node")
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
    AC17: Validate startup preflight for local_main_branch_guard.

    Checks:
    1. check_local_main_branch_state.py exists (startup preflight script)
    2. .codex/rules/default.rules documents startup preflight gate (B5)
    3. .codex/hooks.json has local_main_branch_guard in PreToolUse and PermissionRequest
    4. No double-definition of local_main_branch_guard per event/matcher
    5. Handler form is nested under hooks[] (not at matcher group level)
    """
    failures: list[str] = []

    # Check 1: startup preflight script exists
    preflight_script = REPO_ROOT / "scripts" / "check_local_main_branch_state.py"
    if not preflight_script.exists():
        failures.append(
            "scripts/check_local_main_branch_state.py: startup preflight script missing "
            "(required for local_main_branch_guard — Codex PreToolUse is not a complete interception boundary)"
        )

    # B5: Check 2: .codex/rules/default.rules must document the startup preflight gate
    if not CODEX_RULES_DEFAULT_PATH.exists():
        failures.append(
            f"{CODEX_RULES_DEFAULT_PATH.relative_to(REPO_ROOT)}: rules file missing — "
            "startup preflight gate must be documented here"
        )
    else:
        rules_text = CODEX_RULES_DEFAULT_PATH.read_text(encoding="utf-8")
        if REQUIRED_PREFLIGHT_GATE_CMD not in rules_text:
            failures.append(
                f".codex/rules/default.rules: startup preflight gate not documented — "
                f"must contain: {REQUIRED_PREFLIGHT_GATE_CMD!r}"
            )

    hooks_root = hooks.get("hooks", {})

    # Check 2 & 4: local_main_branch_guard in PreToolUse Bash matcher
    pretool = hooks_root.get("PreToolUse", [])
    bash_pretool = next((e for e in pretool if e.get("matcher") == "^Bash$"), None)
    if bash_pretool is None:
        failures.append(".codex/hooks.json: missing PreToolUse ^Bash$ matcher entry for local_main_branch_guard")
    else:
        # Check handler is nested under hooks[] (not at matcher level)
        nested_hooks = bash_pretool.get("hooks", [])
        if not isinstance(nested_hooks, list):
            failures.append(".codex/hooks.json: PreToolUse ^Bash$ hooks must be a list (nested handler form)")
        else:
            guard_hooks = [h for h in nested_hooks if "local_main_branch_guard" in h.get("command", "")]
            if not guard_hooks:
                failures.append(
                    ".codex/hooks.json: local_main_branch_guard not found in PreToolUse ^Bash$ hooks[] "
                    "(startup preflight not registered)"
                )
            # Check 3: no double-definition
            if len(guard_hooks) > 1:
                failures.append(
                    f".codex/hooks.json: local_main_branch_guard defined {len(guard_hooks)} times "
                    "in PreToolUse ^Bash$ — must not be duplicated"
                )

    # Check 2 & 4: local_main_branch_guard in PermissionRequest Bash matcher
    perm_req = hooks_root.get("PermissionRequest", [])
    bash_perm = next((e for e in perm_req if e.get("matcher") == "^Bash$"), None)
    if bash_perm is None:
        failures.append(".codex/hooks.json: missing PermissionRequest ^Bash$ matcher entry for local_main_branch_guard")
    else:
        nested_hooks = bash_perm.get("hooks", [])
        if not isinstance(nested_hooks, list):
            failures.append(".codex/hooks.json: PermissionRequest ^Bash$ hooks must be a list (nested handler form)")
        else:
            guard_hooks = [h for h in nested_hooks if "local_main_branch_guard" in h.get("command", "")]
            if not guard_hooks:
                failures.append(
                    ".codex/hooks.json: local_main_branch_guard not found in PermissionRequest ^Bash$ hooks[]"
                )
            if len(guard_hooks) > 1:
                failures.append(
                    f".codex/hooks.json: local_main_branch_guard defined {len(guard_hooks)} times "
                    "in PermissionRequest ^Bash$ — must not be duplicated"
                )

    return failures

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assert-required-fields", action="store_true")
    parser.add_argument("--assert-runtime-contract", action="store_true")
    parser.add_argument("--assert-local-main-branch-guard", action="store_true",
                        help="Validate local_main_branch_guard startup preflight (AC17)")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    expectations = load_expectations()
    failures: list[str] = []
    if not args.assert_required_fields and not args.assert_runtime_contract and not args.assert_local_main_branch_guard:
        parser.error("specify at least one assertion flag")
    if args.assert_required_fields:
        failures.extend(assert_required_fields(expectations))
    if args.assert_runtime_contract:
        failures.extend(assert_runtime_contract(expectations))
    if args.assert_local_main_branch_guard:
        hooks = json.loads(HOOKS_PATH.read_text(encoding="utf-8"))
        failures.extend(assert_local_main_branch_guard_preflight(hooks))
    if failures:
        for failure in failures:
            print(f"[FAIL] {failure}")
        return 1
    print("OK: Codex agent contract validation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
