#!/usr/bin/env python3
"""Preflight agy CLI headless support: detect agy --help / agy -p contract."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

EXPECTED_SMOKE = "LOOP_AGY_SMOKE_OK"
SMOKE_PROMPT = f"Return exactly: {EXPECTED_SMOKE}"
SMOKE_TIMEOUT_SECONDS = 20
NONINTERACTIVE_FLAGS = ["-p", "--print", "--prompt"]
UNEXPECTED_CAPABILITY_KEYWORDS = ["chat", "--output-format"]
SMOKE_SAMPLE_MAX_CHARS = 500
LOCAL_ASSET_SERENA_TOOL_POLICY = "exact_match"
SERENA_TOOL_MANIFEST_RELATIVE_PATH = Path(
    ".claude/skills/gemini-cli-headless-delegation/references/serena-tool-manifest.json"
)
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
    "read_file",
    "read_file_content",
    "read_memory",
    "replace_content",
    "replace_in_files",
    "remove_project",
    "replace_lines",
    "replace_regex",
    "replace_symbol_body",
    "rename_symbol",
    "restart_language_server",
    "safe_delete_symbol",
    "switch_modes",
    "think_about_collected_information",
    "think_about_task_adherence",
    "think_about_whether_you_are_done",
    "delete_memory",
    "edit_memory",
    "rename_memory",
    "write_file",
    "write_memory",
})
SERENA_KNOWN_TOOLS = frozenset(SERENA_READ_ONLY_TOOLS | SERENA_DANGEROUS_TOOLS)
SECRET_ENV_KEYS = (
    "AGY_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
    "GITHUB_TOKEN",
)

# Regex patterns for flag detection with word boundaries to prevent false positives
# e.g. --prompting must NOT match -p, --printable must NOT match --print
FLAG_PATTERNS: dict[str, re.Pattern[str]] = {
    "-p": re.compile(r"(?<![\w-])-p(?![\w-])"),
    "--print": re.compile(r"(?<![\w-])--print(?![\w-])"),
    "--prompt": re.compile(r"(?<![\w-])--prompt(?![\w-])"),
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_serena_tool_manifest(repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root or _repo_root()
    manifest = _load_json(root / SERENA_TOOL_MANIFEST_RELATIVE_PATH)
    if not isinstance(manifest, dict):
        raise ValueError("serena manifest must be a JSON object")
    if manifest.get("schema") != "serena_tool_manifest_v1":
        raise ValueError("serena manifest schema must equal serena_tool_manifest_v1")
    for key in ("pinned_ref", "read_only_allowlist", "dangerous_denylist", "known_tools"):
        value = manifest.get(key)
        if key == "pinned_ref":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("serena manifest pinned_ref must be a non-empty string")
            continue
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError(f"serena manifest {key} must be a list of non-empty strings")
    return manifest


def _validate_local_asset_serena_contract(repo_root: Path | None = None) -> list[str]:
    root = repo_root or _repo_root()
    settings_path = root / ".gemini" / "settings.json"
    errors: list[str] = []
    try:
        manifest = load_serena_tool_manifest(root)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        return [f"local_asset_research serena manifest validation failed: {exc}"]

    try:
        settings = _load_json(settings_path)
    except FileNotFoundError:
        return [f"local_asset_research requires {settings_path}"]
    except json.JSONDecodeError as exc:
        return [f"local_asset_research requires valid JSON in {settings_path}: {exc}"]
    if not isinstance(settings, dict):
        return [f"local_asset_research requires {settings_path} to contain a JSON object"]

    expected_read_only = set(manifest["read_only_allowlist"])
    expected_dangerous = set(manifest["dangerous_denylist"])
    known_tools = set(manifest["known_tools"])
    pinned_ref = str(manifest["pinned_ref"])

    mcp = settings.get("mcp")
    allowed = mcp.get("allowed") if isinstance(mcp, dict) else None
    if allowed != ["serena"]:
        errors.append("local_asset_research requires .gemini/settings.json mcp.allowed to equal ['serena']")

    servers = settings.get("mcpServers")
    if not isinstance(servers, dict):
        errors.append("local_asset_research requires .gemini/settings.json mcpServers")
        return errors

    serena = servers.get("serena")
    if not isinstance(serena, dict):
        errors.append("local_asset_research requires .gemini/settings.json mcpServers.serena")
        return errors

    command = serena.get("command")
    args = serena.get("args")
    expected_source = f"git+https://github.com/oraios/serena@{pinned_ref}"
    if command != "uvx" or not isinstance(args, list) or "serena" not in args or "--project-from-cwd" not in args:
        errors.append(
            "local_asset_research requires WSL-local Serena MCP command: uvx ... serena ... --project-from-cwd"
        )
    elif expected_source not in args and not any(
        arg == f"serena=={pinned_ref}" for arg in args if isinstance(arg, str)
    ):
        errors.append(
            "local_asset_research pinned_serena_manifest_mismatch: "
            ".gemini/settings.json mcpServers.serena.args must match checked-in manifest pinned_ref"
        )

    trust = serena.get("trust", False)
    if trust is not False:
        errors.append("local_asset_research requires mcpServers.serena.trust to be false")

    include_tools = serena.get("includeTools")
    if not isinstance(include_tools, list) or not include_tools:
        errors.append("local_asset_research requires mcpServers.serena.includeTools read-only allowlist")
        return errors
    if not all(isinstance(tool, str) for tool in include_tools):
        errors.append("local_asset_research requires includeTools to contain only strings")
        return errors

    include_set = set(include_tools)
    unknown_tools = sorted(include_set - known_tools)
    if unknown_tools:
        errors.append(
            f"local_asset_research unknown_tool_policy({LOCAL_ASSET_SERENA_TOOL_POLICY}) failed: "
            f"unknown tools in includeTools: {', '.join(unknown_tools)}"
        )
    if include_set != expected_read_only:
        missing = sorted(expected_read_only - include_set)
        unexpected = sorted(include_set - expected_read_only)
        if missing:
            errors.append(f"local_asset_research read-only includeTools is incomplete: {', '.join(missing)}")
        if unexpected:
            errors.append(
                f"local_asset_research has unverified MCP tools in includeTools: {', '.join(unexpected)}"
            )

    exclude_tools = serena.get("excludeTools", [])
    if not isinstance(exclude_tools, list):
        errors.append("local_asset_research requires excludeTools to be a list when present")
        return errors
    if not expected_dangerous.issubset(set(exclude_tools)):
        missing_excludes = sorted(expected_dangerous - set(exclude_tools))
        errors.append(f"local_asset_research dangerous tool denylist is incomplete: {', '.join(missing_excludes)}")

    return errors


def _minimal_agy_env() -> dict[str, str]:
    """Return a minimal allowlisted environment for agy subprocess execution."""
    allowlist = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")
    env: dict[str, str] = {}
    for key in allowlist:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


def _redact_output_sample(text: str) -> str:
    """Return a bounded, redacted sample for stdout/stderr capture."""
    sample = text or ""
    for key in SECRET_ENV_KEYS:
        secret = os.environ.get(key)
        if secret:
            sample = sample.replace(secret, "<redacted>")
            if len(secret) >= 12:
                for width in (64, 48, 32, 24, 16, 12):
                    if len(secret) >= width:
                        sample = sample.replace(secret[:width], "<redacted-prefix>")

    home = os.environ.get("HOME")
    if home:
        sample = sample.replace(home, "$HOME")

    sample = re.sub(r"\bgh[pousr]_[A-Za-z0-9_]{8,}\b", "<redacted>", sample)
    sample = re.sub(r"\bsk-[A-Za-z0-9_-]{20,}\b", "<redacted>", sample)

    return sample[:SMOKE_SAMPLE_MAX_CHARS]


def _mask_resolved_path(path: str | None) -> str | None:
    """Return a sanitized resolved path suitable for JSON evidence."""
    if not path:
        return None
    home = os.environ.get("HOME")
    if home and path.startswith(home):
        suffix = path[len(home):].lstrip("/")
        return "$HOME" if not suffix else f"$HOME/{suffix}"
    return Path(path).name


def _resolve_binary() -> str:
    """Return agy binary path, overridable via AGY_BIN env var."""
    return os.environ.get("AGY_BIN", "agy")


def _run(
    argv: list[str],
    cwd: Path | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """subprocess.run wrapper — shell=False is enforced."""
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=_minimal_agy_env(),
        shell=False,
    )


def _run_version(agy_bin: str) -> subprocess.CompletedProcess[str]:
    """Run `agy --version` to confirm binary exists."""
    return _run([agy_bin, "--version"])


def _run_help(agy_bin: str) -> subprocess.CompletedProcess[str]:
    """Run `agy --help` to retrieve help text."""
    return _run([agy_bin, "--help"])


def _parse_help_capabilities(help_text: str) -> tuple[dict[str, bool], list[str]]:
    """Detect -p/--print/--prompt flags and unexpected capabilities.

    Returns a tuple of:
      noninteractive_flags: {"-p": bool, "--print": bool, "--prompt": bool}
      unexpected_capabilities: list of capability strings found

    Uses regex word-boundary matching to avoid false positives:
    e.g. --prompting will NOT match -p, --printable will NOT match --print.
    """
    noninteractive_flags: dict[str, bool] = {}
    for flag, pattern in FLAG_PATTERNS.items():
        noninteractive_flags[flag] = bool(pattern.search(help_text))

    unexpected_capabilities: list[str] = []
    for keyword in UNEXPECTED_CAPABILITY_KEYWORDS:
        if keyword in help_text:
            unexpected_capabilities.append(keyword)

    return noninteractive_flags, unexpected_capabilities


def _run_smoke(agy_bin: str) -> dict[str, Any]:
    """Run smoke check: `agy -p <SMOKE_PROMPT>` in isolated temp cwd.

    Returns dict with ok, argv, exit_code, timed_out, stdout_sample, stderr_sample.
    Success requires exit_code == 0 AND exact sentinel stdout.
    """
    argv = [agy_bin, "-p", SMOKE_PROMPT]
    smoke: dict[str, Any] = {
        "ok": False,
        "argv": argv,
        "exit_code": None,
        "timed_out": False,
        "failure_reason": None,
        "failure_class": None,
        "stdout_sample": "",
        "stderr_sample": "",
    }

    with tempfile.TemporaryDirectory(prefix="agy-preflight-") as temp_dir:
        try:
            proc = _run(argv, cwd=Path(temp_dir), timeout=SMOKE_TIMEOUT_SECONDS)
            smoke["exit_code"] = proc.returncode
            smoke["stdout_sample"] = _redact_output_sample(proc.stdout)
            smoke["stderr_sample"] = _redact_output_sample(proc.stderr)
            stdout = proc.stdout or ""

            if proc.returncode != 0:
                smoke["failure_reason"] = f"agy smoke command exited {proc.returncode}"
                smoke["failure_class"] = "agy_smoke_exit_nonzero"
            elif not stdout.strip():
                is_ci = os.environ.get("CI", "").lower() in {"1", "true", "yes", "on"}
                smoke["failure_reason"] = "agy_output_missing"
                smoke["failure_class"] = "agy_output_missing" if is_ci else "agy_empty_stdout"
            elif stdout.strip() != EXPECTED_SMOKE:
                smoke["failure_reason"] = f"agy_output_mismatch: got {stdout.strip()!r}"
                smoke["failure_class"] = "agy_output_mismatch"
            else:
                smoke["ok"] = True
        except subprocess.TimeoutExpired:
            smoke["timed_out"] = True

    return smoke


def run_preflight(*, validate_local_asset_contract: bool = False) -> dict[str, Any]:
    """Run version → help → smoke checks for agy binary.

    Returns an agy_preflight_result/v1 dict.
    """
    agy_bin = _resolve_binary()

    result: dict[str, Any] = {
        "schema": "agy_preflight_result/v1",
        "ok": False,
        "failure_reason": None,
        "failure_class": None,
        "recovery_action": None,
        "agy": {
            "bin": agy_bin,
            "resolved_path": None,
            "version": None,
        },
        "help": {
            "ok": False,
            "noninteractive_flags": {"-p": False, "--print": False, "--prompt": False},
            "unexpected_capabilities": [],
            "stdout_sample": "",
            "stderr_sample": "",
        },
        "smoke": {
            "ok": False,
            "argv": [],
            "exit_code": None,
            "timed_out": False,
            "stdout_sample": "",
            "stderr_sample": "",
        },
        "warnings": [],
    }

    # Step 1: version check
    try:
        version_proc = _run_version(agy_bin)
    except FileNotFoundError:
        result["failure_reason"] = f"{agy_bin}: command not found"
        result["failure_class"] = "cli_missing"
        result["recovery_action"] = "install agy or set AGY_BIN to a valid path"
        result["warnings"].append(result["failure_reason"])
        return result

    if version_proc.returncode != 0:
        result["failure_reason"] = f"agy --version failed (exit {version_proc.returncode})"
        result["failure_class"] = "cli_missing"
        result["warnings"].append(result["failure_reason"])
        return result

    version_str = version_proc.stdout.strip() or None
    result["agy"]["version"] = version_str
    try:
        import shutil
        resolved = shutil.which(agy_bin)
        result["agy"]["resolved_path"] = _mask_resolved_path(resolved)
    except Exception:
        pass

    # Step 2: help check
    try:
        help_proc = _run_help(agy_bin)
    except FileNotFoundError:
        result["failure_reason"] = f"{agy_bin}: command not found"
        result["failure_class"] = "cli_missing"
        result["warnings"].append(result["failure_reason"])
        return result

    if help_proc.returncode != 0:
        result["failure_reason"] = "agy --help failed"
        result["failure_class"] = "cli_incompatible"
        result["warnings"].append(result["failure_reason"])
        return result

    # Store redacted help output as live probe evidence.
    result["help"]["stdout_sample"] = _redact_output_sample(help_proc.stdout)
    result["help"]["stderr_sample"] = _redact_output_sample(help_proc.stderr)

    help_text = "\n".join(part for part in [help_proc.stdout, help_proc.stderr] if part)
    noninteractive_flags, unexpected_capabilities = _parse_help_capabilities(help_text)
    result["help"]["noninteractive_flags"] = noninteractive_flags
    result["help"]["unexpected_capabilities"] = unexpected_capabilities

    has_noninteractive = any(noninteractive_flags.values())
    result["help"]["ok"] = has_noninteractive

    if not has_noninteractive:
        result["failure_reason"] = "agy --help is missing noninteractive flags (-p / --print / --prompt)"
        result["failure_class"] = "cli_incompatible"
        result["recovery_action"] = "upgrade agy to a version that supports -p / --print / --prompt"
        return result

    # Step 3: smoke check
    try:
        smoke = _run_smoke(agy_bin)
    except subprocess.TimeoutExpired:
        smoke = {
            "ok": False,
            "argv": [agy_bin, "-p", SMOKE_PROMPT],
            "exit_code": None,
            "timed_out": True,
            "failure_reason": "agy smoke timed out",
            "failure_class": "client_subprocess_timeout",
            "stdout_sample": "",
            "stderr_sample": "",
        }

    result["smoke"] = smoke

    if smoke["timed_out"]:
        result["failure_reason"] = "agy smoke check timed out"
        result["failure_class"] = "client_subprocess_timeout"
        result["recovery_action"] = "check agy network connectivity or increase timeout"
        return result

    if not smoke["ok"]:
        result["failure_reason"] = smoke.get("failure_reason") or "agy smoke check failed"
        result["failure_class"] = smoke.get("failure_class") or "agy_output_missing"
        result["recovery_action"] = "check agy configuration and rerun preflight"
        return result

    if validate_local_asset_contract:
        contract_errors = _validate_local_asset_serena_contract()
        local_asset_result = {
            "ok": not contract_errors,
            "errors": contract_errors,
            "unknown_tool_policy": LOCAL_ASSET_SERENA_TOOL_POLICY,
        }
        if local_asset_result["ok"]:
            local_asset_result["status"] = "ok"
        else:
            result["failure_reason"] = local_asset_result["errors"][0]
            result["failure_class"] = "local_asset_contract_invalid"
            result["recovery_action"] = "fix .gemini/settings.json Serena contract for local_asset_research"
        result["local_asset_research"] = local_asset_result

    if result.get("local_asset_research") is not None and not result["local_asset_research"]["ok"]:
        result["ok"] = False
        return result

    result["ok"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    """Entry point for CLI invocation.

    --json: print result to stdout as JSON
    --output-file: write result to file
    Success exits 0, failure exits 1.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_stdout",
        default=False,
        help="Print the preflight result JSON to stdout.",
    )
    parser.add_argument(
        "--local-asset-research",
        action="store_true",
        dest="local_asset_research",
        default=False,
        help="Also validate .gemini settings local_asset_research Serena tool contract.",
    )
    parser.add_argument(
        "--output-file",
        required=False,
        type=Path,
        default=None,
        help="Path to write the preflight result JSON.",
    )
    args = parser.parse_args(argv)

    result = run_preflight(validate_local_asset_contract=args.local_asset_research)

    if args.json_stdout:
        print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.output_file is not None:
        args.output_file.parent.mkdir(parents=True, exist_ok=True)
        with args.output_file.open("w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
