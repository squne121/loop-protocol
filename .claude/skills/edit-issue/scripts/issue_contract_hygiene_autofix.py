#!/usr/bin/env python3
"""
issue_contract_hygiene_autofix.py

Deterministic autofix for trivial format blockers in Issue contract bodies.

Supported repairs:
  C4: Add $ prefix to command lines in fenced bash blocks within Verification Commands section
  C9: Insert ## Runtime Verification Applicability section with decision: not_applicable
      when section is missing and all Allowed Paths are non-runtime

Exit codes:
  0: Repairs applied (body changed)
  1: No repairs needed (body unchanged, including sha256 no_change)
  2: Non-trivial blockers detected or autofixable judgment not possible

Usage:
  python3 issue_contract_hygiene_autofix.py [--body-file <path>] [--out-file <path>]
  cat body.md | python3 issue_contract_hygiene_autofix.py
"""

import argparse
import hashlib
import re
import sys
from typing import Optional


# Paths considered "non-runtime" (workflow/docs/scripts only, no product runtime)
NON_RUNTIME_PATH_PREFIXES = (
    ".claude/",
    "docs/",
    ".github/",
    "scripts/",
)

# Paths that indicate product runtime files (C9 autofix not safe)
RUNTIME_PATH_PREFIXES = (
    "src/",
    "assets/",
    "LICENSES/",
    "public/",
    "dist/",
    "tests/",  # product tests (not .claude/skills/*/tests/)
)


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_section_lines(lines: list[str], heading: str) -> tuple[int, int]:
    """Return (start_line_index, end_line_index_exclusive) for a ## heading section."""
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == heading:
            start = i
            break
    if start is None:
        return (-1, -1)
    for i in range(start + 1, len(lines)):
        if re.match(r"^## ", lines[i]):
            return (start, i)
    return (start, len(lines))


def is_runtime_path(path: str) -> bool:
    """Return True if path indicates a product runtime file."""
    path = path.strip().lstrip("- ").strip("`")
    # If it starts with .claude/skills/*/tests/ it's workflow test (non-runtime)
    if re.match(r"\.claude/skills/[^/]+/tests/", path):
        return False
    for prefix in RUNTIME_PATH_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def parse_allowed_paths(lines: list[str]) -> Optional[list[str]]:
    """Extract allowed paths from ## Allowed Paths section. Returns None if section missing."""
    start, end = extract_section_lines(lines, "## Allowed Paths")
    if start == -1:
        return None
    paths = []
    for i in range(start + 1, end):
        line = lines[i].strip()
        if line.startswith("- "):
            path = line[2:].strip().strip("`")
            paths.append(path)
    return paths


def all_paths_non_runtime(paths: list[str]) -> bool:
    """Return True if all allowed paths are non-runtime."""
    for p in paths:
        if is_runtime_path(p):
            return False
    return True


def has_runtime_verification_section(lines: list[str]) -> bool:
    """Return True if ## Runtime Verification Applicability section exists."""
    for line in lines:
        if line.rstrip() == "## Runtime Verification Applicability":
            return True
    return False


def find_delivery_rule_line(lines: list[str]) -> int:
    """Return the line index of ## Delivery Rule, or -1 if not found."""
    for i, line in enumerate(lines):
        if line.rstrip() == "## Delivery Rule":
            return i
    return -1


def repair_c9(lines: list[str]) -> tuple[list[str], bool]:
    """
    C9 repair: Insert ## Runtime Verification Applicability section with
    decision: not_applicable when:
    - Section is missing
    - All Allowed Paths are non-runtime (or no Allowed Paths section but safe to assume)

    Returns (new_lines, repaired).
    """
    if has_runtime_verification_section(lines):
        return lines, False

    allowed_paths = parse_allowed_paths(lines)
    if allowed_paths is None:
        # No Allowed Paths section — cannot safely auto-classify
        return lines, False

    if not all_paths_non_runtime(allowed_paths):
        # Contains runtime paths — not safe to auto-insert not_applicable
        return lines, False

    rva_block = [
        "## Runtime Verification Applicability\n",
        "本 Issue の変更対象はワークフロー文書・スクリプトのみです。ゲームの実行時動作には影響しません。\n",
        "\n",
        "```yaml\n",
        "decision: not_applicable\n",
        'reason: "Workflow documentation and script changes only. No runtime game behavior changed."\n',
        "```\n",
        "\n",
    ]

    delivery_rule_idx = find_delivery_rule_line(lines)
    if delivery_rule_idx != -1:
        new_lines = lines[:delivery_rule_idx] + rva_block + lines[delivery_rule_idx:]
    else:
        # Append at the end (before last blank line if present)
        new_lines = lines + ["\n"] + rva_block

    return new_lines, True


# Patterns for C4 repair (fenced bash block command line detection)
# A line is a "command line" (needing $ prefix) if:
#   - Not empty
#   - Not a comment line (starts with #)
#   - Not a continuation line (previous non-empty line ends with \)
#   - Not already prefixed with $
#   - Not a shell variable expression at line start (e.g. VAR=..., $VAR)
#   - Not prose (markdown text outside the bash block)
#   - Not a heredoc content line (inside EOF block)


def is_shell_variable_expression(line: str) -> bool:
    """Return True if line starts with a shell variable assignment or expression."""
    stripped = line.strip()
    # Variable assignment: VAR=value or VAR ="value"
    if re.match(r'^[A-Z_][A-Z0-9_]*=', stripped):
        return True
    # Shell variable expansion at start: $VAR, ${VAR}
    if re.match(r'^\$[A-Z_({]', stripped):
        return True
    return False


def repair_c4_in_vc_block(lines: list[str]) -> tuple[list[str], bool]:
    """
    C4 repair: Add $ prefix to command lines in fenced bash blocks within
    the ## Verification Commands section.

    Returns (new_lines, repaired).
    """
    vc_start, vc_end = extract_section_lines(lines, "## Verification Commands")
    if vc_start == -1:
        return lines, False

    new_lines = lines[:]
    repaired = False

    i = vc_start + 1
    while i < vc_end:
        line = new_lines[i]
        # Detect start of fenced bash block
        if re.match(r'^```bash\s*$', line):
            block_start = i
            i += 1
            in_heredoc = False
            heredoc_delimiter: Optional[str] = None
            prev_line_continuation = False

            while i < vc_end:
                bline = new_lines[i]
                # End of fenced block
                if re.match(r'^```\s*$', bline):
                    break

                stripped = bline.rstrip('\n')

                # Track heredoc state
                if in_heredoc:
                    # Check for heredoc end
                    if heredoc_delimiter and stripped.strip() == heredoc_delimiter:
                        in_heredoc = False
                        heredoc_delimiter = None
                    # Lines inside heredoc are not command lines
                    prev_line_continuation = False
                    i += 1
                    continue

                # Check for heredoc start
                heredoc_match = re.search(r"<<['\"]?(\w+)['\"]?", stripped)
                if heredoc_match:
                    # This line starts a heredoc; next lines until delimiter are heredoc content
                    pass  # We'll handle the flag after determining if we prefix this line

                # Empty line
                if not stripped.strip():
                    prev_line_continuation = False
                    i += 1
                    continue

                # Comment line
                if stripped.strip().startswith('#'):
                    prev_line_continuation = False
                    i += 1
                    continue

                # Continuation line (previous ended with \)
                if prev_line_continuation:
                    # This is a continuation line, not a command start
                    prev_line_continuation = stripped.endswith('\\')
                    i += 1
                    continue

                # Already has $ prefix
                if stripped.strip().startswith('$'):
                    prev_line_continuation = stripped.rstrip().endswith('\\')
                    if heredoc_match:
                        in_heredoc = True
                        heredoc_delimiter = heredoc_match.group(1)
                    i += 1
                    continue

                # Shell variable expression (VAR=... at line start)
                if is_shell_variable_expression(stripped.strip()):
                    prev_line_continuation = stripped.rstrip().endswith('\\')
                    if heredoc_match:
                        in_heredoc = True
                        heredoc_delimiter = heredoc_match.group(1)
                    i += 1
                    continue

                # This is a command line — add $ prefix
                # Preserve leading whitespace
                leading = len(stripped) - len(stripped.lstrip())
                indent = stripped[:leading]
                command_part = stripped[leading:]
                new_lines[i] = indent + '$ ' + command_part + '\n'
                repaired = True

                prev_line_continuation = stripped.rstrip().endswith('\\')
                if heredoc_match:
                    in_heredoc = True
                    heredoc_delimiter = heredoc_match.group(1)
                i += 1
            # After the closing ```
            i += 1
            continue
        i += 1

    return new_lines, repaired


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic autofix for trivial format blockers in Issue contract bodies"
    )
    parser.add_argument("--body-file", help="Input body file path (default: stdin)")
    parser.add_argument("--out-file", help="Output file path (default: stdout)")
    args = parser.parse_args()

    # Read input
    if args.body_file:
        try:
            with open(args.body_file, "r", encoding="utf-8") as f:
                original_body = f.read()
        except OSError as e:
            print(f"[ERROR] Cannot read body file: {e}", file=sys.stderr)
            return 2
    else:
        original_body = sys.stdin.read()

    original_sha256 = sha256_of(original_body)
    lines = original_body.splitlines(keepends=True)

    # Apply C4 repair
    lines, c4_repaired = repair_c4_in_vc_block(lines)

    # Apply C9 repair
    lines, c9_repaired = repair_c9(lines)

    new_body = "".join(lines)
    new_sha256 = sha256_of(new_body)

    # sha256 guard: if body unchanged, return exit 1
    if original_sha256 == new_sha256:
        result = {
            "status": "no_change",
            "c4_repaired": False,
            "c9_repaired": False,
            "original_sha256": original_sha256,
            "new_sha256": new_sha256,
        }
        print(f"status: no_change", file=sys.stderr)
        return 1

    # Write output
    if args.out_file:
        try:
            with open(args.out_file, "w", encoding="utf-8") as f:
                f.write(new_body)
        except OSError as e:
            print(f"[ERROR] Cannot write output file: {e}", file=sys.stderr)
            return 2
    else:
        sys.stdout.write(new_body)

    print(
        f"status: repaired  c4={c4_repaired}  c9={c9_repaired}  "
        f"original_sha256={original_sha256[:16]}...  new_sha256={new_sha256[:16]}...",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
