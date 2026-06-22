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

SMOKE_PROMPT = "Do not use any tools. Reply with OK only."
NONINTERACTIVE_FLAGS = ["-p", "--print", "--prompt"]
UNEXPECTED_CAPABILITY_KEYWORDS = ["chat", "--output-format"]

# Regex patterns for flag detection with word boundaries to prevent false positives
# e.g. --prompting must NOT match -p, --printable must NOT match --print
FLAG_PATTERNS: dict[str, re.Pattern[str]] = {
    "-p": re.compile(r"(?<![\w-])-p(?![\w-])"),
    "--print": re.compile(r"(?<![\w-])--print(?![\w-])"),
    "--prompt": re.compile(r"(?<![\w-])--prompt(?![\w-])"),
}


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
    Success requires exit_code == 0 AND non-empty stdout (detects silent output drop).
    """
    argv = [agy_bin, "-p", SMOKE_PROMPT]
    smoke: dict[str, Any] = {
        "ok": False,
        "argv": argv,
        "exit_code": None,
        "timed_out": False,
        "stdout_sample": "",
        "stderr_sample": "",
    }

    with tempfile.TemporaryDirectory(prefix="agy-preflight-") as temp_dir:
        try:
            proc = _run(argv, cwd=Path(temp_dir), timeout=20)
            smoke["exit_code"] = proc.returncode
            smoke["stdout_sample"] = proc.stdout[:500]
            smoke["stderr_sample"] = proc.stderr[:500]
            # Require non-empty stdout to detect silent output drop (exit 0 with no output)
            smoke["ok"] = proc.returncode == 0 and bool(proc.stdout.strip())
        except subprocess.TimeoutExpired:
            smoke["timed_out"] = True

    return smoke


def run_preflight() -> dict[str, Any]:
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
        result["recovery_action"] = f"install agy or set AGY_BIN to a valid path"
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
        result["agy"]["resolved_path"] = resolved
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

    # Store raw help output as live probe evidence
    result["help"]["stdout_sample"] = help_proc.stdout[:2000]
    result["help"]["stderr_sample"] = help_proc.stderr[:500]

    noninteractive_flags, unexpected_capabilities = _parse_help_capabilities(help_proc.stdout)
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
        if smoke["exit_code"] == 0:
            result["failure_reason"] = "smoke exited 0 but produced no output (possible silent output drop)"
        else:
            result["failure_reason"] = f"agy smoke check failed (exit {smoke['exit_code']})"
        result["failure_class"] = "smoke_failed"
        result["recovery_action"] = "check agy configuration and rerun preflight"
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
        "--output-file",
        required=False,
        type=Path,
        default=None,
        help="Path to write the preflight result JSON.",
    )
    args = parser.parse_args(argv)

    result = run_preflight()

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
