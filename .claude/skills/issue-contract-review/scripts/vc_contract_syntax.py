#!/usr/bin/env python3
"""Shared VC grammar helpers for AC markers and preflight-scope parsing.

Also provides:
  - baseline-expect annotation parser (Issue #889)
  - vc-role annotation parser (Issue #889)
  - parse_verification_commands_section() — unified VC section parser (Issue #993)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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

# Grouped AC marker: "# AC1, AC2" or "# AC2, AC3, AC4" (comma-separated, no suffix)
_GROUPED_AC_MARKER_PATTERN = re.compile(
    r"^\s*#\s*(AC\d+(?:\s*,\s*AC\d+)+)\s*$"
)


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


# =============================================================================
# Unified VC section parser (Issue #993)
# =============================================================================


@dataclass
class VcParseError:
    """A single parse error found during VC section parsing.

    Attributes:
        kind: Error kind token (e.g. "colon_marker", "unlabeled_fence",
              "non_dollar_command", "inline_backtick").
        line_number: 1-based line number within the Verification Commands
                     section content (not the whole Issue body).
        raw_line: The raw offending line (stripped of leading/trailing whitespace).
        fix_hint: Human-readable suggestion for how to fix this error.
        rule_id: Optional LP rule ID (e.g. "LP016") for downstream routing.
    """
    kind: str
    line_number: int
    raw_line: str
    fix_hint: str
    rule_id: Optional[str] = None


@dataclass
class VcCommandEntry:
    """A single canonical VC command extracted from a bash fence.

    Attributes:
        ac_refs: Set of AC labels this command is associated with
                 (e.g. {"AC1"} for a `# AC1` marker, {"AC2", "AC3"} for grouped).
                 Empty set means the command is not explicitly labelled.
        command: The raw command string without the leading `$ `.
        line_number: 1-based line number within the VC section content.
        preflight_scope: Value of `# preflight-scope:` annotation if present,
                         otherwise None.
        baseline_expect: Value of `# baseline-expect:` annotation if present,
                         otherwise None.
        vc_role: Value of `# vc-role:` annotation if present, otherwise None.
    """
    ac_refs: set  # set[str]
    command: str
    line_number: int
    preflight_scope: Optional[str] = None
    baseline_expect: Optional[str] = None
    vc_role: Optional[str] = None


@dataclass
class VcParseResult:
    """Result of parsing a Verification Commands section.

    Canonical format:
        ## Verification Commands
        ```bash
        # ACN            (bare marker — no suffix)
        $ command
        ```

    Also supported:
        # AC2, AC3       (grouped marker — #814 compatibility)
        command # AC1    (inline suffix on command line)

    Non-canonical inputs generate entries in ``errors``.

    Attributes:
        commands: List of canonical VcCommandEntry items extracted from bash fences.
        errors: List of VcParseError items for non-canonical inputs.
        ac_refs: Set of all AC labels referenced in commands (union of all
                 VcCommandEntry.ac_refs values). Does NOT include refs that
                 only appear in parse errors.
        has_bash_fence: True if at least one ```bash fence was present.
        has_unlabeled_fence: True if at least one unlabeled ``` fence was present.
        static_errors: Subset of ``errors`` that block static validation
                       (non-canonical inputs; excludes warnings).
    """
    commands: list = field(default_factory=list)   # list[VcCommandEntry]
    errors: list = field(default_factory=list)     # list[VcParseError]
    ac_refs: set = field(default_factory=set)      # set[str]
    has_bash_fence: bool = False
    has_unlabeled_fence: bool = False

    @property
    def static_errors(self) -> list:
        """Return errors that constitute static validation blockers."""
        # All errors are static blockers in the current design.
        return list(self.errors)


def parse_verification_commands_section(vc_section: str) -> "VcParseResult":
    """Parse the content of a '## Verification Commands' section.

    Canonical format (Issue #993):
    - Commands must reside inside bash fenced blocks (triple-backtick bash).
    - AC markers must be bare '# ACN' lines (no suffix).
    - Grouped markers '# AC2, AC3' are supported (#814 compatibility).
    - Inline suffix 'command # ACN' on command lines is supported.
    - Commands must start with ``$ `` (dollar sign + space).

    Non-canonical inputs that generate VcParseError entries:
    - Unlabeled fences (```  without language specifier).
    - AC marker lines with a suffix: ``# AC1:``, ``# AC1 text``, etc.
      → kind="colon_marker" (for colon/fullwidth-colon variants)
        or kind="suffixed_marker" (for other suffixes),
        rule_id="LP016".
    - Command lines without a leading ``$`` inside bash fences
      (non-empty, non-comment lines that are not annotations).
      → kind="non_dollar_command", no rule_id.
    - Inline backtick commands outside bash fences.
      → kind="inline_backtick", no rule_id.

    Args:
        vc_section: The raw text content of the Verification Commands section
                    (i.e., everything after ``## Verification Commands`` up to
                    the next ``##`` heading, as returned by extract_section()).

    Returns:
        VcParseResult with populated commands, errors, ac_refs, has_bash_fence,
        has_unlabeled_fence.
    """
    result = VcParseResult()

    if not vc_section:
        return result

    lines = vc_section.splitlines()
    n = len(lines)

    # -----------------------------------------------------------------------
    # Pass 1: Detect unlabeled fences (``` without language specifier).
    # We need to find fences outside of bash blocks; a simple scan works
    # because nesting is not supported in Markdown.
    # -----------------------------------------------------------------------
    in_fence = False
    fence_lang = ""
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            if not in_fence:
                # Opening fence
                lang_part = stripped[3:].strip().lower()
                in_fence = True
                fence_lang = lang_part
                if not lang_part:
                    result.has_unlabeled_fence = True
                    result.errors.append(VcParseError(
                        kind="unlabeled_fence",
                        line_number=0,  # will be fixed in pass 2
                        raw_line=stripped,
                        fix_hint=(
                            "Use ```bash (not ```) for Verification Commands fences. "
                            "Commands in unlabeled fences are not recognized as canonical VC."
                        ),
                    ))
            else:
                # Closing fence
                in_fence = False
                fence_lang = ""

    # -----------------------------------------------------------------------
    # Pass 2: Extract commands and markers from bash fences,
    # detect colon/suffix AC markers and non-$ commands.
    # -----------------------------------------------------------------------
    in_bash = False
    current_ac_refs: set = set()  # AC refs accumulated for the next command
    bash_block_lines: list = []   # lines inside current bash block (for annotation lookup)
    bash_block_line_offset = 0    # line number (1-based) of first line inside bash block

    # Track line numbers for unlabeled fence errors (fix up pass-1 results)
    unlabeled_fence_error_idx = 0

    # Inline backtick detection outside fences
    # We track whether we're inside any fence to avoid false positives.
    in_any_fence = False

    for line_no_0, raw_line in enumerate(lines):
        line_no = line_no_0 + 1  # 1-based
        stripped = raw_line.strip()

        # ── Fence boundary detection ──────────────────────────────────────
        if stripped.startswith("```"):
            if not in_any_fence:
                lang_part = stripped[3:].strip().lower()
                in_any_fence = True
                if lang_part == "bash":
                    in_bash = True
                    result.has_bash_fence = True
                    bash_block_lines = []
                    bash_block_line_offset = line_no + 1
                    current_ac_refs = set()
                else:
                    # Unlabeled or other language fence — fix up line_number
                    if (unlabeled_fence_error_idx < len(result.errors) and
                            result.errors[unlabeled_fence_error_idx].kind == "unlabeled_fence"):
                        result.errors[unlabeled_fence_error_idx].line_number = line_no
                        unlabeled_fence_error_idx += 1
            else:
                # Closing fence
                in_any_fence = False
                if in_bash:
                    in_bash = False
                    current_ac_refs = set()
                    bash_block_lines = []
            continue

        if not in_any_fence:
            # Outside fences: detect inline backtick VC patterns
            # An inline backtick is flagged only if it looks like a command
            # (i.e., contains `$ ...` or starts with `-` and has backtick).
            # We check for "- `..." list-style VC and inline `$ cmd` patterns.
            if re.search(r'`[^`]+`', stripped):
                # Check if this looks like a VC command pattern
                if re.match(r'^\s*-\s+`', raw_line) or re.search(r'`\$\s+\S', raw_line):
                    result.errors.append(VcParseError(
                        kind="inline_backtick",
                        line_number=line_no,
                        raw_line=stripped,
                        fix_hint=(
                            "Inline backtick commands are not canonical VC format. "
                            "Place commands in a ```bash fenced block with $ prefix."
                        ),
                    ))
            continue

        if not in_bash:
            # Inside a non-bash fence — skip content
            continue

        # ── Inside a bash fence ───────────────────────────────────────────
        bash_block_lines.append(raw_line)
        block_idx = len(bash_block_lines) - 1  # 0-based index within block

        # Grouped AC marker: "# AC2, AC3, AC4"
        grouped_m = _GROUPED_AC_MARKER_PATTERN.match(stripped)
        if grouped_m:
            for ac_tok in re.findall(r"AC\d+", grouped_m.group(1)):
                current_ac_refs.add(ac_tok)
            continue

        # Single AC marker (bare or with suffix)
        ac_label, is_valid = parse_ac_marker_line(stripped)
        if ac_label is not None:
            if is_valid:
                current_ac_refs.add(ac_label)
            else:
                # Detect suffix kind
                suffix_m = re.match(r"^\s*#\s*AC\d+\s*([：:])", stripped)
                if suffix_m:
                    error_kind = "colon_marker"
                    fix_hint = (
                        f"Remove the colon/suffix from the AC marker. "
                        f"Use bare '# {ac_label}' (no colon, no description)."
                    )
                else:
                    error_kind = "suffixed_marker"
                    fix_hint = (
                        f"AC marker must be bare '# {ac_label}' without any suffix. "
                        f"Found: {stripped!r}"
                    )
                result.errors.append(VcParseError(
                    kind=error_kind,
                    line_number=line_no,
                    raw_line=stripped,
                    fix_hint=fix_hint,
                    rule_id="LP016",
                ))
            continue

        # Skip known annotation lines (not commands)
        scope_val, _ = parse_preflight_scope_marker_line(stripped)
        if scope_val is not None:
            continue

        be_val, _ = parse_baseline_expect_annotation(stripped)
        if be_val is not None:
            continue

        role_val, _ = parse_vc_role_annotation(stripped)
        if role_val is not None:
            continue

        # Skip other comment lines and empty lines
        if stripped.startswith("#") or not stripped:
            continue

        # Skip vc-regex-intent annotations
        if re.match(r"^\s*#\s*vc-regex-intent:\s*\S+", stripped):
            continue

        # ── Command line ──────────────────────────────────────────────────
        dollar_m = re.match(r"^\s*\$\s+(.+)$", stripped) or re.match(r"^\$\s*$", stripped)
        if dollar_m:
            cmd_str = dollar_m.group(1).strip() if dollar_m.lastindex else ""

            # Detect inline suffix "command # ACN"
            inline_ac_refs: set = set()
            if cmd_str:
                suffix_m2 = re.search(r"\s+#\s*(.+)\s*$", cmd_str)
                if suffix_m2:
                    suffix_label, suffix_valid = parse_ac_marker_line(f"# {suffix_m2.group(1)}")
                    if suffix_label is not None and suffix_valid:
                        inline_ac_refs.add(suffix_label)
                        cmd_str = re.sub(r"\s+#\s*AC\d+\s*$", "", cmd_str).strip()

            # Resolve AC refs: inline suffix overrides current_ac_refs if present
            if inline_ac_refs:
                resolved_ac = inline_ac_refs
            else:
                resolved_ac = set(current_ac_refs)

            # Extract annotations from the block (using 0-based index within block)
            preflight_scope: Optional[str] = None
            baseline_expect_val: Optional[str] = None
            vc_role_val: Optional[str] = None

            if block_idx > 0:
                ps_marker, ps_known = parse_preflight_scope_marker_line(
                    bash_block_lines[block_idx - 1].strip()
                )
                if ps_marker is not None:
                    preflight_scope = ps_marker

                be_v, _, _ = extract_baseline_expect_annotation(
                    [l.strip() for l in bash_block_lines], block_idx
                )
                baseline_expect_val = be_v

                vc_role_val = extract_vc_role_annotation(
                    [l.strip() for l in bash_block_lines], block_idx
                )

            entry = VcCommandEntry(
                ac_refs=resolved_ac,
                command=cmd_str,
                line_number=line_no,
                preflight_scope=preflight_scope,
                baseline_expect=baseline_expect_val,
                vc_role=vc_role_val,
            )
            result.commands.append(entry)
            result.ac_refs.update(resolved_ac)

            # Reset current_ac_refs after a command consumes it
            # (each command "claims" the accumulated markers)
            current_ac_refs = set()
        else:
            # Non-$ command line inside bash fence
            # This is a non-canonical command format.
            # For backward compatibility, still detect inline suffix AC refs (#AC1 format)
            # so LP010 can match them.  The error is still recorded to trigger LP016/C4.
            non_dollar_suffix_m = re.search(r"\s+#\s*(.+)\s*$", stripped)
            if non_dollar_suffix_m:
                suffix_label2, suffix_valid2 = parse_ac_marker_line(
                    f"# {non_dollar_suffix_m.group(1)}"
                )
                if suffix_label2 is not None and suffix_valid2:
                    result.ac_refs.add(suffix_label2)
            elif current_ac_refs:
                # Backward compat: if valid AC markers were set before this non-$ command,
                # still add them to ac_refs so C5 does not co-fire with C4.
                # (old parser did this; omitting it causes autofix tools to refuse C4-only repairs)
                result.ac_refs.update(current_ac_refs)

            result.errors.append(VcParseError(
                kind="non_dollar_command",
                line_number=line_no,
                raw_line=stripped,
                fix_hint=(
                    "Commands in Verification Commands bash fences must start with '$ '. "
                    f"Found non-$ line: {stripped!r}"
                ),
            ))

    return result
