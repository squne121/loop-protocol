#!/usr/bin/env python3
"""Non-blocking advisory producer for root temporary residue paths."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ADVICE_SCHEMA = "REPO_TEMP_FOLDER_ADVICE_V1"
ADVICE_SCHEMA_V2 = "REPO_TEMP_FOLDER_ADVICE_V2"
APPROVED_REPLACEMENT = "tmp/"
APPROVED_TEMPORARY_ROOTS = ["tmp/", ".claude/tmp/"]
APPROVED_WRITE_ROOTS = ["tmp/"]
DEPRECATED_LEGACY_ROOTS = [".claude/tmp/"]
POLICY_DOC = "docs/dev/repository-folder-policy.md"
ROOT_ALIAS_PATTERN = re.compile(r"^(?:\.tmp(?:-[^/]+)?|\.temp)(?:$|/)")
# V2-only: tool names whose ``file_path`` input represents a write (Write /
# Edit overwrite file contents; Read does not). Bash command classification
# reuses the existing READ_ONLY_COMMANDS / DELETE_COMMANDS / WRITE_COMMANDS
# verb lists below.
LEGACY_ROOT_WRITE_TOOL_NAMES = {"Write", "Edit"}
RAW_COMMAND_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_/.-])(?P<path>\.(?:tmp(?:-[^/\s'\"=]+)?|temp)(?:/[^\s'\"=]*)?)"
)
PWD_PREFIXES = ("$PWD/", "${PWD}/", '"$PWD/', '"${PWD}/')
REPO_ROOT_PREFIXES = (
    "$(git rev-parse --show-toplevel)/",
    '"$(git rev-parse --show-toplevel)/',
)
READ_ONLY_COMMANDS = {
    "cat",
    "find",
    "grep",
    "head",
    "less",
    "ls",
    "rg",
    "sed",
    "stat",
    "tail",
    "wc",
}
DELETE_COMMANDS = {"rm", "rmdir", "unlink"}
WRITE_COMMANDS = {"cp", "echo", "install", "ln", "mkdir", "mv", "printf", "tee", "touch"}
REDIRECTION_PREFIXES = (">>", ">", "1>>", "1>", "2>>", "2>")


@dataclass(frozen=True)
class RootTemporaryResidueMatch:
    observed_path: str
    reason_code: str = "root_temporary_alias"


def _redact_observed_alias(first_segment: str) -> str:
    if first_segment.startswith(".tmp-"):
        return ".tmp-*/"
    return f"{first_segment}/"


def _strip_shell_quotes(value: str) -> str:
    return value.strip().strip("\"'")


def repo_relative_path(raw_path: str, *, cwd: Path, repo_root: Path) -> Path | None:
    candidate = _strip_shell_quotes(raw_path)
    if not candidate:
        return None
    if "\x00" in candidate:
        return None
    absolute_candidate = None
    for prefix in PWD_PREFIXES:
        if candidate.startswith(prefix):
            suffix = candidate[len(prefix) :]
            absolute_candidate = cwd / suffix
            break
    if absolute_candidate is None:
        for prefix in REPO_ROOT_PREFIXES:
            if candidate.startswith(prefix):
                suffix = candidate[len(prefix) :]
                absolute_candidate = repo_root / suffix
                break
    if absolute_candidate is None:
        path_candidate = Path(candidate)
        absolute_candidate = path_candidate if path_candidate.is_absolute() else cwd / path_candidate

    try:
        resolved_root = repo_root.resolve(strict=True)
    except OSError:
        return None
    try:
        resolved_candidate = absolute_candidate.resolve(strict=False)
    except OSError:
        return None
    try:
        return resolved_candidate.relative_to(resolved_root)
    except ValueError:
        return None


def _match_repo_relative_path(candidate: Path) -> RootTemporaryResidueMatch | None:
    normalized = candidate.as_posix()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or "\\" in normalized:
        return None
    first_segment = normalized.split("/", 1)[0]
    if first_segment in {"tmp", ".claude"}:
        return None
    if not ROOT_ALIAS_PATTERN.match(normalized):
        return None
    return RootTemporaryResidueMatch(observed_path=_redact_observed_alias(first_segment))


def _match_legacy_root_relative_path(candidate: Path) -> RootTemporaryResidueMatch | None:
    """V2-only matcher: flags ``.claude/tmp/**`` (the deprecated legacy write root)."""
    normalized = candidate.as_posix()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or "\\" in normalized:
        return None
    if normalized != ".claude/tmp" and not normalized.startswith(".claude/tmp/"):
        return None
    return RootTemporaryResidueMatch(
        observed_path=".claude/tmp/", reason_code="deprecated_legacy_root_write"
    )


def _match_observed_path(
    candidate: str,
    *,
    cwd: Path,
    repo_root: Path,
    matcher=_match_repo_relative_path,
) -> RootTemporaryResidueMatch | None:
    repo_relative = repo_relative_path(candidate, cwd=cwd, repo_root=repo_root)
    if repo_relative is None:
        return None
    return matcher(repo_relative)


def _extract_match_from_token(
    token: str, *, cwd: Path, repo_root: Path, matcher=_match_repo_relative_path
) -> RootTemporaryResidueMatch | None:
    for candidate in (token, token.split("=", 1)[1] if "=" in token else ""):
        if not candidate:
            continue
        match = _match_observed_path(candidate, cwd=cwd, repo_root=repo_root, matcher=matcher)
        if match is not None:
            return match
    return None


def _match_redirection_token(
    token: str, *, cwd: Path, repo_root: Path, matcher=_match_repo_relative_path
) -> RootTemporaryResidueMatch | None:
    for prefix in REDIRECTION_PREFIXES:
        if not token.startswith(prefix):
            continue
        target = token[len(prefix) :]
        if not target:
            return None
        return _match_observed_path(target, cwd=cwd, repo_root=repo_root, matcher=matcher)
    return None


def _match_command(
    command: str, *, cwd: Path, repo_root: Path, matcher=_match_repo_relative_path
) -> RootTemporaryResidueMatch | None:
    try:
        tokens = shlex.split(command, comments=False, posix=True)
    except ValueError:
        tokens = []
    if tokens:
        verb = Path(tokens[0]).name
        if verb in READ_ONLY_COMMANDS or verb in DELETE_COMMANDS:
            return None
        if verb in WRITE_COMMANDS:
            for token in tokens[1:]:
                if token.startswith("-"):
                    continue
                match = _extract_match_from_token(token, cwd=cwd, repo_root=repo_root, matcher=matcher)
                if match is not None:
                    return match
        for token in tokens[1:]:
            redirection_match = _match_redirection_token(
                token, cwd=cwd, repo_root=repo_root, matcher=matcher
            )
            if redirection_match is not None:
                return redirection_match
        for token in tokens[1:]:
            match = _extract_match_from_token(token, cwd=cwd, repo_root=repo_root, matcher=matcher)
            if match is not None:
                return match
    raw_match = RAW_COMMAND_PATTERN.search(command)
    if raw_match is None:
        return None
    return _match_observed_path(raw_match.group("path"), cwd=cwd, repo_root=repo_root, matcher=matcher)


def _payload_cwd(payload: dict[str, Any], *, repo_root: Path) -> Path:
    raw_cwd = payload.get("cwd")
    if isinstance(raw_cwd, str):
        candidate = Path(raw_cwd)
        if candidate.is_absolute():
            return candidate
        return (repo_root / candidate).resolve(strict=False)
    return repo_root


def _detect_legacy_root_write(
    payload: dict[str, Any], tool_input: dict[str, Any], *, cwd: Path, repo_root: Path
) -> RootTemporaryResidueMatch | None:
    """V2-only: flag write operations targeting ``.claude/tmp/**`` (the
    deprecated legacy write root). Read / scan / delete operations must not
    match here — the write/read/delete distinction is derived from
    ``tool_name`` (Write/Edit) for file_path-based tool inputs and from the
    existing verb classification (READ_ONLY_COMMANDS / DELETE_COMMANDS /
    WRITE_COMMANDS) for Bash commands.
    """
    tool_name = payload.get("tool_name")
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str) and tool_name in LEGACY_ROOT_WRITE_TOOL_NAMES:
        match = _match_observed_path(
            file_path, cwd=cwd, repo_root=repo_root, matcher=_match_legacy_root_relative_path
        )
        if match is not None:
            return match
    command = tool_input.get("command")
    if isinstance(command, str):
        match = _match_command(
            command, cwd=cwd, repo_root=repo_root, matcher=_match_legacy_root_relative_path
        )
        if match is not None:
            return match
    return None


def detect_root_temporary_residue(
    payload: dict[str, Any], *, repo_root: Path | None = None, schema_version: str = "v1"
) -> RootTemporaryResidueMatch | None:
    effective_repo_root = (repo_root or Path(__file__).resolve().parents[2]).resolve(strict=False)
    cwd = _payload_cwd(payload, repo_root=effective_repo_root)
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str):
        match = _match_observed_path(file_path, cwd=cwd, repo_root=effective_repo_root)
        if match is not None:
            return match
    command = tool_input.get("command")
    if isinstance(command, str):
        match = _match_command(command, cwd=cwd, repo_root=effective_repo_root)
        if match is not None:
            return match
    if schema_version == "v2":
        return _detect_legacy_root_write(payload, tool_input, cwd=cwd, repo_root=effective_repo_root)
    return None


_V1_MESSAGE_JA = (
    "repo root の一時 alias は残置ノイズになります。tmp/ または .claude/tmp/ を使い、"
    "終了時に削除または報告してください。"
)
_V2_MESSAGE_JA_BY_REASON = {
    "root_temporary_alias": (
        "repo root の一時 alias は残置ノイズになります。新規の書き込みは tmp/ を使ってください。"
        ".claude/tmp/ は非推奨（deprecated）の legacy root です。終了時に削除または報告してください。"
    ),
    "deprecated_legacy_root_write": (
        ".claude/tmp/ は非推奨（deprecated）の legacy root です。新規の書き込みには tmp/ を使ってください。"
        "読み取り・走査・報告・削除は引き続き妨げません。"
    ),
}


def build_temp_folder_advice(
    payload: dict[str, Any], *, repo_root: Path | None = None, schema_version: str = "v1"
) -> dict[str, Any] | None:
    match = detect_root_temporary_residue(payload, repo_root=repo_root, schema_version=schema_version)
    if match is None:
        return None
    if schema_version == "v2":
        return {
            "schema": ADVICE_SCHEMA_V2,
            "block": False,
            "reason_code": match.reason_code,
            "observed_path": match.observed_path,
            "approved_replacement": APPROVED_REPLACEMENT,
            "approved_write_roots": APPROVED_WRITE_ROOTS,
            "deprecated_legacy_roots": DEPRECATED_LEGACY_ROOTS,
            "cleanup_required": True,
            "policy_doc": POLICY_DOC,
            "message_ja": _V2_MESSAGE_JA_BY_REASON[match.reason_code],
        }
    return {
        "schema": ADVICE_SCHEMA,
        "block": False,
        "reason_code": "root_temporary_alias",
        "observed_path": match.observed_path,
        "approved_replacement": APPROVED_REPLACEMENT,
        "approved_temporary_roots": APPROVED_TEMPORARY_ROOTS,
        "cleanup_required": True,
        "policy_doc": POLICY_DOC,
        "message_ja": _V1_MESSAGE_JA,
    }


def _build_hook_envelope(advice: dict[str, Any]) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": f"{advice['schema']} {json.dumps(advice, ensure_ascii=False)}",
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo-root",
        help="Absolute repo root supplied by the hook wrapper for deterministic path resolution.",
    )
    parser.add_argument(
        "--emit-hook-envelope",
        action="store_true",
        help="Emit Codex/Claude hookSpecificOutput envelope instead of inner JSON.",
    )
    parser.add_argument(
        "--schema-version",
        choices=["v1", "v2"],
        default="v1",
        help=(
            "REPO_TEMP_FOLDER_ADVICE schema version to emit. Default (and explicit 'v1') "
            "preserves the exact V1 payload shape; 'v2' emits the write-root/legacy-root "
            "separated payload and additionally flags .claude/tmp/** write operations."
        ),
    )
    args = parser.parse_args()

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0

    repo_root = Path(args.repo_root).resolve(strict=False) if args.repo_root else None
    advice = build_temp_folder_advice(payload, repo_root=repo_root, schema_version=args.schema_version)
    if advice is None:
        return 0

    output = _build_hook_envelope(advice) if args.emit_hook_envelope else advice
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
