#!/usr/bin/env python3
"""
scope_signal_delta.py

Deterministic read-only CLI and library for scope signal delta analysis.

Input (stdin JSON):
  SCOPE_SIGNAL_DELTA_INPUT_V1

Output (stdout JSON):
  SCOPE_SIGNAL_DELTA_V1
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = "scope_signal_delta/v1"

REASON_NEW_IN_SCOPE = "new_in_scope_area"
REASON_NEW_ALLOWED_PATH_LAYER = "new_allowed_path_layer"
REASON_NEW_UNVERIFIABLE_AC = "new_unverifiable_ac"
REASON_NO_SCOPE_SIGNAL = "no_scope_signal"

SUBJECTIVE_KEYWORDS = (
    "適切に",
    "品質を改善",
    "安定する",
    "改善する",
    "最適化",
    "高品質に",
    "improve",
    "enhance",
    "optimize",
    "stabilize",
    "appropriately",
)

PATH_TOKEN_RE = re.compile(r"`(?P<path>[^`\n]+)`|(?P<bare>(?:\.claude|docs|src|scripts|tests|\.github)/[^\s|`]+)")
HEADING_RE = re.compile(r"^[ ]{0,3}##[ \t]+(?P<title>.+?)[ \t#]*$")

INPUT_REQUIRED_FIELDS = ("before_body", "current_body", "after_body", "source_refs")
INPUT_SOURCE_REF_KEYS = ("before", "current", "after")


@dataclass(frozen=True)
class SourceLine:
    number: int
    text: str


def _leading_space_count(line: str) -> int:
    count = 0
    for ch in line:
        if ch == " ":
            count += 1
        else:
            break
    return count


def _parse_fence_opener(line: str) -> tuple[str, int] | None:
    if _leading_space_count(line) > 3:
        return None
    content = line.lstrip(" ")
    if not content or content[0] not in ("`", "~"):
        return None
    fence_char = content[0]
    run_len = 0
    for ch in content:
        if ch == fence_char:
            run_len += 1
        else:
            break
    if run_len < 3:
        return None
    return fence_char, run_len


def _is_fence_closer(line: str, fence_char: str, fence_len: int) -> bool:
    if _leading_space_count(line) > 3:
        return False
    content = line.lstrip(" ").rstrip()
    if not content or any(ch != fence_char for ch in content):
        return False
    return len(content) >= fence_len


def _parse_heading(line: str) -> str | None:
    if _leading_space_count(line) > 3:
        return None
    match = HEADING_RE.match(line.rstrip())
    if not match:
        return None
    return match.group("title").strip()


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def extract_sections(text: str, *, semantic_only: bool = False) -> dict[str, str]:
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_content: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in text.splitlines():
        if not in_fence:
            opener = _parse_fence_opener(line)
            if opener is not None:
                fence_char, fence_len = opener
                in_fence = True
                if current_section is not None and not semantic_only:
                    current_content.append(line)
                continue
        else:
            if _is_fence_closer(line, fence_char, fence_len):
                in_fence = False
                fence_char = ""
                fence_len = 0
                if current_section is not None and not semantic_only:
                    current_content.append(line)
                continue
            if current_section is not None and not semantic_only:
                current_content.append(line)
            continue

        heading = _parse_heading(line)
        if heading is not None:
            if current_section is not None:
                sections[current_section] = "\n".join(current_content).strip()
            current_section = heading
            current_content = []
        elif current_section is not None:
            current_content.append(line)

    if current_section is not None:
        sections[current_section] = "\n".join(current_content).strip()

    return sections


def _extract_sections(text: str) -> dict[str, str]:
    return extract_sections(text)


def _find_section_line_offset(text: str, section_name: str) -> int:
    in_fence = False
    fence_char = ""
    fence_len = 0
    for index, line in enumerate(text.splitlines(), start=1):
        if not in_fence:
            opener = _parse_fence_opener(line)
            if opener is not None:
                fence_char, fence_len = opener
                in_fence = True
                continue
        else:
            if _is_fence_closer(line, fence_char, fence_len):
                in_fence = False
                fence_char = ""
                fence_len = 0
            continue

        heading = _parse_heading(line)
        if heading == section_name:
            return index + 1
    return 1


def iter_section_lines(
    text: str,
    section_name: str,
    *,
    semantic_only: bool = False,
) -> list[SourceLine]:
    lines: list[SourceLine] = []
    current_section: str | None = None
    in_fence = False
    fence_char = ""
    fence_len = 0

    for index, line in enumerate(text.splitlines(), start=1):
        if not in_fence:
            opener = _parse_fence_opener(line)
            if opener is not None:
                fence_char, fence_len = opener
                in_fence = True
                if current_section == section_name and not semantic_only:
                    lines.append(SourceLine(index, line))
                continue
        else:
            if _is_fence_closer(line, fence_char, fence_len):
                in_fence = False
                fence_char = ""
                fence_len = 0
                if current_section == section_name and not semantic_only:
                    lines.append(SourceLine(index, line))
                continue
            if current_section == section_name and not semantic_only:
                lines.append(SourceLine(index, line))
            continue

        heading = _parse_heading(line)
        if heading is not None:
            current_section = heading
            continue
        if current_section == section_name:
            lines.append(SourceLine(index, line))

    return lines


def _iter_section_lines(text: str, section_name: str) -> list[SourceLine]:
    return iter_section_lines(text, section_name)


def _normalize_path(path: str) -> str:
    normalized = path.strip().strip("`").strip()
    normalized = normalized.rstrip("/")
    return normalized.replace("\\", "/")


def _extract_path_items(text: str, section_name: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    for source_line in iter_section_lines(text, section_name, semantic_only=True):
        raw_line = source_line.text
        for match in PATH_TOKEN_RE.finditer(raw_line):
            candidate = match.group("path") or match.group("bare") or ""
            normalized = _normalize_path(candidate)
            if not normalized:
                continue
            items.append(
                {
                    "value": normalized,
                    "start_line": source_line.number,
                    "end_line": source_line.number,
                    "text_sha256": _sha256(raw_line),
                }
            )
    return items


def _extract_in_scope_layers(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    prefixes = (".claude/", "docs/", "src/", "scripts/", "tests/", ".github/")

    for source_line in iter_section_lines(text, "In Scope", semantic_only=True):
        raw_line = source_line.text

        # Note: a single path token (e.g. a backtick-quoted or bare path) may
        # contain more than one of the known prefixes as an embedded
        # substring (for example ".claude/skills/<skill>/tests/<file>.py"
        # contains both ".claude/" and "tests/"). Counting that as two
        # independent layer mentions is a false positive (Issue #1327). We
        # instead extract whole path-like tokens via PATH_TOKEN_RE and only
        # attribute a prefix to a token when the token itself *starts with*
        # that prefix, so a prefix appearing mid-token never counts as an
        # extra layer.
        line_prefixes: set[str] = set()
        for match in PATH_TOKEN_RE.finditer(raw_line):
            candidate = _normalize_path(match.group("path") or match.group("bare") or "")
            for prefix in prefixes:
                if candidate.startswith(prefix):
                    line_prefixes.add(prefix)
                    break
        for prefix in prefixes:
            if prefix in line_prefixes:
                items.append(
                    {
                        "value": prefix.rstrip("/"),
                        "start_line": source_line.number,
                        "end_line": source_line.number,
                        "text_sha256": _sha256(raw_line),
                    }
                )
    return items


def _extract_ac_items(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for source_line in iter_section_lines(text, "Acceptance Criteria", semantic_only=True):
        raw_line = source_line.text
        stripped = raw_line.lstrip()
        if stripped.startswith("- [ ]") or stripped.startswith("- [x]"):
            normalized = re.sub(r"\s+", " ", stripped).strip()
            items.append(
                {
                    "value": normalized,
                    "start_line": source_line.number,
                    "end_line": source_line.number,
                    "text_sha256": _sha256(raw_line),
                    "is_low_verifiability": any(keyword in raw_line for keyword in SUBJECTIVE_KEYWORDS),
                }
            )
    return items


def _to_value_map(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    value_map: dict[str, dict[str, Any]] = {}
    for item in items:
        value_map.setdefault(item["value"], item)
    return value_map


def _top_level_layer(path: str) -> str | None:
    if "/" not in path:
        return None
    return path.split("/", 1)[0]


def _triggering_lines(
    body_version: str,
    source_ref: str | None,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "body_version": body_version,
            "source_ref": source_ref,
            "coordinate_space": "body_absolute_1_based",
            "start_line": item["start_line"],
            "end_line": item["end_line"],
            "text_sha256": item["text_sha256"],
        }
        for item in items
    ]


def compute_scope_signal_delta(payload: dict[str, Any]) -> dict[str, Any]:
    payload = _validate_input(payload)
    before_body = payload["before_body"]
    current_body = payload["current_body"]
    after_body = payload["after_body"]
    source_refs = payload.get("source_refs") or {}

    before_allowed = _extract_path_items(before_body, "Allowed Paths")
    current_allowed = _extract_path_items(current_body, "Allowed Paths")
    after_allowed = _extract_path_items(after_body, "Allowed Paths")

    before_allowed_map = _to_value_map(before_allowed)
    current_allowed_map = _to_value_map(current_allowed)
    after_allowed_map = _to_value_map(after_allowed)

    before_in_scope = _extract_in_scope_layers(before_body)
    after_in_scope = _extract_in_scope_layers(after_body)
    before_in_scope_map = _to_value_map(before_in_scope)
    after_in_scope_map = _to_value_map(after_in_scope)

    before_ac = _extract_ac_items(before_body)
    after_ac = _extract_ac_items(after_body)
    before_ac_map = _to_value_map(before_ac)
    after_ac_map = _to_value_map(after_ac)

    before_allowed_values = set(before_allowed_map)
    current_allowed_values = set(current_allowed_map)
    after_allowed_values = set(after_allowed_map)
    added_allowed_values = sorted(after_allowed_values - before_allowed_values)
    removed_allowed_values = sorted(before_allowed_values - after_allowed_values)
    repeated_allowed_values = sorted(after_allowed_values & before_allowed_values)

    before_allowed_layers = sorted(
        layer for layer in {_top_level_layer(value) for value in before_allowed_values} if layer
    )
    current_allowed_layers = sorted(
        layer for layer in {_top_level_layer(value) for value in current_allowed_values} if layer
    )
    after_allowed_layers = sorted(
        layer for layer in {_top_level_layer(value) for value in after_allowed_values} if layer
    )
    added_allowed_layers = sorted(set(after_allowed_layers) - set(before_allowed_layers))
    removed_allowed_layers = sorted(set(before_allowed_layers) - set(after_allowed_layers))
    repeated_allowed_layers = sorted(set(after_allowed_layers) & set(before_allowed_layers))

    before_in_scope_values = set(before_in_scope_map)
    after_in_scope_values = set(after_in_scope_map)
    added_in_scope_layers = sorted(after_in_scope_values - before_in_scope_values)
    repeated_in_scope_layers = sorted(after_in_scope_values & before_in_scope_values)

    added_low_verifiability = [
        item["value"]
        for item in after_ac
        if item["value"] not in before_ac_map and item["is_low_verifiability"]
    ]

    signals: list[dict[str, Any]] = []

    low_verifiability_items = [after_ac_map[value] for value in added_low_verifiability]
    signals.append(
        {
            "reason_code": REASON_NEW_UNVERIFIABLE_AC,
            "triggered": bool(low_verifiability_items),
            "normalized_value": added_low_verifiability,
            "triggering_lines": _triggering_lines("after", source_refs.get("after"), low_verifiability_items),
        }
    )

    added_allowed_items = [
        after_allowed_map[value]
        for value in added_allowed_values
        if _top_level_layer(value) in set(added_allowed_layers)
    ]
    signals.append(
        {
            "reason_code": REASON_NEW_ALLOWED_PATH_LAYER,
            "triggered": bool(added_allowed_layers),
            "normalized_value": added_allowed_layers,
            "triggering_lines": _triggering_lines("after", source_refs.get("after"), added_allowed_items),
        }
    )

    added_in_scope_items = [after_in_scope_map[value] for value in added_in_scope_layers]
    signals.append(
        {
            "reason_code": REASON_NEW_IN_SCOPE,
            "triggered": bool(added_in_scope_layers),
            "normalized_value": added_in_scope_layers,
            "triggering_lines": _triggering_lines("after", source_refs.get("after"), added_in_scope_items),
        }
    )

    legacy_reason = REASON_NO_SCOPE_SIGNAL
    legacy_triggered = False
    selected_triggering_lines: list[dict[str, Any]] = []
    for reason_code in (
        REASON_NEW_UNVERIFIABLE_AC,
        REASON_NEW_ALLOWED_PATH_LAYER,
        REASON_NEW_IN_SCOPE,
    ):
        signal = next(item for item in signals if item["reason_code"] == reason_code)
        if signal["triggered"]:
            legacy_reason = reason_code
            legacy_triggered = True
            selected_triggering_lines = signal["triggering_lines"]
            break

    result = {
        "schema_version": SCHEMA_VERSION,
        "inputs": {
            "before_body_sha256": _sha256(before_body),
            "current_body_sha256": _sha256(current_body),
            "after_body_sha256": _sha256(after_body),
            "source_refs": {
                "before": source_refs.get("before"),
                "current": source_refs.get("current"),
                "after": source_refs.get("after"),
            },
        },
        "sections": {
            "allowed_paths": {
                "before": sorted(before_allowed_values),
                "current": sorted(current_allowed_values),
                "after": sorted(after_allowed_values),
                "added": added_allowed_values,
                "removed": removed_allowed_values,
                "repeated_existing": repeated_allowed_values,
                "before_layers": before_allowed_layers,
                "current_layers": current_allowed_layers,
                "after_layers": after_allowed_layers,
                "added_layers": added_allowed_layers,
                "removed_layers": removed_allowed_layers,
                "repeated_existing_layers": repeated_allowed_layers,
            },
            "in_scope": {
                "before_layers": sorted(before_in_scope_values),
                "after_layers": sorted(after_in_scope_values),
                "added_layers": added_in_scope_layers,
                "repeated_existing_layers": repeated_in_scope_layers,
            },
            "acceptance_criteria": {
                "added_low_verifiability_items": added_low_verifiability,
                "before": sorted(before_ac_map),
                "after": sorted(after_ac_map),
            },
        },
        "signals": signals,
        "legacy_scope_signal_guard": {
            "triggered": legacy_triggered,
            "reason_code": legacy_reason,
            "excluded_by_anchor_reframe": False,
            "triggering_lines": selected_triggering_lines,
        },
        "suppressions": {
            "anchor_reframe": {
                "status": "not_applicable",
                "implementation_go": False,
                "required_rerun": [],
            }
        },
    }
    return result


def _validate_input(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("input must be an object")
    unknown_fields = sorted(set(payload) - set(INPUT_REQUIRED_FIELDS))
    if unknown_fields:
        raise ValueError(f"unknown input fields: {', '.join(unknown_fields)}")
    for field in ("before_body", "current_body", "after_body"):
        if not isinstance(payload.get(field), str):
            raise ValueError(f"{field} must be a string")
    source_refs = payload.get("source_refs")
    if not isinstance(source_refs, dict):
        raise ValueError("source_refs must be an object")
    unknown_source_ref_keys = sorted(set(source_refs) - set(INPUT_SOURCE_REF_KEYS))
    if unknown_source_ref_keys:
        raise ValueError(f"unknown source_refs fields: {', '.join(unknown_source_ref_keys)}")
    for key in INPUT_SOURCE_REF_KEYS:
        if key not in source_refs:
            raise ValueError(f"source_refs.{key} is required")
        value = source_refs.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"source_refs.{key} must be a string or null")
    return payload


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = compute_scope_signal_delta(_validate_input(payload))
        print(_canonical_json(result))
        return 0
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"invalid_json: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - fail-closed CLI guard
        print(json.dumps({"error": f"internal_error: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# SCOPE_DELTA_AUTHORITY_V1 (#1323) -- classify scope delta authority
# (ai_inferred / human_review_directive / existing_parent_contract /
# related_issue_dependency), additive to SCOPE_SIGNAL_GUARD_DECISION_V2
# (#1090). Supersedes #1008 (trusted anchor scope amendment) and #1011
# (CONTEXT_PROVENANCE_V1) by folding both gaps into a single decision shape.
# ---------------------------------------------------------------------------

SCOPE_DELTA_AUTHORITY_SCHEMA_VERSION = "SCOPE_DELTA_AUTHORITY_V1"
SCOPE_DELTA_AUTHORITY_EVIDENCE_SCHEMA_VERSION = "SCOPE_DELTA_AUTHORITY_EVIDENCE_V1"
CONTRACT_PATCH_PLAN_SCHEMA_VERSION = "CONTRACT_PATCH_PLAN_V1"

AUTHORITY_CATEGORY_AI_INFERRED = "ai_inferred"
AUTHORITY_CATEGORY_HUMAN_REVIEW_DIRECTIVE = "human_review_directive"
AUTHORITY_CATEGORY_EXISTING_PARENT_CONTRACT = "existing_parent_contract"
AUTHORITY_CATEGORY_RELATED_ISSUE_DEPENDENCY = "related_issue_dependency"

DIRECTIVE_CONFIDENCE_EXPLICIT = "explicit"
DIRECTIVE_CONFIDENCE_AMBIGUOUS = "ambiguous"
DIRECTIVE_CONFIDENCE_CONFLICTING = "conflicting"
DIRECTIVE_CONFIDENCE_INFERRED = "inferred"

SCOPE_DELTA_AUTHORITY_ROUTE_CONTRACT_UPDATE_REQUIRED = "contract_update_required"
SCOPE_DELTA_AUTHORITY_ROUTE_HUMAN_ESCALATION = "human_escalation"
SCOPE_DELTA_AUTHORITY_ROUTE_NOT_TRIGGERED = "not_triggered"

REASON_EXPLICIT_HUMAN_CONTRACT_DIRECTIVE = "explicit_human_contract_directive"
REASON_AMBIGUOUS_HUMAN_DIRECTIVE = "ambiguous_human_directive"
REASON_CONFLICTING_HUMAN_DIRECTIVES = "conflicting_human_directives"
REASON_REQUIRES_ISSUE_SPLIT = "requires_issue_split"
REASON_EXPANDS_ALLOWED_PATHS = "expands_allowed_paths"
REASON_CHANGES_PERMISSION_BOUNDARY = "changes_permission_boundary"
REASON_CHANGES_EXTERNAL_SERVICE_BOUNDARY = "changes_external_service_boundary"
REASON_DESTRUCTIVE_OR_NON_IDEMPOTENT_OPERATION = "destructive_or_non_idempotent_operation"
REASON_AI_INFERRED_SCOPE_DELTA = "ai_inferred_scope_delta"
REASON_UNTRUSTED_AUTHOR_ASSOCIATION = "untrusted_author_association"
REASON_MISSING_BASE_ISSUE_BODY_SHA256 = "missing_base_issue_body_sha256"

NEXT_STEP_RERUN_REFINEMENT_AFTER_CONTRACT_UPDATE = "rerun_refinement_after_contract_update"

SCOPE_DELTA_AUTHORITY_TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

_BOUNDARY_FLAG_KEYS = (
    "expands_allowed_paths",
    "changes_permission_boundary",
    "changes_external_service_boundary",
    "destructive_or_non_idempotent_operation",
    "requires_issue_split",
)

_BOUNDARY_KEYWORD_PATTERNS = {
    "changes_permission_boundary": re.compile(
        r"(permission escalat|権限昇格|privilege escalat|grant.*(sudo|root)|sudo access|root access)",
        re.IGNORECASE,
    ),
    "changes_external_service_boundary": re.compile(
        r"(external service|外部サービス|third[- ]party api|external api|call.*external)",
        re.IGNORECASE,
    ),
    "destructive_or_non_idempotent_operation": re.compile(
        r"(destructive|irreversible|non-idempotent|破壊的|force[- ]push|rm -rf|drop table)",
        re.IGNORECASE,
    ),
    "requires_issue_split": re.compile(
        r"(split into (multiple|separate) issues?|別[Ii]ssue.*分割|複数.*[Ii]ssue.*分割|issue.*split)",
        re.IGNORECASE,
    ),
}

_DIRECTIVE_SECTION_MARKERS = (
    "revised acceptance criteria",
    "revised ac",
    "stop condition",
    "precondition",
    "前提条件",
    "allowed paths",
    "allowed paths expansion",
    "verification command",
)

_BULLET_LINE_RE = re.compile(r"^\s*[-*]\s+\S.*$", re.MULTILINE)

_ISSUE_COMMENT_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
    r"/issues/(?P<issue_number>\d+)#issuecomment-(?P<comment_id>\d+)$"
)
_PR_URL_FRAGMENT_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/\d+#")


def detect_boundary_flags(text: "str | None", *, expands_allowed_paths: bool = False) -> dict:
    """AC18: deterministic keyword-based boundary-flag detection.

    `text` is scanned for keywords indicating permission escalation, external
    service usage, destructive/non-idempotent operations, or a need to split
    into a separate Issue. `expands_allowed_paths` is supplied by the caller
    (derived from an actual Allowed Paths diff, not text matching).
    """
    haystack = text or ""
    flags = {
        name: bool(pattern.search(haystack))
        for name, pattern in _BOUNDARY_KEYWORD_PATTERNS.items()
    }
    flags["expands_allowed_paths"] = bool(expands_allowed_paths)
    return {key: flags.get(key, False) for key in _BOUNDARY_FLAG_KEYS}


def extract_directive_markers(text: "str | None") -> list:
    """Detect known directive section markers (Revised AC, Stop Condition, ...)."""
    lowered = (text or "").lower()
    return sorted({marker for marker in _DIRECTIVE_SECTION_MARKERS if marker in lowered})


def extract_directive_items(text: "str | None") -> list:
    """Extract bullet-list lines (candidate directive text) from a comment body."""
    items = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") or stripped.startswith("* "):
            content = stripped[2:].strip()
            if content:
                items.append(content)
    return items


def classify_directive_confidence(text: "str | None", markers: "list | None" = None) -> str:
    """Deterministic confidence classification for a single review comment.

    - no directive markers found -> inferred (the AI would have to infer intent)
    - markers found + at least one bullet-list line -> explicit
    - markers found but no structured bullet list -> ambiguous
    """
    marker_list = markers if markers is not None else extract_directive_markers(text)
    if not marker_list:
        return DIRECTIVE_CONFIDENCE_INFERRED
    if _BULLET_LINE_RE.search(text or ""):
        return DIRECTIVE_CONFIDENCE_EXPLICIT
    return DIRECTIVE_CONFIDENCE_AMBIGUOUS


def parse_issue_comment_url(url: "str | None") -> "dict | None":
    """AC16: structurally parse a GitHub issue-comment URL.

    Returns None for PR review comment/discussion URLs, malformed URLs, or
    non-issue-comment hosts/paths (fail-closed, no substring matching).
    """
    if not isinstance(url, str) or not url:
        return None
    if _PR_URL_FRAGMENT_RE.match(url):
        return None
    match = _ISSUE_COMMENT_URL_RE.match(url)
    if not match:
        return None
    return {
        "owner": match.group("owner"),
        "repo": match.group("repo"),
        "issue_number": int(match.group("issue_number")),
        "comment_id": match.group("comment_id"),
    }


def validate_scope_delta_authority_evidence_url(
    evidence: dict,
    *,
    target_issue_number=None,
    expected_repo=None,
) -> bool:
    """AC16: fail-closed structural URL validation for issue_comment /
    pull_request_review evidence.

    Non issue_comment/pull_request_review source_kind values are not subject
    to this check (parent_issue / related_issue / agent_inferred evidence do
    not carry an issue-comment URL to validate).

    PR #1332 review fix (P0/P1):
    - issue_comment evidence now requires `expected_repo` to be provided by
      the caller (classify_scope_delta_authority) so that a same-issue-number
      URL from a *different* repository is fail-closed rejected rather than
      silently accepted when the caller forgets to pass expected_repo.
    - pull_request_review evidence is not yet structurally verifiable against
      `pull_request_url` / `_links.pull_request` (SCOPE_DELTA_AUTHORITY_EVIDENCE_V1
      does not carry those fields today), so it is fail-closed rejected
      unconditionally until a follow-up adds that verification. A genuine
      issue-comment URL mislabeled as pull_request_review is still detected
      and rejected explicitly for a clearer reason, but the net effect (not
      accepted) is unchanged.
    """
    source_kind = evidence.get("source_kind")
    if source_kind not in ("issue_comment", "pull_request_review"):
        return True
    if source_kind == "pull_request_review":
        # Fail-closed: pull_request_review evidence cannot yet be verified
        # against pull_request_url / _links.pull_request / repo / PR number,
        # so it is never accepted (AC16 hardening, PR #1332 review).
        return False

    if not expected_repo:
        # Fail-closed: repo/owner cross-check is mandatory for issue_comment
        # evidence. Without expected_repo we cannot rule out a same-issue-
        # number URL pointed at a different repository.
        return False

    parsed = parse_issue_comment_url(evidence.get("comment_url"))
    if parsed is None:
        return False
    expected_parts = expected_repo.lower().split("/", 1)
    if [parsed["owner"].lower(), parsed["repo"].lower()] != expected_parts:
        return False
    if target_issue_number is not None and parsed["issue_number"] != target_issue_number:
        return False
    issue_url = evidence.get("issue_url")
    if issue_url and target_issue_number is not None:
        expected_html = f"https://github.com/{expected_repo}/issues/{target_issue_number}"
        expected_api = f"https://api.github.com/repos/{expected_repo}/issues/{target_issue_number}"
        if issue_url.lower() not in (expected_html.lower(), expected_api.lower()):
            return False
    return True


class ContractPatchPlanBaseShaMissingError(ValueError):
    """Raised by build_contract_patch_plan_v1() when base_issue_body_sha256
    is missing (PR #1332 review fix, P1). CONTRACT_PATCH_PLAN_V1 is applied
    against a specific Issue body snapshot by issue-author/edit-issue; a null
    base sha256 would let a stale or mismatched Issue body be patched
    (wrong section, lost concurrent edits) with no way to detect it.
    """


def build_contract_patch_plan_v1(
    *,
    target_issue_number,
    base_issue_body_sha256,
    source_evidence: list,
    operations: list,
) -> dict:
    """AC3/AC19: build a CONTRACT_PATCH_PLAN_V1 payload.

    Generation-only: `forbidden` always lists direct_github_write and
    implementation_phase_transition so the plan can never be mistaken for an
    executed write. Callers (issue-author / edit-issue skill) apply the plan.

    PR #1332 review fix (P1): base_issue_body_sha256 is mandatory and
    non-null (contract_patch_plan_v1.schema.json now requires a non-empty
    string) -- callers must resolve a real Issue body sha256 before a
    contract_update_required route can be materialized into a patch plan.
    """
    if not base_issue_body_sha256:
        raise ContractPatchPlanBaseShaMissingError(
            "base_issue_body_sha256 is required (non-null, non-empty) to "
            "build a CONTRACT_PATCH_PLAN_V1 -- refusing to generate a patch "
            "plan against an unresolved Issue body snapshot"
        )
    return {
        "schema_version": CONTRACT_PATCH_PLAN_SCHEMA_VERSION,
        "target_issue_number": target_issue_number,
        "base_issue_body_sha256": base_issue_body_sha256,
        "source_evidence": source_evidence,
        "operations": operations,
        "forbidden": ["direct_github_write", "implementation_phase_transition"],
        "required_next_step": NEXT_STEP_RERUN_REFINEMENT_AFTER_CONTRACT_UPDATE,
    }


_MARKER_TO_CONTRACT_SECTION = {
    "revised acceptance criteria": "Acceptance Criteria",
    "revised ac": "Acceptance Criteria",
    "stop condition": "Stop Conditions",
    "precondition": "Acceptance Criteria",
    "前提条件": "Acceptance Criteria",
    "allowed paths": "Allowed Paths",
    "allowed paths expansion": "Allowed Paths",
    "verification command": "Verification Commands",
}


def derive_contract_patch_operations(evidence_list: list) -> list:
    """Derive contract_patch_plan_v1.operations from evidence directive markers."""
    operations = []
    for index, evidence in enumerate(evidence_list):
        markers = evidence.get("directive_markers") or []
        directives = evidence.get("extracted_directives") or []
        if not markers:
            continue
        texts = directives if directives else [f"Reflect reviewer directive ({marker})" for marker in markers]
        for marker in markers:
            section = _MARKER_TO_CONTRACT_SECTION.get(marker, "Acceptance Criteria")
            for text in texts:
                operations.append(
                    {
                        "section": section,
                        "op": "append",
                        "text": text,
                        "rationale": f"Directive extracted from trusted review comment ({marker})",
                        "source_evidence_index": index,
                    }
                )
    return operations


def _patch_source_evidence_entry(evidence: dict) -> dict:
    # PR #1332 review fix (P1): carry source_comment_id / extracted_text_sha256
    # / captured_at so issue-author/edit-issue can detect a stale or
    # mismatched source comment before applying an operation derived from it.
    # extracted_text_sha256 hashes only the already-extracted directive
    # texts (never the raw comment body, AC14).
    extracted_directives = evidence.get("extracted_directives") or []
    extracted_text_sha256 = (
        _sha256("\n".join(extracted_directives)) if extracted_directives else None
    )
    return {
        "source_ref": evidence.get("source_ref") or evidence.get("comment_url"),
        "source_body_sha256": evidence.get("body_sha256"),
        "author_association": evidence.get("author_association"),
        "source_comment_id": evidence.get("comment_id"),
        "extracted_text_sha256": extracted_text_sha256,
        "captured_at": evidence.get("captured_at"),
        "start_line": evidence.get("start_line"),
        "end_line": evidence.get("end_line"),
    }


def _provenance_from_evidence(evidence: "dict | None") -> dict:
    evidence = evidence or {}
    return {
        "source_kind": evidence.get("source_kind"),
        "source_ref": evidence.get("source_ref") or evidence.get("comment_url"),
        "body_sha256": evidence.get("body_sha256"),
        "author_association": evidence.get("author_association"),
    }


def _directive_from_evidence(evidence: "dict | None", confidence: "str | None") -> dict:
    evidence = evidence or {}
    return {
        "confidence": confidence,
        "extracted_markers": list(evidence.get("directive_markers") or []),
    }


def _build_scope_delta_authority_result(
    *,
    authority_category: str,
    provenance: dict,
    directive: dict,
    boundary_flags: dict,
    route_action: str,
    reason_code,
    implementation_allowed: bool,
    next_step,
) -> dict:
    return {
        "schema_version": SCOPE_DELTA_AUTHORITY_SCHEMA_VERSION,
        "authority_category": authority_category,
        "provenance": provenance,
        "directive": directive,
        "boundary_flags": {key: bool(boundary_flags.get(key, False)) for key in _BOUNDARY_FLAG_KEYS},
        "route": {
            "action": route_action,
            "reason_code": reason_code,
            "implementation_allowed": implementation_allowed,
            "next_step": next_step,
        },
    }


_BOUNDARY_REASON_PRIORITY = (
    ("destructive_or_non_idempotent_operation", REASON_DESTRUCTIVE_OR_NON_IDEMPOTENT_OPERATION),
    ("changes_permission_boundary", REASON_CHANGES_PERMISSION_BOUNDARY),
    ("changes_external_service_boundary", REASON_CHANGES_EXTERNAL_SERVICE_BOUNDARY),
    ("expands_allowed_paths", REASON_EXPANDS_ALLOWED_PATHS),
    ("requires_issue_split", REASON_REQUIRES_ISSUE_SPLIT),
)


def _first_true_boundary_reason(boundary_flags: dict):
    for key, reason in _BOUNDARY_REASON_PRIORITY:
        if boundary_flags.get(key):
            return reason
    return None


def _classify_single_evidence(evidence: dict) -> dict:
    source_kind = evidence.get("source_kind")
    author_association = evidence.get("author_association")
    markers = evidence.get("directive_markers") or []
    directives = evidence.get("extracted_directives") or []
    boundary_names = set(evidence.get("boundary_flags") or [])
    confidence = evidence.get("confidence")
    if confidence not in (
        DIRECTIVE_CONFIDENCE_EXPLICIT,
        DIRECTIVE_CONFIDENCE_AMBIGUOUS,
        DIRECTIVE_CONFIDENCE_CONFLICTING,
        DIRECTIVE_CONFIDENCE_INFERRED,
    ):
        if markers and directives:
            confidence = DIRECTIVE_CONFIDENCE_EXPLICIT
        elif markers:
            confidence = DIRECTIVE_CONFIDENCE_AMBIGUOUS
        else:
            confidence = DIRECTIVE_CONFIDENCE_INFERRED

    untrusted = False
    if source_kind == "generated_by_agent":
        category = AUTHORITY_CATEGORY_AI_INFERRED
    elif source_kind == "parent_issue":
        category = AUTHORITY_CATEGORY_EXISTING_PARENT_CONTRACT
    elif source_kind == "related_issue":
        category = AUTHORITY_CATEGORY_RELATED_ISSUE_DEPENDENCY
    elif source_kind in ("issue_comment", "pull_request_review"):
        if author_association in SCOPE_DELTA_AUTHORITY_TRUSTED_ASSOCIATIONS:
            category = AUTHORITY_CATEGORY_HUMAN_REVIEW_DIRECTIVE
        else:
            # AC13: fail-closed for CONTRIBUTOR / NONE / bot / missing association.
            category = AUTHORITY_CATEGORY_AI_INFERRED
            untrusted = True
    else:
        category = AUTHORITY_CATEGORY_AI_INFERRED

    return {
        "authority_category": category,
        "confidence": confidence,
        "boundary_names": boundary_names,
        "untrusted": untrusted,
    }


def _has_conflicting_directives(evidence_list: list) -> bool:
    """AC17: multiple trusted reviewers proposing different explicit directives."""
    directive_sets = []
    for evidence in evidence_list:
        directives = evidence.get("extracted_directives") or []
        if directives:
            directive_sets.append(frozenset(d.strip().lower() for d in directives))
    if len(directive_sets) < 2:
        return False
    first = directive_sets[0]
    return any(candidate != first for candidate in directive_sets[1:])


def classify_scope_delta_authority(
    evidence,
    *,
    triggered: bool = True,
    target_issue_number=None,
    base_issue_body_sha256=None,
    expected_repo=None,
) -> dict:
    """AC1-AC19: classify scope_delta_authority for a scope signal delta.

    `evidence` is normalized SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 (single dict or
    a list for multiple reviewers). Never parses raw comment bodies -- only
    consumes already-extracted markers/directives/boundary_flags (AC14).

    `expected_repo` (PR #1332 review fix, P0/P1): the `owner/name` repo the
    caller expects evidence to originate from. It is forwarded to
    validate_scope_delta_authority_evidence_url() so that a same-issue-number
    URL from a *different* repository is fail-closed rejected (AC16
    hardening). When omitted, issue_comment evidence is fail-closed rejected
    by the URL validator (repo cross-check cannot be skipped silently).
    """
    if not triggered:
        return _build_scope_delta_authority_result(
            authority_category=AUTHORITY_CATEGORY_AI_INFERRED,
            provenance=_provenance_from_evidence(None),
            directive=_directive_from_evidence(None, None),
            boundary_flags={},
            route_action=SCOPE_DELTA_AUTHORITY_ROUTE_NOT_TRIGGERED,
            reason_code=None,
            implementation_allowed=True,
            next_step=None,
        )

    if evidence is None:
        evidence_list = []
    elif isinstance(evidence, dict):
        evidence_list = [evidence]
    elif isinstance(evidence, list):
        evidence_list = [item for item in evidence if isinstance(item, dict)]
    else:
        evidence_list = []

    # AC16: drop any evidence entry whose comment_url fails structural
    # validation against the target issue (fail-closed, not raised).
    evidence_list = [
        item
        for item in evidence_list
        if validate_scope_delta_authority_evidence_url(
            item, target_issue_number=target_issue_number, expected_repo=expected_repo
        )
    ]

    if not evidence_list:
        return _build_scope_delta_authority_result(
            authority_category=AUTHORITY_CATEGORY_AI_INFERRED,
            provenance=_provenance_from_evidence({"source_kind": "generated_by_agent"}),
            directive=_directive_from_evidence(None, DIRECTIVE_CONFIDENCE_INFERRED),
            boundary_flags={},
            route_action=SCOPE_DELTA_AUTHORITY_ROUTE_HUMAN_ESCALATION,
            reason_code=REASON_AI_INFERRED_SCOPE_DELTA,
            implementation_allowed=False,
            next_step=None,
        )

    classified = [_classify_single_evidence(item) for item in evidence_list]
    primary = classified[0]
    primary_evidence = evidence_list[0]

    all_boundary_names = set()
    for item in classified:
        all_boundary_names |= item["boundary_names"]
    boundary_flags = {key: (key in all_boundary_names) for key in _BOUNDARY_FLAG_KEYS}

    if primary["untrusted"]:
        return _build_scope_delta_authority_result(
            authority_category=AUTHORITY_CATEGORY_AI_INFERRED,
            provenance=_provenance_from_evidence(primary_evidence),
            directive=_directive_from_evidence(primary_evidence, DIRECTIVE_CONFIDENCE_INFERRED),
            boundary_flags=boundary_flags,
            route_action=SCOPE_DELTA_AUTHORITY_ROUTE_HUMAN_ESCALATION,
            reason_code=REASON_UNTRUSTED_AUTHOR_ASSOCIATION,
            implementation_allowed=False,
            next_step=None,
        )

    category = primary["authority_category"]

    if category in (AUTHORITY_CATEGORY_EXISTING_PARENT_CONTRACT, AUTHORITY_CATEGORY_RELATED_ISSUE_DEPENDENCY):
        return _build_scope_delta_authority_result(
            authority_category=category,
            provenance=_provenance_from_evidence(primary_evidence),
            directive=_directive_from_evidence(primary_evidence, primary["confidence"]),
            boundary_flags=boundary_flags,
            route_action=SCOPE_DELTA_AUTHORITY_ROUTE_NOT_TRIGGERED,
            reason_code=None,
            implementation_allowed=True,
            next_step=None,
        )

    if category == AUTHORITY_CATEGORY_AI_INFERRED:
        return _build_scope_delta_authority_result(
            authority_category=AUTHORITY_CATEGORY_AI_INFERRED,
            provenance=_provenance_from_evidence(primary_evidence),
            directive=_directive_from_evidence(primary_evidence, primary["confidence"]),
            boundary_flags=boundary_flags,
            route_action=SCOPE_DELTA_AUTHORITY_ROUTE_HUMAN_ESCALATION,
            reason_code=REASON_AI_INFERRED_SCOPE_DELTA,
            implementation_allowed=False,
            next_step=None,
        )

    # category == human_review_directive
    boundary_reason = _first_true_boundary_reason(boundary_flags)
    if boundary_reason is not None:
        # AC18: boundary flags gate even a trusted approval.
        return _build_scope_delta_authority_result(
            authority_category=AUTHORITY_CATEGORY_HUMAN_REVIEW_DIRECTIVE,
            provenance=_provenance_from_evidence(primary_evidence),
            directive=_directive_from_evidence(primary_evidence, primary["confidence"]),
            boundary_flags=boundary_flags,
            route_action=SCOPE_DELTA_AUTHORITY_ROUTE_HUMAN_ESCALATION,
            reason_code=boundary_reason,
            implementation_allowed=False,
            next_step=None,
        )

    if _has_conflicting_directives(evidence_list):
        return _build_scope_delta_authority_result(
            authority_category=AUTHORITY_CATEGORY_HUMAN_REVIEW_DIRECTIVE,
            provenance=_provenance_from_evidence(primary_evidence),
            directive=_directive_from_evidence(primary_evidence, DIRECTIVE_CONFIDENCE_CONFLICTING),
            boundary_flags=boundary_flags,
            route_action=SCOPE_DELTA_AUTHORITY_ROUTE_HUMAN_ESCALATION,
            reason_code=REASON_CONFLICTING_HUMAN_DIRECTIVES,
            implementation_allowed=False,
            next_step=None,
        )

    confidence = primary["confidence"]
    if confidence == DIRECTIVE_CONFIDENCE_EXPLICIT:
        # PR #1332 review fix (P1): a CONTRACT_PATCH_PLAN_V1 can only be
        # generated against a resolved Issue body snapshot. Without
        # base_issue_body_sha256, claiming contract_update_required would
        # imply "safe to proceed" while the patch plan itself cannot be
        # safely built (build_contract_patch_plan_v1 fail-closes on this) --
        # so fail closed to human_escalation instead of raising.
        if not base_issue_body_sha256:
            return _build_scope_delta_authority_result(
                authority_category=AUTHORITY_CATEGORY_HUMAN_REVIEW_DIRECTIVE,
                provenance=_provenance_from_evidence(primary_evidence),
                directive=_directive_from_evidence(primary_evidence, DIRECTIVE_CONFIDENCE_EXPLICIT),
                boundary_flags=boundary_flags,
                route_action=SCOPE_DELTA_AUTHORITY_ROUTE_HUMAN_ESCALATION,
                reason_code=REASON_MISSING_BASE_ISSUE_BODY_SHA256,
                implementation_allowed=False,
                next_step=None,
            )
        result = _build_scope_delta_authority_result(
            authority_category=AUTHORITY_CATEGORY_HUMAN_REVIEW_DIRECTIVE,
            provenance=_provenance_from_evidence(primary_evidence),
            directive=_directive_from_evidence(primary_evidence, DIRECTIVE_CONFIDENCE_EXPLICIT),
            boundary_flags=boundary_flags,
            route_action=SCOPE_DELTA_AUTHORITY_ROUTE_CONTRACT_UPDATE_REQUIRED,
            reason_code=REASON_EXPLICIT_HUMAN_CONTRACT_DIRECTIVE,
            implementation_allowed=False,
            next_step=NEXT_STEP_RERUN_REFINEMENT_AFTER_CONTRACT_UPDATE,
        )
        result["contract_patch_plan"] = build_contract_patch_plan_v1(
            target_issue_number=target_issue_number,
            base_issue_body_sha256=base_issue_body_sha256,
            source_evidence=[_patch_source_evidence_entry(item) for item in evidence_list],
            operations=derive_contract_patch_operations(evidence_list),
        )
        return result

    # ambiguous / inferred confidence for a human-sourced comment
    return _build_scope_delta_authority_result(
        authority_category=AUTHORITY_CATEGORY_HUMAN_REVIEW_DIRECTIVE,
        provenance=_provenance_from_evidence(primary_evidence),
        directive=_directive_from_evidence(primary_evidence, DIRECTIVE_CONFIDENCE_AMBIGUOUS),
        boundary_flags=boundary_flags,
        route_action=SCOPE_DELTA_AUTHORITY_ROUTE_HUMAN_ESCALATION,
        reason_code=REASON_AMBIGUOUS_HUMAN_DIRECTIVE,
        implementation_allowed=False,
        next_step=None,
    )
