#!/usr/bin/env python3
"""GitHub PR body validator for LOOP_PROTOCOL."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import yaml

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
SAFETY_COLUMNS = ["Claim", "Implemented?", "Not controlled", "Evidence", "Follow-up"]
INVENTORY_COLUMNS = ["Consumer ファイル", "更新有無", "備考"]
PLACEHOLDER_SNIPPETS = [
    "（例:",
    "（変更前のキー名・フィールド・型）",
    "（変更後のキー名・フィールド・型）",
    "（rg で列挙したファイル）",
    "yes / no / partial",
]
FENCE_PATTERN = re.compile(r"^(```|~~~)")
HEADING_PATTERN = re.compile(r"^##\s+(.+?)\s*$")
YAML_FENCE_PATTERN = re.compile(r"^```(?:yaml|yml)?\s*$", re.IGNORECASE)
YAML_BLOCK_MARKER = "SAFETY_CLAIMS_V1"
FOLLOW_UP_PATTERN = re.compile(r"#\d+")


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


def _get_context_lines(body: str, start_line: int, end_line: int, max_lines: int = 5, max_bytes: int = 2048) -> tuple[list[str], bool]:
    lines = body.split("\n")
    start = max(0, start_line - 1)
    end = min(len(lines), end_line)
    raw_context = lines[start:end]
    truncated = len(raw_context) > max_lines
    context = raw_context[:max_lines]
    result: list[str] = []
    total_bytes = 0
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
    return match.group(1).strip().strip("`")


def _load_changed_paths(changed_paths_file: str | None) -> list[str] | None:
    if not changed_paths_file:
        return None
    paths = Path(changed_paths_file).read_text(encoding="utf-8").splitlines()
    return [path.strip() for path in paths if path.strip()]


def _is_safety_sensitive(changed_paths: list[str]) -> bool:
    return any(pattern in path for path in changed_paths for pattern in SAFETY_SENSITIVE_PATH_PATTERNS)


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
            return [cell.strip() for cell in line.strip().strip("|").split("|")], index
    return None


def _extract_sections(body: str) -> tuple[dict[str, tuple[str, int, int]], dict[str, list[int]]]:
    lines = body.splitlines()
    headings: list[tuple[str, int]] = []
    duplicates: dict[str, list[int]] = {}
    in_fence = False
    fence_token = ""
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if FENCE_PATTERN.match(stripped):
            token = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_token = token
            elif token == fence_token:
                in_fence = False
                fence_token = ""
            continue
        if in_fence:
            continue
        match = HEADING_PATTERN.match(line)
        if not match:
            continue
        name = match.group(1).strip()
        duplicates.setdefault(name, []).append(idx)
        headings.append((name, idx))

    sections: dict[str, tuple[str, int, int]] = {}
    for index, (name, heading_line) in enumerate(headings):
        next_heading = headings[index + 1][1] if index + 1 < len(headings) else len(lines) + 1
        content = "\n".join(lines[heading_line:next_heading - 1]).strip()
        if name not in sections:
            sections[name] = (content, heading_line + 1, next_heading - 1)
    return sections, {name: locs for name, locs in duplicates.items() if len(locs) > 1}


def _extract_safety_claims_yaml(content: str) -> tuple[str | None, int | None, int | None]:
    lines = content.splitlines()
    in_yaml = False
    collected: list[str] = []
    start_line = None
    for idx, line in enumerate(lines, 1):
        if not in_yaml and YAML_FENCE_PATTERN.match(line.strip()):
            in_yaml = True
            start_line = idx + 1
            collected = []
            continue
        if in_yaml and line.strip() == "```":
            block = "\n".join(collected)
            if YAML_BLOCK_MARKER in block or re.search(r"(?m)^safety_claims:\s*$", block):
                return block, start_line, idx - 1
            in_yaml = False
            collected = []
            start_line = None
            continue
        if in_yaml:
            collected.append(line)
    return None, None, None


def _error(body: str, rule_id: str, section: str, line_start: int, line_end: int, message: str, fix_hint: str) -> ValidationError:
    context, truncated = _get_context_lines(body, line_start, line_end)
    return ValidationError(rule_id, "error", section, line_start, line_end, message, context, truncated, fix_hint)


def _validate_lp052(body: str, sections: dict[str, tuple[str, int, int]]) -> list[ValidationError]:
    errors = []
    for section_name in REQUIRED_SECTIONS:
        if section_name not in sections:
            errors.append(ValidationError("LP052", "error", "(global)", 1, 1, f"Missing required section: {section_name}", ["(Section not found)"], False, f"Add '## {section_name}' to the PR body."))
    return errors


def _validate_lp054(body: str, duplicates: dict[str, list[int]]) -> list[ValidationError]:
    return [_error(body, "LP054", name, locs[1], locs[1], f"Duplicate section heading is not allowed: {name}", f"Keep only one '## {name}' section in the PR body.") for name, locs in duplicates.items()]


def _validate_lp053(body: str, sections: dict[str, tuple[str, int, int]]) -> list[ValidationError]:
    info = sections.get("Schema Change Applicability")
    if not info:
        return []
    content, start_line, end_line = info
    decision = _parse_schema_decision(content)
    if decision in SCHEMA_DECISIONS:
        return []
    return [_error(body, "LP053", "Schema Change Applicability", start_line, end_line, "Schema Change Applicability decision is missing or invalid.", "Set decision to schema_change, not_schema_change, or uncertain.")]


def _validate_lp050(
    body: str,
    sections: dict[str, tuple[str, int, int]],
    schema_decision_override: str | None = None,
) -> list[ValidationError]:
    schema_info = sections.get("Schema Change Applicability")
    inventory_info = sections.get("Schema Consumer Inventory")
    if not schema_info or not inventory_info:
        return []
    decision = schema_decision_override or _parse_schema_decision(schema_info[0])
    if decision not in SCHEMA_DECISIONS or decision == "not_schema_change":
        return []
    content, start_line, end_line = inventory_info
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
    return [_error(body, "LP050", "Schema Consumer Inventory", start_line, end_line, "Schema change PR requires non-placeholder inventory with before/after and consumer table.", f"Fill inventory details and remove missing parts: {', '.join(missing_parts)}.")]


def _validate_lp051(body: str, sections: dict[str, tuple[str, int, int]], changed_paths: list[str] | None) -> list[ValidationError]:
    if changed_paths is None or not _is_safety_sensitive(changed_paths):
        return []
    info = sections.get("Safety Claim Matrix")
    if not info:
        return [ValidationError("LP051", "error", "Safety Claim Matrix", 1, 1, "Safety-sensitive PR requires Safety Claim Matrix.", ["(Section not found)"], False, "Add Safety Claim Matrix with evidence and follow-up columns." )]
    content, start_line, end_line = info
    has_na_reason = re.search(r"(?i)\bN/A\b", content) and re.search(r"(?i)\breason\b", content)
    if _is_placeholder_text(content) or has_na_reason:
        return [_error(body, "LP051", "Safety Claim Matrix", start_line, end_line, "Safety-sensitive PR cannot leave Safety Claim Matrix empty, placeholder-only, or N/A with reason.", "Fill concrete safety claims, evidence, and follow-up.")]
    return []


def _validate_lp055(body: str, sections: dict[str, tuple[str, int, int]], is_safety_sensitive: bool) -> list[ValidationError]:
    info = sections.get("Safety Claim Matrix")
    if not info:
        return []
    content, start_line, end_line = info
    if not is_safety_sensitive and re.search(r"(?i)\bN/A\b", content) and re.search(r"(?i)\breason\b", content):
        return []
    if _extract_safety_claims_yaml(content)[0] is not None:
        return []
    header = _find_safety_header_line(content)
    if header is None:
        return [_error(body, "LP055", "Safety Claim Matrix", start_line, end_line, "Safety Claim Matrix header row is missing.", "Add table header with Claim / Implemented? / Not controlled / Evidence / Follow-up.")]
    columns, relative_line = header
    missing = [column for column in SAFETY_COLUMNS if column not in columns]
    if not missing:
        return []
    line_no = start_line + relative_line - 1
    return [_error(body, "LP055", "Safety Claim Matrix", line_no, line_no, f"Safety Claim Matrix header is missing columns: {', '.join(missing)}.", "Restore all required Safety Claim Matrix columns.")]


def _validate_lp056(body: str, sections: dict[str, tuple[str, int, int]], is_safety_sensitive: bool) -> list[ValidationError]:
    info = sections.get("Safety Claim Matrix")
    if not info:
        return []
    content, start_line, _ = info
    if not is_safety_sensitive and re.search(r"(?i)\bN/A\b", content) and re.search(r"(?i)\breason\b", content):
        return []
    if _extract_safety_claims_yaml(content)[0] is not None:
        return []
    for index, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 5 or cells[0] == "Claim" or re.match(r"^[-\s]+$", cells[0]):
            continue
        not_controlled = cells[2]
        follow_up = cells[4]
        if not_controlled and not_controlled.lower() not in {"", "n/a", "-", "none"} and not FOLLOW_UP_PATTERN.search(follow_up):
            line_no = start_line + index - 1
            return [_error(body, "LP056", "Safety Claim Matrix", line_no, line_no, "Not controlled が非空の行には Follow-up の issue 番号が必要です。", "Add #<issue> to Follow-up for the uncontrolled claim.")]
    return []


def _validate_safety_claims_v1_yaml_contract(body: str, sections: dict[str, tuple[str, int, int]]) -> list[ValidationError]:
    info = sections.get("Safety Claim Matrix")
    if not info:
        return []
    content, start_line, _ = info
    yaml_block, rel_start, rel_end = _extract_safety_claims_yaml(content)
    if yaml_block is None:
        return []
    line_start = start_line + (rel_start or 1) - 1
    line_end = start_line + (rel_end or rel_start or 1) - 1
    try:
        payload = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        return [_error(body, "E_SAFETY_CLAIMS_PARSE_ERROR", "Safety Claim Matrix", line_start, line_end, f"SAFETY_CLAIMS_V1 YAML parse failed: {exc}", "Use yaml.safe_load-compatible YAML and remove unsafe tags or invalid syntax.")]
    if not isinstance(payload, dict) or not isinstance(payload.get("safety_claims"), list):
        return [_error(body, "E_SAFETY_CLAIMS_SCHEMA_INVALID", "Safety Claim Matrix", line_start, line_end, "SAFETY_CLAIMS_V1 must be a mapping with a safety_claims list.", "Set top-level key safety_claims: and provide a list of claim objects.")]
    for offset, claim in enumerate(payload["safety_claims"], 1):
        if not isinstance(claim, dict):
            return [_error(body, "E_SAFETY_CLAIMS_SCHEMA_INVALID", "Safety Claim Matrix", line_start, line_end, f"safety_claims[{offset}] must be a mapping.", "Each safety_claims entry must define claim, implemented, evidence, and optional follow_up.")]
        if not isinstance(claim.get("claim"), str) or not claim["claim"].strip():
            return [_error(body, "E_SAFETY_CLAIMS_SCHEMA_INVALID", "Safety Claim Matrix", line_start, line_end, f"safety_claims[{offset}].claim must be a non-empty string.", "Update SAFETY_CLAIMS_V1 to match docs/dev/runtime-verification-policy.md.")]
        if claim.get("implemented") not in {"yes", "partial", "no"}:
            return [_error(body, "E_SAFETY_CLAIMS_SCHEMA_INVALID", "Safety Claim Matrix", line_start, line_end, f"safety_claims[{offset}].implemented must be yes, partial, or no.", "Update SAFETY_CLAIMS_V1 to match docs/dev/runtime-verification-policy.md.")]
        evidence = claim.get("evidence")
        if not isinstance(evidence, list) or not evidence or not all(isinstance(item, str) and item.strip() for item in evidence):
            return [_error(body, "E_SAFETY_CLAIMS_SCHEMA_INVALID", "Safety Claim Matrix", line_start, line_end, f"safety_claims[{offset}].evidence must contain at least one non-empty string.", "Update SAFETY_CLAIMS_V1 to match docs/dev/runtime-verification-policy.md.")]
        not_controlled = claim.get("not_controlled", []) or []
        if not isinstance(not_controlled, list) or not all(isinstance(item, str) and item.strip() for item in not_controlled):
            return [_error(body, "E_SAFETY_CLAIMS_SCHEMA_INVALID", "Safety Claim Matrix", line_start, line_end, f"safety_claims[{offset}].not_controlled must be a list of non-empty strings when present.", "Update SAFETY_CLAIMS_V1 to match docs/dev/runtime-verification-policy.md.")]
        follow_up = claim.get("follow_up", []) or []
        if not isinstance(follow_up, list) or not all(isinstance(item, str) and item.strip() for item in follow_up):
            return [_error(body, "E_SAFETY_CLAIMS_SCHEMA_INVALID", "Safety Claim Matrix", line_start, line_end, f"safety_claims[{offset}].follow_up must be a list of non-empty strings when present.", "Update SAFETY_CLAIMS_V1 to match docs/dev/runtime-verification-policy.md.")]
        if not_controlled and not follow_up:
            return [_error(body, "E_FOLLOW_UP_MISSING_CONTRACT", "Safety Claim Matrix", line_start, line_end, f"safety_claims[{offset}] has not_controlled entries but follow_up is empty or missing.", "Add at least one #<issue> reference to follow_up for every uncontrolled claim.")]
        if not_controlled and not all(FOLLOW_UP_PATTERN.fullmatch(item.strip()) for item in follow_up):
            return [_error(body, "E_FOLLOW_UP_MISSING_CONTRACT", "Safety Claim Matrix", line_start, line_end, f"safety_claims[{offset}] has not_controlled entries but follow_up does not contain only #<issue> references.", "Add #<issue> references to follow_up for every uncontrolled claim.")]
    return []


def _validate_lp057(body: str, sections: dict[str, tuple[str, int, int]], linked_issue: int | None = None) -> list[ValidationError]:
    closes_match = re.search(r"(?i)\bCloses\s+#(\d+)\b", body)
    refs_match = re.search(r"(?i)\bRefs\s+#(\d+)\b", body)
    if closes_match or refs_match:
        match = closes_match or refs_match
        matched_issue = int(match.group(1))
        if linked_issue is not None and matched_issue != linked_issue:
            return [_error(body, "LP057", "Notes", 1, 1, f"PR body references #{matched_issue} but linked issue is #{linked_issue}.", f"Update Closes/Refs to reference #{linked_issue}.")]
        return []
    notes_info = sections.get("Notes")
    if notes_info:
        notes_related = _extract_notes_related_issue(notes_info[0])
        if notes_related:
            try:
                notes_issue = int(notes_related.lstrip("#"))
                if linked_issue is not None and notes_issue != linked_issue:
                    return [_error(body, "LP057", "Notes", notes_info[1], notes_info[1], f"Related issue references #{notes_issue} but linked issue is #{linked_issue}.", f"Update Related issue to reference #{linked_issue}.")]
                return []
            except (ValueError, AttributeError):
                pass
    line_no = notes_info[1] if notes_info else 1
    return [_error(body, "LP057", "Notes", line_no, line_no, "final PR body must contain Closes/Refs or a filled Related issue reference.", "Add Closes #N, Refs #N, or fill Related issue: with a concrete reference.")]


def _validate_lp058(body: str, changed_paths: list[str] | None) -> list[ValidationError]:
    if changed_paths is not None and len(changed_paths) > 0:
        return []
    return [ValidationError("LP058", "error", "(global)", 1, 1, "changed paths could not be resolved deterministically.", ["(changed paths unavailable)"], False, "Pass --changed-paths-file or resolve changed paths from git diff before validation.")]


def validate_pr_body(
    body: str,
    changed_paths: list[str] | None,
    linked_issue: int | None = None,
    schema_decision_override: str | None = None,
) -> ValidationResult:
    body_sha256 = f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"
    sections, duplicates = _extract_sections(body)
    is_safety_sensitive = changed_paths is not None and _is_safety_sensitive(changed_paths)
    errors: list[ValidationError] = []
    errors.extend(_validate_lp052(body, sections))
    errors.extend(_validate_lp054(body, duplicates))
    errors.extend(_validate_lp053(body, sections))
    errors.extend(_validate_lp050(body, sections, schema_decision_override))
    errors.extend(_validate_lp051(body, sections, changed_paths))
    errors.extend(_validate_lp055(body, sections, is_safety_sensitive))
    errors.extend(_validate_lp056(body, sections, is_safety_sensitive))
    errors.extend(_validate_safety_claims_v1_yaml_contract(body, sections))
    errors.extend(_validate_lp057(body, sections, linked_issue))
    errors.extend(_validate_lp058(body, changed_paths))
    return ValidationResult("loop_body_lint/v1", "pr", body_sha256, "fail" if errors else "pass", errors)


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
    result = validate_pr_body(body, changed_paths, args.linked_issue)
    print(json.dumps({"schema": result.schema, "target": result.target, "body_sha256": result.body_sha256, "status": result.status, "errors": [_error_to_dict(error) for error in result.errors]}, indent=2))
    return 1 if result.status == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
