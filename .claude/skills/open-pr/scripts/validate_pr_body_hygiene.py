#!/usr/bin/env python3
"""Compose PR body structure, Japanese content, and draft hygiene checks."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_SCRIPTS_DIR = Path(__file__).resolve().parent
_CREATE_ISSUE_SCRIPTS = _SCRIPTS_DIR.parent.parent / "create-issue" / "scripts"

_pbp = _load_module("prose_boundary_policy", _CREATE_ISSUE_SCRIPTS / "prose_boundary_policy.py")
_vjc = _load_module("validate_japanese_content", _CREATE_ISSUE_SCRIPTS / "validate_japanese_content.py")
_vpb = _load_module("validate_pr_body", _SCRIPTS_DIR / "validate_pr_body.py")

validate_text = _vjc.validate_text
split_markdown_blocks = _vjc.split_markdown_blocks
validate_pr_body = _vpb.validate_pr_body
extract_sections = _vpb._extract_sections
is_placeholder_text = _vpb._is_placeholder_text

_CLOSING_BLOCK_RE = re.compile(
    r"(?is)^\s*(?:[-*]\s*)?"
    r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved):?\s+"
    r"(?:[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+#\d+|#\d+)"
    r"(?:\s*,\s*(?:[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+#\d+|#\d+))*\s*$"
)


def _parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "y"}:
        return True
    if lowered in {"0", "false", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError(f"invalid boolean value: {value}")


def _load_changed_paths(changed_paths_file: str | None) -> list[str] | None:
    if not changed_paths_file:
        return None
    paths = Path(changed_paths_file).read_text(encoding="utf-8").splitlines()
    return [path.strip() for path in paths if path.strip()]


def _is_agent_surface_change(changed_paths: list[str] | None) -> bool:
    if not changed_paths:
        return False
    return any(path == ".claude" or path.startswith(".claude/") for path in changed_paths)


def _has_concrete_safety_matrix(body: str) -> bool:
    sections, _ = extract_sections(body)
    info = sections.get("Safety Claim Matrix")
    if not info:
        return False
    content = info[0].strip()
    if not content:
        return False
    if re.search(r"(?i)\bN/A\b", content) and re.search(r"(?i)\breason\b", content):
        return False
    if is_placeholder_text(content):
        return False
    data_rows = [
        line for line in content.splitlines()
        if line.strip().startswith("|") and not re.match(r"^\|\s*-", line.strip())
    ]
    return len(data_rows) >= 2


def _find_standalone_closing_blocks(body: str) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for index, block in enumerate(split_markdown_blocks(body), 1):
        if block.get("type") != "prose":
            continue
        text = block.get("text", "").strip()
        if not text:
            continue
        candidates = [text]
        candidates.extend(line.strip() for line in text.splitlines() if line.strip())
        for candidate in candidates:
            if not _CLOSING_BLOCK_RE.fullmatch(candidate):
                continue
            failures.append(
                {
                    "rule_id": "HY001",
                    "severity": "error",
                    "section": "Notes",
                    "line_start": 1,
                    "line_end": 1,
                    "message": "Standalone closing keyword block is not allowed.",
                    "minimal_context": [candidate],
                    "context_truncated": False,
                    "fix_hint": "Embed Closes #N inside a Japanese sentence in Notes.",
                    "autofixable": False,
                    "block_index": index,
                }
            )
            break
    return failures


def _build_required_auto_actions(draft: bool) -> list[dict[str, Any]]:
    if not draft:
        return []
    return [
        {
            "kind": "mark_ready_for_review",
            "executor": "open-pr",
            "skill": "open-pr.mark_ready_for_review",
            "blocking_merge_ready": True,
            "mechanical": True,
        }
    ]


@dataclass(frozen=True)
class HygieneResult:
    schema: str
    target: str
    body_sha256: str
    status: Literal["pass", "fail"]
    merge_ready: bool
    required_auto_actions: list[dict[str, Any]]
    validator_results: dict[str, Any]
    errors: list[dict[str, Any]]


def validate_pr_body_hygiene(
    body: str,
    changed_paths: list[str] | None,
    linked_issue: int,
    draft: bool,
) -> HygieneResult:
    body_sha256 = f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"
    errors: list[dict[str, Any]] = []

    pr_result = validate_pr_body(body, changed_paths, linked_issue)
    if pr_result.errors:
        errors.extend(asdict(error) for error in pr_result.errors)

    japanese_result = validate_text(body, threshold=0.1)
    if not japanese_result.passed:
        errors.append(
            {
                "rule_id": "HY002",
                "severity": "error",
                "section": "Summary",
                "line_start": 1,
                "line_end": 1,
                "message": "Japanese content validation failed.",
                "minimal_context": [f"failed_blocks={len(japanese_result.failed_blocks)}"],
                "context_truncated": False,
                "fix_hint": "Increase Japanese prose in each failing block until threshold 0.1 passes.",
                "autofixable": False,
            }
        )

    if _is_agent_surface_change(changed_paths) and not _has_concrete_safety_matrix(body):
        errors.append(
            {
                "rule_id": "HY003",
                "severity": "error",
                "section": "Safety Claim Matrix",
                "line_start": 1,
                "line_end": 1,
                "message": "Agent-surface changes require a concrete Safety Claim Matrix.",
                "minimal_context": ["Safety Claim Matrix"],
                "context_truncated": False,
                "fix_hint": "Provide at least one concrete matrix row with evidence for .claude/** changes.",
                "autofixable": False,
            }
        )

    errors.extend(_find_standalone_closing_blocks(body))

    required_auto_actions = _build_required_auto_actions(draft)
    merge_ready = not errors and not required_auto_actions
    status: Literal["pass", "fail"] = "fail" if errors else "pass"
    validator_results = {
        "pr_body": {
            "schema": pr_result.schema,
            "target": pr_result.target,
            "status": pr_result.status,
            "error_count": len(pr_result.errors),
        },
        "japanese_content": {
            "status": "pass" if japanese_result.passed else "fail",
            "aggregate_ratio": japanese_result.aggregate_ratio,
            "failed_blocks": len(japanese_result.failed_blocks),
            "threshold": japanese_result.threshold,
        },
    }
    return HygieneResult(
        schema="PR_BODY_HYGIENE_RESULT_V1",
        target="pr",
        body_sha256=body_sha256,
        status=status,
        merge_ready=merge_ready,
        required_auto_actions=required_auto_actions,
        validator_results=validator_results,
        errors=errors,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate PR body hygiene before PR creation.")
    parser.add_argument("--body-file", required=True, type=str)
    parser.add_argument("--changed-paths-file", type=str, default="")
    parser.add_argument("--linked-issue", required=True, type=int)
    parser.add_argument("--draft", default="false", type=_parse_bool)
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

    result = validate_pr_body_hygiene(body, changed_paths, args.linked_issue, args.draft)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 1 if result.status == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
