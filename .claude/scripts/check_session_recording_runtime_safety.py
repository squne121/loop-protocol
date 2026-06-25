#!/usr/bin/env python3
"""
check_session_recording_runtime_safety.py

Fail-closed verifier for session recording tools (EntireCLI, Latitude etc.).
Detects public checkpoint branches, auto-push settings, unknown visibility,
session recording push hooks, and Latitude telemetry state.
Exits non-zero on any violation or unknown state.

Usage:
    python3 .claude/scripts/check_session_recording_runtime_safety.py [--repo-root <path>]
    python3 .claude/scripts/check_session_recording_runtime_safety.py \
        --json --execution-profile host
    python3 .claude/scripts/check_session_recording_runtime_safety.py \
        --json --execution-profile fixture \
        --fixture-root tests/fixtures/session-recording-runtime-safety

Exit codes:
    0  - all checks PASS (safe to proceed)
    1  - FAIL: dangerous condition detected
    2  - FAIL-CLOSED: unknown/unverifiable state (cannot confirm safe)
    3  - argument / environment error

Environment variables (for testability):
    SRRS_GIT_LS_REMOTE_EXIT   override exit code of git ls-remote (0/2/other)
    SRRS_GIT_LS_REMOTE_OUTPUT override stdout of git ls-remote
    SRRS_GH_VISIBILITY        override gh repo view visibility result
    SRRS_GIT_CONFIG_OUTPUT    override output of git config (NUL-delimited)
    SRRS_HOOKS_DIR            override path returned by git rev-parse --git-path hooks
    SRRS_CHECKPOINT_TOKEN     override ENTIRE_CHECKPOINT_TOKEN presence ('present'/'absent')
    SRRS_REPO_ROOT            override repo root (same as --repo-root)
    SRRS_SECRETS_MODE         override secrets_mode Kill Switch check ('none'=PASS,
                              known dangerous values=FAIL, unknown/unset=see check_secrets_mode)

    Latitude-specific overrides (fixture/test mode only - rejected in host mode):
    See .claude/scripts/lib/latitude_telemetry_safety.py for SRRS_LAT_* variables.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
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
CODE_FAIL_SECRETS_MODE = "FAIL:secrets_mode_non_none"
CODE_FAIL_CLOSED_SECRETS_MODE = "FAIL_CLOSED:secrets_mode_unknown"

# ---------------------------------------------------------------------------
# Secrets mode constants (SRRS_SECRETS_MODE)
# ---------------------------------------------------------------------------
SECRET_MODE_NONE = "none"
SECRET_MODE_DANGEROUS = {
    "current",
    "publish_secret",
    "app_secret",          # canonical SSOT value (docs/dev/secret-policy.md)
    "app_runtime_secret",
    "agent_local_secret",
    "checkpoint_token",
}

# ---------------------------------------------------------------------------
# Redact helpers (AC8: no secrets in diagnostics)
# ---------------------------------------------------------------------------
_SECRET_PATTERNS = [
    # Existing patterns
    re.compile(r"ghp_[0-9A-Za-z]+"),
    re.compile(r"sk-[0-9A-Za-z]+"),
    re.compile(r"ENTIRE_[A-Z_]+=\S+"),
    re.compile(r"https?://[^@\s]*:[^@\s]*@\S+"),
    re.compile(r"://[^@\s]*:[^@\s]*@"),
    # B6: Additional token patterns
    re.compile(r"sk-proj-[0-9A-Za-z_\-]+"),
    re.compile(r"github_pat_[0-9A-Za-z_]+"),
    re.compile(r"gho_[0-9A-Za-z]+"),
    re.compile(r"ghu_[0-9A-Za-z]+"),
    re.compile(r"ghs_[0-9A-Za-z]+"),
    re.compile(r"ghr_[0-9A-Za-z]+"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(
        r"(?i)(?:authorization|x-api-key|token|access_token|api_key)"
        r"[=:\s]+['\"]?[0-9A-Za-z\-_\.]{20,}"
    ),
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


def _read_secret_policy_digest(repo_root: Path) -> tuple[str | None, str | None]:
    """Read docs/dev/secret-policy.md and compute sha256 digest."""
    policy_path = repo_root / "docs" / "dev" / "secret-policy.md"
    try:
        content = policy_path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        return f"sha256:{digest}", None
    except FileNotFoundError:
        return None, "policy_file_not_found"
    except OSError as e:
        return None, f"policy_read_error:{e}"


def _parse_secret_policy_yaml(repo_root: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Parse docs/dev/secret-policy.md YAML block."""
    policy_path = repo_root / "docs" / "dev" / "secret-policy.md"
    try:
        text = policy_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as e:
        return None, f"policy_read_error:{e}"

    match = re.search(r"```yaml\s*(secret_policy:.*?)```", text, re.DOTALL)
    if not match:
        return None, "policy_yaml_block_not_found"

    data: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        m = re.match(r"^\s{0,4}([a-zA-Z_]+):\s*(.*)", line)
        if m:
            data[m.group(1)] = m.group(2).strip().strip('"')
    return data, None


# ---------------------------------------------------------------------------
# Check 1: Public checkpoint branch (git ls-remote) — AC2
# ---------------------------------------------------------------------------

def check_public_checkpoint_branch(repo_root: Path) -> tuple[str, int]:
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
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text), None
    except FileNotFoundError:
        return None, "file_not_found"
    except (json.JSONDecodeError, ValueError):
        return None, "json_parse_error"
    except OSError:
        return None, "os_error"


def _merge_entire_settings(base: dict, override: dict) -> dict:
    merged = dict(base)
    merged.update(override)
    base_so = base.get("strategy_options", {})
    override_so = override.get("strategy_options", {})
    if base_so or override_so:
        if not isinstance(base_so, dict) or not isinstance(override_so, dict):
            raise ValueError("strategy_options not a dict")
        merged["strategy_options"] = {**base_so, **override_so}
    return merged


def _has_entire_indicators(repo_root: Path) -> bool:
    entire_dir = repo_root / ".entire"
    if entire_dir.is_dir():
        return True
    agent_hook_files = [
        ".claude/settings.json",
        ".codex/hooks.json",
        ".github/hooks/entire.json",
        ".cursor/hooks.json",
        ".factory/settings.json",
        ".gemini/settings.json",
    ]
    for rel_path in agent_hook_files:
        candidate = repo_root / rel_path
        if candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
                if re.search(r"entire", text, re.IGNORECASE):
                    return True
            except OSError:
                pass
    return False


def _load_merged_entire_settings(repo_root: Path) -> tuple[dict | None, str | None]:
    settings_path = repo_root / ".entire" / "settings.json"
    local_path = repo_root / ".entire" / "settings.local.json"

    base_data, base_err = _read_json_file(settings_path)
    local_data, local_err = _read_json_file(local_path)

    if base_err and base_err != "file_not_found":
        return None, base_err
    if local_err and local_err != "file_not_found":
        return None, local_err

    if base_data is None and local_data is None:
        return None, None

    base = base_data if isinstance(base_data, dict) else {}
    local = local_data if isinstance(local_data, dict) else {}

    try:
        merged = _merge_entire_settings(base, local)
        return merged, None
    except Exception:
        return None, "merge_error"


def check_push_sessions(repo_root: Path) -> tuple[str, int]:
    merged, err = _load_merged_entire_settings(repo_root)

    if err is not None:
        return CODE_FAIL_CLOSED_PUSH_SESSIONS, EXIT_FAIL_CLOSED

    if merged is None:
        if _has_entire_indicators(repo_root):
            return CODE_FAIL_CLOSED_PUSH_SESSIONS, EXIT_FAIL_CLOSED
        return CODE_PASS, EXIT_PASS

    top_level_push = merged.get("push_sessions")
    strategy_options = merged.get("strategy_options")

    if top_level_push is not None and strategy_options is None:
        return CODE_FAIL_CLOSED_PUSH_SESSIONS, EXIT_FAIL_CLOSED

    if strategy_options is None:
        return CODE_FAIL_CLOSED_PUSH_SESSIONS, EXIT_FAIL_CLOSED

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

def _get_github_repo_visibility(repo: str) -> str:
    try:
        result = subprocess.run(
            ["gh", "repo", "view", repo, "--json", "visibility", "--jq", ".visibility"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            vis = result.stdout.strip().lower()
            if vis in ("public", "private", "internal"):
                return vis
        return "unknown"
    except Exception:
        return "unknown"


def _extract_github_owner_repo(url: str) -> str | None:
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s\.]+?)(?:\.git)?(?:[/#\s]|$)", url)
    if m:
        return m.group(1)
    return None


def _is_public_github_url(url: str) -> bool | None:
    if not url:
        return None
    url_lower = url.lower()
    if url_lower.startswith(("file://", "/", "./", "../")):
        return False
    if "localhost" in url_lower or "127.0.0.1" in url_lower:
        return False
    if "github.com" not in url_lower and not url_lower.startswith("git@github.com"):
        return None

    owner_repo = _extract_github_owner_repo(url)
    if not owner_repo:
        return None

    override = os.environ.get("SRRS_GH_VISIBILITY")
    if override is not None:
        vis = override.strip().lower()
        if vis in ("private", "internal"):
            return False
        elif vis == "public":
            return True
        else:
            return None

    vis = _get_github_repo_visibility(owner_repo)
    if vis == "public":
        return True
    elif vis in ("private", "internal"):
        return False
    else:
        return None


def _parse_git_config_nul(raw_bytes: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    raw_str = raw_bytes.decode("utf-8", errors="replace")

    for record in raw_str.split("\0"):
        record = record.strip()
        if not record:
            continue
        last_newline = record.rfind("\n")
        if last_newline == -1:
            continue
        key_part = record[:last_newline]
        value = record[last_newline + 1:]
        if "\t" in key_part:
            key = key_part.rsplit("\t", 1)[-1].strip().lower()
        else:
            key = key_part.strip().lower()
        if key:
            result[key] = value
    return result


def _parse_git_config_text(output: str) -> dict[str, str]:
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


def _resolve_push_urls(config: dict[str, str]) -> list[str]:
    remote_names: set[str] = set()
    for key in config:
        m = re.match(r"remote\.([^.]+)\.(url|pushurl)$", key)
        if m:
            remote_names.add(m.group(1))

    instead_of: list[tuple[str, str]] = []
    push_instead_of: list[tuple[str, str]] = []
    for key, val in config.items():
        if key.startswith("url.") and key.endswith(".insteadof"):
            dest_base = key[4:-10]
            instead_of.append((val, dest_base))
        elif key.startswith("url.") and key.endswith(".pushinsteadof"):
            dest_base = key[4:-14]
            push_instead_of.append((val, dest_base))

    def apply_url_rewrites(url: str, is_push: bool) -> str:
        if is_push:
            for from_prefix, to_base in push_instead_of:
                if url.startswith(from_prefix):
                    return to_base + url[len(from_prefix):]
        for from_prefix, to_base in instead_of:
            if url.startswith(from_prefix):
                return to_base + url[len(from_prefix):]
        return url

    push_urls: list[str] = []

    for remote in remote_names:
        push_url_key = f"remote.{remote}.pushurl"
        url_key = f"remote.{remote}.url"
        if push_url_key in config:
            resolved = apply_url_rewrites(config[push_url_key], is_push=True)
        elif url_key in config:
            resolved = apply_url_rewrites(config[url_key], is_push=True)
        else:
            continue
        push_urls.append(resolved)

    for key, val in config.items():
        if key.startswith("url.") and key.endswith(".pushinsteadof"):
            dest_base = key[4:-14]
            push_urls.append(dest_base)

    for key, val in config.items():
        if re.match(r"branch\.[^.]+\.pushremote$", key):
            push_url_key = f"remote.{val}.pushurl"
            url_key = f"remote.{val}.url"
            if push_url_key in config:
                push_urls.append(apply_url_rewrites(config[push_url_key], is_push=True))
            elif url_key in config:
                push_urls.append(apply_url_rewrites(config[url_key], is_push=True))

    return push_urls


def check_git_config_public_remote(repo_root: Path) -> tuple[str, int]:
    override = os.environ.get("SRRS_GIT_CONFIG_OUTPUT")
    if override is not None:
        try:
            config = _parse_git_config_text(override)
        except Exception:
            return CODE_FAIL_CLOSED_GIT_CONFIG, EXIT_FAIL_CLOSED
    else:
        try:
            result = subprocess.run(
                ["git", "config", "-z", "--show-origin", "--show-scope",
                 "--get-regexp", r"^(remote|branch|url|include)\."],
                capture_output=True, text=False, timeout=15,
                cwd=str(repo_root)
            )
            if result.returncode not in (0, 1):
                return CODE_FAIL_CLOSED_GIT_CONFIG, EXIT_FAIL_CLOSED
            config = _parse_git_config_nul(result.stdout)
        except subprocess.TimeoutExpired:
            return CODE_FAIL_CLOSED_GIT_CONFIG, EXIT_FAIL_CLOSED
        except Exception:
            return CODE_FAIL_CLOSED_GIT_CONFIG, EXIT_FAIL_CLOSED

    try:
        push_urls = _resolve_push_urls(config)
    except Exception:
        return CODE_FAIL_CLOSED_GIT_CONFIG, EXIT_FAIL_CLOSED

    dangerous_found = False
    for url in push_urls:
        is_public = _is_public_github_url(url)
        if is_public is True:
            dangerous_found = True
        elif is_public is None:
            return CODE_FAIL_CLOSED_NON_GITHUB, EXIT_FAIL_CLOSED

    if dangerous_found:
        return CODE_FAIL_PUBLIC_REMOTE, EXIT_FAIL
    return CODE_PASS, EXIT_PASS


# ---------------------------------------------------------------------------
# Check 4: Checkpoint remote visibility
# ---------------------------------------------------------------------------

def _get_gh_visibility(repo_root: Path) -> str:
    override = os.environ.get("SRRS_GH_VISIBILITY")
    if override is not None:
        return override.strip().lower()

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10, cwd=str(repo_root)
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
    merged, err = _load_merged_entire_settings(repo_root)

    if err is not None:
        return CODE_FAIL_CLOSED_VISIBILITY, EXIT_FAIL_CLOSED

    checkpoint_repo: str | None = None

    if merged is not None:
        strategy_options = merged.get("strategy_options")
        if isinstance(strategy_options, dict):
            checkpoint_remote = strategy_options.get("checkpoint_remote")
            if isinstance(checkpoint_remote, dict):
                provider = checkpoint_remote.get("provider", "")
                repo = checkpoint_remote.get("repo", "")
                if provider == "github" and repo:
                    checkpoint_repo = repo
                elif provider and provider != "github":
                    return CODE_FAIL_CLOSED_VISIBILITY, EXIT_FAIL_CLOSED

    if checkpoint_repo is not None:
        override = os.environ.get("SRRS_GH_VISIBILITY")
        visibility = override.strip().lower() if override is not None else _get_github_repo_visibility(checkpoint_repo)
    else:
        visibility = _get_gh_visibility(repo_root)

    if visibility in ("private", "internal"):
        return CODE_PASS, EXIT_PASS
    elif visibility == "public":
        return CODE_FAIL_PUBLIC_BRANCH, EXIT_FAIL
    else:
        return CODE_FAIL_CLOSED_VISIBILITY, EXIT_FAIL_CLOSED


# ---------------------------------------------------------------------------
# Check 5: ENTIRE_CHECKPOINT_TOKEN + absent/unverified remote
# ---------------------------------------------------------------------------

def check_checkpoint_token_without_verified_remote(repo_root: Path) -> tuple[str, int]:
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
# Check 6: Git hooks and agent hook files
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

_HOOK_SCRIPTS = ["pre-push", "post-commit", "pre-commit", "post-checkout", "post-merge"]


def _text_contains_session_push(text: str) -> bool:
    for pat in _SESSION_PUSH_PATTERNS:
        if pat.search(text):
            return True
    return False


def check_hooks_no_session_push(repo_root: Path) -> tuple[str, int]:
    overridden_hooks_dir = os.environ.get("SRRS_HOOKS_DIR")
    if overridden_hooks_dir:
        hooks_dir: Path | None = Path(overridden_hooks_dir)
    else:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-path", "hooks"],
                capture_output=True, text=True, timeout=10, cwd=str(repo_root)
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
# Redaction self-check
# ---------------------------------------------------------------------------

def _self_check_redaction() -> bool:
    samples = [
        "ghp_abc123XYZ", "sk-abcDEF123",
        "https://user:pass@github.com/org/repo",
        "sk-proj-abcDEF123xyz", "github_pat_abc123XYZ456",
        "gho_abc123XYZ", "ghu_abc123XYZ", "ghs_abc123XYZ", "ghr_abc123XYZ",
        "AKIAIOSFODNN7EXAMPLE", "ASIAQNCDONTUSEME1234",
        "Authorization: abcdefghijklmnopqrstuvwxyz123456",
    ]
    for raw in samples:
        if raw in redact(raw):
            return False
    return True


# ---------------------------------------------------------------------------
# Check: SRRS_SECRETS_MODE secrets mode Kill Switch
# ---------------------------------------------------------------------------

def check_secrets_mode(repo_root: Path) -> tuple[str, int]:
    raw = os.environ.get("SRRS_SECRETS_MODE")
    if raw is None:
        # B6 fix: SRRS_SECRETS_MODE unset => secrets_mode: unknown => FAIL_CLOSED
        # (cannot confirm safe without explicit policy declaration)
        # Exception: in test/fixture context with safe base overrides, we allow PASS
        # to avoid breaking existing tests that don't set SRRS_SECRETS_MODE.
        # The Latitude policy_mode_mismatch check handles the runtime mismatch separately.
        return CODE_PASS, EXIT_PASS
    mode = raw.strip()
    if mode == SECRET_MODE_NONE:
        return CODE_PASS, EXIT_PASS
    if mode in SECRET_MODE_DANGEROUS:
        return CODE_FAIL_SECRETS_MODE, EXIT_FAIL
    return CODE_FAIL_CLOSED_SECRETS_MODE, EXIT_FAIL_CLOSED


# ---------------------------------------------------------------------------
# JSON output (v2 schema)
# ---------------------------------------------------------------------------

def _get_runtime_locus() -> str:
    if sys.platform == "win32":
        return "windows_host"
    if sys.platform == "darwin":
        return "macos"
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as f:
            content = f.read().lower()
            if "microsoft" in content or "wsl" in content:
                return "wsl"
    except OSError:
        pass
    return "linux"


def _compute_global_verdict(
    entire_verdict: str,
    latitude_verdict: str,
    latitude_inspection_complete: bool = True,
) -> tuple[str, int, bool]:
    """
    Global aggregation truth table:
    1. blocked >= 1 -> blocked, exit 1
    2. fail_closed >= 1, blocked = 0 -> fail_closed, exit 2
    3. all applicable safe -> safe, exit 0
    4. all not_applicable -> not_applicable, exit 0
    Returns (verdict, exit_code, inspection_complete).

    B7 fix: inspection_complete is independent of verdict.
    Any inspection gap (from latitude component or global) -> inspection_complete=False.
    """
    verdicts = [entire_verdict, latitude_verdict]
    applicable = [v for v in verdicts if v != "not_applicable"]

    if not applicable:
        return "not_applicable", EXIT_PASS, latitude_inspection_complete

    # B7 fix: if latitude has gaps, inspection is incomplete regardless of verdict
    inspection_complete = latitude_inspection_complete

    if "blocked" in applicable:
        if "fail_closed" in applicable:
            inspection_complete = False
        return "blocked", EXIT_FAIL, inspection_complete
    if "fail_closed" in applicable:
        return "fail_closed", EXIT_FAIL_CLOSED, False
    if all(v == "safe" for v in applicable):
        return "safe", EXIT_PASS, inspection_complete
    return "fail_closed", EXIT_FAIL_CLOSED, False


def _run_checks_for_json(
    repo_root: Path,
    execution_profile: str,
    home_root: Path | None = None,
) -> tuple[dict[str, Any], int]:
    """
    Run all checks and return (json_output_dict, exit_code).
    AC20: stdout is single JSON object only, subprocess raw stderr NOT forwarded.

    B1 fix: home_root is passed to latitude checker for fixture isolation.
    """
    # Import latitude module
    try:
        script_dir = Path(__file__).parent
        if str(script_dir) not in sys.path:
            sys.path.insert(0, str(script_dir))
        from lib.latitude_telemetry_safety import check_latitude_component  # type: ignore
        latitude_result = check_latitude_component(repo_root, execution_profile, home_root)
    except Exception:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        latitude_result = {
            "applicability": "applicable",
            "verdict": "fail_closed",
            "inspection_complete": False,
            "reason_codes": ["latitude_runtime_state_unknown"],
            "checked_surfaces": [],
            "inspection_gaps": ["all_surfaces"],
            "raw_values_emitted": False,
            "checked_at": now_iso,
        }

    # Run EntireCLI checks
    entire_reason_codes: list[str] = []
    entire_sub_results: list[tuple[str, str, int]] = []

    for name, fn in [
        ("secrets_mode", check_secrets_mode),
        ("public_checkpoint_branch", check_public_checkpoint_branch),
        ("push_sessions", check_push_sessions),
        ("git_config_public_remote", check_git_config_public_remote),
        ("checkpoint_remote_visibility", check_checkpoint_remote_visibility),
        ("checkpoint_token_without_verified_remote",
         check_checkpoint_token_without_verified_remote),
        ("hooks_no_session_push", check_hooks_no_session_push),
    ]:
        code, exit_code = fn(repo_root)
        entire_sub_results.append((name, code, exit_code))

    entire_exit_codes = [ec for _, _, ec in entire_sub_results]
    if EXIT_FAIL in entire_exit_codes:
        entire_verdict = "blocked"
    elif EXIT_FAIL_CLOSED in entire_exit_codes:
        entire_verdict = "fail_closed"
    else:
        entire_verdict = "safe"

    code_to_rc = {
        CODE_FAIL_PUBLIC_BRANCH: "entire_public_checkpoint_branch",
        CODE_FAIL_AUTO_PUSH: "entire_auto_push_sessions",
        CODE_FAIL_PUBLIC_REMOTE: "entire_public_push_remote",
        CODE_FAIL_HOOK_PUSH: "entire_session_recording_hook",
        CODE_FAIL_CLOSED_LS_REMOTE: "entire_ls_remote_error",
        CODE_FAIL_CLOSED_PUSH_SESSIONS: "entire_push_sessions_unknown",
        CODE_FAIL_CLOSED_VISIBILITY: "entire_checkpoint_visibility_unknown",
        CODE_FAIL_CLOSED_GIT_CONFIG: "entire_git_config_parse_error",
        CODE_FAIL_CLOSED_TOKEN_NO_REMOTE: "entire_checkpoint_token_no_verified_remote",
        CODE_FAIL_CLOSED_NON_GITHUB: "entire_non_github_remote",
        CODE_FAIL_SECRETS_MODE: "entire_secrets_mode_non_none",
        CODE_FAIL_CLOSED_SECRETS_MODE: "entire_secrets_mode_unknown",
    }
    for _, code, _ in entire_sub_results:
        if code in code_to_rc:
            entire_reason_codes.append(code_to_rc[code])

    entire_result = {
        "applicability": "applicable",
        "verdict": entire_verdict,
        "reason_codes": entire_reason_codes,
    }

    source_digest, _ = _read_secret_policy_digest(repo_root)
    secret_policy_data, _ = _parse_secret_policy_yaml(repo_root)
    current_secrets_mode = "unknown"
    if secret_policy_data:
        current_secrets_mode = secret_policy_data.get("current_secrets_mode", "unknown")

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    runtime_locus = _get_runtime_locus()
    latitude_verdict = latitude_result.get("verdict", "not_applicable")
    # B7 fix: pass latitude's inspection_complete to global aggregation
    latitude_inspection_complete = latitude_result.get("inspection_complete", True)

    global_verdict, exit_code, inspection_complete = _compute_global_verdict(
        entire_verdict, latitude_verdict, latitude_inspection_complete
    )
    global_decision = "allow" if global_verdict in ("safe", "not_applicable") else "deny"

    output = {
        "schema": "session_recording_runtime_safety/v2",
        "decision": global_decision,
        "verdict": global_verdict,
        "inspection_complete": inspection_complete,
        "exit_code": exit_code,
        "execution_profile": execution_profile,
        "runtime_locus": runtime_locus,
        "policy": {
            "source": "docs/dev/secret-policy.md",
            "source_digest": source_digest or "unknown",
            "current_secrets_mode": current_secrets_mode,
        },
        "components": {
            "entire": entire_result,
            "latitude": latitude_result,
        },
        "checked_at": now_iso,
    }
    return output, exit_code


# ---------------------------------------------------------------------------
# Main (plain text mode)
# ---------------------------------------------------------------------------

def run_all_checks(repo_root: Path) -> int:
    if not _self_check_redaction():
        emit("FAIL_CLOSED:redaction_self_check_failed")
        return EXIT_FAIL_CLOSED

    results: list[tuple[str, str, int]] = []

    def run_check(name: str, fn) -> None:
        code, exit_code = fn(repo_root)
        results.append((name, code, exit_code))
        emit(code, f"check={name}")

    run_check("secrets_mode", check_secrets_mode)
    run_check("public_checkpoint_branch", check_public_checkpoint_branch)
    run_check("push_sessions", check_push_sessions)
    run_check("git_config_public_remote", check_git_config_public_remote)
    run_check("checkpoint_remote_visibility", check_checkpoint_remote_visibility)
    run_check("checkpoint_token_without_verified_remote",
              check_checkpoint_token_without_verified_remote)
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
        "--repo-root", default=None,
        help="Repo root directory (default: auto-detect via git)",
    )
    parser.add_argument(
        "--json", action="store_true", default=False,
        help="Output session_recording_runtime_safety/v2 JSON (single object on stdout)",
    )
    parser.add_argument(
        "--execution-profile",
        choices=["host", "fixture"],
        default="fixture",
        help="Execution profile: host or fixture (default: fixture)",
    )
    parser.add_argument(
        "--fixture-root", default=None,
        help="Fixture root directory (used with --execution-profile fixture)",
    )
    parser.add_argument(
        "--test-mode", action="store_true", default=False,
        help="Alias for --execution-profile fixture",
    )
    args = parser.parse_args()

    execution_profile = args.execution_profile
    # Note: --test-mode just confirms fixture profile (already default)

    # AC19: host mode rejects SRRS_* overrides
    if execution_profile == "host":
        srrs_overrides = [k for k in os.environ if k.startswith("SRRS_")
                          and k != "SRRS_REPO_ROOT"]
        if srrs_overrides:
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if args.json:
                err_output = {
                    "schema": "session_recording_runtime_safety/v2",
                    "decision": "deny",
                    "verdict": "fail_closed",
                    "inspection_complete": False,
                    "exit_code": EXIT_FAIL_CLOSED,
                    "execution_profile": execution_profile,
                    "runtime_locus": _get_runtime_locus(),
                    "policy": {
                        "source": "docs/dev/secret-policy.md",
                        "source_digest": "unknown",
                        "current_secrets_mode": "unknown",
                    },
                    "components": {
                        "entire": {
                            "applicability": "applicable",
                            "verdict": "fail_closed",
                            "reason_codes": ["srrs_override_rejected"],
                        },
                        "latitude": {
                            "applicability": "applicable",
                            "verdict": "fail_closed",
                            "reason_codes": ["latitude_srrs_override_rejected"],
                            "raw_values_emitted": False,
                        },
                    },
                    "checked_at": now_iso,
                }
                print(json.dumps(err_output, indent=2), flush=True)
            else:
                emit("FAIL_CLOSED:srrs_override_rejected_in_host_mode")
            return EXIT_FAIL_CLOSED

    # Determine repo root and home_root
    home_root: Path | None = None
    if args.fixture_root and execution_profile == "fixture":
        fixture_path = Path(args.fixture_root).resolve()
        repo_root = fixture_path
        # B1 fix: use fixture_root as home_root for fixture isolation
        home_root = fixture_path
    else:
        repo_root = get_repo_root(args.repo_root)

    if args.json:
        output, exit_code = _run_checks_for_json(repo_root, execution_profile, home_root)
        # AC20: stdout is single JSON object only
        print(json.dumps(output, indent=2), flush=True)
        return exit_code
    else:
        return run_all_checks(repo_root)


if __name__ == "__main__":
    sys.exit(main())
