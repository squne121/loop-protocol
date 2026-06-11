#!/usr/bin/env python3
"""
check_agent_friendly_stdout.py - CI linter for agent-friendly stdout compliance.

Checks that a stdout fixture (file or stdin) complies with compact agent output rules:
- UTF-8 byte count <= --max-bytes (default 2048)
- No raw diff (diff --git / @@ hunk markers)
- No raw log (Traceback / npm ERR! / ANSI escape sequences)
- No large code fences (>= 50 lines in a single fence block)

exit codes: 0=pass, 1=fail (one or more violations), 2=error (file not found / parse error)

stdout: one line per violation, or "PASS" if compliant.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Violation patterns
# ---------------------------------------------------------------------------

# Raw diff markers
RAW_DIFF_PATTERNS = [
    re.compile(r"^diff --git ", re.MULTILINE),
    re.compile(r"^@@ -\d+", re.MULTILINE),
]

# Raw log / error markers
RAW_LOG_PATTERNS = [
    re.compile(r"^Traceback \(most recent call last\)", re.MULTILINE),
    re.compile(r"^npm ERR!", re.MULTILINE),
]

# ANSI escape sequences
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*[mGKHF]")

# Large code fence: detect ``` blocks with >= 50 lines
CODE_FENCE_PATTERN = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
MAX_FENCE_LINES = 50


def check_stdout(text: str, max_bytes: int = 2048) -> list[str]:
    """
    Check text for agent-friendly stdout compliance.

    Returns a list of violation strings (empty = compliant).
    """
    violations: list[str] = []

    # 1. UTF-8 byte count
    byte_count = len(text.encode("utf-8"))
    if byte_count > max_bytes:
        violations.append(
            f"BYTE_LIMIT_EXCEEDED: {byte_count} UTF-8 bytes > {max_bytes} limit"
        )

    # 2. Raw diff markers
    for pattern in RAW_DIFF_PATTERNS:
        if pattern.search(text):
            violations.append(f"RAW_DIFF_DETECTED: pattern={pattern.pattern!r}")

    # 3. Raw log markers
    for pattern in RAW_LOG_PATTERNS:
        if pattern.search(text):
            violations.append(f"RAW_LOG_DETECTED: pattern={pattern.pattern!r}")

    # 4. ANSI escape sequences
    if ANSI_ESCAPE_PATTERN.search(text):
        violations.append("ANSI_ESCAPE_DETECTED: ANSI escape sequence found")

    # 5. Large code fences
    for match in CODE_FENCE_PATTERN.finditer(text):
        fence_content = match.group(1)
        line_count = fence_content.count("\n") + 1
        if line_count >= MAX_FENCE_LINES:
            violations.append(
                f"LARGE_CODE_FENCE: {line_count} lines in code fence (limit={MAX_FENCE_LINES})"
            )

    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check agent stdout for compliance (byte limit, no raw diff/log/ANSI)"
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        type=Path,
        default=None,
        help="Input file (default: stdin)",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=2048,
        help="Maximum UTF-8 byte count (default: 2048)",
    )
    args = parser.parse_args()

    # Read input
    try:
        if args.input_file:
            if not args.input_file.exists():
                print(f"ERROR: file not found: {args.input_file}", file=sys.stderr)
                return 2
            text = args.input_file.read_text(encoding="utf-8")
        else:
            text = sys.stdin.read()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    violations = check_stdout(text, max_bytes=args.max_bytes)

    if violations:
        for v in violations:
            print(f"FAIL: {v}")
        return 1
    else:
        print("PASS")
        return 0


if __name__ == "__main__":
    sys.exit(main())
