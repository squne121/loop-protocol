#!/usr/bin/env python3
"""Preflight Gemini CLI headless support for the delegation skill."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

SMOKE_PROMPT = "Do not use any tools. Reply with OK only."
SMOKE_MODEL = "gemini-3-flash-preview"
PROMPT_STDIN_MARKER = "Appended to input on stdin"
TRUSTED_DIRECTORY_DIAGNOSTIC = (
    "Gemini CLI trusted directory check failed even with --skip-trust. "
    "This is unexpected; check if the Gemini CLI version supports --skip-trust "
    "or set GEMINI_CLI_TRUST_WORKSPACE=true before rerunning preflight."
)
SERENA_MCP_SERVER_NAME = "serena"
VALIDATED_TOOL_PROFILES = [
    "no_tools",
    "grounded_research",
    "local_asset_research",
    "proposal_only",
    "github_research",
]
PROPOSAL_ONLY_ALLOWED_OUTPUTS = [
    "implementation_draft",
    "issue_authoring_draft",
    "patch_proposal",
    "command_plan",
]
PROPOSAL_ONLY_FORBIDDEN_CAPABILITIES = [
    "file write",
    "shell edit",
    "GitHub mutation",
    "post_to_issue_url",
]
SERENA_READ_ONLY_TOOLS = frozenset({
    "find_file",
    "find_referencing_symbols",
    "find_symbol",
    "get_symbols_overview",
    "list_dir",
    "search_for_pattern",
})
SERENA_DANGEROUS_TOOLS = frozenset({
    "activate_project",
    "create_text_file",
    "delete_lines",
    "execute_shell_command",
    "insert_after_symbol",
    "insert_at_line",
    "insert_before_symbol",
    "prepare_for_new_conversation",
    "read_file_content",
    "read_memory",
    "remove_project",
    "replace_lines",
    "replace_regex",
    "replace_symbol_body",
    "restart_language_server",
    "switch_modes",
    "think_about_collected_information",
    "think_about_task_adherence",
    "think_about_whether_you_are_done",
    "write_file",
    "write_memory",
})


def _dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")


def _run(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _validate_local_asset_research_settings(repo_root: Path | None = None) -> list[str]:
    root = repo_root or _repo_root()
    settings_path = root / ".gemini" / "settings.json"
    errors: list[str] = []
    try:
        settings = _load_json(settings_path)
    except FileNotFoundError:
        return [f"local_asset_research requires {settings_path}"]
    except json.JSONDecodeError as exc:
        return [f"local_asset_research requires valid JSON in {settings_path}: {exc}"]
    if not isinstance(settings, Mapping):
        return [f"local_asset_research requires {settings_path} to contain a JSON object"]

    mcp = settings.get("mcp")
    allowed_servers = mcp.get("allowed") if isinstance(mcp, Mapping) else None
    if allowed_servers != [SERENA_MCP_SERVER_NAME]:
        errors.append("local_asset_research requires .gemini/settings.json mcp.allowed to equal ['serena']")

    servers = settings.get("mcpServers")
    if not isinstance(servers, Mapping):
        return errors + ["local_asset_research requires .gemini/settings.json mcpServers"]
    serena = servers.get(SERENA_MCP_SERVER_NAME)
    if not isinstance(serena, Mapping):
        return errors + ["local_asset_research requires .gemini/settings.json mcpServers.serena"]

    command = serena.get("command")
    args = serena.get("args")
    if command != "uvx" or not isinstance(args, list) or "serena" not in args or "--project-from-cwd" not in args:
        errors.append("local_asset_research requires WSL-local Serena MCP command: uvx ... serena ... --project-from-cwd")

    trust = serena.get("trust", False)
    if trust is not False:
        errors.append("local_asset_research requires mcpServers.serena.trust to be false")

    include_tools = serena.get("includeTools")
    if not isinstance(include_tools, list) or not include_tools:
        errors.append("local_asset_research requires mcpServers.serena.includeTools read-only allowlist")
    elif not all(isinstance(tool, str) for tool in include_tools):
        errors.append("local_asset_research requires includeTools to contain only strings")
    else:
        include_set = set(include_tools)
        unexpected = sorted(include_set - SERENA_READ_ONLY_TOOLS)
        missing = sorted(SERENA_READ_ONLY_TOOLS - include_set)
        dangerous = sorted(include_set & SERENA_DANGEROUS_TOOLS)
        if unexpected:
            errors.append(f"local_asset_research has unverified MCP tools in includeTools: {', '.join(unexpected)}")
        if missing:
            errors.append(f"local_asset_research read-only includeTools is incomplete: {', '.join(missing)}")
        if dangerous:
            errors.append(f"local_asset_research includes dangerous Serena MCP tools: {', '.join(dangerous)}")

    exclude_tools = serena.get("excludeTools", [])
    if not isinstance(exclude_tools, list):
        errors.append("local_asset_research requires excludeTools to be a list when present")
    elif not SERENA_DANGEROUS_TOOLS.issubset(set(exclude_tools)):
        missing_excludes = sorted(SERENA_DANGEROUS_TOOLS - set(exclude_tools))
        errors.append(f"local_asset_research dangerous tool denylist is incomplete: {', '.join(missing_excludes)}")

    return errors


def _parse_json_object(stdout: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return None, str(exc)
    if not isinstance(parsed, dict):
        return None, "expected a JSON object"
    return parsed, None


def _is_trusted_directory_error(stderr: str) -> bool:
    normalized = stderr.lower()
    return (
        "trusted directory" in normalized
        or "gemini_cli_trust_workspace" in normalized
        or "--skip-trust" in normalized
    )


def _stderr_warnings(stderr: str) -> list[str]:
    warnings = [line.strip() for line in stderr.splitlines() if line.strip()]
    if _is_trusted_directory_error(stderr) and TRUSTED_DIRECTORY_DIAGNOSTIC not in warnings:
        warnings.insert(0, TRUSTED_DIRECTORY_DIAGNOSTIC)
    return warnings


def _help_supports_prompt_stdin(help_text: str) -> bool:
    return PROMPT_STDIN_MARKER in help_text


def _command_not_found_result(result: dict[str, Any], command: str) -> dict[str, Any]:
    reason = f"{command}: command not found"
    result["failure_reason"] = reason
    result["warnings"].append(reason)
    return result


def run_preflight() -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "gemini_headless_preflight_result/v1",
        "ok": False,
        "failure_reason": None,
        "failure_class": None,
        "recovery_action": None,
        "validated_tool_profiles": VALIDATED_TOOL_PROFILES[:],
        "version": {"ok": False, "stdout": "", "stderr": "", "value": None},
        "help": {
            "ok": False,
            "stdout": "",
            "stderr": "",
            "required_flags": ["--model", "--prompt", "--output-format", "--approval-mode", "--skip-trust"],
            "missing_flags": [],
        },
        "smoke": {
            "ok": False,
            "command": [],
            "stdout": "",
            "stderr": "",
            "response_text": None,
            "stats": None,
        },
        "local_asset_research": {
            "ok": False,
            "settings_path": str(_repo_root() / ".gemini" / "settings.json"),
            "prompt_stdin_supported": False,
            "read_only_tools": sorted(SERENA_READ_ONLY_TOOLS),
            "errors": [],
        },
        "proposal_only": {
            "ok": True,
            "allowed_outputs": PROPOSAL_ONLY_ALLOWED_OUTPUTS[:],
            "forbidden_capabilities": PROPOSAL_ONLY_FORBIDDEN_CAPABILITIES[:],
            "write_owner": "Codex",
        },
        "gh_cli": {
            "ok": False,
            "version": None,
            "auth_status": None,
            "errors": [],
        },
        "warnings": [],
    }

    try:
        version_proc = _run(["gemini", "--version"])
    except FileNotFoundError:
        return _command_not_found_result(result, "gemini")
    result["version"]["stdout"] = version_proc.stdout
    result["version"]["stderr"] = version_proc.stderr
    result["version"]["ok"] = version_proc.returncode == 0
    result["version"]["value"] = version_proc.stdout.strip() or None
    if not result["version"]["ok"]:
        result["failure_reason"] = "gemini --version failed"
        result["warnings"].extend(_stderr_warnings(version_proc.stderr))
        return result

    try:
        help_proc = _run(["gemini", "--help"])
    except FileNotFoundError:
        return _command_not_found_result(result, "gemini")
    result["help"]["stdout"] = help_proc.stdout
    result["help"]["stderr"] = help_proc.stderr
    result["help"]["ok"] = help_proc.returncode == 0
    if not result["help"]["ok"]:
        result["failure_reason"] = "gemini --help failed"
        result["warnings"].extend(_stderr_warnings(help_proc.stderr))
        return result

    missing_flags = [
        flag
        for flag in result["help"]["required_flags"]
        if flag not in help_proc.stdout
    ]
    result["help"]["missing_flags"] = missing_flags
    if missing_flags:
        result["failure_reason"] = f"gemini --help is missing: {', '.join(missing_flags)}"
        return result

    local_asset_errors = _validate_local_asset_research_settings()
    local_asset_errors.extend(
        [
            "gemini --help requires --prompt + stdin append support for long local_asset_research context",
        ]
        if not _help_supports_prompt_stdin(help_proc.stdout)
        else []
    )
    result["local_asset_research"]["errors"] = local_asset_errors
    result["local_asset_research"]["prompt_stdin_supported"] = _help_supports_prompt_stdin(help_proc.stdout)
    result["local_asset_research"]["ok"] = not local_asset_errors
    if local_asset_errors:
        result["failure_reason"] = local_asset_errors[0]
        result["warnings"].extend(local_asset_errors)
        return result

    with tempfile.TemporaryDirectory(prefix="gemini-preflight-") as temp_dir:
        smoke_command = [
            "gemini",
            "--model",
            SMOKE_MODEL,
            "--approval-mode",
            "plan",
            "--skip-trust",
            "--prompt",
            SMOKE_PROMPT,
            "--output-format",
            "json",
        ]
        try:
            smoke_proc = _run(smoke_command, cwd=Path(temp_dir))
        except FileNotFoundError:
            return _command_not_found_result(result, "gemini")
        result["smoke"]["command"] = smoke_command
        result["smoke"]["stdout"] = smoke_proc.stdout
        result["smoke"]["stderr"] = smoke_proc.stderr

        envelope, parse_error = _parse_json_object(smoke_proc.stdout)
        if parse_error:
            if _is_trusted_directory_error(smoke_proc.stderr):
                result["failure_reason"] = (
                    f"smoke JSON parse failed: {parse_error}; "
                    f"{TRUSTED_DIRECTORY_DIAGNOSTIC}"
                )
                result["failure_class"] = "trusted_workspace_required"
                result["recovery_action"] = (
                    "set GEMINI_CLI_TRUST_WORKSPACE=true and rerun preflight"
                )
            else:
                result["failure_reason"] = f"smoke JSON parse failed: {parse_error}"
            result["warnings"].extend(_stderr_warnings(smoke_proc.stderr))
            return result

        response_text = envelope.get("response")
        if not isinstance(response_text, str):
            result["failure_reason"] = "smoke response is missing"
            return result

        result["smoke"]["response_text"] = response_text
        result["smoke"]["stats"] = envelope.get("stats")
        result["smoke"]["ok"] = smoke_proc.returncode == 0 and response_text.strip() == "OK"
        if not result["smoke"]["ok"]:
            result["failure_reason"] = "smoke returned a non-OK result"
            result["warnings"].extend(_stderr_warnings(smoke_proc.stderr))
            return result

        if smoke_proc.stderr.strip():
            result["warnings"].extend(_stderr_warnings(smoke_proc.stderr))

    # gh_cli preflight: check gh --version and gh auth status.
    # gh_cli failures are recorded as warnings only and do NOT set the top-level ok=False.
    # Callers that use github_research must check result["gh_cli"]["ok"] individually.
    gh_cli_errors: list[str] = []
    try:
        gh_version_proc = _run(["gh", "--version"])
        if gh_version_proc.returncode == 0:
            result["gh_cli"]["version"] = gh_version_proc.stdout.strip().splitlines()[0] if gh_version_proc.stdout.strip() else None
        else:
            gh_cli_errors.append("gh --version failed")
            result["warnings"].extend(_stderr_warnings(gh_version_proc.stderr))
    except FileNotFoundError:
        gh_cli_errors.append("gh: command not found")
        result["warnings"].append("gh: command not found (github_research unavailable; install gh and run gh auth login)")

    if not gh_cli_errors:
        try:
            gh_auth_proc = _run(["gh", "auth", "status"])
            auth_ok = gh_auth_proc.returncode == 0
            result["gh_cli"]["auth_status"] = (gh_auth_proc.stdout + gh_auth_proc.stderr).strip() or None
            if not auth_ok:
                gh_cli_errors.append("gh auth status failed: not authenticated")
                result["warnings"].extend(_stderr_warnings(gh_auth_proc.stderr))
        except FileNotFoundError:
            gh_cli_errors.append("gh: command not found")
            result["warnings"].append("gh: command not found (github_research unavailable)")

    result["gh_cli"]["errors"] = gh_cli_errors
    result["gh_cli"]["ok"] = not gh_cli_errors
    # gh_cli failures are surfaced as warnings for observability but do not block
    # proposal_only / grounded_research / no_tools / local_asset_research callers.
    if gh_cli_errors:
        result["warnings"].append(
            f"gh_cli check failed: {gh_cli_errors[0]} — github_research profile will be unavailable"
        )

    result["ok"] = True
    return result


_COMPACT_NESTED_EXCLUDED: dict[str, frozenset[str]] = {
    "version": frozenset({"stdout", "stderr"}),
    "help": frozenset({"stdout", "stderr", "required_flags"}),
    "smoke": frozenset({"command", "stdout", "stderr", "stats"}),
}


def _strip_verbose_subfields(result: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *result* with verbose diagnostic subfields removed.

    Each section listed in ``_COMPACT_NESTED_EXCLUDED`` has its named subfields
    stripped.  Top-level scalar fields (``ok``, ``failure_reason``, ``warnings``,
    ``schema``) are always preserved unchanged.

    Note: This function operates on *nested* dict sections (version, help, smoke),
    stripping their verbose subfields. It is distinct from ``_apply_compact`` in
    ``run_gemini_headless.py``, which removes *top-level* keys from a flat result dict.

    Edge-case notes
    ---------------
    * If a nested section value is ``None`` (not a ``dict``), the ``isinstance``
      guard ensures it is kept as-is rather than raising a ``TypeError``.  In
      practice ``run_preflight()`` always initialises every section as a dict,
      so this branch is purely defensive.
    * Fields whose keys are not in ``_COMPACT_NESTED_EXCLUDED`` pass through
      without modification regardless of their type.
    """
    stripped: dict[str, Any] = {}
    for key, value in result.items():
        excluded = _COMPACT_NESTED_EXCLUDED.get(key)
        if excluded is not None and isinstance(value, dict):
            stripped[key] = {k: v for k, v in value.items() if k not in excluded}
        else:
            stripped[key] = value
    return stripped


def _build_next_action(result: dict[str, Any]) -> dict[str, Any]:
    """Build the next_action field for all results.

    For success results: next_action points to running the actual delegation wrapper.
    For failure results: next_action points to the appropriate recovery command.
    """
    failure_class = result.get("failure_class")
    if failure_class == "trusted_workspace_required":
        argv = ["setup_check.py", "--json", "--fix"]
        return {"argv": argv, "command": " ".join(argv)}
    if result.get("ok"):
        # Success: suggest running the delegation wrapper.
        argv = ["run_gemini_headless.py", "--request-file", "<request.json>", "--output-file", "<result.json>"]
        return {"argv": argv, "command": " ".join(argv)}
    # Generic failure: suggest re-running preflight after fixing the issue.
    argv = ["preflight_gemini_headless.py", "--output-file", "tmp/preflight.json"]
    return {"argv": argv, "command": " ".join(argv)}


def _inject_next_action(result: dict[str, Any]) -> dict[str, Any]:
    """Inject next_action into results if not already present.

    Always injected so callers can rely on next_action being present.
    """
    if "next_action" not in result:
        result = dict(result)
        result["next_action"] = _build_next_action(result)
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-file",
        required=False,
        type=Path,
        default=None,
        help="Path to write the preflight result JSON. Optional when --json is used.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        default=False,
        help=(
            "Omit verbose diagnostic fields from output JSON to reduce context window usage."
            " Excluded subfields: version.stdout/stderr, help.stdout/stderr/required_flags,"
            " smoke.command/stdout/stderr/stats."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_stdout",
        default=False,
        help=(
            "Print the preflight result JSON to stdout. "
            "Can be combined with --output-file to both write a file and print to stdout."
        ),
    )
    return parser


def _print_stdout_summary(result: dict[str, Any], output_file: Path | None) -> None:
    """Print a human/agent-friendly summary to stdout after JSON is saved."""
    if result["ok"]:
        version = result.get("version", {}).get("value") or "unknown"
        print(f"[gemini-preflight] ok: Gemini CLI {version} is ready")
    else:
        failure_class = result.get("failure_class")
        recovery_action = result.get("recovery_action")
        failure_reason = result.get("failure_reason")
        next_action = result.get("next_action", {})
        if failure_class == "trusted_workspace_required":
            print(
                f"[gemini-preflight] error: trusted_workspace_required"
                f" — {failure_reason}"
            )
            if next_action.get("command"):
                print(f"[gemini-preflight] next_action: {next_action['command']}")
            elif recovery_action:
                print(
                    f"[gemini-preflight] recovery: {recovery_action}"
                    f" (GEMINI_CLI_TRUST_WORKSPACE=true)"
                )
        elif failure_reason:
            print(f"[gemini-preflight] error: {failure_reason}")
            if next_action.get("command"):
                print(f"[gemini-preflight] next_action: {next_action['command']}")
        else:
            print(
                "[gemini-preflight] error: preflight failed"
                " (no failure reason available; see result JSON)"
            )
    if output_file is not None:
        print(f"[gemini-preflight] result saved to: {output_file}")


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.output_file is None and not args.json_stdout:
        # Default: require at least one output mechanism.
        print(
            "[gemini-preflight] error: specify --output-file <path> or --json (or both)",
            file=__import__("sys").stderr,
        )
        return 2
    result = run_preflight()
    result = _inject_next_action(result)
    if args.compact:
        result = _strip_verbose_subfields(result)
    if args.output_file is not None:
        _dump_json(args.output_file, result)
    if args.json_stdout:
        print(__import__("json").dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_stdout_summary(result, args.output_file)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
