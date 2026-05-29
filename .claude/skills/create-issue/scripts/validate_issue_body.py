#!/usr/bin/env python3
"""GitHub Issue body validator for LOOP_PROTOCOL.

Validates Issue body against rule LP001-LP030 and returns JSON-formatted errors.
Used as a pre-write hook in create_issue_txn.py.

Exit codes:
  0: validation pass (no errors)
  1: validation fail (errors returned in JSON)
  2: internal error or CLI usage error
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
import yaml
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal


# =============================================================================
# Type definitions
# =============================================================================

@dataclass(frozen=True)
class ValidationError:
    """A single validation error with metadata."""
    rule_id: str
    severity: Literal["error", "warning"]
    section: str
    line_start: int
    line_end: int
    message: str
    minimal_context: list[str]
    context_truncated: bool
    fix_hint: str = ""
    autofixable: bool = False
    expected: list[str] | None = None  # For LP010
    actual: list[str] | None = None    # For LP010


@dataclass(frozen=True)
class ValidationResult:
    """Complete validation result."""
    schema: str
    target: str
    body_sha256: str
    status: Literal["pass", "fail"]
    errors: list[ValidationError]


# =============================================================================
# Validation Rules
# =============================================================================

def _extract_section(body: str, section_name: str) -> tuple[str, int, int] | None:
    """Extract section content and line numbers.

    Returns (content, start_line, end_line) or None if not found.
    Line numbers are 1-indexed.
    """
    lines = body.split('\n')
    pattern = re.compile(rf'^##\s+{re.escape(section_name)}\s*$', re.IGNORECASE)

    start_idx = None
    for i, line in enumerate(lines):
        if pattern.match(line):
            start_idx = i
            break

    if start_idx is None:
        return None

    # Find next section or end of document
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if re.match(r'^##\s+', lines[i]):
            end_idx = i
            break

    # Extract content (skip header line)
    content = '\n'.join(lines[start_idx + 1:end_idx])
    return content.strip(), start_idx + 1, end_idx - 1


def _extract_sections(body: str) -> dict[str, tuple[str, int, int]]:
    """Extract all markdown sections with their line ranges."""
    sections = {}
    lines = body.split('\n')

    for i, line in enumerate(lines):
        if re.match(r'^##\s+', line):
            section_name = re.sub(r'^##\s+', '', line).strip()
            # Find content until next section
            content_start = i + 1
            content_end = len(lines)
            for j in range(i + 1, len(lines)):
                if re.match(r'^##\s+', lines[j]):
                    content_end = j
                    break

            content = '\n'.join(lines[content_start:content_end]).strip()
            sections[section_name] = (content, i + 1, content_end)

    return sections


def _get_context_lines(body: str, start_line: int, end_line: int, max_lines: int = 5, max_bytes: int = 2048) -> tuple[list[str], bool]:
    """Extract context lines around the error range.

    Returns (context_lines, truncated_flag).
    Lines are 1-indexed.
    Both max_lines and max_bytes limits are enforced.
    """
    lines = body.split('\n')

    # Clamp to actual line count
    start = max(0, start_line - 1)
    end = min(len(lines), end_line)

    context = lines[start:end][:max_lines]  # First limit to max_lines

    # Then enforce byte limit
    truncated = False
    result = []
    total_bytes = 0

    for line in context:
        encoded = line.encode('utf-8')
        # Account for newline separator (1 byte)
        line_with_newline_cost = len(encoded) + 1

        if total_bytes + line_with_newline_cost > max_bytes:
            # This line would exceed limit
            if total_bytes == 0:
                # First line is too long, truncate it
                remaining = max_bytes
                truncated_line = encoded[:remaining].decode('utf-8', errors='ignore')
                result.append(truncated_line)
            truncated = True
            break

        result.append(line)
        total_bytes += line_with_newline_cost

    return result, truncated


def _extract_ac_numbers(body: str) -> set[str]:
    """Extract AC numbers from 'Acceptance Criteria' section."""
    section_info = _extract_section(body, "Acceptance Criteria")
    if not section_info:
        return set()

    content, _, _ = section_info
    # Match lines like: - [ ] AC1: ... or - [x] AC1: ...
    pattern = r'- \[[^\]]*\]\s+AC(\d+):'
    matches = re.findall(pattern, content)
    return {f"AC{m}" for m in matches}


def _extract_vc_ac_numbers(body: str) -> set[str]:
    """Extract AC numbers referenced in Verification Commands section."""
    section_info = _extract_section(body, "Verification Commands")
    if not section_info:
        return set()

    content, _, _ = section_info
    # Match comment markers: # AC1, # AC2, etc. in executable command lines
    pattern = r'#\s+AC(\d+)'
    matches = re.findall(pattern, content)
    return {f"AC{m}" for m in matches}


def _validate_lp001_missing_required_section(body: str) -> list[ValidationError]:
    """LP001: Detect missing required sections."""
    required_sections = [
        "Acceptance Criteria",
        "Verification Commands",
        "Allowed Paths"
    ]

    sections = _extract_sections(body)
    errors = []

    for section_name in required_sections:
        if section_name not in sections:
            errors.append(ValidationError(
                rule_id="LP001",
                severity="error",
                section="(global)",
                line_start=1,
                line_end=1,
                message=f"Missing required section: {section_name}",
                minimal_context=["(Section not found)"],
                context_truncated=False,
                fix_hint=f"Add '## {section_name}' section to the Issue body.",
                autofixable=False
            ))

    return errors


def _validate_lp002_invalid_machine_readable_contract(body: str) -> list[ValidationError]:
    """LP002: Detect invalid Machine-Readable Contract YAML."""
    section_info = _extract_section(body, "Machine-Readable Contract")
    if not section_info:
        # LP002 not applicable if no contract section exists
        return []

    content, start_line, end_line = section_info

    # Try to extract YAML block
    yaml_match = re.search(r'```yaml\n(.*?)\n```', content, re.DOTALL)
    if not yaml_match:
        context, trunc = _get_context_lines(body, start_line, end_line)
        return [ValidationError(
            rule_id="LP002",
            severity="error",
            section="Machine-Readable Contract",
            line_start=start_line,
            line_end=end_line,
            message="Machine-Readable Contract block must use ```yaml ... ``` fence",
            minimal_context=context,
            context_truncated=trunc,
            fix_hint="Wrap YAML contract in ```yaml ... ``` code fence.",
            autofixable=False
        )]

    yaml_content = yaml_match.group(1)

    # Parse and validate YAML
    errors = []

    try:
        data = yaml.safe_load(yaml_content)
    except yaml.YAMLError as exc:
        context, trunc = _get_context_lines(body, start_line, end_line)
        return [ValidationError(
            rule_id="LP002",
            severity="error",
            section="Machine-Readable Contract",
            line_start=start_line,
            line_end=end_line,
            message=f"Machine-Readable Contract YAML syntax error: {str(exc)[:100]}",
            minimal_context=context,
            context_truncated=trunc,
            fix_hint="Fix YAML syntax errors in contract block.",
            autofixable=False
        )]

    # Check if parsed data is a dict
    if not isinstance(data, dict):
        context, trunc = _get_context_lines(body, start_line, end_line)
        return [ValidationError(
            rule_id="LP002",
            severity="error",
            section="Machine-Readable Contract",
            line_start=start_line,
            line_end=end_line,
            message="Machine-Readable Contract YAML must be a dictionary",
            minimal_context=context,
            context_truncated=trunc,
            fix_hint="Ensure YAML contract root is a dictionary (key: value pairs).",
            autofixable=False
        )]

    # Check for required fields
    required_contract_fields = [
        "contract_schema_version",
        "issue_kind"
    ]

    for field in required_contract_fields:
        if field not in data:
            context, trunc = _get_context_lines(body, start_line, end_line)
            errors.append(ValidationError(
                rule_id="LP002",
                severity="error",
                section="Machine-Readable Contract",
                line_start=start_line,
                line_end=end_line,
                message=f"Machine-Readable Contract missing required field: {field}",
                minimal_context=context,
                context_truncated=trunc,
                fix_hint=f"Add '{field}: <value>' to YAML contract block.",
                autofixable=False
            ))

    return errors


def _validate_lp010_ac_vc_mismatch(body: str) -> list[ValidationError]:
    """LP010: Detect mismatch between AC and VC numbers."""
    ac_numbers = _extract_ac_numbers(body)
    vc_numbers = _extract_vc_ac_numbers(body)

    if ac_numbers == vc_numbers:
        return []

    # Find which section to report error on
    vc_section_info = _extract_section(body, "Verification Commands")
    if not vc_section_info:
        return []

    _, start_line, end_line = vc_section_info
    context, trunc = _get_context_lines(body, start_line, end_line)

    missing_in_vc = ac_numbers - vc_numbers
    extra_in_vc = vc_numbers - ac_numbers

    message = "AC ⇔ VC number set mismatch"
    if missing_in_vc:
        message += f" (missing in VC: {', '.join(sorted(missing_in_vc))})"
    if extra_in_vc:
        message += f" (extra in VC: {', '.join(sorted(extra_in_vc))})"

    return [ValidationError(
        rule_id="LP010",
        severity="error",
        section="Verification Commands",
        line_start=start_line,
        line_end=end_line,
        message=message,
        minimal_context=context,
        context_truncated=trunc,
        fix_hint="Ensure each AC has exactly one corresponding VC comment.",
        autofixable=False,
        expected=sorted(ac_numbers),
        actual=sorted(vc_numbers)
    )]


def _validate_lp011_verification_command_format(body: str) -> list[ValidationError]:
    """LP011: Detect invalid Verification Commands format."""
    section_info = _extract_section(body, "Verification Commands")
    if not section_info:
        return []

    content, start_line, end_line = section_info
    lines = body.split('\n')[start_line - 1:end_line]

    # Each AC should have at least one command in a fenced bash block
    # Look for ```bash blocks with # AC<N> markers
    errors = []

    bash_blocks = re.findall(r'```bash\n(.*?)\n```', content, re.DOTALL)
    if not bash_blocks:
        context, trunc = _get_context_lines(body, start_line, end_line)
        return [ValidationError(
            rule_id="LP011",
            severity="error",
            section="Verification Commands",
            line_start=start_line,
            line_end=end_line,
            message="Verification Commands must use ```bash ... ``` fenced blocks",
            minimal_context=context,
            context_truncated=trunc,
            fix_hint="Wrap all commands in ```bash ... ``` code fence.",
            autofixable=False
        )]

    return errors


def _validate_lp012_rg_encoding_flag(body: str) -> list[ValidationError]:
    """LP012: Detect misuse of rg -E flag.

    The -E flag in ripgrep is for --encoding (specifying file encoding),
    not for ERE like grep -E. Combining rg with -E (encoding flag) is error-prone.
    """
    section_info = _extract_section(body, "Verification Commands")
    if not section_info:
        return []

    content, start_line, end_line = section_info
    lines = body.split('\n')[start_line - 1:end_line]

    errors = []
    current_line = start_line

    for line in lines:
        # Skip pure comment lines or empty lines
        if line.strip().startswith('#') or not line.strip():
            current_line += 1
            continue

        # Check if this is an executable command line (not a comment)
        if 'rg' in line:
            try:
                # Remove trailing # AC<N> marker before tokenizing
                cleaned = re.sub(r'\s+#\s*AC\d+\s*:?\s*$', '', line)
                tokens = shlex.split(cleaned, posix=True)

                # Check for 'rg' command with '-E' token
                if tokens and tokens[0] == 'rg' and '-E' in tokens:
                    context, trunc = _get_context_lines(body, current_line, current_line)
                    errors.append(ValidationError(
                        rule_id="LP012",
                        severity="error",
                        section="Verification Commands",
                        line_start=current_line,
                        line_end=current_line,
                        message="rg -E flag (encoding) should not be used. Use rg -P for pattern matching instead.",
                        minimal_context=context,
                        context_truncated=trunc,
                        fix_hint="Replace 'rg -E' with 'rg -P' for extended regex patterns.",
                        autofixable=False
                    ))
            except ValueError:
                # shlex.split failed - not a valid command, skip
                pass

        current_line += 1

    return errors


def _validate_lp013_deletion_negative_grep(body: str) -> list[ValidationError]:
    """LP013: Detect deletion check without explicit literal targets."""
    section_info = _extract_section(body, "Verification Commands")
    if not section_info:
        return []

    content, start_line, end_line = section_info
    lines = body.split('\n')[start_line - 1:end_line]

    errors = []
    current_line = start_line

    for line in lines:
        if 'grep -v' in line or 'rg -v' in line:
            # Check if there's an explicit literal target (e.g., test -f, grep -q)
            if not any(x in line for x in ['test -f', 'test -d', 'grep -q', 'rg -q']):
                context, trunc = _get_context_lines(body, current_line, current_line)
                errors.append(ValidationError(
                    rule_id="LP013",
                    severity="warning",
                    section="Verification Commands",
                    line_start=current_line,
                    line_end=current_line,
                    message="Negative grep (-v) without explicit literal target may be ambiguous",
                    minimal_context=context,
                    context_truncated=trunc,
                    fix_hint="Add explicit file/pattern check before using grep -v.",
                    autofixable=False
                ))

        current_line += 1

    return errors


def _validate_lp014_markdown_backtick_grep(body: str) -> list[ValidationError]:
    """LP014: Detect grep on markdown backticks (common mistake)."""
    section_info = _extract_section(body, "Verification Commands")
    if not section_info:
        return []

    content, start_line, end_line = section_info
    lines = body.split('\n')[start_line - 1:end_line]

    errors = []
    current_line = start_line

    for line in lines:
        # Check for grep/rg used directly without capturing the command block first
        if any(x in line for x in ['grep', 'rg']) and '```' in line:
            context, trunc = _get_context_lines(body, current_line, current_line)
            errors.append(ValidationError(
                rule_id="LP014",
                severity="warning",
                section="Verification Commands",
                line_start=current_line,
                line_end=current_line,
                message="Grep/rg on markdown backticks may fail on literal backtick characters",
                minimal_context=context,
                context_truncated=trunc,
                fix_hint="Extract command content before grepping.",
                autofixable=False
            ))

        current_line += 1

    return errors


def _validate_lp015_baseline_vc_heading_only(body: str) -> list[ValidationError]:
    """LP015: Detect baseline VC that matches only heading (broad match)."""
    section_info = _extract_section(body, "Verification Commands")
    if not section_info:
        return []

    content, start_line, end_line = section_info

    # Check for commands that only match section headings (##)
    if 'grep' in content and '##' in content:
        # Syntactic broad-match detection
        if re.search(r'grep.*##', content):
            context, trunc = _get_context_lines(body, start_line, end_line)
            return [ValidationError(
                rule_id="LP015",
                severity="warning",
                section="Verification Commands",
                line_start=start_line,
                line_end=end_line,
                message="VC grep on '##' may match heading markers only (broad match)",
                minimal_context=context,
                context_truncated=trunc,
                fix_hint="Be more specific in grep pattern to avoid matching just headers.",
                autofixable=False
            )]

    return []


def _validate_lp020_runtime_verification_incomplete(body: str) -> list[ValidationError]:
    """LP020: Detect incomplete Runtime Verification Applicability."""
    section_info = _extract_section(body, "Runtime Verification Applicability")
    if not section_info:
        # If section exists, check completeness
        return []

    content, start_line, end_line = section_info

    # Check for decision field
    if not re.search(r'^\s*-?\s*decision:\s*(not_applicable|immediate|deferred)', content, re.MULTILINE):
        context, trunc = _get_context_lines(body, start_line, end_line)
        return [ValidationError(
            rule_id="LP020",
            severity="error",
            section="Runtime Verification Applicability",
            line_start=start_line,
            line_end=end_line,
            message="Runtime Verification Applicability must include 'decision' field",
            minimal_context=context,
            context_truncated=trunc,
            fix_hint="Add 'decision: not_applicable|immediate|deferred' to section.",
            autofixable=False
        )]

    # If decision is 'deferred', check for required fields
    if re.search(r'decision:\s*deferred', content, re.IGNORECASE):
        required_deferred_fields = ['deferred_destination', 'deferred_verification_condition']
        errors = []
        for field in required_deferred_fields:
            if field not in content:
                context, trunc = _get_context_lines(body, start_line, end_line)
                errors.append(ValidationError(
                    rule_id="LP020",
                    severity="error",
                    section="Runtime Verification Applicability",
                    line_start=start_line,
                    line_end=end_line,
                    message=f"deferred decision requires '{field}' field",
                    minimal_context=context,
                    context_truncated=trunc,
                    fix_hint=f"Add '{field}' field when decision is deferred.",
                    autofixable=False
                ))
        return errors

    return []


def _validate_lp016_vc_ac_marker_with_description(body: str) -> list[ValidationError]:
    """LP016: Detect VC AC markers with inline description suffix.

    Valid form:   # AC1
    Invalid form: # AC1: some description text

    The '# AC<N>: ...' form (with colon + text) is not a bare AC marker and
    causes ambiguity in AC-to-VC traceability tooling. Only bare '# AC<N>'
    standalone comment lines are permitted as AC markers in VC sections.
    """
    section_info = _extract_section(body, "Verification Commands")
    if not section_info:
        return []

    content, start_line, end_line = section_info
    lines = body.split('\n')[start_line - 1:end_line]

    errors = []
    # Match lines that are pure comment-only lines (no leading spaces before #)
    # with the pattern: # AC<N>: <description>
    pattern = re.compile(r'^\s*#\s+AC\d+\s*:')

    current_line = start_line
    for line in lines:
        if pattern.match(line):
            context, trunc = _get_context_lines(body, current_line, current_line)
            errors.append(ValidationError(
                rule_id="LP016",
                severity="error",
                section="Verification Commands",
                line_start=current_line,
                line_end=current_line,
                message=(
                    f"VC AC marker must be bare '# AC<N>' without description suffix. "
                    f"Found: {line.strip()!r}"
                ),
                minimal_context=context,
                context_truncated=trunc,
                fix_hint="Change '# AC<N>: description' to bare '# AC<N>' on its own line.",
                autofixable=False
            ))
        current_line += 1

    return errors


def _validate_lp030_forbidden_authoring_doc_path(body: str) -> list[ValidationError]:
    """LP030: Detect reference to forbidden authoring doc path."""
    forbidden_paths = ["docs/dev/body-authoring.md"]

    errors = []
    lines = body.split('\n')

    for i, line in enumerate(lines, 1):
        for path in forbidden_paths:
            if path in line:
                context, trunc = _get_context_lines(body, i, i)
                errors.append(ValidationError(
                    rule_id="LP030",
                    severity="error",
                    section="(global)",
                    line_start=i,
                    line_end=i,
                    message=f"Reference to forbidden path: {path}",
                    minimal_context=context,
                    context_truncated=trunc,
                    fix_hint=f"Remove reference to {path}.",
                    autofixable=False
                ))

    return errors


# =============================================================================
# Main validation dispatcher
# =============================================================================

def validate_issue_body(body: str) -> ValidationResult:
    """Run all validation rules and return aggregated results."""

    # Compute SHA256 of body
    body_bytes = body.encode('utf-8')
    body_sha256 = f"sha256:{hashlib.sha256(body_bytes).hexdigest()}"

    # Run all validators
    all_errors: list[ValidationError] = []

    all_errors.extend(_validate_lp001_missing_required_section(body))
    all_errors.extend(_validate_lp002_invalid_machine_readable_contract(body))
    all_errors.extend(_validate_lp010_ac_vc_mismatch(body))
    all_errors.extend(_validate_lp011_verification_command_format(body))
    all_errors.extend(_validate_lp012_rg_encoding_flag(body))
    all_errors.extend(_validate_lp013_deletion_negative_grep(body))
    all_errors.extend(_validate_lp014_markdown_backtick_grep(body))
    all_errors.extend(_validate_lp015_baseline_vc_heading_only(body))
    all_errors.extend(_validate_lp016_vc_ac_marker_with_description(body))
    all_errors.extend(_validate_lp020_runtime_verification_incomplete(body))
    all_errors.extend(_validate_lp030_forbidden_authoring_doc_path(body))

    # Determine overall status
    has_errors = any(e.severity == "error" for e in all_errors)
    status = "fail" if has_errors else "pass"

    return ValidationResult(
        schema="loop_body_lint/v1",
        target="issue",
        body_sha256=body_sha256,
        status=status,
        errors=all_errors
    )


# =============================================================================
# CLI and JSON serialization
# =============================================================================

def _error_to_dict(error: ValidationError) -> dict:
    """Convert ValidationError to JSON-serializable dict."""
    d = asdict(error)
    # Ensure all fields are present
    if d.get('expected') is None:
        del d['expected']
    if d.get('actual') is None:
        del d['actual']
    return d


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate GitHub Issue body against LOOP_PROTOCOL rules"
    )
    parser.add_argument(
        "--body-file",
        type=str,
        help="Path to issue body file"
    )
    parser.add_argument(
        "--body",
        type=str,
        default="",
        help="Issue body text (used if --body-file not provided)"
    )

    args = parser.parse_args(argv)

    # Read body
    body = ""
    if args.body_file:
        try:
            body = Path(args.body_file).read_text(encoding='utf-8')
        except OSError as exc:
            print(f"ERROR: Cannot read body file: {exc}", file=sys.stderr)
            return 2
    else:
        body = args.body

    if not body:
        print("ERROR: No body provided (--body or --body-file required)", file=sys.stderr)
        return 2

    # Validate
    result = validate_issue_body(body)

    # Output JSON
    output = {
        "schema": result.schema,
        "target": result.target,
        "body_sha256": result.body_sha256,
        "status": result.status,
        "errors": [_error_to_dict(e) for e in result.errors]
    }

    print(json.dumps(output, indent=2))

    # Exit codes
    if result.status == "fail":
        return 1
    else:
        return 0


if __name__ == "__main__":
    sys.exit(main())
