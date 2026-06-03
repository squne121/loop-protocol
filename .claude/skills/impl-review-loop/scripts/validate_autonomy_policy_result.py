#!/usr/bin/env python3
"""validate_autonomy_policy_result.py

AUTONOMY_POLICY_V1 に基づく終了結果検証器。

Usage:
    python3 validate_autonomy_policy_result.py \\
        --policy <path-to-autonomy-policy.md> \\
        --agent-dir <path-to-agents-dir> \\
        --terminal-output-file <path-to-loop-result-file>

Exit codes:
    0  - pass (IMPL_REVIEW_LOOP_RESULT_V1 marker found with valid YAML, policy checks pass)
    1  - blocked (marker missing, malformed YAML, required fields absent, or policy violation)

Scope boundary note:
    Read-only agent enforcement covers Edit/Write/MultiEdit direct tool access.
    Bash-based write paths (tee, redirects, inline Python) are a known residual risk
    not enforced by this validator. Use disallowedTools to restrict Bash if needed.
    This validator treats Bash access as non-controlled with respect to write-path
    enforcement for read-only agents.
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

import yaml


# Marker that must appear in terminal output as an HTML comment
REQUIRED_MARKER = "IMPL_REVIEW_LOOP_RESULT_V1"
REQUIRED_MARKER_COMMENT = f"<!-- {REQUIRED_MARKER} -->"

# Required fields in the IMPL_REVIEW_LOOP_RESULT_V1 YAML block
REQUIRED_RESULT_FIELDS = {"schema_version", "status", "termination_reason", "merge_ready"}

# Write-capable tools (any of these in a read-only agent = violation)
WRITE_TOOLS = {"Edit", "Write", "MultiEdit"}


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


def extract_policy_yaml(policy_file: Path) -> Optional[dict]:
    """Parse the fenced YAML AUTONOMY_POLICY_V1 block from the policy markdown file.

    Looks for a fenced code block starting with ```yaml that contains AUTONOMY_POLICY_V1:.
    Returns the parsed dict or None on failure.
    """
    if not policy_file.exists():
        return None

    content = policy_file.read_text(encoding="utf-8")

    # Find all fenced YAML blocks
    pattern = re.compile(r"```yaml\n(.*?)```", re.DOTALL)
    for match in pattern.finditer(content):
        block_text = match.group(1)
        if "AUTONOMY_POLICY_V1:" in block_text:
            try:
                parsed = yaml.safe_load(block_text)
                if isinstance(parsed, dict) and "AUTONOMY_POLICY_V1" in parsed:
                    return parsed["AUTONOMY_POLICY_V1"]
            except yaml.YAMLError:
                return None

    return None


def check_marker(terminal_output_file: Path) -> tuple[bool, str, bool]:
    """Check that the terminal output file contains the IMPL_REVIEW_LOOP_RESULT_V1 marker.

    The marker must appear as <!-- IMPL_REVIEW_LOOP_RESULT_V1 --> (HTML comment)
    followed by a fenced YAML block with required fields.

    For backward compatibility, also accepts the marker as a YAML key inside a
    fenced block (e.g., IMPL_REVIEW_LOOP_RESULT_V1: ...).

    Returns (passed, reason, found_bool).
    """
    if not terminal_output_file.exists():
        return False, f"Terminal output file not found: {terminal_output_file}", False

    content = terminal_output_file.read_text(encoding="utf-8")

    # Check for HTML comment marker followed by fenced YAML
    if REQUIRED_MARKER_COMMENT in content:
        # Find fenced YAML block after the marker comment
        comment_pos = content.index(REQUIRED_MARKER_COMMENT)
        after_comment = content[comment_pos + len(REQUIRED_MARKER_COMMENT):]
        yaml_block_match = re.search(r"```yaml\n(.*?)```", after_comment, re.DOTALL)
        if yaml_block_match:
            yaml_text = yaml_block_match.group(1)
            try:
                parsed = yaml.safe_load(yaml_text)
            except yaml.YAMLError as e:
                return (
                    False,
                    f"IMPL_REVIEW_LOOP_RESULT_V1 YAML block after marker comment is malformed: {e}",
                    True,
                )
            if not isinstance(parsed, dict):
                return (
                    False,
                    "IMPL_REVIEW_LOOP_RESULT_V1 YAML block is not a mapping",
                    True,
                )
            # Validate required fields (the YAML key may or may not be IMPL_REVIEW_LOOP_RESULT_V1)
            if REQUIRED_MARKER in parsed:
                result_data = parsed[REQUIRED_MARKER]
            else:
                result_data = parsed
            return _validate_result_fields(result_data)
        else:
            return (
                False,
                f"HTML comment marker '{REQUIRED_MARKER_COMMENT}' found but no fenced YAML block follows it",
                True,
            )

    # Also accept marker as YAML key inside a fenced code block (no HTML comment required
    # for this path, but bare substring match alone is not sufficient — we still need
    # to find and parse a proper fenced YAML block).
    yaml_pattern = re.compile(r"```yaml\n(.*?)```", re.DOTALL)
    for match in yaml_pattern.finditer(content):
        block_text = match.group(1)
        if REQUIRED_MARKER in block_text:
            try:
                parsed = yaml.safe_load(block_text)
            except yaml.YAMLError as e:
                return (
                    False,
                    f"Fenced YAML block containing '{REQUIRED_MARKER}' is malformed: {e}",
                    True,
                )
            if not isinstance(parsed, dict):
                return False, "YAML block containing marker is not a mapping", True
            if REQUIRED_MARKER in parsed:
                result_data = parsed[REQUIRED_MARKER]
                return _validate_result_fields(result_data)
            # Marker key is not top-level — treat as not found
            break

    return (
        False,
        (
            f"Required marker '{REQUIRED_MARKER}' not found as HTML comment "
            f"('{REQUIRED_MARKER_COMMENT}') or as top-level YAML key in a fenced block. "
            "Freeform prose embedding the string is not sufficient."
        ),
        False,
    )


def _validate_result_fields(result_data: object) -> tuple[bool, str, bool]:
    """Validate required fields in the IMPL_REVIEW_LOOP_RESULT_V1 data dict."""
    if not isinstance(result_data, dict):
        return (
            False,
            f"IMPL_REVIEW_LOOP_RESULT_V1 value is not a mapping (got {type(result_data).__name__})",
            True,
        )
    missing = REQUIRED_RESULT_FIELDS - result_data.keys()
    if missing:
        return (
            False,
            f"IMPL_REVIEW_LOOP_RESULT_V1 missing required fields: {sorted(missing)}",
            True,
        )
    return True, "", True


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
    and that disallowedTools includes write tools.

    Scope: This check covers Edit/Write/MultiEdit direct tool access only.
    Bash-based write paths (tee, redirects, inline Python) are a known residual
    risk not enforced here. Use disallowedTools to restrict Bash if needed.
    """
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


def check_write_capable_agent_policy(
    agent_file: Path, policy_data: Optional[dict]
) -> tuple[bool, str]:
    """Check that a write-capable agent has role_category + justification in the policy.

    Parses the AUTONOMY_POLICY_V1 write_capable_agents list and finds the specific
    entry for this agent. Does NOT do whole-document string search — each agent must
    have its own role_category and non-empty justification.
    """
    if policy_data is None:
        return False, "Could not parse AUTONOMY_POLICY_V1 YAML block from policy file"

    agent_ref = f".claude/agents/{agent_file.name}"
    write_capable = policy_data.get("write_capable_agents", [])

    if not isinstance(write_capable, list):
        return False, "Policy write_capable_agents is not a list"

    # Find the specific entry for this agent
    for entry in write_capable:
        if not isinstance(entry, dict):
            continue
        if entry.get("agent") == agent_ref:
            # Verify role_category is present and non-empty
            role_category = entry.get("role_category", "")
            if not role_category or not str(role_category).strip():
                return (
                    False,
                    f"Write-capable agent '{agent_ref}' has missing or empty 'role_category' in policy",
                )
            # Verify justification is present and non-empty
            justification = entry.get("justification", "")
            if not justification or not str(justification).strip():
                return (
                    False,
                    f"Write-capable agent '{agent_ref}' has missing or empty 'justification' in policy",
                )
            return True, ""

    return (
        False,
        f"Write-capable agent '{agent_ref}' not found in policy write_capable_agents list",
    )


def run_checks(
    policy_file: Path,
    agent_dir: Path,
    terminal_output_file: Path,
) -> tuple[bool, list[str], dict]:
    """Run all policy checks. Returns (passed, list_of_blocked_reasons, ac_summary)."""
    blocked_reasons: list[str] = []

    # Parse policy YAML once
    policy_data = extract_policy_yaml(policy_file)

    # Derive agent lists from policy (fall back to empty lists if policy unreadable)
    checked_subagents: list[str] = []
    read_only_agent_names: set[str] = set()
    write_capable_agent_names: set[str] = set()

    if policy_data is not None:
        # checked_subagents from policy subagent_security_ac
        ac = policy_data.get("subagent_security_ac", {})
        if isinstance(ac, dict):
            for path in ac.get("checked_subagents", []):
                checked_subagents.append(str(path))

        # read_only_agents from policy
        for entry in policy_data.get("read_only_agents", []):
            if isinstance(entry, dict) and "agent" in entry:
                # Extract filename from path like .claude/agents/test-runner.md
                fname = Path(entry["agent"]).name
                read_only_agent_names.add(fname)

        # write_capable_agents from policy
        for entry in policy_data.get("write_capable_agents", []):
            if isinstance(entry, dict) and "agent" in entry:
                fname = Path(entry["agent"]).name
                write_capable_agent_names.add(fname)
    else:
        blocked_reasons.append(f"Could not parse AUTONOMY_POLICY_V1 YAML block from policy file: {policy_file}")

    # 1. Check terminal output marker
    marker_ok, marker_reason, marker_found = check_marker(terminal_output_file)
    if not marker_ok:
        blocked_reasons.append(marker_reason)

    # AC tracking
    explicit_tools_ok_all = True
    read_only_clear_all = True
    write_capable_justified_all = True

    # 2. Check each agent
    all_agent_names = list(read_only_agent_names | write_capable_agent_names)

    for agent_name in sorted(all_agent_names):
        agent_file = agent_dir / agent_name

        # 2a. Explicit tools declared
        tools_ok, tools_reason = check_explicit_tools(agent_file)
        if not tools_ok:
            blocked_reasons.append(tools_reason)
            explicit_tools_ok_all = False

        # 2b. Read-only agents must not have write tools
        if agent_name in read_only_agent_names:
            ro_ok, ro_reason = check_read_only_agent(agent_file)
            if not ro_ok:
                blocked_reasons.append(ro_reason)
                read_only_clear_all = False

        # 2c. Write-capable agents must have justification in policy
        if agent_name in write_capable_agent_names:
            wc_ok, wc_reason = check_write_capable_agent_policy(agent_file, policy_data)
            if not wc_ok:
                blocked_reasons.append(wc_reason)
                write_capable_justified_all = False

    ac_summary = {
        "checked_subagents": checked_subagents,
        "read_only_agents_clear": read_only_clear_all,
        "write_capable_agents_have_justification": write_capable_justified_all,
        "explicit_tools_declared": explicit_tools_ok_all,
    }

    return len(blocked_reasons) == 0, blocked_reasons, ac_summary


def main() -> int:
    args = parse_args()

    policy_file = Path(args.policy)
    agent_dir = Path(args.agent_dir)
    terminal_output_file = Path(args.terminal_output_file)

    passed, blocked_reasons, ac_summary = run_checks(policy_file, agent_dir, terminal_output_file)

    # Determine marker found status for result output
    if terminal_output_file.exists():
        _marker_ok, _reason, marker_found = check_marker(terminal_output_file)
    else:
        marker_found = False

    # Output validation result YAML
    result: dict = {
        "AUTONOMY_POLICY_VALIDATION_RESULT_V1": {
            "schema_version": 1,
            "policy_ref": str(policy_file),
            "status": "pass" if passed else "blocked",
            "terminal_result_marker": {
                "expected": REQUIRED_MARKER,
                "found": marker_found,
            },
            "subagent_security_ac": ac_summary,
            "blocked_reasons": blocked_reasons,
        }
    }

    print(yaml.dump(result, allow_unicode=True, default_flow_style=False, sort_keys=False), end="")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
