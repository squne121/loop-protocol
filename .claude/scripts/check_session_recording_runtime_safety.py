#!/usr/bin/env python3
"""
check_session_recording_runtime_safety.py

Fail-closed verifier for session recording tools (EntireCLI etc.).
Detects public checkpoint branches, auto-push settings, unknown visibility,
and session recording push hooks. Exits non-zero on any violation or unknown state.

Usage:
    python3 .claude/scripts/check_session_recording_runtime_safety.py [--repo-root <path>]

Exit codes:
    0  - all checks PASS (safe to proceed)
    1  - FAIL: dangerous condition detected
    2  - FAIL-CLOSED: unknown/unverifiable state (cannot confirm safe)
    3  - argument / environment error

Environment variables (for testability):
    SRRS_GIT_LS_REMOTE_EXIT   override exit code of git ls-remote (0/2/other)
    SRRS_GIT_LS_REMOTE_OUTPUT override stdout of git ls-remote
    SRRS_GH_VISIBILITY        override gh repo view visibility result
    SRRS_GIT_CONFIG_OUTPUT    override output of git config --show-origin
    SRRS_HOOKS_DIR            override path returned by git rev-parse --git-path hooks
    SRRS_CHECKPOINT_TOKEN     override ENTIRE_CHECKPOINT_TOKEN presence ('present'/'absent')
    SRRS_REPO_ROOT            override repo root (same as --repo-root)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_FAIL_CLOSED = 2
EXIT_ARG_ERROR = 3

# ---------------------------------------------------------------------------
# Diagnostic codes (machine-readable, no secrets)
# ---------------------------------------------------------------------------
CODE_PASS = "PASS"
CODE_FAIL_PUBLIC_BRANCH = "FAIL:public_checkpoint_branch_present"
CODE_FAIL_AUTO_PUSH = "FAIL:auto_push_sessions_enabled"
CODE_FAIL_PUBLIC_REMOTE = "FAIL:public_push_remote_detected"
CODE_FAIL_HOOK_PUSH = "FAIL:session_recording_hook_present"
CODE_FAIL_CLOSED_LS_REMOTE = "FAIL_CLOSED:ls_remote_error"
CODE_FAIL_CLOSED_PUSH_SESSIONS = "FAIL_CLOSED:push_sessions_unknown"
CODE_FAIL_CLOSED_VISIBILITY = "FAIL_CLOSED:checkpoint_remote_visibility_unknown"
CODE_FAIL_CLOSED_GIT_CONFIG = "FAIL_CLOSED:git_config_parse_error"
CODE_FAIL_CLOSED_TOKEN_NO_REMOTE = "FAIL_CLOSED:checkpoint_token_present_no_verified_remote"
CODE_FAIL_CLOSED_NON_GITHUB = "FAIL_CLOSED:non_github_remote_not_in_allowlist"

# ---------------------------------------------------------------------------
# Redact helpers (AC8: no secrets in diagnostics)
# ---------------------------------------------------------------------------
_SECRET_PATTERNS = [
    re.compile(r"ghp_[0-9A-Za-z]+"),
    re.compile(r"sk-[0-9A-Za-z]+"),
    re.compile(r"ENTIRE_[A-Z_]+=\S+"),
    re.compile(r"https?://[^@\s]*:[^@\s]*@\S+"),
    re.compile(r"://[^@\s]*:[^@\s]*@"),
]
_ABS_PATH_POSIX = re.compile(r"/(?:home|Users|root|tmp|var|opt|usr)/\S+")
_ABS_PATH_WINDOWS = re.compile(r"[A-Z]:\\[^\s]+")


def redact(text: str) -> str:
    """Remove secrets and absolute paths from diagnostic output."""
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    text = _ABS_PATH_POSIX.sub("[ABS_PATH]", text)
    text = _ABS_PATH_WINDOWS.sub("[ABS_PATH]", text)
    return text


def emit(code: str, detail: str = "") -> None:
    """Print a machine-readable diagnostic line."""
    safe_detail = redact(detail)
    if safe_detail:
        print(f"{code} | {safe_detail}", flush=True)
    else:
        print(code, flush=True)


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def get_repo_root(cli_root: str | None) -> Path:
    """Determine repo root from CLI arg, env var, or git."""
    env_root = os.environ.get("SRRS_REPO_ROOT")
    if cli_root:
        return Path(cli_root).resolve()
    if env_root:
        return Path(env_root).resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return Path(result.stdout.strip()).resolve()
    except Exception:
        pass
    return Path.cwd()


# ---------------------------------------------------------------------------
# Check 1: Public checkpoint branch (git ls-remote)
# AC2
# ---------------------------------------------------------------------------

def check_public_checkpoint_branch(repo_root: Path) -> tuple[str, int]:
    """
    exit 0 from ls-remote => branch exists => FAIL
    exit 2 => branch absent => PASS (for this check)
    other non-zero => FAIL-CLOSED
    """
    override_exit = os.environ.get("SRRS_GIT_LS_REMOTE_EXIT")
    if override_exit is not None:
        rc = int(override_exit)
    else:
        try:
            result = subprocess.run(
                ["git", "ls-remote", "--exit-code", "--refs", "origin",
                 "refs/heads/entire/checkpoints/v1"],
                capture_output=True, text=True, timeout=30,
                cwd=str(repo_root)
            )
            rc = result.returncode
        except subprocess.TimeoutExpired:
            return CODE_FAIL_CLOSED_LS_REMOTE, EXIT_FAIL_CLOSED
        except Exception:
            return CODE_FAIL_CLOSED_LS_REMOTE, EXIT_FAIL_CLOSED

    if rc == 0:
        return CODE_FAIL_PUBLIC_BRANCH, EXIT_FAIL
    elif rc == 2:
        return CODE_PASS, EXIT_PASS
    else:
        return CODE_FAIL_CLOSED_LS_REMOTE, EXIT_FAIL_CLOSED


# ---------------------------------------------------------------------------
# Check 2: push_sessions setting (AC3, AC18, AC19)
# ---------------------------------------------------------------------------

def _read_json_file(path: Path) -> tuple[Any, str | None]:
    """Return (parsed_data, error_message). error_message is None on success."""
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text), None
    except FileNotFoundError:
        return None, "file_not_found"
    except (json.JSONDecodeError, ValueError) as exc:
        return None, f"json_parse_error"
    except OSError as exc:
        return None, f"os_error"


def _merge_entire_settings(base: dict, override: dict) -> dict:
    """Shallow-merge override on top of base, deep-merge strategy_options."""
    merged = dict(base)
    merged.update(override)
    base_so = base.get("strategy_options", {})
    override_so = override.get("strategy_options", {})
    if base_so or override_so:
        if not isinstance(base_so, dict) or not isinstance(override_so, dict):
            raise ValueError("strategy_options not a dict")
        merged["strategy_options"] = {**base_so, **override_so}
    return merged


def check_push_sessions(repo_root: Path) -> tuple[str, int]:
    """
    Evaluate strategy_options.push_sessions from merged entire settings.
    true => FAIL, unknown/parse error => FAIL-CLOSED, false => PASS
    """
    settings_path = repo_root / ".entire" / "settings.json"
    local_path = repo_root / ".entire" / "settings.local.json"

    base_data, base_err = _read_json_file(settings_path)
    local_data, local_err = _read_json_file(local_path)

    if base_err and base_err != "file_not_found":
        return CODE_FAIL_CLOSED_PUSH_SESSIONS, EXIT_FAIL_CLOSED
    if local_err and local_err != "file_not_found":
        return CODE_FAIL_CLOSED_PUSH_SESSIONS, EXIT_FAIL_CLOSED

    base = base_data if isinstance(base_data, dict) else {}
    local = local_data if isinstance(local_data, dict) else {}

    # Neither file exists => not configured => PASS
    if base_data is None and local_data is None:
        return CODE_PASS, EXIT_PASS

    try:
        merged = _merge_entire_settings(base, local)
    except Exception:
        return CODE_FAIL_CLOSED_PUSH_SESSIONS, EXIT_FAIL_CLOSED

    strategy_options = merged.get("strategy_options")

    # AC19: top-level push_sessions key without nested strategy_options => FAIL-CLOSED
    top_level_push = merged.get("push_sessions")
    if top_level_push is not None and strategy_options is None:
        return CODE_FAIL_CLOSED_PUSH_SESSIONS, EXIT_FAIL_CLOSED

    if strategy_options is None:
        return CODE_PASS, EXIT_PASS

    if not isinstance(strategy_options, dict):
        return CODE_FAIL_CLOSED_PUSH_SESSIONS, EXIT_FAIL_CLOSED

    push_sessions = strategy_options.get("push_sessions")

    if push_sessions is True:
        return CODE_FAIL_AUTO_PUSH, EXIT_FAIL
    elif push_sessions is False:
        return CODE_PASS, EXIT_PASS
    else:
        return CODE_FAIL_CLOSED_PUSH_SESSIONS, EXIT_FAIL_CLOSED


# ---------------------------------------------------------------------------
# Check 3: Effective git config (public push remote) (AC4, AC20, AC21)
# ---------------------------------------------------------------------------

def _is_public_github_url(url: str) -> bool | None:
    """
    True if github.com URL (assume public).
    None if non-GitHub (unknown, FAIL-CLOSED).
    False if clearly local/private.
    """
    if not url:
        return None
    url_lower = url.lower()
    if url_lower.startswith(("file://", "/", "./", "../")):
        return False
    if "localhost" in url_lower or "127.0.0.1" in url_lower:
        return False
    if "github.com" in url_lower:
        return True
    return None


def _parse_git_config_output(output: str) -> dict[str, str]:
    """Parse output of git config --show-origin --show-scope --get-regexp."""
    result: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            key = parts[-2].strip().lower()
            val = parts[-1].strip()
            result[key] = val
        elif len(parts) == 2:
            key = parts[0].strip().lower()
            val = parts[1].strip()
            result[key] = val
    return result


def check_git_config_public_remote(repo_root: Path) -> tuple[str, int]:
    """
    Evaluate effective git config for public push remotes.
    FAIL if public push destination detected.
    FAIL-CLOSED if config cannot be parsed.
    """
    override = os.environ.get("SRRS_GIT_CONFIG_OUTPUT")
    if override is not None:
        config_output = override
    else:
        try:
            result = subprocess.run(
                ["git", "config", "--show-origin", "--show-scope",
                 "--get-regexp", r"^(remote|branch|url)\."],
                capture_output=True, text=True, timeout=15,
                cwd=str(repo_root)
            )
            if result.returncode not in (0, 1):
                return CODE_FAIL_CLOSED_GIT_CONFIG, EXIT_FAIL_CLOSED
            config_output = result.stdout
        except subprocess.TimeoutExpired:
            return CODE_FAIL_CLOSED_GIT_CONFIG, EXIT_FAIL_CLOSED
        except Exception:
            return CODE_FAIL_CLOSED_GIT_CONFIG, EXIT_FAIL_CLOSED

    try:
        config = _parse_git_config_output(config_output)
    except Exception:
        return CODE_FAIL_CLOSED_GIT_CONFIG, EXIT_FAIL_CLOSED

    dangerous_keys = set()

    for key, val in config.items():
        # remote.*.pushurl
        if re.match(r"remote\.[^.]+\.pushurl$", key):
            is_public = _is_public_github_url(val)
            if is_public is True:
                dangerous_keys.add(key)
            elif is_public is None:
                return CODE_FAIL_CLOSED_NON_GITHUB, EXIT_FAIL_CLOSED

        # remote.*.url (used for push when no pushurl set)
        elif re.match(r"remote\.[^.]+\.url$", key):
            remote_name = key.split(".")[1]
            pushurl_key = f"remote.{remote_name}.pushurl"
            if pushurl_key not in config:
                is_public = _is_public_github_url(val)
                if is_public is True:
                    dangerous_keys.add(key)
                elif is_public is None:
                    return CODE_FAIL_CLOSED_NON_GITHUB, EXIT_FAIL_CLOSED

        # url.*.pushInsteadOf — key is "url.<dest-url>.pushinsteadof"
        # The dest URL can contain dots, so we strip prefix/suffix rather than use [^.]+
        elif key.startswith("url.") and key.endswith(".pushinsteadof"):
            # Extract dest URL: strip "url." prefix and ".pushinsteadof" suffix
            dest_url = key[len("url."):-len(".pushinsteadof")]
            is_public = _is_public_github_url(dest_url)
            if is_public is True:
                dangerous_keys.add(key)
            elif is_public is None:
                return CODE_FAIL_CLOSED_NON_GITHUB, EXIT_FAIL_CLOSED

    if dangerous_keys:
        return CODE_FAIL_PUBLIC_REMOTE, EXIT_FAIL

    return CODE_PASS, EXIT_PASS


# ---------------------------------------------------------------------------
# Check 4: Checkpoint remote visibility (AC5, AC6, AC12, AC13, AC15, AC16)
# ---------------------------------------------------------------------------

def _get_gh_visibility(repo_root: Path) -> str:
    """Returns 'public', 'private', 'internal', or 'unknown'."""
    override = os.environ.get("SRRS_GH_VISIBILITY")
    if override is not None:
        return override.strip().lower()

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
            cwd=str(repo_root)
        )
        if result.returncode != 0:
            return "unknown"
        remote_url = result.stdout.strip()
        m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", remote_url)
        if not m:
            return "unknown"
        owner_repo = m.group(1)
        gh_result = subprocess.run(
            ["gh", "repo", "view", owner_repo, "--json", "visibility", "--jq", ".visibility"],
            capture_output=True, text=True, timeout=30
        )
        if gh_result.returncode != 0:
            return "unknown"
        vis = gh_result.stdout.strip().lower()
        if vis in ("public", "private", "internal"):
            return vis
        return "unknown"
    except subprocess.TimeoutExpired:
        return "unknown"
    except Exception:
        return "unknown"


def check_checkpoint_remote_visibility(repo_root: Path) -> tuple[str, int]:
    """
    private/internal => PASS (private_verified)
    public => FAIL
    unknown/error/non-GitHub => FAIL-CLOSED
    """
    visibility = _get_gh_visibility(repo_root)

    if visibility in ("private", "internal"):
        return CODE_PASS, EXIT_PASS
    elif visibility == "public":
        return CODE_FAIL_PUBLIC_BRANCH, EXIT_FAIL
    else:
        return CODE_FAIL_CLOSED_VISIBILITY, EXIT_FAIL_CLOSED


# ---------------------------------------------------------------------------
# Check 5: ENTIRE_CHECKPOINT_TOKEN + absent/unverified remote (AC14, AC15, AC23)
# ---------------------------------------------------------------------------

def check_checkpoint_token_without_verified_remote(repo_root: Path) -> tuple[str, int]:
    """
    If ENTIRE_CHECKPOINT_TOKEN present and remote not confirmed private => FAIL-CLOSED.
    """
    override = os.environ.get("SRRS_CHECKPOINT_TOKEN")
    if override is not None:
        token_present = override.strip().lower() == "present"
    else:
        token_present = bool(os.environ.get("ENTIRE_CHECKPOINT_TOKEN"))

    if not token_present:
        return CODE_PASS, EXIT_PASS

    visibility = _get_gh_visibility(repo_root)
    if visibility in ("private", "internal"):
        return CODE_PASS, EXIT_PASS
    else:
        return CODE_FAIL_CLOSED_TOKEN_NO_REMOTE, EXIT_FAIL_CLOSED


# ---------------------------------------------------------------------------
# Check 6: Git hooks and agent hook files (AC7, AC24)
# ---------------------------------------------------------------------------

_SESSION_PUSH_PATTERNS = [
    re.compile(r"entire.*push", re.IGNORECASE),
    re.compile(r"push.*entire", re.IGNORECASE),
    re.compile(r"git\s+push.*checkpoint", re.IGNORECASE),
    re.compile(r"checkpoint.*push", re.IGNORECASE),
    re.compile(r"session.*push", re.IGNORECASE),
    re.compile(r"push.*session", re.IGNORECASE),
    re.compile(r"entire/checkpoints", re.IGNORECASE),
]

_AGENT_HOOK_FILES = [
    ".claude/settings.json",
    ".codex/hooks.json",
    ".github/hooks/entire.json",
    ".cursor/hooks.json",
    ".factory/settings.json",
    ".gemini/settings.json",
    ".opencode/plugins/entire.ts",
    ".pi/extensions/entire/index.ts",
]

_HOOK_SCRIPTS = [
    "pre-push",
    "post-commit",
    "pre-commit",
    "post-checkout",
    "post-merge",
]


def _text_contains_session_push(text: str) -> bool:
    for pat in _SESSION_PUSH_PATTERNS:
        if pat.search(text):
            return True
    return False


def check_hooks_no_session_push(repo_root: Path) -> tuple[str, int]:
    """
    Scan git hooks and agent hook files for session recording push commands.
    """
    overridden_hooks_dir = os.environ.get("SRRS_HOOKS_DIR")
    if overridden_hooks_dir:
        hooks_dir: Path | None = Path(overridden_hooks_dir)
    else:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-path", "hooks"],
                capture_output=True, text=True, timeout=10,
                cwd=str(repo_root)
            )
            if result.returncode != 0:
                hooks_dir = None
            else:
                hooks_dir = Path(result.stdout.strip())
                if not hooks_dir.is_absolute():
                    hooks_dir = (repo_root / hooks_dir).resolve()
        except Exception:
            hooks_dir = None

    violations: list[str] = []

    if hooks_dir and hooks_dir.is_dir():
        for hook_name in _HOOK_SCRIPTS:
            hook_path = hooks_dir / hook_name
            if hook_path.is_file():
                try:
                    text = hook_path.read_text(encoding="utf-8", errors="replace")
                    if _text_contains_session_push(text):
                        violations.append(f"hook:{hook_name}")
                except OSError:
                    pass

    for rel_path in _AGENT_HOOK_FILES:
        agent_path = repo_root / rel_path
        if agent_path.is_file():
            try:
                text = agent_path.read_text(encoding="utf-8", errors="replace")
                if _text_contains_session_push(text):
                    violations.append(f"agent_hook:{rel_path}")
            except OSError:
                pass

    if violations:
        return CODE_FAIL_HOOK_PUSH, EXIT_FAIL

    return CODE_PASS, EXIT_PASS


# ---------------------------------------------------------------------------
# Redaction self-check (AC25 defense)
# ---------------------------------------------------------------------------

def _self_check_redaction() -> bool:
    """Verify redact() strips known secret patterns. Returns True if safe."""
    samples = [
        "ghp_abc123XYZ",
        "sk-abcDEF123",
        "https://user:pass@github.com/org/repo",
    ]
    for raw in samples:
        if raw in redact(raw):
            return False
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_all_checks(repo_root: Path) -> int:
    """
    Run all checks. Return worst exit code:
    FAIL (1) or FAIL-CLOSED (2) beats PASS (0).
    """
    if not _self_check_redaction():
        emit("FAIL_CLOSED:redaction_self_check_failed")
        return EXIT_FAIL_CLOSED

    results: list[tuple[str, str, int]] = []

    def run_check(name: str, fn) -> None:
        code, exit_code = fn(repo_root)
        results.append((name, code, exit_code))
        emit(code, f"check={name}")

    run_check("public_checkpoint_branch", check_public_checkpoint_branch)
    run_check("push_sessions", check_push_sessions)
    run_check("git_config_public_remote", check_git_config_public_remote)
    run_check("checkpoint_remote_visibility", check_checkpoint_remote_visibility)
    run_check("checkpoint_token_without_verified_remote", check_checkpoint_token_without_verified_remote)
    run_check("hooks_no_session_push", check_hooks_no_session_push)

    exit_codes = [r[2] for r in results]
    if EXIT_FAIL in exit_codes:
        return EXIT_FAIL
    if EXIT_FAIL_CLOSED in exit_codes:
        return EXIT_FAIL_CLOSED
    return EXIT_PASS


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed verifier for session recording runtime safety."
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repo root directory (default: auto-detect via git)",
    )
    args = parser.parse_args()

    repo_root = get_repo_root(args.repo_root)
    return run_all_checks(repo_root)


if __name__ == "__main__":
    sys.exit(main())
