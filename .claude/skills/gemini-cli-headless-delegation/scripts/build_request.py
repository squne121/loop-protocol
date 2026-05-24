#!/usr/bin/env python3
"""build_request.py — Build a delegation_request_v1 JSON for gemini-cli-headless-delegation.

Usage:
    uv run python3 build_request.py \\
      --profile <tool_profile> \\
      --objective <str> \\
      [--instruction <str> ...]  \\
      [--context-file <path> ...] \\
      [--gh-pr <N>] \\
      [--gh-issue <N>] \\
      [--output <path>]

Exit codes:
    0  Request JSON written and validated successfully.
    1  Validation or usage error (failure JSON written to --output when provided).
    2  Internal error.

The generated JSON conforms to delegation_request_v1 schema and is validated
against run_gemini_headless.validate_request before being written.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA = "delegation_request_v1"
DEFAULT_INSTRUCTIONS_BY_PROFILE: dict[str, list[str]] = {
    "no_tools": [
        "Summarise the key findings from the provided context files.",
        "List any gaps or issues found with evidence.",
    ],
    "grounded_research": [
        "Search for authoritative sources relevant to the objective.",
        "Summarise findings with evidence and citations.",
    ],
    "local_asset_research": [
        "Use Serena MCP read-only tools to investigate the objective.",
        "List file paths and symbol names as evidence.",
    ],
    "proposal_only": [
        "Draft a proposal addressing the objective.",
        "Return the proposal as structured text only; do not execute commands.",
    ],
    "github_research": [
        "Investigate the GitHub resources relevant to the objective.",
        "Summarise findings with links to issues, PRs, or comments as evidence.",
    ],
}
DEFAULT_OUTPUT_SECTIONS_BY_PROFILE: dict[str, list[str]] = {
    "no_tools": ["Summary", "Findings", "Evidence"],
    "grounded_research": ["Summary", "Findings", "Evidence"],
    "local_asset_research": ["Summary", "Findings", "Evidence"],
    "proposal_only": ["implementation_draft"],
    "github_research": ["Summary", "Findings", "Evidence"],
}
VALID_PROFILES = frozenset(DEFAULT_INSTRUCTIONS_BY_PROFILE.keys())

# ---------------------------------------------------------------------------
# Loader: run_gemini_headless.validate_request
# ---------------------------------------------------------------------------


def _load_validate_request():
    """Dynamically load validate_request from run_gemini_headless.py."""
    script_dir = Path(__file__).resolve().parent
    module_path = script_dir / "run_gemini_headless.py"
    spec = importlib.util.spec_from_file_location("run_gemini_headless", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load run_gemini_headless from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_request


# ---------------------------------------------------------------------------
# Failure JSON helpers
# ---------------------------------------------------------------------------


def _build_failure_json(
    failure_class: str,
    failure_reason: str,
    next_action_argv: list[str],
    next_action_command: str,
) -> dict[str, Any]:
    return {
        "schema": "build_request_failure_v1",
        "ok": False,
        "failure_class": failure_class,
        "failure_reason": failure_reason,
        "next_action": {
            "argv": next_action_argv,
            "command": next_action_command,
        },
    }


def _write_failure(
    output: Path | None,
    failure_class: str,
    failure_reason: str,
    next_action_argv: list[str],
) -> None:
    next_action_command = shlex.join(next_action_argv)
    payload = _build_failure_json(
        failure_class=failure_class,
        failure_reason=failure_reason,
        next_action_argv=next_action_argv,
        next_action_command=next_action_command,
    )
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Command-line reconstruction helpers
# ---------------------------------------------------------------------------


def _build_full_argv(
    profile: str,
    objective: str,
    instructions: list[str] | None,
    context_file: str,
    output: Path | None,
) -> list[str]:
    """Build the full argv needed to re-run build_request.py with given args.

    Used for next_action.argv in failure JSON so callers can retry the complete
    command rather than a stub (B3).
    """
    argv = [
        "uv", "run", "python3",
        ".claude/skills/gemini-cli-headless-delegation/scripts/build_request.py",
    ]
    if profile:
        argv += ["--profile", profile]
    if objective:
        argv += ["--objective", objective]
    if instructions:
        for inst in instructions:
            argv += ["--instruction", inst]
    argv += ["--context-file", context_file]
    if output is not None:
        argv += ["--output", str(output)]
    return argv


# ---------------------------------------------------------------------------
# Context file resolution
# ---------------------------------------------------------------------------


def _resolve_context_files(
    raw_paths: list[str],
    base_dir: Path,
    output: Path | None,
    profile: str = "",
    objective: str = "",
    instructions: list[str] | None = None,
) -> list[str] | None:
    """Resolve context files to absolute paths. Returns None on failure."""
    resolved: list[str] = []
    for raw in raw_paths:
        p = Path(raw)
        if not p.is_absolute():
            p = (base_dir / raw).resolve()
        else:
            p = p.resolve()
        if not p.exists():
            # B2: use failure_class='context_file_missing' (matches Issue #313 contract)
            # B3: include complete argv so callers can re-run the full command
            _write_failure(
                output=output,
                failure_class="context_file_missing",
                failure_reason=f"context file not found: {raw} (resolved: {p})",
                next_action_argv=_build_full_argv(
                    profile=profile,
                    objective=objective,
                    instructions=instructions,
                    context_file=str(p),
                    output=output,
                ),
            )
            return None
        if not p.is_file():
            # B2/B3: same fix for is-not-a-file case
            _write_failure(
                output=output,
                failure_class="context_file_missing",
                failure_reason=f"context file is not a regular file: {raw} (resolved: {p})",
                next_action_argv=_build_full_argv(
                    profile=profile,
                    objective=objective,
                    instructions=instructions,
                    context_file=str(p),
                    output=output,
                ),
            )
            return None
        resolved.append(str(p))
    return resolved


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------


def build_request(
    profile: str,
    objective: str,
    instructions: list[str] | None,
    context_files: list[str] | None,
    gh_pr: int | None,
    gh_issue: int | None,
    output: Path | None,
    base_dir: Path | None = None,
) -> int:
    """Build and validate a delegation_request_v1.

    Returns exit code: 0 = success, 1 = validation/usage error, 2 = internal error.
    """
    if profile not in VALID_PROFILES:
        _write_failure(
            output=output,
            failure_class="invalid_profile",
            failure_reason=f"tool_profile '{profile}' is not valid; choose one of: {sorted(VALID_PROFILES)}",
            next_action_argv=["build_request.py", "--profile", "<valid_profile>", "--objective", objective],
        )
        return 1

    # Resolve base dir for relative context file paths
    cwd = base_dir or Path.cwd()

    # B5: --gh-pr / --gh-issue are only allowed with github_research profile.
    if (gh_pr is not None or gh_issue is not None) and profile != "github_research":
        _write_failure(
            output=output,
            failure_class="validation_error",
            failure_reason=(
                f"--gh-pr/--gh-issue (gh_commands) are only supported with github_research profile, got: {profile}"
            ),
            next_action_argv=_build_full_argv(
                profile="github_research",
                objective=objective,
                instructions=instructions,
                context_file="<context-file>",
                output=output,
            ),
        )
        return 1

    # B4: --instruction fail-closed when explicitly provided but count < 2.
    # When instructions is None, use profile defaults (OK).
    # When instructions is explicitly provided (non-None), require >= 2 entries.
    if instructions is not None and len(instructions) < 2:
        _write_failure(
            output=output,
            failure_class="validation_error",
            failure_reason="--instruction must be specified at least twice when provided explicitly",
            next_action_argv=_build_full_argv(
                profile=profile,
                objective=objective,
                instructions=instructions,
                context_file="<context-file>",
                output=output,
            ),
        )
        return 1

    # Resolve context_files
    raw_context = context_files or []
    if not raw_context:
        _write_failure(
            output=output,
            failure_class="context_file_missing",
            failure_reason=(
                "context_files is required (at least 1 file must be specified). "
                "Use --context-file <path> to add a context file."
            ),
            next_action_argv=_build_full_argv(
                profile=profile,
                objective=objective,
                instructions=instructions,
                context_file="<path>",
                output=output,
            ),
        )
        return 1
    resolved_context = _resolve_context_files(
        raw_paths=raw_context,
        base_dir=cwd,
        output=output,
        profile=profile,
        objective=objective,
        instructions=instructions,
    )
    if resolved_context is None:
        return 1

    # Resolve instructions: use profile defaults when not explicitly provided.
    effective_instructions = instructions if instructions is not None else DEFAULT_INSTRUCTIONS_BY_PROFILE[profile]

    # Resolve output sections
    output_sections = DEFAULT_OUTPUT_SECTIONS_BY_PROFILE[profile]

    # Build gh_commands if gh-pr or gh-issue specified (only for github_research — already validated above)
    gh_commands: list[dict[str, list[str]]] | None = None
    if gh_pr is not None or gh_issue is not None:
        gh_commands = []
        if gh_issue is not None:
            gh_commands.append({"argv": ["issue", "view", str(gh_issue)]})
        if gh_pr is not None:
            gh_commands.append({"argv": ["pr", "view", str(gh_pr)]})

    request: dict[str, Any] = {
        "schema": SCHEMA,
        "objective": objective,
        "instructions": effective_instructions,
        "tool_profile": profile,
        "output_sections": output_sections,
        "context_files": resolved_context,
        "timeout_sec": 600,
    }
    if gh_commands:
        request["gh_commands"] = gh_commands

    # Validate via run_gemini_headless.validate_request
    try:
        validate_request = _load_validate_request()
        validation_errors = validate_request(request, request_path=output)
    except Exception as exc:  # pylint: disable=broad-except
        _write_failure(
            output=output,
            failure_class="internal_error",
            failure_reason=f"Failed to load validate_request: {exc}",
            next_action_argv=["build_request.py", "--help"],
        )
        return 2

    if validation_errors:
        _write_failure(
            output=output,
            failure_class="validation_error",
            failure_reason=validation_errors[0],
            next_action_argv=["build_request.py", "--profile", profile, "--objective", objective, "--help"],
        )
        return 1

    # Write output
    payload_str = json.dumps(request, ensure_ascii=False, indent=2) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload_str, encoding="utf-8")
        print(f"[build_request] request written to: {output}")
    else:
        print(payload_str, end="")

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--profile",
        required=True,
        metavar="TOOL_PROFILE",
        help=f"tool_profile for the request. Valid values: {sorted(VALID_PROFILES)}",
    )
    parser.add_argument(
        "--objective",
        required=True,
        metavar="STR",
        help="Specific objective for the Gemini delegation.",
    )
    parser.add_argument(
        "--instruction",
        dest="instructions",
        action="append",
        default=None,
        metavar="STR",
        help=(
            "Instruction to add to the request. Repeat for multiple instructions. "
            "Defaults to profile-specific instructions when omitted."
        ),
    )
    parser.add_argument(
        "--context-file",
        dest="context_files",
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "Path to a context file. Repeat for multiple files. "
            "Relative paths are resolved from cwd."
        ),
    )
    parser.add_argument(
        "--gh-pr",
        type=int,
        default=None,
        metavar="N",
        help="GitHub PR number to add as a gh_commands entry (pr view <N>).",
    )
    parser.add_argument(
        "--gh-issue",
        type=int,
        default=None,
        metavar="N",
        help="GitHub Issue number to add as a gh_commands entry (issue view <N>).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="Output path for the request JSON. Prints to stdout when omitted.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return build_request(
        profile=args.profile,
        objective=args.objective,
        instructions=args.instructions,
        context_files=args.context_files,
        gh_pr=args.gh_pr,
        gh_issue=args.gh_issue,
        output=args.output,
    )


if __name__ == "__main__":
    raise SystemExit(main())
