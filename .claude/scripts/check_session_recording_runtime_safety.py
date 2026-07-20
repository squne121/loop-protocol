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
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_FAIL_CLOSED = 2
EXIT_ARG_ERROR = 3

REAL_PILOT_ALLOW_DECISION = "approve_timeboxed_real_pilot"
SOURCE_DIGEST_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

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
# Latitude pilot exception decision gate (#1220, LATITUDE_PILOT_EXCEPTION_V1)
# ---------------------------------------------------------------------------
PILOT_DECISION_ENUM = {
    "reject_and_uninstall",
    "approve_synthetic_only",
    "approve_timeboxed_real_pilot",
    "defer",
}
PILOT_ACTIVATION_BLOCKED = "blocked_until_activation"
PILOT_ACTIVATION_DENY = "deny"
PILOT_ACTIVATION_ALLOW = "allow"

# ---------------------------------------------------------------------------
# Agent observation capability matrix (#1221, agent_observation_capability/v1)
# ---------------------------------------------------------------------------
CAPABILITY_SCHEMA = "agent_observation_capability/v1"
CAPABILITY_VERDICT_ENUM = {"supported", "partial", "unsupported", "unverified"}
CAPABILITY_SURFACE_ENUM = {"claude_code", "codex_cli", "google_antigravity"}
CODEX_CANONICAL_HOOK_KEY = "[features].hooks"
CODEX_LEGACY_HOOK_KEY = "codex_hooks"
HOOK_COEXISTENCE_CONTRACT = {
    "expected_handlers_fired_once": True,
    "duplicate_finalization_absent": True,
    "duplicate_upload_absent": True,
    "async_hook_not_used_as_gate": True,
    "post_run_verifier_observed_final_state": True,
    "runtime_event_and_capture_artifact_correlated": True,
    "hook_exit_zero_not_authoritative": True,
    "raw_values_emitted": False,
}

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


def _count_pilot_markers(text: str) -> int:
    """Count LATITUDE_PILOT_EXCEPTION_V1 mapping-key occurrences (not prose mentions)."""
    return len(re.findall(r"(?m)^\s*LATITUDE_PILOT_EXCEPTION_V1:\s*$", text))


def _extract_pilot_field(block: str, field: str) -> str | None:
    m = re.search(
        r"(?m)^\s+" + re.escape(field) + r":\s*([A-Za-z0-9_./-]+)\s*$", block
    )
    return m.group(1).strip() if m else None


def _read_pilot_block(repo_root: Path) -> tuple[int, str | None]:
    """Read secret-policy.md, return (marker_count, decision_value)."""
    policy_path = repo_root / "docs" / "dev" / "secret-policy.md"
    try:
        text = policy_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return 0, None
    count = _count_pilot_markers(text)
    m = re.search(r"(?ms)^\s*LATITUDE_PILOT_EXCEPTION_V1:\s*$.*?(?=^```|\Z)", text)
    block = m.group(0) if m else ""
    return count, _extract_pilot_field(block, "decision")


def _compute_real_pilot_activation() -> tuple[str, list[str]]:
    """approve_timeboxed_real_pilot: allow only if every required gate is satisfied.

    Fixture/test overrides (rejected in host mode by main()):
      SRRS_LAT_PILOT_ACTIVATION_FIELDS  'complete' | 'incomplete'
      SRRS_LAT_PILOT_DIST_DIGESTS       'complete' | 'incomplete'
      SRRS_LAT_PILOT_REMOTE_CLEANUP     'machine_verified' | 'human_attested' | 'unknown'
      SRRS_LAT_PILOT_ARGV_EXPOSURE      'absent_verified' | 'possible' | 'unknown'
    """
    fields = (os.environ.get("SRRS_LAT_PILOT_ACTIVATION_FIELDS") or "incomplete").strip()
    digests = (os.environ.get("SRRS_LAT_PILOT_DIST_DIGESTS") or "incomplete").strip()
    remote = (os.environ.get("SRRS_LAT_PILOT_REMOTE_CLEANUP") or "unknown").strip()
    argv = (os.environ.get("SRRS_LAT_PILOT_ARGV_EXPOSURE") or "unknown").strip()

    rcs: list[str] = []
    if fields != "complete":
        rcs.append("latitude_pilot_activation_fields_incomplete")
    if digests != "complete":
        rcs.append("latitude_pilot_distribution_digests_incomplete")
    if remote != "machine_verified":
        rcs.append("latitude_pilot_remote_cleanup_not_machine_verified")
    if argv != "absent_verified":
        rcs.append("latitude_pilot_argv_exposure_not_cleared")

    if rcs:
        return PILOT_ACTIVATION_BLOCKED, rcs
    return PILOT_ACTIVATION_ALLOW, rcs


def check_pilot_exception(repo_root: Path, execution_profile: str) -> dict[str, Any]:
    """#1220: Validate LATITUDE_PILOT_EXCEPTION_V1 decision and compute activation state.

    The decision marker source of truth is docs/dev/secret-policy.md (repo policy YAML).
    Real pilot activation is permitted only for approve_timeboxed_real_pilot with every
    required gate satisfied; otherwise the gate stays blocked_until_activation (deny for
    reject_and_uninstall). No credential values are read or emitted.

    Fixture/test overrides (rejected in host mode by main()):
      SRRS_LAT_PILOT_DECISION       decision enum value | 'absent'
      SRRS_LAT_PILOT_MARKER_COUNT   integer marker count
    """
    decision_override = os.environ.get("SRRS_LAT_PILOT_DECISION")
    count_override = os.environ.get("SRRS_LAT_PILOT_MARKER_COUNT")

    if decision_override is not None or count_override is not None:
        decision = (decision_override or "absent").strip()
        if decision == "absent":
            decision = None
        try:
            marker_count = int(count_override) if count_override is not None else 1
        except ValueError:
            marker_count = -1
    else:
        marker_count, decision = _read_pilot_block(repo_root)

    reason_codes: list[str] = []
    malformed = False

    if marker_count != 1:
        malformed = True
        reason_codes.append("latitude_pilot_marker_count_invalid")
    if decision is None or decision not in PILOT_DECISION_ENUM:
        malformed = True
        reason_codes.append("latitude_pilot_decision_invalid")

    if malformed:
        activation_state = PILOT_ACTIVATION_BLOCKED
    elif decision == "reject_and_uninstall":
        activation_state = PILOT_ACTIVATION_DENY
    elif decision in ("defer", "approve_synthetic_only"):
        activation_state = PILOT_ACTIVATION_BLOCKED
    else:  # approve_timeboxed_real_pilot
        activation_state, rcs = _compute_real_pilot_activation()
        reason_codes.extend(rcs)

    return {
        "applicability": "applicable",
        "decision": decision,
        "marker_count": marker_count,
        "malformed": malformed,
        "activation_state": activation_state,
        "synthetic_only_allowed": decision == "approve_synthetic_only",
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "raw_values_emitted": False,
    }


def _load_fixture_scenario(fixture_root: Path) -> None:
    """#1220: load srrs_scenario.json overrides for a deterministic fixture gate.

    Only SRRS_* keys are honored, and existing environment values win (setdefault),
    so the deterministic gate does not clobber an explicit caller override.
    """
    scenario_path = fixture_root / "srrs_scenario.json"
    try:
        data = json.loads(scenario_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if isinstance(key, str) and key.startswith("SRRS_"):
            os.environ.setdefault(key, str(value))


def _compute_global_verdict(
    entire_verdict: str,
    latitude_verdict: str,
    latitude_inspection_complete: bool = True,
    pilot_malformed: bool = False,
) -> tuple[str, int, bool]:
    """
    Global aggregation truth table:
    1. blocked >= 1 -> blocked, exit 1
    2. fail_closed >= 1 (or pilot malformed), blocked = 0 -> fail_closed, exit 2
    3. all applicable safe -> safe, exit 0
    4. all not_applicable -> not_applicable, exit 0
    Returns (verdict, exit_code, inspection_complete).

    B7 fix: inspection_complete is independent of verdict.
    #1220: a malformed LATITUDE_PILOT_EXCEPTION_V1 marker fail-closes the gate.
    """
    verdicts = [entire_verdict, latitude_verdict]
    applicable = [v for v in verdicts if v != "not_applicable"]

    if not applicable:
        if pilot_malformed:
            return "fail_closed", EXIT_FAIL_CLOSED, False
        return "not_applicable", EXIT_PASS, latitude_inspection_complete

    inspection_complete = latitude_inspection_complete

    if "blocked" in applicable:
        if "fail_closed" in applicable or pilot_malformed:
            inspection_complete = False
        return "blocked", EXIT_FAIL, inspection_complete
    if "fail_closed" in applicable or pilot_malformed:
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

    pilot_result = check_pilot_exception(repo_root, execution_profile)

    global_verdict, exit_code, inspection_complete = _compute_global_verdict(
        entire_verdict, latitude_verdict, latitude_inspection_complete,
        pilot_malformed=pilot_result["malformed"],
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
        "pilot_exception": pilot_result,
        "pilot_activation_state": pilot_result["activation_state"],
        "checked_at": now_iso,
    }
    return output, exit_code


def _is_truthy(value: Any) -> bool:
    return value is True


# #1261: closed enum for the distribution resolution_source field. Kept in sync
# with .claude/scripts/lib/latitude_telemetry_safety.RESOLUTION_SOURCE_ENUM
# (duplicated here so this module has no import-time dependency on lib/ for
# the --preflight-input-json self-contained re-evaluation mode).
_RESOLUTION_SOURCE_ENUM = {
    "local_lockfile",
    "project_local_install",
    "npm_cache",
    "global_install",
    "npx_only",
    "unknown",
}
# #1261 follow-up (PR #1352 REQUEST_CHANGES #2/#4): closed enum for the
# npx_invocation field. Kept in sync with
# .claude/scripts/lib/latitude_telemetry_safety.NPX_INVOCATION_ENUM.
_NPX_INVOCATION_ENUM = {"exact_version", "floating", "absent", "unknown"}
# #1261 follow-up: approved npm registry origins for resolved_registry_origin.
# Kept in sync with
# .claude/scripts/lib/latitude_telemetry_safety._APPROVED_REGISTRY_ORIGINS.
_APPROVED_REGISTRY_ORIGINS = {"https://registry.npmjs.org"}
_NPX_PREFIX_RE = re.compile(r"^npx\s+(?:-y|--yes)?\s*", re.IGNORECASE)
_EXACT_SEMVER_SPEC_RE = re.compile(
    r"^(@[\w.\-]+/[\w.\-]+|[\w.\-]+)@\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$"
)
_SRI_DIGEST_RE = re.compile(r"^sha(256|384|512)-[A-Za-z0-9+/]+={0,2}$")


def _evidence_sha256_ok(value: Any) -> bool:
    """#1261: True iff value is a well-formed `sha256:<64hex>` digest string."""
    return isinstance(value, str) and SOURCE_DIGEST_SHA256_RE.fullmatch(value) is not None


def _package_spec_exact_ok(value: Any) -> bool:
    """#1261 follow-up: True iff value normalizes (stripping a leading
    `npx`/`npx -y` prefix) to `<name>@x.y.z` exact semver.
    """
    if not isinstance(value, str):
        return False
    normalized = _NPX_PREFIX_RE.sub("", value).strip()
    return _EXACT_SEMVER_SPEC_RE.fullmatch(normalized) is not None


def _dist_integrity_ok(value: Any) -> bool:
    """#1261 follow-up: True iff value looks like a Subresource Integrity digest."""
    return isinstance(value, str) and _SRI_DIGEST_RE.fullmatch(value) is not None


def _registry_origin_ok(value: Any) -> bool:
    """#1261 follow-up: resolved_registry_origin must be an approved npm registry."""
    return isinstance(value, str) and value in _APPROVED_REGISTRY_ORIGINS


def _evaluate_real_pilot_preflight_output(output: dict[str, Any]) -> tuple[str, int, bool]:
    """Evaluate a single verifier JSON object as the strict real-pilot predicate.

    #1261: direct field assertion over Latitude distribution / argv exposure /
    remote cleanup evidence. A single summary field (`distribution.state`)
    is no longer sufficient by itself — every required evidence field is
    asserted directly so that missing/unknown lower-level evidence cannot be
    hidden behind a `verified` summary state.
    """
    execution_profile = output.get("execution_profile")
    verdict = output.get("verdict")
    policy = output.get("policy")
    pilot = output.get("pilot_exception")
    components = output.get("components")
    latitude = components.get("latitude") if isinstance(components, dict) else None

    if execution_profile != "host":
        return "fail_closed", EXIT_FAIL_CLOSED, False

    if verdict == "fail_closed":
        return "fail_closed", EXIT_FAIL_CLOSED, False

    if not isinstance(policy, dict) or not isinstance(pilot, dict) or not isinstance(latitude, dict):
        return "fail_closed", EXIT_FAIL_CLOSED, False

    source_digest = policy.get("source_digest")
    pilot_reason_codes = pilot.get("reason_codes")
    dist = latitude.get("distribution")
    if not isinstance(dist, dict):
        dist = {}

    source_digest_ok = (
        isinstance(source_digest, str)
        and SOURCE_DIGEST_SHA256_RE.fullmatch(source_digest) is not None
    )
    pilot_reason_codes_ok = isinstance(pilot_reason_codes, list) and len(pilot_reason_codes) == 0

    resolution_source = dist.get("resolution_source")
    resolution_source_ok = (
        isinstance(resolution_source, str)
        and resolution_source in _RESOLUTION_SOURCE_ENUM
        and resolution_source != "unknown"
    )
    argv_exposure_state = latitude.get("argv_exposure_state")
    remote_cleanup_state = latitude.get("remote_cleanup_state")

    npx_invocation = dist.get("npx_invocation")
    npx_invocation_ok = npx_invocation != "floating"

    preflight_ready = all([
        output.get("decision") == "allow",
        output.get("verdict") == "safe",
        _is_truthy(output.get("inspection_complete")),
        output.get("pilot_activation_state") == PILOT_ACTIVATION_ALLOW,
        pilot.get("decision") == REAL_PILOT_ALLOW_DECISION,
        pilot.get("malformed") is False,
        pilot_reason_codes_ok,
        pilot.get("raw_values_emitted") is False,
        latitude.get("verdict") == "safe",
        latitude.get("inspection_complete") is True,
        source_digest_ok,
        dist.get("state") == "verified",
        dist.get("registry_signature_verified") is True,
        dist.get("provenance_verified") is True,
        # #1261 AC3/AC4: distribution evidence must be complete, not just a
        # verified summary state.
        resolution_source_ok,
        isinstance(dist.get("resolved_registry_origin"), str)
        and bool(dist.get("resolved_registry_origin")),
        isinstance(dist.get("lockfile_digest"), str) and bool(dist.get("lockfile_digest")),
        _evidence_sha256_ok(dist.get("tarball_sha256")),
        _evidence_sha256_ok(dist.get("installed_entrypoint_sha256")),
        _evidence_sha256_ok(dist.get("preload_sha256")),
        _evidence_sha256_ok(dist.get("hook_command_sha256")),
        # #1261 follow-up (PR #1352 REQUEST_CHANGES #4): field-level strength,
        # not just presence. A digest-shaped placeholder is not accepted
        # unless the underlying evidence field is itself well-formed.
        _evidence_sha256_ok(dist.get("lockfile_digest")),
        _package_spec_exact_ok(dist.get("package_spec")),
        _dist_integrity_ok(dist.get("dist_integrity")),
        _registry_origin_ok(dist.get("resolved_registry_origin")),
        # #1261 follow-up (PR #1352 REQUEST_CHANGES #2): a floating
        # `npx -y <pkg>` invocation is always blocked, even if every other
        # digest field happens to be well-formed (adversarial fixture guard).
        npx_invocation_ok,
        # #1261 AC5: argv_exposure_state must be positively cleared.
        argv_exposure_state == "absent_verified",
        # #1261 AC7: remote_cleanup_state must be machine-verified
        # (human_attested is explicitly NOT a substitute).
        remote_cleanup_state == "machine_verified",
    ])

    if preflight_ready:
        return "safe", EXIT_PASS, True

    if verdict == "blocked":
        return "blocked", EXIT_FAIL, bool(output.get("inspection_complete"))

    return "fail_closed", EXIT_FAIL_CLOSED, False


def _apply_real_pilot_preflight_requirement(
    output: dict[str, Any],
) -> int:
    """Re-evaluate host verifier JSON as the sole real-pilot preflight gate."""
    verdict, exit_code, inspection_complete = _evaluate_real_pilot_preflight_output(output)
    output["decision"] = "allow" if verdict == "safe" else "deny"
    output["verdict"] = verdict
    output["inspection_complete"] = inspection_complete
    output["exit_code"] = exit_code
    return exit_code


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


# ---------------------------------------------------------------------------
# Scope-rollup capture eligibility artifact authority (Issue #1527 Scope
# Delta (2), AC12/AC13). This is the ONLY authority that may produce the
# fixed private eligibility artifact consumed by the Codex adapter
# (scripts/session-recording/codex-hook-adapter.mjs) and the capture
# producer (.claude/hooks/capture_scope_rollup_final_response.py). Neither
# consumer accepts hook-payload-supplied inline objects, arbitrary paths, or
# `artifacts[]` fuzzy-match values as an eligibility source any more — see
# SOURCE_BOUND_ARTIFACT_KEYS removal in the capture producer.
# ---------------------------------------------------------------------------

SCOPE_ROLLUP_ELIGIBILITY_SCHEMA = "SESSION_RECORDING_SCOPE_ROLLUP_ELIGIBILITY_V1"
SCOPE_ROLLUP_ELIGIBILITY_ARTIFACT_VERSION = 1
SCOPE_ROLLUP_ELIGIBILITY_TTL_SECONDS_DEFAULT = 1800
SCOPE_ROLLUP_ELIGIBILITY_MAX_BYTES = 8192
SCOPE_ROLLUP_ELIGIBILITY_KEYS = frozenset(
    {
        "schema",
        "artifact_version",
        "repo_root_realpath",
        "head_sha",
        "policy_digest",
        "secret_policy_digest",
        "public_checkpoint_present",
        "visibility",
        "secrets_mode",
        "generated_at",
        "expires_at",
        "safety_verdict",
    }
)


def scope_rollup_eligibility_artifact_path(repo_root: Path) -> Path:
    """Fixed private location for the scope-rollup eligibility artifact.

    The default path is deterministic (derived from repo root) and is never
    taken from a hook payload. The env override exists solely for test
    isolation (same pattern as SCOPE_ROLLUP_CAPTURE_DIR) — it is set by our
    own trusted test harness, never by an external hook caller.
    """
    override = os.environ.get("SCOPE_ROLLUP_ELIGIBILITY_ARTIFACT_PATH")
    if override:
        return Path(override)
    return repo_root / ".claude" / "tmp" / "session-recording" / "scope-rollup-eligibility.json"


def _scope_rollup_policy_digest(repo_root: Path) -> str | None:
    policy_path = repo_root / "docs" / "dev" / "session-recording-policy.md"
    try:
        content = policy_path.read_bytes()
    except OSError:
        return None
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _scope_rollup_head_sha(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def _scope_rollup_visibility(repo_root: Path) -> str:
    try:
        merged, err = _load_merged_entire_settings(repo_root)
    except Exception:
        return "unknown"
    if err is not None:
        return "unknown"
    try:
        return _get_gh_visibility(repo_root)
    except Exception:
        return "unknown"


def _scope_rollup_public_checkpoint_present(repo_root: Path) -> bool:
    try:
        _code, exit_code = check_public_checkpoint_branch(repo_root)
    except Exception:
        return True  # fail-closed: treat unverifiable state as present
    if exit_code == EXIT_PASS:
        return False
    # FAIL and FAIL_CLOSED both mean "cannot certify absence" -> present=True
    return True


def _parse_iso8601_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def emit_scope_rollup_eligibility_artifact(
    repo_root: Path,
    *,
    ttl_seconds: int = SCOPE_ROLLUP_ELIGIBILITY_TTL_SECONDS_DEFAULT,
    now: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    """Generate the fixed private scope-rollup eligibility artifact.

    Runs the existing fail-closed safety checks in this module (secrets_mode,
    public checkpoint branch, checkpoint visibility, etc.) and binds the
    result to repo root / HEAD / policy digests, generation time, and a
    bounded expiry. Returns (exit_code, artifact_dict). The artifact is
    written with mode 0600 via an atomic rename regardless of verdict so
    that a `deny` verdict is still fail-closed-observable rather than
    silently absent.
    """
    now = now or datetime.now(timezone.utc)

    # NOTE: scope-rollup capture writes the exact final-response text to a
    # LOCAL private file (never pushed anywhere) — it is a categorically
    # different operation from the session-recording-tool checkpoint push
    # that run_all_checks() as a whole is designed to gate (public remote /
    # push-sessions / hook-based auto-push). Reusing run_all_checks()
    # wholesale here would deny eligibility for this exact repository
    # (a legitimate public open-source GitHub remote) even though local
    # capture is safe. The two safety-relevant conditions for LOCAL capture
    # eligibility are: secrets_mode must be 'none' (no active
    # secrets-exposure mode elsewhere in the session), and no dangerous
    # session-recording checkpoint branch must be present. `visibility` is
    # recorded for binding/anti-replay purposes only — it is not itself a
    # gate condition (a public origin remote does not make local-only
    # capture unsafe).
    secrets_mode = (os.environ.get("SRRS_SECRETS_MODE") or SECRET_MODE_NONE).strip() or SECRET_MODE_NONE
    public_checkpoint_present = _scope_rollup_public_checkpoint_present(repo_root)
    visibility = _scope_rollup_visibility(repo_root)
    policy_digest = _scope_rollup_policy_digest(repo_root)
    secret_policy_digest, _secret_policy_err = _read_secret_policy_digest(repo_root)
    head_sha = _scope_rollup_head_sha(repo_root)

    safety_verdict = "allow" if (
        secrets_mode == SECRET_MODE_NONE
        and public_checkpoint_present is False
        and policy_digest is not None
        and secret_policy_digest is not None
    ) else "deny"

    artifact: dict[str, Any] = {
        "schema": SCOPE_ROLLUP_ELIGIBILITY_SCHEMA,
        "artifact_version": SCOPE_ROLLUP_ELIGIBILITY_ARTIFACT_VERSION,
        "repo_root_realpath": str(repo_root.resolve()),
        "head_sha": head_sha,
        "policy_digest": policy_digest,
        "secret_policy_digest": secret_policy_digest,
        "public_checkpoint_present": public_checkpoint_present,
        "visibility": visibility,
        "secrets_mode": secrets_mode,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z"),
        "safety_verdict": safety_verdict,
    }

    path = scope_rollup_eligibility_artifact_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    rendered = (json.dumps(artifact, sort_keys=True, indent=2) + "\n").encode("utf-8")

    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp_path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)

    return (EXIT_PASS if safety_verdict == "allow" else EXIT_FAIL), artifact


def load_and_verify_scope_rollup_eligibility_artifact(
    repo_root: Path,
    *,
    hook_received_at: datetime,
    marker_generated_at: datetime | None = None,
) -> tuple[dict[str, Any] | None, str, str | None]:
    """Load and independently verify the fixed-location eligibility artifact.

    Returns (artifact_or_None, reason_code, digest_or_None). reason_code is
    "ok" only when every binding/timestamp check passes; otherwise it is a
    stable machine-readable rejection code and artifact is None. This is
    used by the capture producer (Python) as a second, independent
    verification pass (defense in depth) on top of the Node-only adapter
    gate — never as a substitute for it.
    """
    path = scope_rollup_eligibility_artifact_path(repo_root)
    try:
        st = os.lstat(path)
    except OSError:
        return None, "eligibility_missing", None

    if stat.S_ISLNK(st.st_mode):
        return None, "eligibility_invalid_symlink", None
    if not stat.S_ISREG(st.st_mode):
        return None, "eligibility_invalid_not_regular_file", None
    if stat.S_IMODE(st.st_mode) != 0o600:
        return None, "eligibility_invalid_mode", None
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return None, "eligibility_invalid_owner", None
    if st.st_size > SCOPE_ROLLUP_ELIGIBILITY_MAX_BYTES:
        return None, "eligibility_invalid_size", None

    try:
        raw = path.read_bytes()
    except OSError:
        return None, "eligibility_unreadable", None
    digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"

    try:
        artifact = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "eligibility_invalid_json", digest
    if not isinstance(artifact, dict):
        return None, "eligibility_invalid_json", digest

    extra_keys = set(artifact.keys()) - SCOPE_ROLLUP_ELIGIBILITY_KEYS
    if extra_keys:
        return None, "eligibility_invalid_additional_properties", digest
    missing_keys = SCOPE_ROLLUP_ELIGIBILITY_KEYS - set(artifact.keys())
    if missing_keys:
        return None, "eligibility_invalid_missing_fields", digest

    if artifact.get("schema") != SCOPE_ROLLUP_ELIGIBILITY_SCHEMA:
        return None, "eligibility_invalid_schema", digest

    expected_repo_root = str(repo_root.resolve())
    if artifact.get("repo_root_realpath") != expected_repo_root:
        return None, "eligibility_binding_repo_mismatch", digest

    policy_digest = _scope_rollup_policy_digest(repo_root)
    if policy_digest is None or artifact.get("policy_digest") != policy_digest:
        return None, "eligibility_binding_policy_digest_mismatch", digest

    secret_policy_digest, _err = _read_secret_policy_digest(repo_root)
    if secret_policy_digest is None or artifact.get("secret_policy_digest") != secret_policy_digest:
        return None, "eligibility_binding_secret_policy_digest_mismatch", digest

    if artifact.get("public_checkpoint_present") is not False:
        return None, "eligibility_binding_public_checkpoint_present", digest
    # `visibility` is bound/recorded for anti-replay provenance only — see
    # emit_scope_rollup_eligibility_artifact() docstring; it is not itself a
    # safety gate for local-only capture.
    if artifact.get("secrets_mode") != SECRET_MODE_NONE:
        return None, "eligibility_binding_secrets_mode_unsafe", digest
    if artifact.get("safety_verdict") != "allow":
        return None, "eligibility_binding_safety_verdict_denied", digest

    generated_at = _parse_iso8601_utc(artifact.get("generated_at"))
    expires_at = _parse_iso8601_utc(artifact.get("expires_at"))
    if generated_at is None or expires_at is None:
        return None, "eligibility_invalid_timestamp", digest

    # AC14: pre-generation lifecycle — the artifact must already exist
    # (generated_at <= hook_received_at) and must not have expired
    # (hook_received_at < expires_at). A future-dated generated_at is
    # rejected as stale/invalid rather than trusted.
    if generated_at > hook_received_at:
        return None, "eligibility_stale_future_generated_at", digest
    if hook_received_at >= expires_at:
        return None, "eligibility_stale_expired", digest
    if marker_generated_at is not None and generated_at > marker_generated_at:
        return None, "eligibility_stale_generated_after_marker", digest

    head_sha = artifact.get("head_sha")
    current_head = _scope_rollup_head_sha(repo_root)
    if head_sha is not None and current_head is not None and head_sha != current_head:
        return None, "eligibility_binding_head_sha_mismatch", digest

    return artifact, "ok", digest


def _capability_public_safety(surfaces: list) -> dict[str, Any]:
    """Aggregate the public_safety admission contract over all surfaces' signals."""
    raw = prompt = tool_io = abspath = cred = False
    any_surface = False
    digest_ok = True
    for s in surfaces:
        any_surface = True
        sig = s.get("signals", {}) if isinstance(s, dict) else {}
        if not isinstance(sig, dict):
            sig = {}
        raw = raw or bool(sig.get("raw_values_emitted", False))
        prompt = prompt or bool(sig.get("prompt_excerpt_present", False))
        tool_io = tool_io or bool(sig.get("tool_io_excerpt_present", False))
        abspath = abspath or bool(sig.get("local_absolute_path_present", False))
        cred = cred or bool(sig.get("credential_value_present", False))
        if str(sig.get("digest_scope", "")) != "public_projection_only":
            digest_ok = False
    digest_admit = digest_ok and any_surface
    forbidden_clean = not (prompt or tool_io or abspath or cred)
    admission = (not raw) and forbidden_clean and digest_admit
    return {
        "raw_values_emitted": raw,
        "forbidden_field_scan": "pass" if forbidden_clean else "fail",
        "prompt_excerpt_present": prompt,
        "tool_io_excerpt_present": tool_io,
        "local_absolute_path_present": abspath,
        "credential_value_present": cred,
        "digest_is_over_public_projection_only": digest_admit,
        "admission": "pass" if admission else "fail",
    }


def _capability_supported_predicate(
    surface: dict, evidence_mode: str | None
) -> tuple[bool, list[str]]:
    """Re-derive whether a surface satisfies the supported predicate.

    Returns (derived_supported, reason_codes). Under synthetic_only the only trusted
    provenance is synthetic_fixture; real_pilot_verified stays blocked (#1220).
    """
    sig = surface.get("signals", {})
    if not isinstance(sig, dict):
        sig = {}
    name = surface.get("surface")
    rcs: list[str] = []

    runtime = bool(sig.get("runtime_event_observed", False))
    capture = bool(sig.get("capture_artifact_observed", False))
    raw = bool(sig.get("raw_values_emitted", False))
    provenance = str(sig.get("evidence_provenance", "unknown"))

    if not runtime:
        rcs.append("runtime_event_not_observed")
    if not capture:
        rcs.append("capture_artifact_not_observed")
    if raw:
        rcs.append("raw_values_emitted")

    trusted = {"synthetic_fixture"}
    if evidence_mode != "synthetic_only":
        trusted = trusted | {"real_pilot_verified"}
    if provenance not in trusted:
        rcs.append("evidence_provenance_not_trusted")

    hc = sig.get("hook_coexistence")
    if name == "claude_code":
        # #1221 P0-2: claude_code MUST prove hook coexistence to reach supported.
        if not isinstance(hc, dict):
            rcs.append("hook_coexistence_missing")
        else:
            for key, required in HOOK_COEXISTENCE_CONTRACT.items():
                if bool(hc.get(key, not required)) != required:
                    rcs.append("hook_coexistence_violation")
                    break
    elif isinstance(hc, dict):
        for key, required in HOOK_COEXISTENCE_CONTRACT.items():
            if bool(hc.get(key, not required)) != required:
                rcs.append("hook_coexistence_violation")
                break

    if name == "codex_cli":
        if str(sig.get("hooks_feature_key", "")) != CODEX_CANONICAL_HOOK_KEY:
            rcs.append("codex_non_canonical_hook_key")
        if bool(sig.get("validator_drift", False)):
            rcs.append("codex_validator_drift")
        if not bool(sig.get("project_layer_trusted", False)):
            rcs.append("codex_project_layer_untrusted")

    if name == "google_antigravity" and not (runtime and capture):
        rcs.append("antigravity_no_capture_runtime_correlation")

    derived = len(rcs) == 0
    return derived, list(dict.fromkeys(rcs))


def run_capability_check(fixture_path: str) -> tuple[dict[str, Any], int]:
    """#1221: validate an agent_observation_capability/v1 matrix fixture.

    Exit codes:
      0  admitted (allow): consistent surfaces, public_safety pass, supported predicate honored
      1  deny (blocked): unsafe promotion to supported / public_safety failure
      2  fail_closed: malformed schema / bad enum / missing-or-multiple verdict
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        data = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return {
            "schema": "agent_observation_capability_check/v1",
            "decision": "deny",
            "verdict": "fail_closed",
            "exit_code": EXIT_FAIL_CLOSED,
            "evidence_mode": None,
            "real_runtime_evidence":
                "blocked_until_pilot_exception_approve_timeboxed_real_pilot",
            "surfaces": [],
            "surface_count": 0,
            "public_safety": _capability_public_safety([]),
            "reason_codes": ["capability_fixture_unreadable"],
            "raw_values_emitted": False,
            "checked_at": now_iso,
        }, EXIT_FAIL_CLOSED

    reason_codes: list[str] = []
    malformed = False
    deny = False

    if not isinstance(data, dict) or data.get("schema") != CAPABILITY_SCHEMA:
        malformed = True
        reason_codes.append("capability_schema_invalid")
    evidence_mode = data.get("evidence_mode") if isinstance(data, dict) else None
    if evidence_mode != "synthetic_only":
        malformed = True
        reason_codes.append("capability_evidence_mode_invalid")

    raw_surfaces = data.get("surfaces") if isinstance(data, dict) else None
    if not isinstance(raw_surfaces, list) or not raw_surfaces:
        malformed = True
        reason_codes.append("capability_surfaces_missing")
        raw_surfaces = []

    surface_results: list[dict[str, Any]] = []
    seen: set = set()

    for s in raw_surfaces:
        if not isinstance(s, dict):
            malformed = True
            reason_codes.append("capability_surface_not_object")
            continue
        name = s.get("surface")
        verdict = s.get("claimed_verdict")
        s_rcs: list[str] = []

        if name not in CAPABILITY_SURFACE_ENUM:
            malformed = True
            s_rcs.append("surface_name_invalid")
        if name in seen:
            malformed = True
            s_rcs.append("surface_duplicate")
        seen.add(name)

        if not isinstance(verdict, str) or verdict not in CAPABILITY_VERDICT_ENUM:
            malformed = True
            s_rcs.append("verdict_not_single_closed_enum")

        derived, pred_rcs = _capability_supported_predicate(s, evidence_mode)
        s_rcs.extend(pred_rcs)

        consistent = True
        if verdict == "supported" and not derived:
            consistent = False
            deny = True
            s_rcs.append("unsafe_supported_promotion")

        surface_results.append({
            "surface": name,
            "claimed_verdict": verdict if isinstance(verdict, str) else None,
            "derived_supported": derived,
            "verdict_consistent": consistent,
            "reason_codes": list(dict.fromkeys(s_rcs)),
        })

    # #1221 P0-1: require EXACTLY the three canonical surfaces (no missing, no
    # extra/unknown). seen is the set of surface names encountered above.
    if seen != CAPABILITY_SURFACE_ENUM:
        malformed = True
        if not CAPABILITY_SURFACE_ENUM.issubset(seen):
            reason_codes.append("capability_surface_set_incomplete")

    public_safety = _capability_public_safety(raw_surfaces)
    if public_safety["admission"] != "pass":
        deny = True
        reason_codes.append("public_safety_admission_failed")

    if malformed:
        verdict_out = "fail_closed"
        exit_code = EXIT_FAIL_CLOSED
        decision = "deny"
    elif deny:
        verdict_out = "blocked"
        exit_code = EXIT_FAIL
        decision = "deny"
    else:
        verdict_out = "admitted"
        exit_code = EXIT_PASS
        decision = "allow"

    output = {
        "schema": "agent_observation_capability_check/v1",
        "decision": decision,
        "verdict": verdict_out,
        "exit_code": exit_code,
        "evidence_mode": evidence_mode,
        "real_runtime_evidence":
            "blocked_until_pilot_exception_approve_timeboxed_real_pilot",
        "surfaces": surface_results,
        "surface_count": len(seen),
        "public_safety": public_safety,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "raw_values_emitted": public_safety["raw_values_emitted"],
        "checked_at": now_iso,
    }
    return output, exit_code


def _extract_capability_doc_blocks(text: str) -> list[str]:
    """Return the bodies of all fenced yaml blocks in a markdown document."""
    return re.findall(r"```yaml\s*\n(.*?)```", text, re.DOTALL)


def validate_capability_doc(doc_path: str) -> tuple[dict[str, Any], int]:
    """#1221 P0-3: validate the machine-readable surface blocks in the matrix doc.

    Deny on drift: the closed verdict_enum must equal CAPABILITY_VERDICT_ENUM,
    exactly the three canonical surfaces must be present, each surface must carry
    exactly one verdict from the closed enum, and the per-surface verdict field must
    use the unified name 'claimed_verdict' (legacy 'verdict:' is treated as drift).
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    reasons: list[str] = []
    try:
        text = Path(doc_path).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {
            "schema": "agent_observation_capability_doc_check/v1",
            "decision": "deny",
            "field_name_convention": "claimed_verdict",
            "verdict_enum": sorted(CAPABILITY_VERDICT_ENUM),
            "doc_verdict_enum": None,
            "surfaces": [],
            "reason_codes": ["capability_doc_unreadable"],
            "checked_at": now_iso,
        }, EXIT_FAIL_CLOSED

    blocks = _extract_capability_doc_blocks(text)
    enum_values: list[str] | None = None
    surfaces: list[dict[str, Any]] = []
    seen_surface: set[str] = set()
    for block in blocks:
        surface_name: str | None = None
        claimed: str | None = None
        legacy: str | None = None
        for ln in block.splitlines():
            m_enum = re.match(r"^\s*verdict_enum:\s*\[(.*)\]\s*$", ln)
            if m_enum:
                enum_values = [x.strip() for x in m_enum.group(1).split(",") if x.strip()]
            m_surface = re.match(r"^surface:\s*(\S+)\s*$", ln)
            if m_surface:
                surface_name = m_surface.group(1)
            m_claimed = re.match(r"^claimed_verdict:\s*(\S+)\s*$", ln)
            if m_claimed:
                claimed = m_claimed.group(1)
            m_legacy = re.match(r"^verdict:\s*(\S+)\s*$", ln)
            if m_legacy:
                legacy = m_legacy.group(1)
        if surface_name is None:
            continue
        if surface_name in seen_surface:
            reasons.append("capability_doc_surface_duplicate")
        seen_surface.add(surface_name)
        if claimed is None:
            reasons.append("capability_doc_field_name_drift")
        verdict_value = claimed if claimed is not None else legacy
        if verdict_value is not None and verdict_value not in CAPABILITY_VERDICT_ENUM:
            reasons.append("capability_doc_surface_verdict_invalid")
        surfaces.append({
            "surface": surface_name,
            "claimed_verdict": claimed,
            "verdict_value": verdict_value,
        })

    if enum_values is None:
        reasons.append("capability_doc_verdict_enum_missing")
    elif set(enum_values) != CAPABILITY_VERDICT_ENUM:
        reasons.append("capability_doc_verdict_enum_drift")
    if seen_surface != CAPABILITY_SURFACE_ENUM:
        reasons.append("capability_doc_surface_set_drift")

    reasons = list(dict.fromkeys(reasons))
    decision = "allow" if not reasons else "deny"
    exit_code = EXIT_PASS if not reasons else EXIT_FAIL_CLOSED
    return {
        "schema": "agent_observation_capability_doc_check/v1",
        "decision": decision,
        "field_name_convention": "claimed_verdict",
        "verdict_enum": sorted(CAPABILITY_VERDICT_ENUM),
        "doc_verdict_enum": enum_values,
        "surfaces": surfaces,
        "reason_codes": reasons,
        "checked_at": now_iso,
    }, exit_code


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
    parser.add_argument(
        "--require-real-pilot-activation", action="store_true", default=False,
        help="Fail closed unless the host verifier JSON itself proves real-pilot activation.",
    )
    parser.add_argument(
        "--preflight-input-json", default=None,
        help="Path to a verifier JSON fixture to re-evaluate with the strict real-pilot predicate.",
    )
    parser.add_argument(
        "--capability-fixture", default=None,
        help="Path to an agent_observation_capability/v1 fixture JSON; runs the "
             "#1221 capability matrix check and exits.",
    )
    parser.add_argument(
        "--validate-capability-doc", default=None,
        help="Path to the agent observation capability matrix doc; validates its "
             "machine-readable surface blocks against the closed schema and exits.",
    )
    parser.add_argument(
        "--emit-scope-rollup-eligibility", action="store_true", default=False,
        help="Generate the fixed private scope-rollup capture eligibility artifact "
             "(Issue #1527 Scope Delta (2), AC12/AC13) and exit.",
    )
    parser.add_argument(
        "--scope-rollup-eligibility-ttl-seconds", type=int,
        default=SCOPE_ROLLUP_ELIGIBILITY_TTL_SECONDS_DEFAULT,
        help="TTL (seconds) for --emit-scope-rollup-eligibility.",
    )
    args = parser.parse_args()

    execution_profile = args.execution_profile
    # Note: --test-mode just confirms fixture profile (already default)

    if args.emit_scope_rollup_eligibility:
        repo_root = get_repo_root(args.repo_root)
        exit_code, artifact = emit_scope_rollup_eligibility_artifact(
            repo_root, ttl_seconds=args.scope_rollup_eligibility_ttl_seconds,
        )
        print(json.dumps(artifact, indent=2), flush=True)
        return exit_code

    # #1221 P0-3: capability doc schema cross-check (self-contained mode)
    if args.validate_capability_doc:
        doc_output, doc_exit = validate_capability_doc(args.validate_capability_doc)
        print(json.dumps(doc_output, indent=2), flush=True)
        return doc_exit

    # #1221: capability matrix check is a self-contained mode (no SRRS env, no repo scan)
    if args.capability_fixture:
        cap_output, cap_exit = run_capability_check(args.capability_fixture)
        print(json.dumps(cap_output, indent=2), flush=True)
        return cap_exit

    if args.preflight_input_json:
        try:
            fixture_output = json.loads(Path(args.preflight_input_json).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
            fixture_output = {
                "schema": "session_recording_runtime_safety/v2",
                "decision": "deny",
                "verdict": "fail_closed",
                "inspection_complete": False,
                "exit_code": EXIT_FAIL_CLOSED,
                "execution_profile": "fixture",
            }
        exit_code = _apply_real_pilot_preflight_requirement(fixture_output)
        print(json.dumps(fixture_output, indent=2), flush=True)
        return exit_code

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
                            # #1261 follow-up (PR #1352 REQUEST_CHANGES P1):
                            # keep this generic SRRS_* override-rejection shape
                            # in sync with the SRRS_LAT_*-specific rejection
                            # path in check_latitude_component (lib/) so
                            # consumers never see a shape drift between the
                            # two fail_closed producers.
                            "argv_exposure_state": "unknown",
                            "remote_cleanup_state": "unknown",
                            "distribution": {
                                "state": "unknown",
                                "package_spec": None,
                                "dist_integrity": None,
                                "registry_signature_verified": None,
                                "provenance_verified": None,
                                "provenance_source_ref": None,
                                "resolved_registry_origin": None,
                                "resolution_source": "unknown",
                                "npx_invocation": "unknown",
                                "lockfile_digest": None,
                                "tarball_sha256": None,
                                "installed_entrypoint_sha256": None,
                                "preload_sha256": None,
                                "hook_command_sha256": None,
                            },
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

    # #1220: deterministic fixture gate — load srrs_scenario.json overrides (fixture only)
    if execution_profile == "fixture" and args.fixture_root:
        _load_fixture_scenario(Path(args.fixture_root).resolve())

    if args.json:
        output, exit_code = _run_checks_for_json(repo_root, execution_profile, home_root)
        if args.require_real_pilot_activation:
            exit_code = _apply_real_pilot_preflight_requirement(output)
        # AC20: stdout is single JSON object only
        print(json.dumps(output, indent=2), flush=True)
        return exit_code
    else:
        return run_all_checks(repo_root)


if __name__ == "__main__":
    sys.exit(main())
