#!/usr/bin/env python3
"""Governed Python invocation policy checker (Issue #1193).

Scans governed surfaces for dependency-bearing Python invocations that do not
comply with the project's uv run --locked policy, and for pytest invocations
that bypass the canonical uv run --locked pytest form.

Governed surfaces:
  - .github/workflows/**/*.yml and .github/actions/**/*.yml  (run: blocks)
  - docs/dev/**/*.md                                          (fenced code blocks)
  - .claude/skills/**/SKILL.md                               (fenced code blocks)
  - package.json                                             (scripts values)

Exclusions:
  - scripts/ci/fixtures/python_invocation_policy/**
  - scripts/ci/tests/test_python_invocation_policy.py
  - Markdown fenced blocks preceded by <!-- policy-example --> comment

Policy:
  ALLOWED:
    uv run --locked pytest <args>
    uv run --isolated --locked [--no-default-groups] python[3] <script.py> <args>
    uv run --locked python[3] <script.py> <args>
    python3 -  (heredoc stdin)         if in exceptions registry
    python3 -c <code>                  if in exceptions registry
    python3 <script.py>                if in exceptions registry
    python  <script.py>                if in exceptions registry

  VIOLATION:
    python -m pytest / python3 -m pytest
    uv run pytest (no --locked)
    uv run --locked python -m pytest / uv run --locked python3 -m pytest (AC2a)
    uv run --locked -- python -m pytest (AC2a)
    uv run python[3] <script.py> (no --locked)  (AC3a)
    python3 <script.py> (not in exceptions)
    python  <script.py> (not in exceptions)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIXTURE_PREFIX = "scripts/ci/fixtures/python_invocation_policy"
TEST_FILE_EXCL = "scripts/ci/tests/test_python_invocation_policy.py"
CHECKER_FILE_EXCL = "scripts/ci/check_python_invocation_policy.py"
EXCEPTIONS_PATH = "scripts/ci/python_invocation_policy_exceptions.json"

SURFACE_GLOBS = [
    ".github/workflows/**/*.yml",
    ".github/workflows/**/*.yaml",
    ".github/actions/**/*.yml",
    ".github/actions/**/*.yaml",
    "docs/dev/**/*.md",
    "package.json",
]

SKILL_MD_PATTERN = ".claude/skills/**/SKILL.md"

_RE_FENCE_OPEN = re.compile(r"^(\s*)(`{3,}|~{3,})\s*(?:\S+)?\s*$")
_RE_FENCE_CLOSE_PREFIX = re.compile(r"^(\s*)(`{3,}|~{3,})\s*$")
_RE_POLICY_EXAMPLE_COMMENT = re.compile(r"<!--\s*policy-example\s*-->", re.IGNORECASE)

# Heredoc detection in shell scripts
_RE_HEREDOC_START = re.compile(r"<<-?'?\"?([A-Za-z_][A-Za-z0-9_]*)'?\"?\s*$")

# Python command detection (conservative: matches start of a command)
_RE_PYTHON_CMD = re.compile(
    r"(?:^|(?<=[&|;(\s]))(?:uv\s+run|python3?)\s"
)


# ---------------------------------------------------------------------------
# Violation / Result types
# ---------------------------------------------------------------------------

@dataclass
class Violation:
    file: str
    line_num: int
    line_text: str
    violation_type: str
    suggestion: str | None = None


@dataclass
class CheckResult:
    violations: list[Violation] = field(default_factory=list)
    scanned_files: list[str] = field(default_factory=list)
    exceptions_loaded: int = 0
    surface_count: int = 0


# ---------------------------------------------------------------------------
# Exceptions registry
# ---------------------------------------------------------------------------

EXCEPTION_REQUIRED_FIELDS = ("exact_argv_pattern", "reason", "scope")
EXCEPTION_ALLOWED_SCOPES = ("stdlib_only", "bootstrap")


def validate_exceptions_schema(data: dict) -> list[str]:
    """Validate the exceptions registry against the AC4a/AC4c/AC4d schema.

    Each entry MUST contain string fields ``exact_argv_pattern`` and
    ``reason`` plus a ``scope`` restricted to ``stdlib_only`` | ``bootstrap``.
    Returns a list of human-readable error strings; an empty list means the
    registry is valid. Any non-empty result is fail-closed by callers.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["registry root must be a JSON object"]
    exceptions = data.get("exceptions")
    if not isinstance(exceptions, list):
        return ["registry must contain an 'exceptions' array"]
    for idx, entry in enumerate(exceptions):
        if not isinstance(entry, dict):
            errors.append(f"exceptions[{idx}] must be an object")
            continue
        for field_name in EXCEPTION_REQUIRED_FIELDS:
            if field_name not in entry:
                errors.append(f"exceptions[{idx}] missing required field '{field_name}'")
            elif field_name != "scope" and not isinstance(entry[field_name], str):
                errors.append(f"exceptions[{idx}].{field_name} must be a string")
            elif field_name != "scope" and not entry[field_name].strip():
                errors.append(f"exceptions[{idx}].{field_name} must be non-empty")
        scope = entry.get("scope")
        if "scope" in entry and scope not in EXCEPTION_ALLOWED_SCOPES:
            errors.append(
                f"exceptions[{idx}].scope must be one of {EXCEPTION_ALLOWED_SCOPES}, got {scope!r}"
            )
    return errors


def load_exceptions(repo_root: Path, *, validate: bool = True) -> list[dict]:
    """Load the exceptions registry from JSON.

    When ``validate`` is True (default) the registry is schema-validated and a
    ``ValueError`` is raised fail-closed on any violation (AC4c).
    """
    exc_path = repo_root / EXCEPTIONS_PATH
    if not exc_path.exists():
        return []
    data = json.loads(exc_path.read_text(encoding="utf-8"))
    if validate:
        errors = validate_exceptions_schema(data)
        if errors:
            raise ValueError(
                "invalid python_invocation_policy_exceptions.json:\n  "
                + "\n  ".join(errors)
            )
    return data.get("exceptions", [])


def _tokenize_pattern(pattern: str) -> list[str]:
    try:
        return shlex.split(pattern)
    except ValueError:
        return pattern.split()


def matches_exception(argv_tokens: list[str], exceptions: list[dict]) -> bool:
    """Return True if the invocation argv starts with an exception pattern."""
    for exc in exceptions:
        pattern_tokens = _tokenize_pattern(exc.get("exact_argv_pattern", ""))
        if not pattern_tokens:
            continue
        n = len(pattern_tokens)
        if argv_tokens[:n] == pattern_tokens:
            return True
    return False


# ---------------------------------------------------------------------------
# Invocation tokenisation / classification
# ---------------------------------------------------------------------------

def _extract_argv_from_line(line: str) -> list[str] | None:
    """Extract a potential Python invocation from a shell line.

    Returns the argv token list starting from python/uv, or None.
    """
    stripped = line.strip()
    # Skip comments
    if stripped.startswith("#"):
        return None
    # Strip common prefixes: `if !`, assignment prefix `VAR=$(`, etc.
    # We look for the first occurrence of known Python launchers
    for launcher in ("uv run", "uv  run", "python3 ", "python3\t", "python ", "python\t"):
        idx = stripped.find(launcher)
        if idx == -1:
            # also check at start with these patterns
            continue
        # Check if the character before launcher is a word char (avoid false match)
        if idx > 0 and stripped[idx - 1].isalnum():
            continue
        # Skip `uv python ...` (uv's python-management subcommand, e.g.
        # `uv python install`) which is NOT an interpreter invocation.
        if launcher.lstrip().startswith("python") and stripped[:idx].rstrip().endswith("uv"):
            continue
        candidate = stripped[idx:]
        # Stop at first heredoc marker or shell redirect
        # We want to capture just the command, not the redirect
        # Simple approach: stop at << or > or | (unless it's part of >>>)
        # We do NOT stop at >& since that's part of output redirect
        # For our purposes, just stop at <<
        heredoc_match = re.search(r"\s+<<", candidate)
        if heredoc_match:
            candidate = candidate[:heredoc_match.start()].strip()
        # Also stop at pipeline: but keep the invocation before the pipe
        # For policy checking, we just need the first command
        pipe_match = re.search(r"(?<!\|)\|(?!\|)", candidate)
        if pipe_match:
            candidate = candidate[:pipe_match.start()].strip()
        try:
            tokens = shlex.split(candidate)
        except ValueError:
            # shlex fails on e.g. process substitutions; fallback to split
            tokens = candidate.split()
        if not tokens:
            continue
        return tokens
    return None


def _is_locked_uv_run(tokens: list[str]) -> bool:
    """Check if tokens represent a `uv run --locked` invocation."""
    # tokens: ['uv', 'run', flags..., cmd, args...]
    # --locked must appear somewhere before the actual command
    if len(tokens) < 3:
        return False
    # Collect flags between 'run' and the first non-flag argument
    flags = []
    i = 2
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("-"):
            flags.append(t)
            i += 1
        elif t == "python" or t == "python3" or t == "pytest":
            break
        elif t == "--":
            # end of options
            i += 1
            break
        else:
            break
    return "--locked" in flags


def _get_uv_run_command(tokens: list[str]) -> list[str]:
    """Return the sub-command tokens after `uv run [flags]`.

    Returns the remaining tokens starting from the sub-command.
    """
    if len(tokens) < 3:
        return []
    i = 2
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("-"):
            i += 1
        elif t == "--":
            i += 1
            break
        else:
            break
    return tokens[i:]


def _looks_like_script(arg: str) -> bool:
    """Return True if arg looks like a Python script path.

    A script invocation targets a `.py` file. Bare words such as `install`
    (from `uv python install`) or natural-language prose tokens (e.g. Japanese
    text following the word `python3`) are not script paths and must not be
    flagged as invocations.
    """
    return arg.endswith(".py")


def classify_invocation(
    tokens: list[str],
    exceptions: list[dict],
) -> tuple[bool, str]:
    """Classify a command invocation.

    Returns (is_violation, violation_type_or_reason).
    is_violation=False means ALLOWED or SKIPPED (not a Python invocation).
    """
    if not tokens:
        return False, "no_tokens"

    # -----------------------------------------------------------------------
    # Direct python3 / python invocations
    # -----------------------------------------------------------------------
    if tokens[0] in ("python3", "python"):
        # Check: -m pytest
        if len(tokens) >= 3 and tokens[1] == "-m" and tokens[2] == "pytest":
            return True, "direct_python_m_pytest"
        # Check exceptions registry
        if matches_exception(tokens, exceptions):
            return False, "exception_match"
        # Any other direct python3/python invocation with a script arg is a violation
        if len(tokens) >= 2 and not tokens[1].startswith("-") and _looks_like_script(tokens[1]):
            # It's a script invocation
            return True, "direct_python_script"
        # python3 or python with no script arg — not a Python invocation we govern
        return False, "no_script_arg"

    # -----------------------------------------------------------------------
    # uv run ... invocations
    # -----------------------------------------------------------------------
    if tokens[0] == "uv" and len(tokens) >= 2 and tokens[1] == "run":
        locked = _is_locked_uv_run(tokens)
        sub = _get_uv_run_command(tokens)
        if not sub:
            return False, "uv_run_no_subcommand"

        sub_cmd = sub[0]

        # -- pytest as direct sub-command --
        if sub_cmd == "pytest":
            if not locked:
                return True, "uv_run_pytest_no_locked"
            return False, "uv_run_locked_pytest_ok"

        # -- python/python3 as sub-command --
        if sub_cmd in ("python", "python3"):
            if len(sub) >= 3 and sub[1] == "-m" and sub[2] == "pytest":
                # AC2a: uv run --locked python -m pytest is a violation
                return True, "uv_run_python_m_pytest"
            # Script invocation (arg is a .py file path, not -c or -)
            if len(sub) >= 2 and sub[1] not in ("-c", "-", "--") and _looks_like_script(sub[1]):
                # It's a script file invocation
                if not locked:
                    return True, "uv_run_python_script_no_locked"
                return False, "uv_run_locked_python_script_ok"
            # -c or - or no args: not a script, not flagged by AC3/AC3a
            return False, "uv_run_python_inline"

        return False, "uv_run_other_command"

    return False, "not_python_invocation"


# ---------------------------------------------------------------------------
# File content extractors
# ---------------------------------------------------------------------------

def iter_yaml_run_lines(content: str) -> Iterator[tuple[int, str]]:
    """Yield (line_num, line_text) for shell lines from YAML run: blocks.

    Uses a line-by-line state machine to:
    - Detect run: block starts (run: |, run: >, run: <inline>)
    - Track indentation level to detect block end
    - Skip heredoc bodies
    """
    lines = content.splitlines()
    in_run_block = False
    run_indent: int | None = None
    heredoc_delimiter: str | None = None
    run_line_re = re.compile(r'^(\s*)run:\s*(.*)$')

    i = 0
    while i < len(lines):
        line = lines[i]

        if not in_run_block:
            m = run_line_re.match(line)
            if m:
                rest = m.group(2).strip()
                if rest in ("|", ">", "|2", ">2", "|-", ">-", ""):
                    # multi-line block starts on next line
                    in_run_block = True
                    run_indent = None  # will be determined from first content line
                    i += 1
                    continue
                elif rest:
                    # inline: `run: some command`
                    yield (i + 1, rest)
                    i += 1
                    continue
            i += 1
            continue

        # Inside run block
        stripped = line.lstrip()
        if not stripped:
            # blank line inside block
            if run_indent is not None:
                i += 1
                continue
            else:
                i += 1
                continue

        current_indent = len(line) - len(stripped)

        if run_indent is None:
            # First non-blank line sets the indent level
            run_indent = current_indent

        if current_indent < run_indent:
            # Block ended
            in_run_block = False
            run_indent = None
            heredoc_delimiter = None
            # Don't advance i — process this line again
            continue

        # Inside block — check for heredoc
        if heredoc_delimiter is not None:
            # Inside heredoc body: check for end delimiter
            if stripped.strip() == heredoc_delimiter:
                heredoc_delimiter = None
            # Skip heredoc content lines (they're Python code)
            i += 1
            continue

        # Check for heredoc start
        hm = _RE_HEREDOC_START.search(stripped)
        if hm:
            heredoc_delimiter = hm.group(1)
            # The line itself (before the heredoc) is still a shell command
            yield (i + 1, stripped)
            i += 1
            continue

        yield (i + 1, stripped)
        i += 1


def iter_markdown_code_lines(content: str) -> Iterator[tuple[int, str]]:
    """Yield (line_num, line_text) for lines in non-exempted fenced code blocks.

    Skips blocks immediately preceded by <!-- policy-example --> comment.
    """
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        # Check for fence start
        fence_m = _RE_FENCE_OPEN.match(line)
        if fence_m:
            fence_chars = fence_m.group(2)
            fence_len = len(fence_chars)
            fence_char = fence_chars[0]

            # Check if preceded by policy-example comment (look backwards)
            is_example = False
            j = i - 1
            while j >= 0 and not lines[j].strip():
                j -= 1
            if j >= 0 and _RE_POLICY_EXAMPLE_COMMENT.search(lines[j]):
                is_example = True

            # Find the closing fence
            i += 1
            while i < len(lines):
                close_line = lines[i]
                close_m = _RE_FENCE_CLOSE_PREFIX.match(close_line)
                if (
                    close_m
                    and close_m.group(2)[0] == fence_char
                    and len(close_m.group(2)) >= fence_len
                ):
                    i += 1
                    break
                if not is_example:
                    yield (i + 1, close_line)
                i += 1
            continue

        i += 1


def iter_package_json_lines(content: str) -> Iterator[tuple[int, str]]:
    """Yield (line_num, line_text) for script values in package.json."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return
    scripts = data.get("scripts", {})
    # Approximate line numbers by searching the content
    lines = content.splitlines()
    for _name, value in scripts.items():
        if not isinstance(value, str):
            continue
        # Find line number by searching for the value
        for line_num, line in enumerate(lines, 1):
            if value in line:
                yield (line_num, value)
                break
        else:
            yield (0, value)


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------

def should_exclude(file_path: str, repo_root: str) -> bool:
    """Return True if this file should be excluded from scanning."""
    rel = os.path.relpath(file_path, repo_root)
    norm = rel.replace("\\", "/")
    if norm.startswith(FIXTURE_PREFIX):
        return True
    if norm == TEST_FILE_EXCL:
        return True
    if norm == CHECKER_FILE_EXCL:
        return True
    return False


def scan_file(
    file_path: str,
    repo_root: str,
    exceptions: list[dict],
) -> list[Violation]:
    """Scan a single file for Python invocation policy violations."""
    rel = os.path.relpath(file_path, repo_root).replace("\\", "/")
    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    violations: list[Violation] = []

    # Choose extractor based on file type
    if rel.endswith((".yml", ".yaml")):
        line_iter = iter_yaml_run_lines(content)
    elif rel.endswith(".md"):
        line_iter = iter_markdown_code_lines(content)
    elif rel == "package.json":
        line_iter = iter_package_json_lines(content)
    else:
        return []

    for line_num, line_text in line_iter:
        tokens = _extract_argv_from_line(line_text)
        if tokens is None:
            continue
        is_violation, vtype = classify_invocation(tokens, exceptions)
        if is_violation:
            suggestion = _make_suggestion(tokens, vtype)
            violations.append(
                Violation(
                    file=rel,
                    line_num=line_num,
                    line_text=line_text.strip(),
                    violation_type=vtype,
                    suggestion=suggestion,
                )
            )

    return violations


def _make_suggestion(tokens: list[str], vtype: str) -> str | None:
    if vtype == "uv_run_pytest_no_locked":
        return "Replace with: uv run --locked pytest ..."
    if vtype == "uv_run_python_m_pytest":
        return "Replace with: uv run --locked pytest ..."
    if vtype == "direct_python_m_pytest":
        return "Replace with: uv run --locked pytest ..."
    if vtype == "uv_run_python_script_no_locked":
        return "Add --locked: uv run --locked python3 <script> ..."
    if vtype == "direct_python_script":
        return "Add to exceptions registry or migrate to: uv run --locked python3 <script>"
    return None


# ---------------------------------------------------------------------------
# Surface discovery
# ---------------------------------------------------------------------------

def collect_surface_files(repo_root: Path) -> list[str]:
    """Collect all governed surface file paths."""
    files: list[str] = []
    root = str(repo_root)

    for pattern in SURFACE_GLOBS:
        # Expand glob
        matched = sorted(repo_root.glob(pattern))
        for p in matched:
            if p.is_file():
                files.append(str(p))

    # SKILL.md files (recursive)
    for p in sorted(repo_root.glob(SKILL_MD_PATTERN)):
        if p.is_file():
            files.append(str(p))

    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for f in files:
        k = os.path.realpath(f)
        if k not in seen:
            seen.add(k)
            result.append(f)

    # Exclude worktrees and other non-standard paths
    filtered = []
    for f in result:
        rel = os.path.relpath(f, root).replace("\\", "/")
        if ".claude/worktrees/" in rel:
            continue
        if should_exclude(f, root):
            continue
        filtered.append(f)

    return filtered


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_check(
    repo_root: Path,
    strict: bool = False,
) -> CheckResult:
    """Run the policy check against the repo."""
    exceptions = load_exceptions(repo_root)
    surface_files = collect_surface_files(repo_root)

    result = CheckResult(exceptions_loaded=len(exceptions))

    for file_path in surface_files:
        result.scanned_files.append(
            os.path.relpath(file_path, str(repo_root)).replace("\\", "/")
        )
        result.surface_count += 1
        violations = scan_file(file_path, str(repo_root), exceptions)
        result.violations.extend(violations)

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Python invocation policy on governed surfaces.",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Path to repo root (default: current directory)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any violations found",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    result = run_check(repo_root, strict=args.strict)

    if args.json_output:
        out = {
            "scanned_files": result.scanned_files,
            "surface_count": result.surface_count,
            "exceptions_loaded": result.exceptions_loaded,
            "violation_count": len(result.violations),
            "violations": [
                {
                    "file": v.file,
                    "line_num": v.line_num,
                    "line_text": v.line_text,
                    "violation_type": v.violation_type,
                    "suggestion": v.suggestion,
                }
                for v in result.violations
            ],
        }
        print(json.dumps(out, indent=2))
    else:
        if result.violations:
            print(f"Python invocation policy violations ({len(result.violations)}):")
            for v in result.violations:
                print(f"  {v.file}:{v.line_num}  [{v.violation_type}]")
                print(f"    {v.line_text[:120]}")
                if v.suggestion:
                    print(f"    => {v.suggestion}")
        else:
            print(
                f"OK: 0 violations in {result.surface_count} governed surface files "
                f"({result.exceptions_loaded} exceptions loaded)"
            )

    if result.violations:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
