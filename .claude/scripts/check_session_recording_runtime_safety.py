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
    SRRS_GIT_CONFIG_OUTPUT    override output of git config (NUL-delimited)
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
    re.compile(r"(?i)(?:authorization|x-api-key|token|access_token|api_key)[=:\s]+['\"]?[0-9A-Za-z\-_\.]{20,}"),
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
        return None, "json_parse_error"
    except OSError as exc:
        return None, "os_error"


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


def _has_entire_indicators(repo_root: Path) -> bool:
    """
    Return True if EntireCLI indicators are present (e.g., .entire directory or
    agent hook files mentioning entire).
    """
    entire_dir = repo_root / ".entire"
    if entire_dir.is_dir():
        return True
    # Check agent hook files for EntireCLI references
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
    """
    Load and merge .entire/settings.json and .entire/settings.local.json.
    Returns (merged_dict, error_message).
    Returns (None, None) if neither file exists.
    Returns (None, error) on parse error.
    """
    settings_path = repo_root / ".entire" / "settings.json"
    local_path = repo_root / ".entire" / "settings.local.json"

    base_data, base_err = _read_json_file(settings_path)
    local_data, local_err = _read_json_file(local_path)

    if base_err and base_err != "file_not_found":
        return None, base_err
    if local_err and local_err != "file_not_found":
        return None, local_err

    # Neither file exists
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
    """
    Evaluate strategy_options.push_sessions from merged entire settings.

    B1 fix: FAIL-CLOSED when:
    - settings files exist but strategy_options is missing
    - settings files absent but EntireCLI indicators present
    - push_sessions key is missing from strategy_options
    - push_sessions is not a bool
    true => FAIL, false => PASS
    """
    merged, err = _load_merged_entire_settings(repo_root)

    if err is not None:
        return CODE_FAIL_CLOSED_PUSH_SESSIONS, EXIT_FAIL_CLOSED

    # Neither settings file exists
    if merged is None:
        # If EntireCLI indicators present, we cannot confirm safe
        if _has_entire_indicators(repo_root):
            return CODE_FAIL_CLOSED_PUSH_SESSIONS, EXIT_FAIL_CLOSED
        # No EntireCLI indicators -> EntireCLI not in use, out of scope
        return CODE_PASS, EXIT_PASS

    # AC19: top-level push_sessions key without nested strategy_options => FAIL-CLOSED
    top_level_push = merged.get("push_sessions")
    strategy_options = merged.get("strategy_options")

    if top_level_push is not None and strategy_options is None:
        return CODE_FAIL_CLOSED_PUSH_SESSIONS, EXIT_FAIL_CLOSED

    # B1 fix: strategy_options missing entirely => FAIL-CLOSED
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
        # push_sessions key missing or non-bool => FAIL-CLOSED
        return CODE_FAIL_CLOSED_PUSH_SESSIONS, EXIT_FAIL_CLOSED


# ---------------------------------------------------------------------------
# Check 3: Effective git config (public push remote) (AC4, AC20, AC21)
# B3: Use git config -z for NUL-delimited parsing
# B4: Add branch.*.pushRemote, remote.pushDefault, url.*.insteadOf
# ---------------------------------------------------------------------------

def _get_github_repo_visibility(repo: str) -> str:
    """
    B5: Get GitHub repo visibility via gh repo view.
    repo: "owner/repo" string (must not contain URL prefix).
    Returns: "public" | "private" | "internal" | "unknown"
    """
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
    """Extract 'owner/repo' from a GitHub URL or return None."""
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s\.]+?)(?:\.git)?(?:[/#\s]|$)", url)
    if m:
        return m.group(1)
    return None


def _is_public_github_url(url: str) -> bool | None:
    """
    B5 fix: Check GitHub URL visibility via gh repo view rather than assuming public.

    Returns:
      True  if the repo is public
      False if clearly local, or if the repo is private/internal
      None  if non-GitHub host (unknown -> FAIL-CLOSED)
    """
    if not url:
        return None
    url_lower = url.lower()
    if url_lower.startswith(("file://", "/", "./", "../")):
        return False
    if "localhost" in url_lower or "127.0.0.1" in url_lower:
        return False
    if "github.com" not in url_lower and not url_lower.startswith("git@github.com"):
        return None  # non-GitHub -> FAIL-CLOSED

    # GitHub URL: check actual visibility
    owner_repo = _extract_github_owner_repo(url)
    if not owner_repo:
        # Cannot determine owner/repo -> FAIL-CLOSED
        return None

    # Check env override first (for tests using SRRS_GH_VISIBILITY at call site)
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
    """
    B3 fix: Parse NUL-delimited output of git config -z --show-origin --show-scope.

    Format per entry (NUL-terminated):
      <file-origin>\n<scope>\n<key>\n<value>

    When --show-origin and --show-scope are both present, each NUL-terminated
    record has the format:
      file:<path>\0<scope>\0<key>\n<value>
    Actually git config -z output separates key from value with newline and
    terminates each record with NUL. With --show-origin --show-scope the format is:
      file:<origin>\tscope\tkey\nvalue\0

    We parse both formats robustly by splitting on NUL and then extracting
    the last "key\nvalue" portion of each record.
    """
    result: dict[str, str] = {}
    raw_str = raw_bytes.decode("utf-8", errors="replace")

    for record in raw_str.split("\0"):
        record = record.strip()
        if not record:
            continue

        # The key\nvalue part is always at the end.
        # With --show-origin --show-scope, record looks like:
        #   "file:.git/config\tlocal\tremote.origin.url\nhttps://github.com/..."
        # Without those flags:
        #   "remote.origin.url\nhttps://github.com/..."
        # Split on the last \n to separate value from everything before it.
        last_newline = record.rfind("\n")
        if last_newline == -1:
            continue
        key_part = record[:last_newline]
        value = record[last_newline + 1:]

        # key_part may be tab-separated origin/scope prefix + key
        # The actual key is always the last token after any tabs
        if "\t" in key_part:
            key = key_part.rsplit("\t", 1)[-1].strip().lower()
        else:
            key = key_part.strip().lower()

        if key:
            result[key] = value

    return result


def _parse_git_config_text(output: str) -> dict[str, str]:
    """
    Fallback: parse text output of git config --show-origin --show-scope --get-regexp.
    Used when SRRS_GIT_CONFIG_OUTPUT override is provided as text.
    """
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
    """
    B4 fix: Resolve all effective push destination URLs from git config, considering:
    - remote.*.pushurl (highest priority per remote)
    - remote.*.url (fallback when no pushurl)
    - remote.pushDefault (overrides which remote is used for unqualified pushes)
    - branch.*.pushRemote (per-branch override)
    - url.*.insteadOf (URL rewriting applied before push)
    - url.*.pushInsteadOf (push-specific URL rewriting)

    Returns a list of resolved push URLs (may contain duplicates; caller deduplicates).
    """
    # Collect all remote names
    remote_names: set[str] = set()
    for key in config:
        m = re.match(r"remote\.([^.]+)\.(url|pushurl)$", key)
        if m:
            remote_names.add(m.group(1))

    # Build insteadOf maps (applied to fetch URLs)
    instead_of: list[tuple[str, str]] = []  # (from, to)
    push_instead_of: list[tuple[str, str]] = []  # (from, to)
    for key, val in config.items():
        if key.startswith("url.") and key.endswith(".insteadof"):
            dest_base = key[4:-10]  # strip "url." and ".insteadof"
            instead_of.append((val, dest_base))
        elif key.startswith("url.") and key.endswith(".pushinsteadof"):
            dest_base = key[4:-14]  # strip "url." and ".pushinsteadof"
            push_instead_of.append((val, dest_base))

    def apply_url_rewrites(url: str, is_push: bool) -> str:
        """Apply url.*.insteadOf and url.*.pushInsteadOf rewrites."""
        # pushInsteadOf takes priority for push operations
        if is_push:
            for from_prefix, to_base in push_instead_of:
                if url.startswith(from_prefix):
                    return to_base + url[len(from_prefix):]
        # insteadOf applies to all operations (including push when no pushInsteadOf matches)
        for from_prefix, to_base in instead_of:
            if url.startswith(from_prefix):
                return to_base + url[len(from_prefix):]
        return url

    push_urls: list[str] = []

    for remote in remote_names:
        # Determine push URL for this remote
        push_url_key = f"remote.{remote}.pushurl"
        url_key = f"remote.{remote}.url"

        if push_url_key in config:
            raw_url = config[push_url_key]
            # pushurl is used directly for push (no insteadOf, but pushInsteadOf applies)
            resolved = apply_url_rewrites(raw_url, is_push=True)
        elif url_key in config:
            raw_url = config[url_key]
            resolved = apply_url_rewrites(raw_url, is_push=True)
        else:
            continue

        push_urls.append(resolved)

    # Also collect push destination URLs derived from pushInsteadOf keys
    # (These rewrite a source prefix to a dest base: the dest base IS the push destination)
    for key, val in config.items():
        if key.startswith("url.") and key.endswith(".pushinsteadof"):
            dest_base = key[4:-14]
            push_urls.append(dest_base)

    # Collect branch.*.pushRemote - these point to remotes; we already collected all remotes above
    # But we add them to ensure branch-specific push remotes are considered
    push_remote_default = config.get("remote.pushdefault", "")
    for key, val in config.items():
        if re.match(r"branch\.[^.]+\.pushremote$", key):
            # val is a remote name; get its push URL
            push_url_key = f"remote.{val}.pushurl"
            url_key = f"remote.{val}.url"
            if push_url_key in config:
                resolved = apply_url_rewrites(config[push_url_key], is_push=True)
                push_urls.append(resolved)
            elif url_key in config:
                resolved = apply_url_rewrites(config[url_key], is_push=True)
                push_urls.append(resolved)

    return push_urls


def check_git_config_public_remote(repo_root: Path) -> tuple[str, int]:
    """
    Evaluate effective git config for public push remotes.
    B3 fix: Use NUL-delimited parsing via git config -z.
    B4 fix: Consider branch.*.pushRemote, remote.pushDefault, url.*.insteadOf.
    B5 fix: Use gh repo view to verify GitHub URL visibility.
    FAIL if public push destination detected.
    FAIL-CLOSED if config cannot be parsed.
    """
    override = os.environ.get("SRRS_GIT_CONFIG_OUTPUT")
    if override is not None:
        # Override is provided as text (from tests); use text parser
        try:
            config = _parse_git_config_text(override)
        except Exception:
            return CODE_FAIL_CLOSED_GIT_CONFIG, EXIT_FAIL_CLOSED
    else:
        try:
            result = subprocess.run(
                ["git", "config", "-z", "--show-origin", "--show-scope",
                 "--get-regexp", r"^(remote|branch|url|include)\."],
                capture_output=True, text=False,  # binary mode for NUL handling
                timeout=15,
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
            # Non-GitHub or unknown -> FAIL-CLOSED
            return CODE_FAIL_CLOSED_NON_GITHUB, EXIT_FAIL_CLOSED

    if dangerous_found:
        return CODE_FAIL_PUBLIC_REMOTE, EXIT_FAIL

    return CODE_PASS, EXIT_PASS


# ---------------------------------------------------------------------------
# Check 4: Checkpoint remote visibility (AC5, AC6, AC12, AC13, AC15, AC16)
# B2 fix: Read checkpoint_remote from .entire/settings*.json
# ---------------------------------------------------------------------------

def _get_gh_visibility_for_url(repo_root: Path, url: str) -> str:
    """Get visibility for a specific URL."""
    override = os.environ.get("SRRS_GH_VISIBILITY")
    if override is not None:
        return override.strip().lower()

    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s\.]+?)(?:\.git)?(?:[/#\s]|$)", url)
    if not m:
        return "unknown"
    owner_repo = m.group(1)
    return _get_github_repo_visibility(owner_repo)


def _get_gh_visibility(repo_root: Path) -> str:
    """Returns 'public', 'private', 'internal', or 'unknown' for origin remote."""
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
    B2 fix: Read strategy_options.checkpoint_remote from .entire/settings*.json
    and verify visibility of that specific repo (not just origin).

    private/internal => PASS (private_verified)
    public => FAIL
    unknown/error/non-GitHub => FAIL-CLOSED
    """
    # Try to get checkpoint_remote from settings
    merged, err = _load_merged_entire_settings(repo_root)

    if err is not None:
        # Parse error -> cannot determine checkpoint remote -> FAIL-CLOSED
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
                    # Non-GitHub checkpoint provider -> FAIL-CLOSED
                    return CODE_FAIL_CLOSED_VISIBILITY, EXIT_FAIL_CLOSED
                # If provider is empty or repo is empty, fall through to origin check

    if checkpoint_repo is not None:
        # Check visibility of the specified checkpoint repo
        override = os.environ.get("SRRS_GH_VISIBILITY")
        if override is not None:
            visibility = override.strip().lower()
        else:
            visibility = _get_github_repo_visibility(checkpoint_repo)
    else:
        # No checkpoint_remote configured -> fall back to origin visibility
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
        # B6: Additional patterns
        "sk-proj-abcDEF123xyz",
        "github_pat_abc123XYZ456",
        "gho_abc123XYZ",
        "ghu_abc123XYZ",
        "ghs_abc123XYZ",
        "ghr_abc123XYZ",
        "AKIAIOSFODNN7EXAMPLE",
        "ASIAQNCDONTUSEME1234",
        "Authorization: abcdefghijklmnopqrstuvwxyz123456",
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
