#!/usr/bin/env python3
"""Shared VC grammar helpers for AC markers and preflight-scope parsing."""

from __future__ import annotations

import re

# Valid preflight-scope marker values recognized by both validator and preflight runtime.
VALID_PRE_FLIGHT_SCOPE_VALUES = ("pr_review_only", "runtime_only")

# Marker comment pattern (single-line comment prefix only).
_AC_MARKER_PATTERN = re.compile(r"^\s*#\s*AC(\d+)\b(.*)$")
_PRE_FLIGHT_SCOPE_PATTERN = re.compile(r"^\s*#\s*preflight-scope:\s*(.*?)\s*$")


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
