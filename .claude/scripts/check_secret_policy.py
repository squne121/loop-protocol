#!/usr/bin/env python3
"""Structural checker for secret-policy.md (secret_policy/v1).

Validates:
1.  Fenced YAML block (```yaml) can be parsed with yaml.safe_load
2.  schema == secret_policy/v1
3.  current_secrets_mode in {none, publish_secret, app_secret, unknown}
4.  5 category headings present (current, publish_secret, app_runtime_secret,
    agent_local_secret, checkpoint_token)
5.  Each category section contains the 4 required keywords:
    現状 / 発生条件 / 取り扱い / 漏洩
6.  taxonomy_mapping section exists and contains none:, publish_secret:,
    app_secret:, unknown:
7.  CLAUDE.md and .claude/rules/project-constitution.md both contain
    'secret-policy'
8.  No VITE_*(SECRET|TOKEN|KEY|PASSWORD|PRIVATE) in src / .github / docs /
    package.json / vite.config.* (excluding secret-policy.md itself)

Usage:
    python3 .claude/scripts/check_secret_policy.py docs/dev/secret-policy.md
    python3 .claude/scripts/check_secret_policy.py \\
        docs/dev/secret-policy.md \\
        --claude-md CLAUDE.md \\
        --constitution .claude/rules/project-constitution.md

Exit codes:
    0 - all checks passed
    1 - one or more checks failed
    2 - file not found / argument error
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from typing import List, Tuple

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REQUIRED_CATEGORIES = [
    "current",
    "publish_secret",
    "app_runtime_secret",
    "agent_local_secret",
    "checkpoint_token",
]

REQUIRED_KEYWORDS = ["現状", "発生条件", "取り扱い", "漏洩"]

VALID_SECRETS_MODES = {"none", "publish_secret", "app_secret", "unknown"}

TAXONOMY_REQUIRED_KEYS = ["none:", "publish_secret:", "app_secret:", "unknown:"]

VITE_SENSITIVE_PATTERN = re.compile(r"VITE_[A-Za-z0-9_]*(SECRET|TOKEN|KEY|PASSWORD|PRIVATE)")


def read_file(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except OSError:
        return None


def extract_fenced_yaml_blocks(content: str) -> List[str]:
    """Return all content inside ```yaml ... ``` fences."""
    pattern = re.compile(r"```yaml\s*\n(.*?)```", re.DOTALL)
    return pattern.findall(content)


def find_section_text(content: str, heading_keyword: str) -> str:
    """Return the text under the first heading that contains heading_keyword,
    up to the next heading of the same or higher level."""
    lines = content.splitlines()
    in_section = False
    section_lines: List[str] = []
    heading_level = 0
    for line in lines:
        if not in_section:
            m = re.match(r"(#{1,6})\s+.*" + re.escape(heading_keyword), line)
            if m:
                in_section = True
                heading_level = len(m.group(1))
                section_lines.append(line)
        else:
            m2 = re.match(r"(#{1,6})\s+", line)
            if m2 and len(m2.group(1)) <= heading_level:
                break
            section_lines.append(line)
    return "\n".join(section_lines)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_yaml_parseable(content: str) -> Tuple[bool, str, dict]:
    blocks = extract_fenced_yaml_blocks(content)
    if not blocks:
        return False, "No fenced YAML block (```yaml) found", {}
    if not _YAML_AVAILABLE:
        return False, "PyYAML not installed; cannot parse YAML blocks", {}
    for i, block in enumerate(blocks):
        try:
            data = yaml.safe_load(block)
            if isinstance(data, dict):
                return True, f"YAML block #{i+1} parsed successfully", data
        except yaml.YAMLError as exc:
            return False, f"YAML block #{i+1} parse error: {exc}", {}
    return False, "No YAML block parsed to a dict", {}


def check_schema(data: dict) -> Tuple[bool, str]:
    schema = data.get("schema") or data.get("secret_policy", {}).get("schema") if isinstance(data.get("secret_policy"), dict) else data.get("schema")
    # Support top-level schema key or nested under secret_policy:
    if isinstance(data.get("secret_policy"), dict):
        schema = data["secret_policy"].get("schema")
    else:
        schema = data.get("schema")
    if schema == "secret_policy/v1":
        return True, "schema == secret_policy/v1"
    return False, f"schema field missing or wrong: {schema!r} (expected 'secret_policy/v1')"


def check_secrets_mode(data: dict) -> Tuple[bool, str]:
    if isinstance(data.get("secret_policy"), dict):
        mode = data["secret_policy"].get("current_secrets_mode")
    else:
        mode = data.get("current_secrets_mode")
    if mode in VALID_SECRETS_MODES:
        return True, f"current_secrets_mode = {mode!r}"
    return False, f"current_secrets_mode missing or invalid: {mode!r} (expected one of {VALID_SECRETS_MODES})"


def check_category_headings(content: str) -> Tuple[bool, str]:
    missing = []
    for cat in REQUIRED_CATEGORIES:
        # Accept ## or ### heading containing the category name
        if not re.search(r"#{2,3}\s+.*" + re.escape(cat), content):
            missing.append(cat)
    if missing:
        return False, f"Missing category headings: {missing}"
    return True, "All 5 category headings found"


def check_category_keywords(content: str) -> Tuple[bool, str]:
    failures = []
    for cat in REQUIRED_CATEGORIES:
        section = find_section_text(content, cat)
        missing_kw = [kw for kw in REQUIRED_KEYWORDS if kw not in section]
        if missing_kw:
            failures.append(f"{cat}: missing {missing_kw}")
    if failures:
        return False, f"Category keyword failures: {failures}"
    return True, "All categories contain required keywords"


def check_taxonomy_mapping(content: str) -> Tuple[bool, str]:
    if "taxonomy_mapping" not in content:
        return False, "taxonomy_mapping section not found"
    missing = [k for k in TAXONOMY_REQUIRED_KEYS if k not in content]
    if missing:
        return False, f"taxonomy_mapping missing keys: {missing}"
    return True, "taxonomy_mapping section present with all required keys"


def check_secret_policy_in_docs(claude_md: str, constitution: str) -> Tuple[bool, str]:
    problems = []
    c1 = read_file(claude_md)
    if c1 is None:
        problems.append(f"{claude_md}: file not found")
    elif "secret-policy" not in c1:
        problems.append(f"{claude_md}: 'secret-policy' not found")

    c2 = read_file(constitution)
    if c2 is None:
        problems.append(f"{constitution}: file not found")
    elif "secret-policy" not in c2:
        problems.append(f"{constitution}: 'secret-policy' not found")

    if problems:
        return False, "; ".join(problems)
    return True, "secret-policy referenced in CLAUDE.md and project-constitution.md"


def check_vite_sensitive(policy_path: str) -> Tuple[bool, str]:
    """Check that no VITE_*(SENSITIVE) pattern exists in source paths."""
    # Determine repo root from the policy file path
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(policy_path))))
    # Directories / files to scan
    scan_targets = ["src", ".github", "docs", "package.json"]
    glob_extra = ["vite.config.*"]
    exclude_file = os.path.abspath(policy_path)

    violations: List[str] = []
    for target in scan_targets:
        full_target = os.path.join(repo_root, target)
        if not os.path.exists(full_target):
            continue
        _scan_for_vite(full_target, exclude_file, violations)

    # Also scan vite.config.* files at repo root
    import glob as _glob
    for pattern in glob_extra:
        for f in _glob.glob(os.path.join(repo_root, pattern)):
            if os.path.abspath(f) != exclude_file and os.path.isfile(f):
                _scan_file_for_vite(f, violations)

    if violations:
        return False, f"VITE_ sensitive pattern found: {violations[:5]}"
    return True, "No VITE_*(SECRET|TOKEN|KEY|PASSWORD|PRIVATE) found in sources"


def _scan_for_vite(path: str, exclude: str, violations: List[str]) -> None:
    if os.path.isfile(path):
        if os.path.abspath(path) != exclude:
            _scan_file_for_vite(path, violations)
    elif os.path.isdir(path):
        for root, dirs, files in os.walk(path):
            # Skip node_modules
            dirs[:] = [d for d in dirs if d != "node_modules"]
            for fname in files:
                fp = os.path.join(root, fname)
                if os.path.abspath(fp) != exclude:
                    _scan_file_for_vite(fp, violations)


def _scan_file_for_vite(path: str, violations: List[str]) -> None:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for lineno, line in enumerate(f, 1):
                if VITE_SENSITIVE_PATTERN.search(line):
                    violations.append(f"{path}:{lineno}: {line.rstrip()}")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Structural checker for secret-policy.md")
    parser.add_argument("policy_path", help="Path to secret-policy.md")
    parser.add_argument(
        "--claude-md",
        default="CLAUDE.md",
        help="Path to CLAUDE.md (default: CLAUDE.md)",
    )
    parser.add_argument(
        "--constitution",
        default=".claude/rules/project-constitution.md",
        help="Path to project-constitution.md",
    )
    args = parser.parse_args()

    content = read_file(args.policy_path)
    if content is None:
        print(f"Error: file not found: {args.policy_path}", file=sys.stderr)
        return 2

    results: List[Tuple[str, bool, str]] = []

    # 1. YAML parseable
    ok, msg, yaml_data = check_yaml_parseable(content)
    results.append(("YAML parseable", ok, msg))

    # 2. schema == secret_policy/v1
    if ok:
        ok2, msg2 = check_schema(yaml_data)
    else:
        ok2, msg2 = False, "Skipped (YAML parse failed)"
    results.append(("schema == secret_policy/v1", ok2, msg2))

    # 3. current_secrets_mode valid
    if ok:
        ok3, msg3 = check_secrets_mode(yaml_data)
    else:
        ok3, msg3 = False, "Skipped (YAML parse failed)"
    results.append(("current_secrets_mode valid", ok3, msg3))

    # 4. Category headings
    ok4, msg4 = check_category_headings(content)
    results.append(("Category headings (5)", ok4, msg4))

    # 5. Category keywords
    ok5, msg5 = check_category_keywords(content)
    results.append(("Category keywords", ok5, msg5))

    # 6. taxonomy_mapping
    ok6, msg6 = check_taxonomy_mapping(content)
    results.append(("taxonomy_mapping section", ok6, msg6))

    # 7. secret-policy in CLAUDE.md + constitution
    ok7, msg7 = check_secret_policy_in_docs(args.claude_md, args.constitution)
    results.append(("secret-policy in governance docs", ok7, msg7))

    # 8. VITE_ sensitive check
    ok8, msg8 = check_vite_sensitive(args.policy_path)
    results.append(("No VITE_* sensitive in sources", ok8, msg8))

    # Print results
    all_pass = True
    for name, passed, message in results:
        status = "PASS" if passed else "FAIL"
        out = sys.stdout if passed else sys.stderr
        print(f"  [{status}] {name}: {message}", file=out)
        if not passed:
            all_pass = False

    if all_pass:
        print("\nAll checks passed.")
        return 0
    else:
        print("\nOne or more checks FAILED.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
