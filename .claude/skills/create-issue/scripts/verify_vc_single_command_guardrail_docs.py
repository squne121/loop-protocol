#!/usr/bin/env python3
"""
verify_vc_single_command_guardrail_docs.py

AC2 / AC5 / AC6 checker: fenced code blocks (```bash / ```sh) 内の
compound shell operator 違反を検出する。

対象ファイル:
  - .claude/skills/create-issue/references/body-authoring.md
  - .claude/skills/create-issue/SKILL.md

使い方:
  uv run python3 .claude/skills/create-issue/scripts/verify_vc_single_command_guardrail_docs.py --strict

終了コード:
  0 = 違反なし (PASS)
  1 = 違反あり (FAIL) [--strict 時]
"""

import argparse
import re
import sys
from pathlib import Path


# Shell control operators that constitute a violation.
# Checked OUTSIDE of quoted string contexts.
# Note: redirect operators (<, >, <<, >>, <<<) are intentionally excluded
# because angle-bracket placeholders (e.g. <file>, <pattern>) would cause
# false positives in documentation examples.
OPERATOR_CHECKS = [
    # && — short-circuit AND
    (r"&&", "&&"),
    # || — short-circuit OR; detect || not part of a longer word
    (r"\|\|", "||"),
    # | — pipe; detect | not part of || (i.e. not preceded/followed by |)
    (r"(?<!\|)\|(?!\|)", "|"),
    # ; — sequential execution
    (r";", ";"),
    # & — background execution; detect single & not part of &&
    (r"(?<![&])&(?![&])", "&"),
]

# Lines that start with # (comments) are excluded from operator checks
# because comment lines document what will happen, not actual commands.
COMMENT_LINE_RE = re.compile(r"^\s*#")


def _remove_quoted_strings(line: str) -> str:
    """
    Remove single-quoted and double-quoted string literals from a line
    so that operators inside them are not flagged.

    Uses simplified regex substitution (no nested quotes).
    Sufficient for VC example detection in Markdown docs.
    """
    # Remove double-quoted strings
    line = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
    # Remove single-quoted strings
    line = re.sub(r"'(?:[^'\\]|\\.)*'", "''", line)
    return line


def _is_operator_violation(line: str) -> tuple:
    """
    Check if a line contains a shell control operator outside quoted strings.
    Returns (violated: bool, operator_found: str).
    """
    # Skip comment lines
    if COMMENT_LINE_RE.match(line):
        return False, ""

    stripped = _remove_quoted_strings(line)
    for pattern, label in OPERATOR_CHECKS:
        if re.search(pattern, stripped):
            return True, label
    return False, ""


def _extract_fenced_blocks(content: str) -> list:
    """
    Extract lines within fenced code blocks tagged as bash or sh.
    Returns list of (line_number, line_content) tuples.
    """
    lines = content.splitlines()
    result = []
    in_block = False

    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not in_block:
            if re.match(r"^```\s*(bash|sh)\s*$", stripped):
                in_block = True
        else:
            if stripped == "```":
                in_block = False
            else:
                result.append((i, line))

    return result


def check_file(path: Path) -> list:
    """
    Check a single file for compound shell violations in fenced bash/sh blocks.
    Returns list of (filepath, line_number, line_content, operator).
    """
    violations = []
    content = path.read_text(encoding="utf-8")
    blocks = _extract_fenced_blocks(content)
    for lineno, line in blocks:
        violated, op = _is_operator_violation(line)
        if violated:
            violations.append((str(path), lineno, line.rstrip(), op))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify VC_SINGLE_COMMAND_GUARDRAIL: no compound shell operators in fenced code blocks."
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 on any violation.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root path (default: CWD).",
    )
    args = parser.parse_args()

    repo_root = args.repo_root or Path.cwd()

    target_files = [
        repo_root / ".claude/skills/create-issue/references/body-authoring.md",
        repo_root / ".claude/skills/create-issue/SKILL.md",
    ]

    all_violations = []

    for fpath in target_files:
        if not fpath.exists():
            print("[WARN] File not found (skipped): {}".format(fpath), file=sys.stderr)
            continue
        violations = check_file(fpath)
        all_violations.extend(violations)

    if all_violations:
        print("[FAIL] {} compound shell violation(s) detected:\n".format(len(all_violations)))
        for filepath, lineno, line, op in all_violations:
            print("  {}:{}: operator={!r}".format(filepath, lineno, op))
            print("    {}".format(line))
            print()
        if args.strict:
            return 1
        return 0
    else:
        print("[PASS] No compound shell violations detected in fenced code blocks.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
