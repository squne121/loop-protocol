#!/usr/bin/env python3
"""setup_check.py — Runtime setup checker for gemini-cli-headless-delegation skill.

Usage:
    uv run python3 setup_check.py [--json]

Exit codes:
    0  All checks passed (fully ready)
    1  Dependency missing or misconfigured (recoverable)
    2  Execution environment error (unexpected failure)

Test execution note:
    Run tests with dependencies pre-installed:
        uv run --with pytest --with pyyaml python -m pytest tests/

This script uses only the standard library (subprocess, json, pathlib, sys, os)
to minimise external dependencies.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERENA_MCP_SERVER_NAME = "serena"
SERENA_MCP_PACKAGE = "git+https://github.com/oraios/serena"
SERENA_READ_ONLY_TOOLS = [
    "find_file",
    "find_referencing_symbols",
    "find_symbol",
    "get_symbols_overview",
    "list_dir",
    "search_for_pattern",
]

GEMINI_REQUIRED_TOOLS = ["node", "gemini", "python3", "uv", "uvx"]
AGY_REQUIRED_TOOLS = ["agy", "python3", "uv"]
SUPPORTED_PROVIDERS = frozenset({"gemini", "agy", "auto"})

# Timeout for Serena MCP availability check (seconds).
SERENA_CHECK_TIMEOUT = 30
# Timeout for smoke prompt (seconds).
SMOKE_PROMPT_TIMEOUT = 20

# Valid auth status values (fixed enum — do not change without updating Issue #1081).
AUTH_STATUS_VALUES = frozenset([
    "authenticated",
    "authenticated_api_key",
    "oauth_sunset",
    "ineligible_tier",
    "unauthenticated",
    "auth_failed",
    "timeout",
    "gemini_not_found",
])

# Recovery message template for auth failures that warrant API key / agy migration info.
_RECOVERY_APIKEY_AND_AGY = [
    "Temporary workaround: set GEMINI_API_KEY environment variable "
    "(existence is detected; value is never logged — do not share or commit the key).",
    "Permanent solution: migrate to agy (Antigravity CLI) — see #104.",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(command: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _redact_api_key(text: str) -> str:
    """Remove GEMINI_API_KEY value from text before output.

    Security: the key value is NEVER stored in variables or included in outputs.
    This redaction is a last-resort safety net for cases where external processes
    (e.g. the gemini CLI itself) include the key in their own error output.
    """
    secret = os.environ.get("GEMINI_API_KEY")
    if secret and secret in text:
        return text.replace(secret, "[REDACTED]")
    return text


def _trusted_folders_path() -> Path:
    """Return the path to trustedFolders.json, honouring GEMINI_CLI_TRUSTED_FOLDERS_PATH override."""
    override = os.environ.get("GEMINI_CLI_TRUSTED_FOLDERS_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemini" / "trustedFolders.json"


def _git_repo_root() -> Path | None:
    """Return the absolute repo root via git rev-parse, or None on failure."""
    try:
        result = _run(["git", "rev-parse", "--show-toplevel"])
        if result.returncode == 0:
            return Path(result.stdout.strip()).resolve()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _tool_version(tool: str) -> str | None:
    """Return the version string for a tool, or None if not found."""
    try:
        result = _run([tool, "--version"], timeout=10)
        if result.returncode == 0:
            return (result.stdout.strip() or result.stderr.strip()).splitlines()[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


# ---------------------------------------------------------------------------
# Check: dependency tools
# ---------------------------------------------------------------------------


def check_tools(required_tools: list[str] | None = None) -> dict[str, Any]:
    """Check that all required tools are present and record versions."""
    tools = required_tools or GEMINI_REQUIRED_TOOLS
    versions: dict[str, str | None] = {}
    missing: list[str] = []
    for tool in tools:
        ver = _tool_version(tool)
        versions[tool] = ver
        if ver is None:
            missing.append(tool)

    ok = len(missing) == 0
    result: dict[str, Any] = {"ok": ok, "versions": versions}
    if missing:
        result["missing"] = missing
        recovery = [f"Install missing tool(s): {', '.join(missing)}"]
        if any(tool in {"node", "gemini"} for tool in missing):
            recovery.append("  node/gemini: npm install -g @google/gemini-cli (requires node >= 18)")
        if "agy" in missing:
            recovery.append("  agy:        install Antigravity CLI and ensure `agy` is on PATH")
        if any(tool in {"uv", "uvx"} for tool in missing):
            recovery.append("  uv/uvx:      curl -LsSf https://astral.sh/uv/install.sh | sh")
        result["recovery"] = recovery
    return result


def _load_preflight_agy_module():
    module_path = Path(__file__).with_name("preflight_agy.py")
    spec = importlib.util.spec_from_file_location("preflight_agy", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_agy_preflight() -> dict[str, Any]:
    """Run provider-aware agy preflight through the sibling script."""
    module = _load_preflight_agy_module()
    return module.run_preflight()


# ---------------------------------------------------------------------------
# Check: trustedFolders.json
# ---------------------------------------------------------------------------


def check_trusted_folders(repo_root: Path | None = None, fix: bool = False) -> dict[str, Any]:
    """Check (and optionally register) repo root in ~/.gemini/trustedFolders.json.

    Logic:
    - trustedFolders.json uses a dict schema: {path: "TRUST_FOLDER" | "TRUST_PARENT"}.
    - If the repo root (TRUST_FOLDER) or a parent directory (TRUST_PARENT) is
      already present, perform no-op (idempotent). Existing entries are preserved.
    - When fix=False (default): check-only, no HOME or repo side-effects.
      Returns ok=False with status="needs_fix" when the entry is missing.
    - When fix=True: append the repo root as {repo_root: "TRUST_FOLDER"} and persist.
    """
    root = repo_root or _git_repo_root()
    if root is None:
        return {
            "ok": False,
            "status": "error",
            "detail": "Could not determine repository root via git rev-parse --show-toplevel.",
            "recovery": ["Run setup_check.py from inside the LOOP_PROTOCOL git repository."],
        }

    trusted_path = _trusted_folders_path()
    trust_folder = str(root)

    # Load existing dict. The gemini CLI schema is {path: "TRUST_FOLDER" | "TRUST_PARENT"}.
    existing: dict[str, str] = {}
    if trusted_path.exists():
        try:
            data = json.loads(trusted_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                existing = {str(k): str(v) for k, v in data.items()}
        except (json.JSONDecodeError, OSError):
            pass  # Treat as empty; will overwrite below.

    # Check for TRUST_FOLDER (exact match) or TRUST_PARENT (ancestor directory).
    for entry, trust_type in existing.items():
        entry_path = Path(entry)
        if entry_path == root and trust_type == "TRUST_FOLDER":
            # TRUST_FOLDER already present — no-op. Existing entries are preserved.
            return {"ok": True, "status": "already_trusted", "path": trust_folder}
        if trust_type == "TRUST_PARENT":
            try:
                root.relative_to(entry_path)
                # TRUST_PARENT present — no-op. Existing entries are preserved.
                return {"ok": True, "status": "parent_trusted", "path": trust_folder, "parent": entry}
            except ValueError:
                pass

    # Entry is missing.
    if not fix:
        return {
            "ok": False,
            "status": "needs_fix",
            "path": trust_folder,
            "detail": f"'{trust_folder}' is not in {trusted_path}. Run with --fix to register it.",
            "recovery": [
                "Run: setup_check.py --json --fix",
                f"Or manually add '{trust_folder}' to {trusted_path}",
            ],
        }

    # fix=True: Append new entry and persist. Existing entries are preserved.
    existing[trust_folder] = "TRUST_FOLDER"
    try:
        trusted_path.parent.mkdir(parents=True, exist_ok=True)
        trusted_path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return {
            "ok": False,
            "status": "write_error",
            "detail": str(exc),
            "recovery": [f"Manually add '{trust_folder}' to {trusted_path}"],
        }

    return {"ok": True, "status": "added", "path": trust_folder}


# ---------------------------------------------------------------------------
# Check: Serena MCP availability
# ---------------------------------------------------------------------------


def check_serena_mcp() -> dict[str, Any]:
    """Verify that Serena MCP can be invoked via uvx (without full install).

    The serena package exposes the 'serena' executable (not the old 'serena-mcp' command).
    We invoke 'serena --help' to verify availability.
    """
    cmd = [
        "uvx",
        "--from",
        SERENA_MCP_PACKAGE,
        "serena",
        "--help",
    ]
    try:
        result = _run(cmd, timeout=SERENA_CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "status": "timeout",
            "detail": f"uvx serena --help timed out after {SERENA_CHECK_TIMEOUT}s.",
            "recovery": [
                "Check network connectivity to https://github.com/oraios/serena",
                "Pre-cache with: uvx --from git+https://github.com/oraios/serena serena --help",
            ],
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "status": "uvx_not_found",
            "detail": "uvx not found in PATH.",
            "recovery": ["Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"],
        }

    if result.returncode == 0 or "Usage" in (result.stdout + result.stderr):
        return {"ok": True, "status": "available"}

    return {
        "ok": False,
        "status": "unavailable",
        "returncode": result.returncode,
        "stderr": result.stderr[:500],
        "recovery": [
            "Verify internet access to github.com/oraios/serena",
            "Check that uvx is up-to-date: uv self update",
        ],
    }


# ---------------------------------------------------------------------------
# Check: .gemini/settings.json
# ---------------------------------------------------------------------------

_SETTINGS_TEMPLATE: dict[str, Any] = {
    "mcp": {
        "allowed": [SERENA_MCP_SERVER_NAME],
    },
    "mcpServers": {
        SERENA_MCP_SERVER_NAME: {
            "command": "uvx",
            "args": [
                "--from",
                SERENA_MCP_PACKAGE,
                "serena",
                "--project-from-cwd",
            ],
            "trust": False,
            "includeTools": SERENA_READ_ONLY_TOOLS,
        }
    },
}


def check_gemini_settings(repo_root: Path | None = None, fix: bool = False) -> dict[str, Any]:
    """Check (and optionally create) .gemini/settings.json.

    When fix=False (default): check-only, no repo side-effects.
    When fix=True: generate template if absent. Never overwrites an existing file.
    """
    root = repo_root or _git_repo_root()
    if root is None:
        return {
            "ok": False,
            "status": "error",
            "detail": "Could not determine repository root.",
            "recovery": ["Run setup_check.py from inside the LOOP_PROTOCOL git repository."],
        }

    settings_path = root / ".gemini" / "settings.json"
    if settings_path.exists():
        return {"ok": True, "status": "exists", "path": str(settings_path)}

    # File is absent.
    if not fix:
        return {
            "ok": False,
            "status": "needs_fix",
            "path": str(settings_path),
            "detail": f"{settings_path} is absent. Run with --fix to create the Serena MCP template.",
            "recovery": [
                "Run: setup_check.py --json --fix",
                f"Or manually create {settings_path} with the Serena MCP template.",
            ],
        }

    # fix=True: Generate template.
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(_SETTINGS_TEMPLATE, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return {
            "ok": False,
            "status": "write_error",
            "detail": str(exc),
            "recovery": [f"Manually create {settings_path} with the Serena MCP template."],
        }

    return {"ok": True, "status": "created", "path": str(settings_path)}


# ---------------------------------------------------------------------------
# Check: account authentication (smoke prompt)
# ---------------------------------------------------------------------------

# Specific service-termination phrases for oauth_sunset.
# Use only specific multi-word phrases to avoid false positives from unrelated errors.
_SUNSET_PHRASES = (
    "service has been discontinued",
    "sunset",
    "replaced by antigravity",
    "gemini code assist for individuals",
    "transitioning gemini cli to antigravity",
)

# Compound check for oauth_sunset: require a combination of context words when
# using short keywords to avoid mis-classifying "model no longer supported" etc.
def _is_oauth_sunset(text: str) -> bool:
    """Return True only for specific Gemini OAuth termination indicators."""
    if "service has been discontinued" in text:
        return True
    if "sunset" in text and ("google" in text or "gemini" in text or "oauth" in text):
        return True
    if "replaced by antigravity" in text:
        return True
    if "gemini code assist for individuals" in text:
        return True
    if "transitioning gemini cli to antigravity" in text:
        return True
    if "google login" in text and "no longer" in text:
        return True
    if "oauth" in text and ("terminated" in text or "sunset" in text):
        return True
    return False


# Specific IneligibleTierError phrases.
def _is_ineligible_tier(text: str) -> bool:
    """Return True only for specific account-tier ineligibility indicators."""
    if "ineligibletiererror" in text:
        return True
    if "not eligible for this tier" in text:
        return True
    if "account does not qualify for" in text:
        return True
    return False


def check_auth() -> dict[str, Any]:
    """Run a short smoke prompt to verify Gemini CLI authentication.

    Authentication itself is a human responsibility (pre-requisite).
    This check only verifies that cached credentials are working.

    auth.status values (fixed enum — see AUTH_STATUS_VALUES):
        authenticated         — OAuth login active and working; api_key_present field
                                indicates whether GEMINI_API_KEY is also set in the env
        authenticated_api_key — Reserved for future use when API key use can be confirmed
        oauth_sunset          — Google OAuth login via Gemini CLI has been discontinued
        ineligible_tier       — Account does not qualify for required Gemini tier
        unauthenticated       — Not logged in (gemini auth login required)
        auth_failed           — Unknown auth failure
        timeout               — Smoke prompt timed out
        gemini_not_found      — gemini CLI not installed

    Security: GEMINI_API_KEY existence is detected but its value is NEVER logged,
    printed, or included in any output field (detail / recovery / result JSON).
    Raw CLI output is redacted via _redact_api_key() before being placed in detail.

    Model override: set GEMINI_SETUP_CHECK_MODEL env var to override smoke model
    (default: gemini-2.0-flash).
    """
    # Detect GEMINI_API_KEY presence only — never expose the value.
    has_api_key = bool(os.environ.get("GEMINI_API_KEY"))

    # Allow model override for smoke prompt.
    model = os.environ.get("GEMINI_SETUP_CHECK_MODEL", "gemini-2.0-flash")

    try:
        result = _run(
            ["gemini", "--prompt", "ok", "--model", model],
            timeout=SMOKE_PROMPT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "status": "timeout",
            "detail": "gemini --prompt 'ok' timed out.",
            "recovery": _RECOVERY_APIKEY_AND_AGY + [
                "Check network connectivity and gemini CLI version.",
            ],
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "status": "gemini_not_found",
            "detail": "gemini CLI not found.",
            "recovery": ["npm install -g @google/gemini-cli"],
        }

    if result.returncode == 0:
        # Success: always return 'authenticated'. api_key_present is informational only.
        # 'authenticated_api_key' is reserved for future use when CLI confirms the key
        # was the active auth provider (presence alone is not sufficient evidence).
        return {
            "ok": True,
            "status": "authenticated",
            "api_key_present": has_api_key,
        }

    combined = (result.stdout + result.stderr).lower()

    # Detect Google OAuth sunset / service termination for Gemini CLI.
    if _is_oauth_sunset(combined):
        return {
            "ok": False,
            "status": "oauth_sunset",
            "detail": "Google OAuth login via Gemini CLI has been discontinued.",
            "recovery": _RECOVERY_APIKEY_AND_AGY,
        }

    # IneligibleTierError — account does not qualify for required Gemini tier.
    if _is_ineligible_tier(combined):
        return {
            "ok": False,
            "status": "ineligible_tier",
            "detail": "Account is not eligible for the required Gemini tier.",
            "recovery": _RECOVERY_APIKEY_AND_AGY,
        }

    # Generic auth / login required.
    _AUTH_KEYWORDS = ("auth", "login", "credential", "sign in", "not logged")
    if any(kw in combined for kw in _AUTH_KEYWORDS):
        # Redact API key from raw CLI output before including in detail.
        raw_detail = (result.stderr or result.stdout)[:300]
        return {
            "ok": False,
            "status": "unauthenticated",
            "detail": _redact_api_key(raw_detail),
            "recovery": [
                "Run: gemini auth login",
                "Authentication is a human pre-requisite; setup_check cannot automate OAuth login.",
            ],
        }

    # Unknown auth failure.
    return {
        "ok": False,
        "status": "auth_failed",
        "returncode": result.returncode,
        "recovery": _RECOVERY_APIKEY_AND_AGY + [
            "Or run: gemini auth login",
            "Check: gemini --version",
        ],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _run_gemini_provider_checks(repo_root: Path | None = None, fix: bool = False) -> dict[str, Any]:
    root = repo_root or _git_repo_root()
    tools_result = check_tools(GEMINI_REQUIRED_TOOLS)
    trusted_result = check_trusted_folders(root, fix=fix)
    serena_result = check_serena_mcp()
    settings_result = check_gemini_settings(root, fix=fix)
    auth_result = check_auth()

    all_ok = all(
        r["ok"]
        for r in [tools_result, trusted_result, serena_result, settings_result, auth_result]
    )

    # Determine exit code.
    # 0 = all ok; 1 = recoverable dependency issue; 2 = env error
    if all_ok:
        exit_code = 0
    elif any(
        r.get("status") in ("error", "write_error")
        for r in [trusted_result, settings_result, serena_result]
    ):
        exit_code = 2
    else:
        exit_code = 1

    return {
        "ok": all_ok,
        "exit_code": exit_code,
        "provider": "gemini",
        "selected_provider": "gemini",
        "tools": tools_result,
        "trusted_folders": trusted_result,
        "serena_mcp": serena_result,
        "gemini_settings": settings_result,
        "auth": auth_result,
    }


def _run_agy_provider_checks(repo_root: Path | None = None, fix: bool = False) -> dict[str, Any]:
    _ = repo_root or _git_repo_root()
    tools_result = check_tools(AGY_REQUIRED_TOOLS)
    agy_preflight = check_agy_preflight() if tools_result["ok"] else {
        "schema": "agy_preflight_result/v1",
        "ok": False,
        "failure_reason": "agy preflight skipped because required agy tools are missing",
        "failure_class": "cli_missing",
        "recovery_action": "install missing agy prerequisites first",
    }
    unsupported_fix = bool(fix)
    ok = tools_result["ok"] and agy_preflight["ok"] and not unsupported_fix
    exit_code = 0 if ok else 1
    warnings: list[str] = []
    if unsupported_fix:
        warnings.append("unsupported_provider_option: provider=agy does not support --fix")

    return {
        "ok": ok,
        "exit_code": exit_code,
        "provider": "agy",
        "selected_provider": "agy",
        "tools": tools_result,
        "agy_preflight": agy_preflight,
        "skipped_gemini_checks": [
            "trusted_folders",
            "serena_mcp",
            "gemini_settings",
            "auth",
            "node",
            "gemini",
            "uvx",
        ],
        "warnings": warnings,
        "unsupported_provider_option": unsupported_fix,
    }


def _run_auto_provider_checks(repo_root: Path | None = None, fix: bool = False) -> dict[str, Any]:
    provider_attempts: list[dict[str, Any]] = []
    agy_result = _run_agy_provider_checks(repo_root=repo_root, fix=fix)
    provider_attempts.append({
        "provider": "agy",
        "ok": agy_result["ok"],
        "exit_code": agy_result["exit_code"],
    })
    if agy_result["ok"]:
        agy_result["provider"] = "auto"
        agy_result["selected_provider"] = "agy"
        agy_result["provider_attempts"] = provider_attempts
        return agy_result

    gemini_result = _run_gemini_provider_checks(repo_root=repo_root, fix=fix)
    provider_attempts.append({
        "provider": "gemini",
        "ok": gemini_result["ok"],
        "exit_code": gemini_result["exit_code"],
    })
    gemini_result["provider"] = "auto"
    gemini_result["selected_provider"] = "gemini"
    gemini_result["provider_attempts"] = provider_attempts
    gemini_result["warnings"] = list(gemini_result.get("warnings", [])) + [
        f"agy_attempt_failed: {agy_result.get('agy_preflight', {}).get('failure_class') or 'tool_missing'}"
    ]
    return gemini_result


def run_all_checks(
    repo_root: Path | None = None,
    fix: bool = False,
    provider: str = "gemini",
) -> dict[str, Any]:
    """Execute provider-aware checks and return a consolidated result dict."""
    if provider not in SUPPORTED_PROVIDERS:
        return {
            "ok": False,
            "exit_code": 1,
            "provider": provider,
            "selected_provider": None,
            "failure_class": "unsupported_provider_option",
            "failure_reason": f"provider must be one of {sorted(SUPPORTED_PROVIDERS)}",
        }
    if provider == "agy":
        return _run_agy_provider_checks(repo_root=repo_root, fix=fix)
    if provider == "auto":
        return _run_auto_provider_checks(repo_root=repo_root, fix=fix)
    return _run_gemini_provider_checks(repo_root=repo_root, fix=fix)


def _human_readable(result: dict[str, Any]) -> str:
    lines: list[str] = []
    overall = "PASS" if result["ok"] else "FAIL"
    lines.append(
        f"setup_check: {overall} (provider={result.get('provider')} selected_provider={result.get('selected_provider')} exit_code={result['exit_code']})"
    )
    lines.append("")

    # Tools
    tools = result["tools"]
    lines.append("[tools]")
    for tool, ver in tools["versions"].items():
        status = ver if ver else "MISSING"
        lines.append(f"  {tool}: {status}")
    if not tools["ok"]:
        for hint in tools.get("recovery", []):
            lines.append(f"  recovery: {hint}")

    lines.append("")

    if "provider_attempts" in result:
        lines.append("[provider_attempts]")
        for attempt in result["provider_attempts"]:
            lines.append(
                f"  {attempt['provider']}: ok={attempt['ok']} exit_code={attempt['exit_code']}"
            )
        lines.append("")

    if result.get("selected_provider") == "agy":
        preflight = result["agy_preflight"]
        lines.append(f"[agy_preflight] ok={preflight['ok']} failure_class={preflight.get('failure_class')}")
        for skipped in result.get("skipped_gemini_checks", []):
            lines.append(f"  skipped: {skipped}")
        for warning in result.get("warnings", []):
            lines.append(f"  warning: {warning}")
        return "\n".join(lines)

    # Trusted folders
    tf = result["trusted_folders"]
    lines.append(f"[trusted_folders] ok={tf['ok']} status={tf.get('status', '?')}")
    if "path" in tf:
        lines.append(f"  path: {tf['path']}")
    if not tf["ok"]:
        for hint in tf.get("recovery", []):
            lines.append(f"  recovery: {hint}")

    lines.append("")

    # Serena MCP
    sm = result["serena_mcp"]
    lines.append(f"[serena_mcp] ok={sm['ok']} status={sm.get('status', '?')}")
    if not sm["ok"]:
        for hint in sm.get("recovery", []):
            lines.append(f"  recovery: {hint}")

    lines.append("")

    # Settings
    gs = result["gemini_settings"]
    lines.append(f"[gemini_settings] ok={gs['ok']} status={gs.get('status', '?')}")
    if "path" in gs:
        lines.append(f"  path: {gs['path']}")
    if not gs["ok"]:
        for hint in gs.get("recovery", []):
            lines.append(f"  recovery: {hint}")

    lines.append("")

    # Auth
    auth = result["auth"]
    lines.append(f"[auth] ok={auth['ok']} status={auth.get('status', '?')}")
    if not auth["ok"]:
        for hint in auth.get("recovery", []):
            lines.append(f"  recovery: {hint}")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check runtime prerequisites for gemini-cli-headless-delegation."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON (machine-readable).",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        dest="fix",
        default=False,
        help=(
            "Allow side-effects: register repo root in ~/.gemini/trustedFolders.json "
            "and create .gemini/settings.json template when absent. "
            "Without --fix, this script is check-only (no HOME or repo mutations)."
        ),
    )
    parser.add_argument(
        "--provider",
        default="gemini",
        help="Select provider: gemini, agy, or auto.",
    )
    args = parser.parse_args()

    try:
        result = run_all_checks(fix=args.fix, provider=args.provider)
    except Exception as exc:  # pylint: disable=broad-except
        err = {"ok": False, "exit_code": 2, "error": str(exc)}
        if args.json_output:
            print(json.dumps(err, ensure_ascii=False, indent=2))
        else:
            print(f"setup_check: ERROR — {exc}", file=sys.stderr)
        sys.exit(2)

    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_human_readable(result))

    sys.exit(result["exit_code"])


if __name__ == "__main__":
    main()
