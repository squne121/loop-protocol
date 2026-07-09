#!/usr/bin/env python3
"""Non-blocking advisory producer for root temporary residue paths."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from dataclasses import dataclass
from typing import Any


ADVICE_SCHEMA = "REPO_TEMP_FOLDER_ADVICE_V1"
APPROVED_REPLACEMENT = "tmp/"
APPROVED_TEMPORARY_ROOTS = ["tmp/", ".claude/tmp/"]
POLICY_DOC = "docs/dev/repository-folder-policy.md"
ROOT_ALIAS_PATTERN = re.compile(r"^(?:\.tmp(?:-[^/]+)?|\.temp)(?:$|/)")
RAW_COMMAND_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_/.-])(?P<path>\.(?:tmp(?:-[^/\s'\"=]+)?|temp)(?:/[^\s'\"=]*)?)"
)


@dataclass(frozen=True)
class RootTemporaryResidueMatch:
    observed_path: str


def _normalize_candidate(value: str) -> str | None:
    candidate = value.strip().strip("\"'")
    if not candidate or candidate.startswith("/"):
        return None
    if "\\" in candidate or "\x00" in candidate:
        return None
    candidate = candidate.removeprefix("./")
    if not candidate or candidate.startswith("../"):
        return None
    return candidate


def _match_observed_path(candidate: str) -> RootTemporaryResidueMatch | None:
    normalized = _normalize_candidate(candidate)
    if normalized is None:
        return None
    first_segment = normalized.split("/", 1)[0]
    if first_segment in {"tmp", ".claude"}:
        return None
    if not ROOT_ALIAS_PATTERN.match(normalized):
        return None
    return RootTemporaryResidueMatch(observed_path=f"{first_segment}/")


def _extract_match_from_token(token: str) -> RootTemporaryResidueMatch | None:
    for candidate in (token, token.split("=", 1)[1] if "=" in token else ""):
        if not candidate:
            continue
        match = _match_observed_path(candidate)
        if match is not None:
            return match
    return None


def _match_command(command: str) -> RootTemporaryResidueMatch | None:
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        tokens = []
    for token in tokens:
        match = _extract_match_from_token(token)
        if match is not None:
            return match
    raw_match = RAW_COMMAND_PATTERN.search(command)
    if raw_match is None:
        return None
    return _match_observed_path(raw_match.group("path"))


def detect_root_temporary_residue(payload: dict[str, Any]) -> RootTemporaryResidueMatch | None:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str):
        match = _match_observed_path(file_path)
        if match is not None:
            return match
    command = tool_input.get("command")
    if isinstance(command, str):
        return _match_command(command)
    return None


def build_temp_folder_advice(payload: dict[str, Any]) -> dict[str, Any] | None:
    match = detect_root_temporary_residue(payload)
    if match is None:
        return None
    return {
        "schema": ADVICE_SCHEMA,
        "block": False,
        "reason_code": "root_temporary_alias",
        "observed_path": match.observed_path,
        "approved_replacement": APPROVED_REPLACEMENT,
        "approved_temporary_roots": APPROVED_TEMPORARY_ROOTS,
        "cleanup_required": True,
        "policy_doc": POLICY_DOC,
        "message_ja": (
            "repo root の一時 alias は残置ノイズになります。tmp/ または .claude/tmp/ を使い、"
            "終了時に削除または報告してください。"
        ),
    }


def _build_hook_envelope(advice: dict[str, Any]) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": f"{ADVICE_SCHEMA} {json.dumps(advice, ensure_ascii=False)}",
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--emit-hook-envelope",
        action="store_true",
        help="Emit Codex/Claude hookSpecificOutput envelope instead of inner JSON.",
    )
    args = parser.parse_args()

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0

    advice = build_temp_folder_advice(payload)
    if advice is None:
        return 0

    output = _build_hook_envelope(advice) if args.emit_hook_envelope else advice
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
