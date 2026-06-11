#!/usr/bin/env python3
"""
check_vc_scope.py - VC scope preflight checker for issue-refinement-loop.

Checks Verification Commands in an issue body for:
- Missing $ prefix (VC_MISSING_DOLLAR_PREFIX) - warn, exit 1
- Legacy python3 usage (VC_LEGACY_PYTHON3) - blocked, exit 2
- Paths outside Allowed Paths (VC_SCOPE_OUTSIDE_ALLOWED_PATH) - blocked, exit 2
- Broad search paths (VC_SCOPE_BROAD_SEARCH_PATH) - blocked, exit 2
- Unparseable commands (VC_PARSE_INDETERMINATE) - warn, exit 1

stdout: only STATUS / SUMMARY / NEXT_ACTION / EVIDENCE / BLOCKERS / ARTIFACT lines
exit codes: 0=pass, 1=warn, 2=blocked
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_STDOUT_KEYS = {"STATUS", "SUMMARY", "NEXT_ACTION", "EVIDENCE", "BLOCKERS", "ARTIFACT"}

LEVEL_BLOCKED = "blocked"
LEVEL_WARN = "warn"
LEVEL_INFO = "info"

EXIT_PASS = 0
EXIT_WARN = 1
EXIT_BLOCKED = 2

REASON_MISSING_DOLLAR = "VC_MISSING_DOLLAR_PREFIX"
REASON_LEGACY_PYTHON3 = "VC_LEGACY_PYTHON3"
REASON_OUTSIDE_ALLOWED = "VC_SCOPE_OUTSIDE_ALLOWED_PATH"
REASON_BROAD_SEARCH = "VC_SCOPE_BROAD_SEARCH_PATH"
REASON_PARSE_INDETERMINATE = "VC_PARSE_INDETERMINATE"
REASON_PROSE_REFERENCE_ONLY = "VC_PROSE_REFERENCE_ONLY"

# Regex for bare python/python3/python3.x as argv[0] (not inside uv run)
# Matches when python/python3/python3.x is the first token of a command
_LEGACY_PYTHON_RE = re.compile(
    r"(?:^|(?:&&|\|\||\|)\s*)"  # start or after shell operator
    r"(python3?(?:\.\d+)?)\s+"  # python/python3/python3.x followed by space
)

# Matches uv run (with optional flags) before python/pytest
_UV_RUN_PREFIX_RE = re.compile(
    r"(?:^|\s)uv\s+run(?:\s+--\S+)*\s+(?:python3?(?:\.\d+)?|pytest)\b"
)

# Matches glob characters
_GLOB_RE = re.compile(r"[\*\?\[]")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _extract_section(body: str, section_name: str) -> list[tuple[int, str]]:
    """Extract lines from a named section (## Section) in the body.
    Returns list of (line_number_1indexed, line_text) tuples.
    Stops at next ## heading.
    """
    lines = body.splitlines()
    in_section = False
    result: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped == f"## {section_name}":
            in_section = True
            continue
        if in_section:
            if stripped.startswith("## "):
                break
            result.append((i, line))
    return result


def _extract_vc_commands(body: str) -> list[tuple[int, str, bool]]:
    """Extract command lines from ## Verification Commands section.

    Returns list of (line_number, line_text, has_dollar_prefix).
    Only processes lines within code fences inside the VC section.
    Lines starting with '# ' are comments and skipped.
    Non-dollar non-empty non-comment lines in fences are flagged for missing prefix.
    """
    lines = body.splitlines()
    in_vc_section = False
    in_fence = False
    fence_marker = ""
    result: list[tuple[int, str, bool]] = []

    for i, line in enumerate(lines, start=1):
        stripped = line.strip()

        # Detect section transitions
        if stripped == "## Verification Commands":
            in_vc_section = True
            continue
        if in_vc_section and stripped.startswith("## "):
            in_vc_section = False
            in_fence = False
            continue

        if not in_vc_section:
            continue

        # Track code fences
        if stripped.startswith("```"):
            if not in_fence:
                in_fence = True
                fence_marker = stripped[:3]
            elif stripped == fence_marker or stripped == "```":
                in_fence = False
            continue

        if not in_fence:
            continue

        # Skip empty lines and comment lines
        if not stripped:
            continue
        if stripped.startswith("# "):
            continue

        # Check for $ prefix
        if stripped.startswith("$ "):
            result.append((i, line, True))
        else:
            # Non-empty, non-comment line in VC fence without $ prefix
            result.append((i, line, False))

    return result


def _extract_allowed_paths(body: str) -> list[str]:
    """Extract paths from ## Allowed Paths section.

    Each bullet's first backtick code span is taken as the path.
    Falls back to the raw text after '- ' if no backtick span found.
    """
    section_lines = _extract_section(body, "Allowed Paths")
    paths = []
    for _, line in section_lines:
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        content = stripped[2:].strip()
        # Try to extract first backtick code span
        m = re.search(r"`([^`]+)`", content)
        if m:
            paths.append(m.group(1))
        else:
            # Use the raw content (may have parenthetical annotations)
            # Remove parenthetical at end
            raw = re.sub(r"\s*\(.*?\)\s*$", "", content).strip()
            if raw:
                paths.append(raw)
    return paths


# ---------------------------------------------------------------------------
# Path analysis
# ---------------------------------------------------------------------------

def _is_absolute_path(p: str) -> bool:
    return p.startswith("/") or p.startswith("~")


def _has_parent_traversal(p: str) -> bool:
    """Check if path contains .. (unresolved parent traversal)."""
    parts = p.replace("\\", "/").split("/")
    return ".." in parts


def _path_is_allowed(candidate: str, allowed_paths: list[str]) -> bool:
    """Check if candidate path is within any allowed path.

    - Exact file match: candidate == allowed_path
    - Directory match: allowed_path ends with / and candidate starts with it
    - Prefix match: candidate starts with allowed_path + /
    - Also match if candidate starts with allowed_path (no trailing /)
    """
    for ap in allowed_paths:
        ap_norm = ap.rstrip("/")
        cand_norm = candidate.rstrip("/")
        if cand_norm == ap_norm:
            return True
        if cand_norm.startswith(ap_norm + "/"):
            return True
    return False


def _glob_static_prefix(pattern: str) -> str:
    """Get the static (non-glob) prefix of a glob pattern."""
    m = _GLOB_RE.search(pattern)
    if m is None:
        return pattern
    prefix = pattern[: m.start()]
    # Trim to last slash
    slash_pos = prefix.rfind("/")
    if slash_pos >= 0:
        return prefix[: slash_pos + 1]
    return ""


def _broad_search_check(path: str, allowed_paths: list[str]) -> bool:
    """Return True if path is a broad search (glob whose static prefix covers more than Allowed Paths).

    A glob is "broad" if its static prefix is strictly a parent directory of ALL allowed paths
    (i.e., the prefix is less specific than every allowed path, meaning it could match paths
    outside the allowed paths).

    Examples:
    - prefix='.claude/skills/', allowed=['.claude/skills/issue-refinement-loop/...'] -> broad
      (the prefix covers .claude/skills/other-skill/ which is not allowed)
    - prefix='.claude/skills/issue-refinement-loop/', allowed=['.claude/skills/issue-refinement-loop/...'] -> not broad
    """
    if not _GLOB_RE.search(path):
        return False
    prefix = _glob_static_prefix(path)
    if not prefix:
        # No static prefix at all — definitely broad
        return True
    prefix_norm = prefix.rstrip("/")
    # Broad if the static prefix is a strict parent of at least one allowed path
    # AND none of the allowed paths is within/equal to the prefix AND
    # the prefix is not itself contained within any single allowed path's scope.
    #
    # Decision: broad = prefix_norm is a strict ancestor of at LEAST ONE allowed path
    # (meaning the glob can extend beyond what's allowed)
    #
    # Not broad = prefix_norm starts with at least one allowed path's value
    # (meaning the glob is constrained within an allowed directory)
    for ap in allowed_paths:
        ap_norm = ap.rstrip("/")
        # If the prefix is within (starts with) an allowed path, it's not broad
        if prefix_norm.startswith(ap_norm):
            return False
    # If no allowed path contains the prefix, the prefix is broader than the allowed paths
    return True


def _extract_paths_from_command(command_stripped: str) -> list[str]:
    """Heuristically extract file/directory path arguments from a shell command.

    Returns list of path strings found in the command.
    This is best-effort; unparseable forms trigger VC_PARSE_INDETERMINATE.
    """
    # Remove the leading '$ ' if present
    cmd = command_stripped
    if cmd.startswith("$ "):
        cmd = cmd[2:]

    # Tokenize simply: split on spaces, handling simple quoted strings
    # For complex quoting, mark as indeterminate
    try:
        tokens = _simple_tokenize(cmd)
    except ValueError:
        return []

    paths = []
    skip_next = False
    for i, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        # Skip flags
        if token.startswith("-"):
            # Some flags take a value argument (e.g. -n, -F), skip next for those
            # We can't fully know, so we skip only if next token is not path-like
            continue
        # Skip known command names (first token, uv, run, pytest, pnpm, rg, etc.)
        if i == 0:
            continue
        # Skip 'run', '--locked', etc. subcommands/flags
        if token in ("run", "--locked", "python3", "python", "pytest"):
            continue
        # Path-like: contains / or . or starts with .
        if "/" in token or token.startswith("."):
            paths.append(token)

    return paths


def _simple_tokenize(cmd: str) -> list[str]:
    """Simple shell tokenizer. Raises ValueError on complex/unparseable quoting."""
    tokens = []
    current = []
    i = 0
    in_single = False
    in_double = False

    while i < len(cmd):
        c = cmd[i]
        if in_single:
            if c == "'":
                in_single = False
            else:
                current.append(c)
        elif in_double:
            if c == '"':
                in_double = False
            elif c == "\\":
                i += 1
                if i < len(cmd):
                    current.append(cmd[i])
            else:
                current.append(c)
        elif c == "'":
            in_single = True
        elif c == '"':
            in_double = True
        elif c == "\\":
            i += 1
            if i < len(cmd):
                current.append(cmd[i])
        elif c in (" ", "\t"):
            if current:
                tokens.append("".join(current))
                current = []
        elif c in ("|", "&", ";", ">", "<", "(", ")", "`", "$"):
            # Shell metacharacters - simplified handling
            if current:
                tokens.append("".join(current))
                current = []
            # Skip metacharacters for our purposes
        else:
            current.append(c)
        i += 1

    if in_single or in_double:
        raise ValueError("Unclosed quote")

    if current:
        tokens.append("".join(current))

    return tokens


# ---------------------------------------------------------------------------
# Command analysis
# ---------------------------------------------------------------------------

def _check_legacy_python(stripped_cmd: str) -> bool:
    """Return True if command uses bare python/python3 (not via uv run)."""
    cmd = stripped_cmd
    if cmd.startswith("$ "):
        cmd = cmd[2:]

    # If uv run ... python/pytest is present, it's allowed
    if _UV_RUN_PREFIX_RE.search(cmd):
        return False

    # Check for bare python/python3/python3.x as a command token
    # We need to find it as argv[0] or after shell operators
    # Split on common shell operators to get subcommands
    subcommands = re.split(r"(?:&&|\|\||;|\|)\s*", cmd)
    for sub in subcommands:
        sub = sub.strip()
        # Check if subcommand starts with python/python3/python3.x followed by space or EOF
        if re.match(r"python3?(?:\.\d+)?(?:\s|$)", sub):
            return True

    return False


def _check_command(
    line_num: int,
    raw_line: str,
    has_dollar: bool,
    allowed_paths: list[str],
) -> list[dict]:
    """Analyze a single VC command line. Returns list of finding dicts."""
    findings = []
    stripped = raw_line.strip()

    if not has_dollar:
        findings.append({
            "reason_code": REASON_MISSING_DOLLAR,
            "level": LEVEL_WARN,
            "message": f"VC command line does not start with '$ ' (line {line_num})",
            "line": line_num,
            "command": stripped,
            "evidence": stripped,
        })
        # Still analyze the content for other issues
        cmd_for_analysis = stripped
    else:
        cmd_for_analysis = stripped

    # Check legacy python
    if _check_legacy_python(cmd_for_analysis):
        findings.append({
            "reason_code": REASON_LEGACY_PYTHON3,
            "level": LEVEL_BLOCKED,
            "message": f"Legacy python3/python call without 'uv run' at line {line_num}",
            "line": line_num,
            "command": stripped,
            "evidence": cmd_for_analysis,
        })

    # Extract paths for scope checking
    if allowed_paths:
        try:
            paths = _extract_paths_from_command(cmd_for_analysis)
        except Exception:
            paths = []
            findings.append({
                "reason_code": REASON_PARSE_INDETERMINATE,
                "level": LEVEL_WARN,
                "message": f"Could not parse command at line {line_num}",
                "line": line_num,
                "command": stripped,
                "evidence": cmd_for_analysis,
            })

        for p in paths:
            # Skip empty
            if not p:
                continue

            # Check absolute path or parent traversal
            if _is_absolute_path(p) or _has_parent_traversal(p):
                findings.append({
                    "reason_code": REASON_OUTSIDE_ALLOWED,
                    "level": LEVEL_BLOCKED,
                    "message": f"Absolute or parent-traversal path '{p}' at line {line_num}",
                    "line": line_num,
                    "command": stripped,
                    "evidence": p,
                })
                continue

            # Check glob broad search
            if _GLOB_RE.search(p):
                if _broad_search_check(p, allowed_paths):
                    findings.append({
                        "reason_code": REASON_BROAD_SEARCH,
                        "level": LEVEL_BLOCKED,
                        "message": f"Broad glob/search path '{p}' at line {line_num}",
                        "line": line_num,
                        "command": stripped,
                        "evidence": p,
                    })
                # If glob is within allowed path, no finding
                continue

            # Check if path is outside allowed paths
            if not _path_is_allowed(p, allowed_paths):
                findings.append({
                    "reason_code": REASON_OUTSIDE_ALLOWED,
                    "level": LEVEL_BLOCKED,
                    "message": f"Path '{p}' is outside Allowed Paths at line {line_num}",
                    "line": line_num,
                    "command": stripped,
                    "evidence": p,
                })

    return findings


# ---------------------------------------------------------------------------
# Stdout output
# ---------------------------------------------------------------------------

def _emit(key: str, value: str) -> None:
    """Emit a single stdout line. Only allowed keys are permitted."""
    assert key in ALLOWED_STDOUT_KEYS, f"BUG: disallowed key {key}"
    print(f"{key}: {value}")


def _emit_results(
    findings: list[dict],
    status: str,
    exit_code: int,
    artifact_path: Optional[str],
) -> None:
    """Print structured results to stdout."""
    blocked = [f for f in findings if f["level"] == LEVEL_BLOCKED]
    warns = [f for f in findings if f["level"] == LEVEL_WARN]

    _emit("STATUS", status.upper())

    total = len(findings)
    _emit("SUMMARY", f"{total} finding(s): {len(blocked)} blocked, {len(warns)} warn")

    if exit_code == EXIT_PASS:
        _emit("NEXT_ACTION", "pass — no action required")
    elif exit_code == EXIT_WARN:
        _emit("NEXT_ACTION", "warn — review findings before submitting issue")
    else:
        _emit("NEXT_ACTION", "blocked — fix all blocked findings before submitting issue")

    if warns:
        evidence_parts = "; ".join(
            f"line {f['line']}: {f['reason_code']}" for f in warns
        )
        _emit("EVIDENCE", evidence_parts)

    if blocked:
        blocker_parts = "; ".join(
            f"line {f['line']}: {f['reason_code']} ({f['evidence']})" for f in blocked
        )
        _emit("BLOCKERS", blocker_parts)

    if artifact_path:
        _emit("ARTIFACT", artifact_path)


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------

def _write_artifact(
    findings: list[dict],
    status: str,
    exit_code: int,
    mode: str,
    body: str,
    allowed_paths: list[str],
    artifact_dir: Optional[str] = None,
) -> str:
    """Write artifact JSON and return the path."""
    body_sha256 = hashlib.sha256(body.encode()).hexdigest()

    artifact = {
        "schema_version": "vc_scope_check.v1",
        "status": status,
        "exit_code": exit_code,
        "mode": mode,
        "issue_body_sha256": body_sha256,
        "allowed_paths": allowed_paths,
        "findings": findings,
    }

    if artifact_dir:
        os.makedirs(artifact_dir, exist_ok=True)
        artifact_path = os.path.join(artifact_dir, "vc_scope_check.json")
    else:
        tmp = tempfile.mkdtemp(prefix="vc_scope_check_")
        artifact_path = os.path.join(tmp, "vc_scope_check.json")

    with open(artifact_path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, indent=2, ensure_ascii=False)
        f.write("\n")

    return artifact_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="VC scope preflight checker for issue-refinement-loop"
    )
    parser.add_argument(
        "--issue-body-file",
        help="Path to issue body file (if not given, reads from stdin)",
    )
    parser.add_argument(
        "--repo-root",
        help="Repository root path (unused in current implementation, reserved)",
    )
    parser.add_argument(
        "--mode",
        default="issue-refinement",
        choices=["issue-refinement"],
        help="Checker mode (default: issue-refinement)",
    )
    parser.add_argument(
        "--artifact-dir",
        help="Directory to write artifact JSON (default: temp dir)",
    )

    args = parser.parse_args()

    # Read issue body
    if args.issue_body_file:
        with open(args.issue_body_file, encoding="utf-8") as f:
            body = f.read()
    else:
        body = sys.stdin.read()

    # Extract allowed paths
    allowed_paths = _extract_allowed_paths(body)

    # Extract VC commands
    vc_commands = _extract_vc_commands(body)

    # Analyze each command
    all_findings: list[dict] = []
    for line_num, raw_line, has_dollar in vc_commands:
        findings = _check_command(line_num, raw_line, has_dollar, allowed_paths)
        all_findings.extend(findings)

    # Determine overall status and exit code
    has_blocked = any(f["level"] == LEVEL_BLOCKED for f in all_findings)
    has_warn = any(f["level"] == LEVEL_WARN for f in all_findings)

    if has_blocked:
        status = "blocked"
        exit_code = EXIT_BLOCKED
    elif has_warn:
        status = "warn"
        exit_code = EXIT_WARN
    else:
        status = "pass"
        exit_code = EXIT_PASS

    # Write artifact
    artifact_path = _write_artifact(
        findings=all_findings,
        status=status,
        exit_code=exit_code,
        mode=args.mode,
        body=body,
        allowed_paths=allowed_paths,
        artifact_dir=args.artifact_dir,
    )

    # Emit results to stdout
    _emit_results(all_findings, status, exit_code, artifact_path)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
