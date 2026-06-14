#!/usr/bin/env python3
"""Shared VC grammar helpers for AC markers and preflight-scope parsing.

Also provides:
  - baseline-expect annotation parser (Issue #889)
  - vc-role annotation parser (Issue #889)
"""

from __future__ import annotations

import re
from typing import Optional

# Valid preflight-scope marker values recognized by both validator and preflight runtime.
VALID_PRE_FLIGHT_SCOPE_VALUES = ("pr_review_only", "runtime_only")

# Valid baseline-expect annotation values (Issue #889).
# "pass"     - VC expected to exit 0 at baseline (promotion/refactor issue)
# "fail"     - VC expected to exit non-0 at baseline (new implementation)
# "deferred" - VC baseline run is deferred (equiv. to pr_review_only scope)
VALID_BASELINE_EXPECT_VALUES = ("pass", "fail", "deferred")

# Marker comment pattern (single-line comment prefix only).
_AC_MARKER_PATTERN = re.compile(r"^\s*#\s*AC(\d+)\b(.*)$")
_PRE_FLIGHT_SCOPE_PATTERN = re.compile(r"^\s*#\s*preflight-scope:\s*(.*?)\s*$")
_BASELINE_EXPECT_PATTERN = re.compile(r"^\s*#\s*baseline-expect:\s*(.*?)\s*$")
_VC_ROLE_PATTERN = re.compile(r"^\s*#\s*vc-role:\s*(.*?)\s*$")


def parse_ac_marker_line(line: str) -> tuple[str | None, bool]:
    """Parse standalone AC marker comment line.

    Args:
        line: A single source line.

    Returns:
        tuple[str|None, bool]: (marker_label, is_valid)

        marker_label = 'AC1', 'AC2', ... when line is an AC marker comment.
        is_valid = True only for bare '# AC1' / '# AC1   ' style forms.

    Notes:
        Any suffix after the AC number (": text", "：text", "- text", "— text")
        is treated as invalid, so `_extract_vc_ac_numbers` in strict mode will not
        treat it as a match.
    """

    match = _AC_MARKER_PATTERN.match(line)
    if not match:
        return None, False

    label = f"AC{match.group(1)}"
    suffix = match.group(2).strip()
    return (label, not bool(suffix))


def parse_preflight_scope_marker_line(line: str) -> tuple[str | None, bool]:
    """Parse standalone preflight-scope marker line.

    Returns:
        tuple[str|None, bool]: (scope_value, is_known_value)

        scope_value is extracted raw value (without surrounding whitespace) when the
        line is a preflight-scope marker, or None otherwise.
        is_known_value is True when scope_value is one of
        VALID_PRE_FLIGHT_SCOPE_VALUES.

    Empty value and whitespace-only values are treated as markers but not known.
    """

    match = _PRE_FLIGHT_SCOPE_PATTERN.match(line)
    if not match:
        return None, False

    value = match.group(1).strip()
    return value, value in VALID_PRE_FLIGHT_SCOPE_VALUES


def parse_baseline_expect_annotation(line: str) -> tuple[Optional[str], bool]:
    """Parse standalone baseline-expect annotation line (Issue #889).

    Format: ``# baseline-expect: pass|fail|deferred``

    Args:
        line: A single source line.

    Returns:
        tuple[str|None, bool]: (value, is_known_value)

        value is the extracted annotation value when the line matches, or None.
        is_known_value is True when value is one of VALID_BASELINE_EXPECT_VALUES.

    Semantics:
        baseline-expect is an "execution result classification annotation"
        (not a safety policy bypass annotation).  It tells the preflight
        runtime what the author *expects* the VC to return at baseline:

        - ``pass``    : VC is expected to exit 0 at baseline (promotion/refactor)
        - ``fail``    : VC is expected to exit non-0 at baseline (new implementation)
        - ``deferred``: VC baseline run is deferred (like pr_review_only scope)

    Important: baseline-expect does NOT override static blockers.
    unsafe_command / compound / trivially-pass / broad search path detection
    takes precedence over any baseline-expect annotation.
    """
    match = _BASELINE_EXPECT_PATTERN.match(line)
    if not match:
        return None, False
    value = match.group(1).strip()
    return value, value in VALID_BASELINE_EXPECT_VALUES


def parse_vc_role_annotation(line: str) -> tuple[Optional[str], bool]:
    """Parse standalone vc-role annotation line (Issue #889).

    Format: ``# vc-role: <role>``

    Currently advisory (informational only).  The parser returns the raw value
    for downstream use.

    Returns:
        tuple[str|None, bool]: (value, True if value is non-empty)
    """
    match = _VC_ROLE_PATTERN.match(line)
    if not match:
        return None, False
    value = match.group(1).strip()
    return value if value else None, bool(value)


def extract_baseline_expect_annotation(
    lines: list,
    target_line_idx: int,
) -> tuple:
    """Extract ``# baseline-expect:`` annotation from the contiguous comment block
    immediately preceding a VC command line (Issue #889).

    Scope rules (consistent with existing vc-regex-intent annotation scoping):
    - Only the contiguous block of comment/annotation lines directly before
      target_line_idx is considered (0-based index within ``lines``).
    - An empty line or a ``$ command`` line terminates the block.
    - ``# preflight-scope:``, ``# AC<N>``, and ``# vc-role:`` markers are
      transparent (allowed in the same block).

    Args:
        lines: All lines of the bash block (list, 0-indexed).
        target_line_idx: 0-based index of the command line (``$ <cmd>``).

    Returns:
        tuple[value, line_number, raw_line]:
          value       - annotation value string or None
          line_number - 1-based line number within ``lines`` or None
          raw_line    - raw annotation line text or None
    """
    found_value: Optional[str] = None
    found_line_no: Optional[int] = None
    found_raw: Optional[str] = None

    for offset in range(1, target_line_idx + 1):
        line_idx = target_line_idx - offset
        if line_idx < 0:
            break
        line = lines[line_idx].strip()

        # Empty line: stop scanning
        if not line:
            break

        # $ command line: stop scanning (another command intervened)
        if re.match(r"^\$\s+", line) or re.match(r"^\$\s*$", line):
            break

        # baseline-expect annotation: record it and continue scanning
        # BLOCKER 2 fix: check is_known_value; invalid values are treated as None
        # so that typos (e.g. "pas") do not silently degrade to a missing annotation.
        value, is_known_value = parse_baseline_expect_annotation(line)
        if value is not None:
            if is_known_value:
                found_value = value
            else:
                # Invalid annotation value: store as sentinel "__invalid__" so
                # baseline_vc_preflight can emit human_judgment / invalid_baseline_expect_annotation.
                # Using None here would silently treat as "no annotation".
                found_value = f"__invalid__:{value}"
            found_line_no = line_idx + 1  # 1-based
            found_raw = line
            continue

        # preflight-scope marker: transparent
        scope, _ = parse_preflight_scope_marker_line(line)
        if scope is not None:
            continue

        # AC marker: transparent
        ac_label, is_valid = parse_ac_marker_line(line)
        if ac_label is not None and is_valid:
            continue

        # vc-role annotation: transparent
        role, _ = parse_vc_role_annotation(line)
        if role is not None:
            continue

        # Any other line: stop scanning
        break

    return found_value, found_line_no, found_raw


def extract_vc_role_annotation(
    lines: list,
    target_line_idx: int,
) -> Optional[str]:
    """Extract ``# vc-role:`` annotation from the contiguous comment block
    preceding a VC command line (Issue #889).

    Uses the same scope rules as ``extract_baseline_expect_annotation``.

    Returns:
        role value string or None.
    """
    for offset in range(1, target_line_idx + 1):
        line_idx = target_line_idx - offset
        if line_idx < 0:
            break
        line = lines[line_idx].strip()

        if not line:
            break
        if re.match(r"^\$\s+", line) or re.match(r"^\$\s*$", line):
            break

        role, _ = parse_vc_role_annotation(line)
        if role is not None:
            return role

        # Transparent markers
        scope, _ = parse_preflight_scope_marker_line(line)
        if scope is not None:
            continue

        ac_label, is_valid = parse_ac_marker_line(line)
        if ac_label is not None and is_valid:
            continue

        v, _ = parse_baseline_expect_annotation(line)
        if v is not None:
            continue

        # Any other line: stop
        break

    return None
