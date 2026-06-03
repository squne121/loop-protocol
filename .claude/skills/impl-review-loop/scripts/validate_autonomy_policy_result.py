#!/usr/bin/env python3
"""validate_autonomy_policy_result.py

AUTONOMY_POLICY_V1 に基づく終了結果検証器。

Usage:
    python3 validate_autonomy_policy_result.py \\
        --policy <path-to-autonomy-policy.md> \\
        --agent-dir <path-to-agents-dir> \\
        --terminal-output-file <path-to-loop-result-file>

Exit codes:
    0  - pass (IMPL_REVIEW_LOOP_RESULT_V1 marker found, policy checks pass)
    1  - blocked (marker missing, malformed YAML, or policy violation)
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

import yaml


# Marker that must appear in terminal output
REQUIRED_MARKER = "IMPL_REVIEW_LOOP_RESULT_V1"

# Write-capable tools (any of these in a read-only agent = violation)
WRITE_TOOLS = {"Edit", "Write", "MultiEdit"}

# Agents expected to be read-only (disallowedTools must include write tools)
READ_ONLY_AGENTS = {
    "test-runner.md",
    "pr-reviewer.md",
}

# Agents expected to be write-capable (must have justification in policy)
WRITE_CAPABLE_AGENTS = {
    "implementation-worker.md",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate AUTONOMY_POLICY_V1 compliance for impl-review-loop termination."
    )
    parser.add_argument(
        "--policy",
        required=True,
        help="Path to autonomy-policy.md (AUTONOMY_POLICY_V1 SSOT)",
    )
    parser.add_argument(
        "--agent-dir",
        required=True,
        help="Path to directory containing agent .md files (e.g. .claude/agents/)",
    )
    parser.add_argument(
        "--terminal-output-file",
        required=True,
        help="Path to file containing the terminal loop result (must include IMPL_REVIEW_LOOP_RESULT_V1 marker)",
    )
    return parser.parse_args()


def check_marker(terminal_output_file: Path) -> tuple[bool, str]:
    """Check that the terminal output file contains the IMPL_REVIEW_LOOP_RESULT_V1 marker."""
    if not terminal_output_file.exists():
        return False, f"Terminal output file not found: {terminal_output_file}"

    content = terminal_output_file.read_text(encoding="utf-8")
    if REQUIRED_MARKER in content:
        return True, ""
    return False, f"Required marker '{REQUIRED_MARKER}' not found in terminal output (freeform prose only)"


def parse_agent_frontmatter(agent_file: Path) -> Optional[dict]:
    """Parse YAML frontmatter from an agent .md file.

    Returns parsed dict or None if frontmatter is absent/malformed.
    """
    if not agent_file.exists():
        return None

    content = agent_file.read_text(encoding="utf-8")
    # YAML frontmatter is between first --- and second ---
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return None

    try:
        return yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return None


def check_explicit_tools(agent_file: Path) -> tuple[bool, str]:
    """Check that agent declares explicit tools list."""
    fm = parse_agent_frontmatter(agent_file)
    if fm is None:
        return False, f"{agent_file.name}: could not parse frontmatter"

    if "tools" not in fm:
        return False, f"{agent_file.name}: missing 'tools' declaration (explicit tools required)"

    tools = fm["tools"]
    if not isinstance(tools, list):
        return False, f"{agent_file.name}: 'tools' must be a list"

    return True, ""


def check_read_only_agent(agent_file: Path) -> tuple[bool, str]:
    """Check that a read-only agent does NOT have write tools declared in 'tools',
    and that disallowedTools includes write tools."""
    fm = parse_agent_frontmatter(agent_file)
    if fm is None:
        return False, f"{agent_file.name}: could not parse frontmatter"

    declared_tools = set(fm.get("tools", []))
    disallowed_tools = set(fm.get("disallowedTools", []))

    # Check that none of the write tools appear in the 'tools' list
    write_tools_in_tools = declared_tools & WRITE_TOOLS
    if write_tools_in_tools:
        return (
            False,
            f"{agent_file.name}: read-only agent has write tool(s) in 'tools': {sorted(write_tools_in_tools)}",
        )

    # Check that disallowedTools covers all write tools
    missing_from_disallowed = WRITE_TOOLS - disallowed_tools
    if missing_from_disallowed:
        return (
            False,
            f"{agent_file.name}: read-only agent missing write tool(s) in 'disallowedTools': {sorted(missing_from_disallowed)}",
        )

    return True, ""


def check_write_capable_agent_policy(agent_file: Path, policy_file: Path) -> tuple[bool, str]:
    """Check that a write-capable agent has role_category + justification in the policy."""
    if not policy_file.exists():
        return False, f"Policy file not found: {policy_file}"

    policy_content = policy_file.read_text(encoding="utf-8")

    agent_name = agent_file.name
    # We look for the agent path reference in policy content
    # The policy references agents as .claude/agents/<name>
    agent_ref = f".claude/agents/{agent_name}"

    if agent_ref not in policy_content:
        return (
            False,
            f"Write-capable agent '{agent_ref}' not found in policy write_capable_agents section",
        )

    # Check that both role_category and justification appear after the agent reference
    # We do a simple check: both strings appear in the policy document
    if "role_category" not in policy_content:
        return False, f"Policy missing 'role_category' for write-capable agent {agent_name}"

    if "justification" not in policy_content:
        return False, f"Policy missing 'justification' for write-capable agent {agent_name}"

    return True, ""


def run_checks(
    policy_file: Path,
    agent_dir: Path,
    terminal_output_file: Path,
) -> tuple[bool, list[str]]:
    """Run all policy checks. Returns (passed, list_of_blocked_reasons)."""
    blocked_reasons: list[str] = []

    # 1. Check terminal output marker
    marker_ok, marker_reason = check_marker(terminal_output_file)
    if not marker_ok:
        blocked_reasons.append(marker_reason)

    # 2. Check each agent
    all_agent_names = (
        list(READ_ONLY_AGENTS) + list(WRITE_CAPABLE_AGENTS)
    )

    for agent_name in all_agent_names:
        agent_file = agent_dir / agent_name

        # 2a. Explicit tools declared
        tools_ok, tools_reason = check_explicit_tools(agent_file)
        if not tools_ok:
            blocked_reasons.append(tools_reason)

        # 2b. Read-only agents must not have write tools
        if agent_name in READ_ONLY_AGENTS:
            ro_ok, ro_reason = check_read_only_agent(agent_file)
            if not ro_ok:
                blocked_reasons.append(ro_reason)

        # 2c. Write-capable agents must have justification in policy
        if agent_name in WRITE_CAPABLE_AGENTS:
            wc_ok, wc_reason = check_write_capable_agent_policy(agent_file, policy_file)
            if not wc_ok:
                blocked_reasons.append(wc_reason)

    return len(blocked_reasons) == 0, blocked_reasons


def main() -> int:
    args = parse_args()

    policy_file = Path(args.policy)
    agent_dir = Path(args.agent_dir)
    terminal_output_file = Path(args.terminal_output_file)

    passed, blocked_reasons = run_checks(policy_file, agent_dir, terminal_output_file)

    # Output validation result YAML
    result: dict = {
        "AUTONOMY_POLICY_VALIDATION_RESULT_V1": {
            "schema_version": 1,
            "policy_ref": str(policy_file),
            "status": "pass" if passed else "blocked",
            "terminal_result_marker": {
                "expected": REQUIRED_MARKER,
                "found": REQUIRED_MARKER in (terminal_output_file.read_text(encoding="utf-8") if terminal_output_file.exists() else ""),
            },
            "subagent_security_ac": {
                "checked_subagents": [
                    f".claude/agents/{n}" for n in sorted(list(READ_ONLY_AGENTS) + list(WRITE_CAPABLE_AGENTS))
                ],
            },
            "blocked_reasons": blocked_reasons,
        }
    }

    print(yaml.dump(result, allow_unicode=True, default_flow_style=False, sort_keys=False), end="")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
