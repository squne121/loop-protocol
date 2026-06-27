#!/usr/bin/env python3
"""git_ref_probe.py — read-only Git branch/ref probe (Issue #1197).

Outputs ``GIT_REF_PROBE_RESULT_V1`` JSON to stdout (one object only).
stderr is capped at 5 lines. No raw commands, env secrets, or absolute
paths are emitted to stderr.

Usage:
    uv run python3 scripts/agent-ops/git_ref_probe.py --branch <name> [--remote <name>] --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCHEMA = "GIT_REF_PROBE_RESULT_V1"

# Maximum number of stderr lines emitted (AC4)
_MAX_STDERR_LINES = 5

# Regex: a string that looks like an absolute path or secret-like token.
# We redact these from error messages before printing to stderr.
_SECRET_LIKE_RE = re.compile(
    r"(/[^\s]+)|"               # absolute path
    r"([A-Za-z0-9+/]{40,}=*)",  # base64-like long token
)


def _redact(text: str) -> str:
    """Redact absolute paths and secret-like tokens from a string."""
    return _SECRET_LIKE_RE.sub("<redacted>", text)


def _bounded_stderr(lines: list[str]) -> None:
    """Emit at most _MAX_STDERR_LINES redacted lines to stderr."""
    for line in lines[:_MAX_STDERR_LINES]:
        print(_redact(line.rstrip()), file=sys.stderr)


def _run_git(project_root: str, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git") or "git"
    return subprocess.run(
        [git, "-C", project_root, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )


def _git_out(project_root: str, *args: str, timeout: float = 10.0) -> str | None:
    """Run a git command and return stripped stdout, or None on error."""
    try:
        result = _run_git(project_root, *args, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    val = result.stdout.strip()
    return val if val else None


def _probe_local(project_root: str, branch: str) -> dict:
    """Probe local branch existence and OID."""
    ref = f"refs/heads/{branch}"
    oid = _git_out(project_root, "rev-parse", "--verify", ref)
    exists = oid is not None
    return {
        "exists": exists,
        "ref": ref if exists else None,
        "oid": oid,
    }


def _probe_remote(project_root: str, branch: str, remote: str) -> dict:
    """Probe remote-tracking ref for branch at remote."""
    remote_ref = f"refs/remotes/{remote}/{branch}"
    oid = _git_out(project_root, "rev-parse", "--verify", remote_ref)
    exists = oid is not None
    return {
        "mode": "origin_branch",
        "ref": remote_ref if exists else None,
        "exists": exists,
        "oid": oid,
    }


def _probe_upstream(project_root: str, branch: str) -> dict:
    """Probe configured upstream tracking for branch."""
    # git for-each-ref with shell=False
    try:
        result = _run_git(
            project_root,
            "for-each-ref",
            "--format=%(upstream:short)\t%(upstream:track)",
            f"refs/heads/{branch}",
            timeout=10.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"configured": False, "track": None}

    if result.returncode != 0 or not result.stdout.strip():
        return {"configured": False, "track": None}

    parts = result.stdout.strip().split("\t", 1)
    upstream_short = parts[0].strip() if parts else ""
    track = parts[1].strip() if len(parts) > 1 else ""

    if not upstream_short:
        return {"configured": False, "track": None}
    return {"configured": True, "track": track or None}


def probe(branch: str, remote: str = "origin", project_root: str | None = None) -> dict:
    """Run all ref probes and return GIT_REF_PROBE_RESULT_V1 dict."""
    if project_root is None:
        project_root = os.getcwd()
    project_root = os.path.realpath(project_root)

    errors: list[str] = []

    local = _probe_local(project_root, branch)
    remote_info = _probe_remote(project_root, branch, remote)
    upstream = _probe_upstream(project_root, branch)

    return {
        "schema": SCHEMA,
        "branch": branch,
        "local": local,
        "remote": remote_info,
        "upstream": upstream,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Git branch/ref read-only probe")
    parser.add_argument("--branch", required=True, help="Branch name to probe")
    parser.add_argument("--remote", default="origin", help="Remote name (default: origin)")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output JSON (always true, kept for explicit invocation)")
    args = parser.parse_args()

    result = probe(branch=args.branch, remote=args.remote)
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
