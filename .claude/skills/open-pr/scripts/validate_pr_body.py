#!/usr/bin/env python3
"""GitHub PR body validator for LOOP_PROTOCOL."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal


SCHEMA_DECISIONS = {"schema_change", "not_schema_change", "uncertain"}
REQUIRED_SECTIONS = [
    "Summary",
    "Checks",
    "Schema Change Applicability",
    "Schema Consumer Inventory",
    "Safety Claim Matrix",
    "Notes",
]
SAFETY_SENSITIVE_PATH_PATTERNS = [
    "transport",
    "permission",
    "sandbox",
    "auth",
    "mcp",
    ".claude/skills/",
    ".github/workflows/",
]
SAFETY_COLUMNS = [
    "Claim",
    "Implemented?",
    "Not controlled",
    "Evidence",
    "Follow-up",
]
INVENTORY_COLUMNS = [
    "Consumer ファイル",
    "更新有無",
    "備考",
]
PLACEHOLDER_SNIPPETS = [
    "（例:",
    "変更対象 schema:",
    "（変更前のキー名・フィールド・型）",
    "（変更後のキー名・フィールド・型）",
    "（rg で列挙したファイル）",
    "yes / no / partial",
    "# before",
    "# after",
]


@dataclass(frozen=True)
class ValidationError:
    rule_id: str
    severity: Literal["error"]
    section: str
    line_start: int
    line_end: int
    message: str
    minimal_context: list[str]
    context_truncated: bool
    fix_hint: str = ""
    autofixable: bool = False


@dataclass(frozen=True)
class ValidationResult:
    schema: str
    target: str
    body_sha256: str
    status: Literal["pass", "fail"]
    errors: list[ValidationError]


def _extract_section(body: str, section_name: str) -> tuple[str, int, int] | None:
    lines = body.split("\n")
    pattern = re.compile(rf"^##\s+{re.escape(section_name)}\s*$", re.IGNORECASE)
    start_idx = None
    for idx, line in enumerate(lines):
        if pattern.match(line):
            start_idx = idx
            break
    if start_idx is None:
        return None

    end_idx = len(lines)
    for idx in range(start_idx + 1, len(lines)):
        if re.match(r"^##\s+", lines[idx]):
            end_idx = idx
            break

    content = "\n".join(lines[start_idx + 1:end_idx]).strip()
    return content, start_idx + 1, end_idx - 1


def _extract_sections(body: str) -> dict[str, tuple[str, int, int]]:
    sections: dict[str, tuple[str, int, int]] = {}
    lines = body.split("\n")
    for idx, line in enumerate(lines):
        if re.match(r"^##\s+", line):
            name = re.sub(r"^##\s+", "", line).strip()
            end_idx = len(lines)
            for next_idx in range(idx + 1, len(lines)):
                if re.match(r"^##\s+", lines[next_idx]):
                    end_idx = next_idx
                    break
            sections[name] = ("\n".join(lines[idx + 1:end_idx]).strip(), idx + 1, end_idx - 1)
    return sections


def _get_context_lines(
    body: str,
    start_line: int,
    end_line: int,
    max_lines: int = 5,
    max_bytes: int = 2048,
) -> tuple[list[str], bool]:
    lines = body.split("\n")
    start = max(0, start_line - 1)
    end = min(len(lines), end_line)
    context = lines[start:end][:max_lines]
    result: list[str] = []
    total_bytes = 0
    truncated = False
    for line in context:
        encoded = line.encode("utf-8")
        line_cost = len(encoded) + 1
        if total_bytes + line_cost > max_bytes:
            if total_bytes == 0:
                result.append(encoded[:max_bytes].decode("utf-8", errors="ignore"))
            truncated = True
            break
        result.append(line)
        total_bytes += line_cost
    return result, truncated


def _is_placeholder_text(text: str) -> bool:
    stripped = text.strip()
    if stripped.lower() in {"", "todo", "tbd"}:
        return True
    lowered = stripped.lower()
    if lowered == "n/a":
        return True
    return any(snippet.lower() in lowered for snippet in PLACEHOLDER_SNIPPETS)


def _parse_schema_decision(content: str) -> str | None:
    match = re.search(r"(?im)^\s*-\s*decision:\s*(.+?)\s*$", content)
    if not match:
        return None
    raw_value = match.group(1).strip().strip("`")
    if raw_value in SCHEMA_DECISIONS:
        return raw_value
    return raw_value


def _load_changed_paths(changed_paths_file: str | None) -> list[str] | None:
    if not changed_paths_file:
        return None
    paths = Path(changed_paths_file).read_text(encoding="utf-8").splitlines()
    return [path.strip() for path in paths if path.strip()]


def _is_safety_sensitive(changed_paths: list[str]) -> bool:
    for path in changed_paths:
        for pattern in SAFETY_SENSITIVE_PATH_PATTERNS:
            if pattern in path:
                return True
    return False


def _extract_notes_related_issue(notes_content: str) -> str | None:
    match = re.search(r"(?im)^\s*-\s*Related issue:\s*(.+?)\s*$", notes_content)
    if not match:
        return None
    value = match.group(1).strip()
    if value in {"", "N/A"}:
        return None
    return value


def _find_safety_header_line(content: str) -> tuple[list[str], int] | None:
    for index, line in enumerate(content.splitlines(), 1):
        if line.strip().startswith("|") and "Claim" in line and "Follow-up" in line:
            columns = [cell.strip() for cell in line.strip().strip("|").split("|")]
            return columns, index
    return None


def _validate_lp052(body: str, sections: dict[str, tuple[str, int, int]]) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for section_name in REQUIRED_SECTIONS:
        if section_name not in sections:
            errors.append(
                ValidationError(
                    rule_id="LP052",
                    severity="error",
                    section="(global)",
                    line_start=1,
                    line_end=1,
                    message=f"Missing required section: {section_name}",
                    minimal_context=["(Section not found)"],
                    context_truncated=False,
                    fix_hint=f"Add '## {section_name}' to the PR body.",
                )
            )
    return errors


def _validate_lp053(body: str, sections: dict[str, tuple[str, int, int]]) -> list[ValidationError]:
    info = sections.get("Schema Change Applicability")
    if not info:
        return []
    content, start_line, end_line = info
    decision = _parse_schema_decision(content)
    if decision in SCHEMA_DECISIONS:
        return []
    context, truncated = _get_context_lines(body, start_line, end_line)
    return [
        ValidationError(
            rule_id="LP053",
            severity="error",
            section="Schema Change Applicability",
            line_start=start_line,
            line_end=end_line,
            message="Schema Change Applicability decision is missing or invalid.",
            minimal_context=context,
            context_truncated=truncated,
            fix_hint="Set decision to schema_change, not_schema_change, or uncertain.",
        )
    ]


def _validate_lp050(body: str, sections: dict[str, tuple[str, int, int]]) -> list[ValidationError]:
    schema_info = sections.get("Schema Change Applicability")
    inventory_info = sections.get("Schema Consumer Inventory")
    if not schema_info or not inventory_info:
        return []

    decision = _parse_schema_decision(schema_info[0])
    if decision not in {"schema_change", "uncertain", "not_schema_change"}:
        return []

    content, start_line, end_line = inventory_info
    context, truncated = _get_context_lines(body, start_line, end_line)

    if decision == "not_schema_change":
        if re.search(r"(?i)\bN/A\b", content) and re.search(r"(?i)\breason\b", content):
            return []
        if re.search(r"(?i)\bN/A\b", content) and len(content.strip()) > 3:
            return []
        return []

    missing_parts: list[str] = []
    if _is_placeholder_text(content):
        missing_parts.append("placeholder content")
    if "# before" not in content.lower():
        missing_parts.append("before block")
    if "# after" not in content.lower():
        missing_parts.append("after block")
    if not all(column in content for column in INVENTORY_COLUMNS):
        missing_parts.append("consumer inventory table")

    if not missing_parts:
        return []

    return [
        ValidationError(
            rule_id="LP050",
            severity="error",
            section="Schema Consumer Inventory",
            line_start=start_line,
            line_end=end_line,
            message="Schema change PR requires non-placeholder inventory with before/after and consumer table.",
            minimal_context=context,
            context_truncated=truncated,
            fix_hint=f"Fill inventory details and remove missing parts: {', '.join(missing_parts)}.",
        )
    ]


def _validate_lp051(
    body: str,
    sections: dict[str, tuple[str, int, int]],
    changed_paths: list[str] | None,
) -> list[ValidationError]:
    if changed_paths is None or not _is_safety_sensitive(changed_paths):
        return []
    info = sections.get("Safety Claim Matrix")
    if not info:
        context = ["(Section not found)"]
        return [
            ValidationError(
                rule_id="LP051",
                severity="error",
                section="Safety Claim Matrix",
                line_start=1,
                line_end=1,
                message="Safety-sensitive PR requires Safety Claim Matrix.",
                minimal_context=context,
                context_truncated=False,
                fix_hint="Add Safety Claim Matrix with evidence and follow-up columns.",
            )
        ]

    content, start_line, end_line = info
    if _is_placeholder_text(content):
        context, truncated = _get_context_lines(body, start_line, end_line)
        return [
            ValidationError(
                rule_id="LP051",
                severity="error",
                section="Safety Claim Matrix",
                line_start=start_line,
                line_end=end_line,
                message="Safety-sensitive PR cannot leave Safety Claim Matrix empty or placeholder-only.",
                minimal_context=context,
                context_truncated=truncated,
                fix_hint="Fill concrete safety claims, evidence, and follow-up.",
            )
        ]
    return []


def _validate_lp055(body: str, sections: dict[str, tuple[str, int, int]]) -> list[ValidationError]:
    info = sections.get("Safety Claim Matrix")
    if not info:
        return []
    content, start_line, end_line = info
    if re.search(r"(?i)\bN/A\b", content) and re.search(r"(?i)\breason\b", content):
        return []
    header = _find_safety_header_line(content)
    if header is None:
        context, truncated = _get_context_lines(body, start_line, end_line)
        return [
            ValidationError(
                rule_id="LP055",
                severity="error",
                section="Safety Claim Matrix",
                line_start=start_line,
                line_end=end_line,
                message="Safety Claim Matrix header row is missing.",
                minimal_context=context,
                context_truncated=truncated,
                fix_hint="Add table header with Claim / Implemented? / Not controlled / Evidence / Follow-up.",
            )
        ]

    columns, relative_line = header
    missing = [column for column in SAFETY_COLUMNS if column not in columns]
    if not missing:
        return []
    line_no = start_line + relative_line - 1
    context, truncated = _get_context_lines(body, line_no, line_no)
    return [
        ValidationError(
            rule_id="LP055",
            severity="error",
            section="Safety Claim Matrix",
            line_start=line_no,
            line_end=line_no,
            message=f"Safety Claim Matrix header is missing columns: {', '.join(missing)}.",
            minimal_context=context,
            context_truncated=truncated,
            fix_hint="Restore all required Safety Claim Matrix columns.",
        )
    ]


def _validate_lp056(body: str, sections: dict[str, tuple[str, int, int]]) -> list[ValidationError]:
    info = sections.get("Safety Claim Matrix")
    if not info:
        return []
    content, start_line, _ = info
    if re.search(r"(?i)\bN/A\b", content) and re.search(r"(?i)\breason\b", content):
        return []
    lines = content.splitlines()
    for index, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 5:
            continue
        if cells[0] == "Claim" or re.match(r"^[-\s]+$", cells[0]):
            continue
        not_controlled = cells[2]
        follow_up = cells[4]
        if not_controlled and not_controlled.lower() not in {"", "n/a", "-", "none"}:
            if not re.search(r"#\d+", follow_up):
                line_no = start_line + index - 1
                context, truncated = _get_context_lines(body, line_no, line_no)
                return [
                    ValidationError(
                        rule_id="LP056",
                        severity="error",
                        section="Safety Claim Matrix",
                        line_start=line_no,
                        line_end=line_no,
                        message="Not controlled が非空の行には Follow-up の issue 番号が必要です。",
                        minimal_context=context,
                        context_truncated=truncated,
                        fix_hint="Add #<issue> to Follow-up for the uncontrolled claim.",
                    )
                ]
    return []


def _validate_lp057(body: str, sections: dict[str, tuple[str, int, int]]) -> list[ValidationError]:
    if re.search(r"(?i)\b(?:Closes|Refs)\s+#\d+\b", body):
        return []
    notes_info = sections.get("Notes")
    if notes_info and _extract_notes_related_issue(notes_info[0]):
        return []

    line_no = notes_info[1] if notes_info else 1
    context, truncated = _get_context_lines(body, line_no, line_no if notes_info else 1)
    return [
        ValidationError(
            rule_id="LP057",
            severity="error",
            section="Notes",
            line_start=line_no,
            line_end=line_no,
            message="final PR body must contain Closes/Refs or a filled Related issue reference.",
            minimal_context=context,
            context_truncated=truncated,
            fix_hint="Add Closes #N, Refs #N, or fill Related issue: with a concrete reference.",
        )
    ]


def _validate_lp058(body: str, changed_paths: list[str] | None) -> list[ValidationError]:
    if changed_paths is not None:
        return []
    return [
        ValidationError(
            rule_id="LP058",
            severity="error",
            section="(global)",
            line_start=1,
            line_end=1,
            message="changed paths could not be resolved deterministically.",
            minimal_context=["(changed paths unavailable)"],
            context_truncated=False,
            fix_hint="Pass --changed-paths-file or resolve changed paths from git diff before validation.",
        )
    ]


def validate_pr_body(body: str, changed_paths: list[str] | None) -> ValidationResult:
    body_sha256 = f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"
    sections = _extract_sections(body)
    errors: list[ValidationError] = []
    errors.extend(_validate_lp052(body, sections))
    errors.extend(_validate_lp053(body, sections))
    errors.extend(_validate_lp050(body, sections))
    errors.extend(_validate_lp051(body, sections, changed_paths))
    errors.extend(_validate_lp055(body, sections))
    errors.extend(_validate_lp056(body, sections))
    errors.extend(_validate_lp057(body, sections))
    errors.extend(_validate_lp058(body, changed_paths))
    status: Literal["pass", "fail"] = "fail" if errors else "pass"
    return ValidationResult(
        schema="loop_body_lint/v1",
        target="pr",
        body_sha256=body_sha256,
        status=status,
        errors=errors,
    )


def _error_to_dict(error: ValidationError) -> dict[str, object]:
    return asdict(error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate GitHub PR body against LOOP_PROTOCOL rules")
    parser.add_argument("--body-file", required=True, type=str)
    parser.add_argument("--changed-paths-file", type=str, default="")
    parser.add_argument("--linked-issue", required=True, type=int)
    args = parser.parse_args(argv)

    try:
        body = Path(args.body_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: Cannot read body file: {exc}", file=sys.stderr)
        return 2

    try:
        changed_paths = _load_changed_paths(args.changed_paths_file or None)
    except OSError as exc:
        print(f"ERROR: Cannot read changed-paths file: {exc}", file=sys.stderr)
        return 2

    result = validate_pr_body(body, changed_paths)
    print(
        json.dumps(
            {
                "schema": result.schema,
                "target": result.target,
                "body_sha256": result.body_sha256,
                "status": result.status,
                "errors": [_error_to_dict(error) for error in result.errors],
            },
            indent=2,
        )
    )
    return 1 if result.status == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
