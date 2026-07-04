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

INPUT_REQUIRED_FIELDS = ("before_body", "current_body", "after_body", "source_refs")
INPUT_SOURCE_REF_KEYS = ("before", "current", "after")


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


def _extract_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_content: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in text.splitlines():
        stripped = line.strip()
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence_char = stripped[0]
                fence_len = 0
                for ch in stripped:
                    if ch == fence_char:
                        fence_len += 1
                    else:
                        break
                in_fence = True
                if current_section is not None:
                    current_content.append(line)
                continue
        else:
            if stripped and all(ch == fence_char for ch in stripped) and len(stripped) >= fence_len:
                in_fence = False
                fence_char = ""
                fence_len = 0
                if current_section is not None:
                    current_content.append(line)
                continue
            if current_section is not None:
                current_content.append(line)
            continue

        if line.startswith("## "):
            if current_section is not None:
                sections[current_section] = "\n".join(current_content).strip()
            current_section = line[3:].strip()
            current_content = []
        elif current_section is not None:
            current_content.append(line)

    if current_section is not None:
        sections[current_section] = "\n".join(current_content).strip()

    return sections


def _find_section_line_offset(text: str, section_name: str) -> int:
    in_fence = False
    fence_char = ""
    fence_len = 0
    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence_char = stripped[0]
                fence_len = len(stripped) - len(stripped.lstrip(fence_char))
                in_fence = True
                continue
        else:
            if stripped and all(ch == fence_char for ch in stripped) and len(stripped) >= fence_len:
                in_fence = False
                fence_char = ""
                fence_len = 0
            continue

        if line.startswith("## ") and line[3:].strip() == section_name:
            return index + 1
    return 1


def _normalize_path(path: str) -> str:
    normalized = path.strip().strip("`").strip()
    normalized = normalized.rstrip("/")
    return normalized.replace("\\", "/")


def _extract_path_items(text: str, section_name: str) -> list[dict[str, Any]]:
    sections = _extract_sections(text)
    section_text = sections.get(section_name, "")
    section_start = _find_section_line_offset(text, section_name)
    items: list[dict[str, Any]] = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    for local_line, raw_line in enumerate(section_text.splitlines(), start=0):
        stripped = raw_line.strip()
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence_char = stripped[0]
                fence_len = len(stripped) - len(stripped.lstrip(fence_char))
                in_fence = True
                continue
        else:
            if stripped and all(ch == fence_char for ch in stripped) and len(stripped) >= fence_len:
                in_fence = False
                fence_char = ""
                fence_len = 0
            continue

        for match in PATH_TOKEN_RE.finditer(raw_line):
            candidate = match.group("path") or match.group("bare") or ""
            normalized = _normalize_path(candidate)
            if not normalized:
                continue
            items.append(
                {
                    "value": normalized,
                    "start_line": section_start + local_line,
                    "end_line": section_start + local_line,
                    "text_sha256": _sha256(raw_line),
                }
            )
    return items


def _extract_in_scope_layers(text: str) -> list[dict[str, Any]]:
    sections = _extract_sections(text)
    section_text = sections.get("In Scope", "")
    section_start = _find_section_line_offset(text, "In Scope")
    items: list[dict[str, Any]] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    prefixes = (".claude/", "docs/", "src/", "scripts/", "tests/", ".github/")

    for local_line, raw_line in enumerate(section_text.splitlines(), start=0):
        stripped = raw_line.strip()
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence_char = stripped[0]
                fence_len = len(stripped) - len(stripped.lstrip(fence_char))
                in_fence = True
                continue
        else:
            if stripped and all(ch == fence_char for ch in stripped) and len(stripped) >= fence_len:
                in_fence = False
                fence_char = ""
                fence_len = 0
            continue

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
                        "start_line": section_start + local_line,
                        "end_line": section_start + local_line,
                        "text_sha256": _sha256(raw_line),
                    }
                )
    return items


def _extract_ac_items(text: str) -> list[dict[str, Any]]:
    sections = _extract_sections(text)
    section_text = sections.get("Acceptance Criteria", "")
    section_start = _find_section_line_offset(text, "Acceptance Criteria")
    items: list[dict[str, Any]] = []
    for local_line, raw_line in enumerate(section_text.splitlines(), start=0):
        stripped = raw_line.lstrip()
        if stripped.startswith("- [ ]") or stripped.startswith("- [x]"):
            normalized = re.sub(r"\s+", " ", stripped).strip()
            items.append(
                {
                    "value": normalized,
                    "start_line": section_start + local_line,
                    "end_line": section_start + local_line,
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
