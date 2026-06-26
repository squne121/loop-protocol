#!/usr/bin/env python3
"""
controlled_skill_mutation_exec.py

Single executor for CONTROLLED_SKILL_MUTATION_COMMAND_POLICY entries.
Invoked by agents via the exact argv form defined in controlled_skill_mutation_policy.py.

Design: Direct script allow for publish_termination_report.py is denied. Only this
executor is allow-listed in settings.json. The executor enforces:
  - command_id whitelist (termination_report.publish only)
  - repo binding (--repo must be TRUSTED_REPO)
  - issue binding (--issue-number must match LOOP_ISSUE_NUMBER env if set)
  - input-file binding (must be in active issue artifact subtree and exist)
  - environment sanitization (PUBLISH_ARTIFACT_DIR / PYTHONPATH / PYTHONHOME /
    GH_EDITOR / EDITOR / VISUAL / BROWSER overridden/removed)
  - module realpath inspection (publisher / renderer canonical path check)
  - idempotency (marker file pre-check; no double-post)
  - postcondition (git status must show no tracked-file changes)
  - comment read-back (comment id / url / body hash recorded)

Exit codes:
  0 - publish succeeded
  1 - publish failed or idempotency marker already set
  2 - validation error (wrong args, wrong issue, wrong file, etc.)

Issue #1166.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Path resolution ───────────────────────────────────────────────────────────

_THIS_FILE = Path(__file__).resolve()
# scripts/agent-guards/ -> scripts/ -> project_root
PROJECT_ROOT = _THIS_FILE.parent.parent.parent

_PUBLISHER_SCRIPT_REL = (
    ".claude/skills/issue-refinement-loop/scripts/publish_termination_report.py"
)
_RENDERER_SCRIPT_REL = (
    ".claude/skills/issue-refinement-loop/scripts/render_termination_report.py"
)
_PROSE_BOUNDARY_REL = (
    ".claude/skills/issue-refinement-loop/scripts/prose_boundary_policy.py"
)

# ── Import shared policy ──────────────────────────────────────────────────────

sys.path.insert(0, str(_THIS_FILE.parent))
from controlled_skill_mutation_policy import (
    COMMAND_ID_PUBLISH,
    TRUSTED_REPO,
    ENV_SANITIZE_KEYS,
    ALLOWED_WRITE_ROOTS,
)

# ── Result schema ─────────────────────────────────────────────────────────────

RESULT_SCHEMA = "CONTROLLED_SKILL_MUTATION_RESULT_V1"

# ── Environment sanitization ─────────────────────────────────────────────────


def _build_sanitized_env(project_root: Path, issue_number: int) -> dict[str, str]:
    """Build a sanitized environment for the publisher subprocess.

    Removes or overrides env vars that could redirect artifacts, shadow modules,
    or open interactive editors/browsers.
    """
    env = os.environ.copy()

    # Remove env vars that could interfere
    for key in ENV_SANITIZE_KEYS:
        env.pop(key, None)

    # Set canonical artifact dir (issue-scoped) so publisher writes to the right place
    artifact_dir = project_root / "artifacts" / str(issue_number)
    env["PUBLISH_ARTIFACT_DIR"] = str(artifact_dir)

    # Clear Python path overrides to prevent module shadowing
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)

    # Prevent any editor/browser from being opened
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"

    return env


# ── Module realpath inspection ────────────────────────────────────────────────


def _check_module_realpaths(project_root: Path) -> list[str]:
    """Return list of realpath violations. Empty list = all OK.

    Checks that publisher / renderer / prose_boundary_policy resolve to canonical
    paths under project_root. Prevents module shadowing (AC16).
    """
    errors = []
    for rel in (_PUBLISHER_SCRIPT_REL, _RENDERER_SCRIPT_REL, _PROSE_BOUNDARY_REL):
        canonical = (project_root / rel).resolve()
        if not canonical.exists():
            # Script doesn't exist (may be normal in some contexts) — warn but don't fail
            continue
        if not str(canonical).startswith(str(project_root)):
            errors.append(
                f"module_shadowing: {rel} resolved to {canonical}, "
                f"expected under {project_root}"
            )
    return errors


# ── Input file validation ─────────────────────────────────────────────────────


def _validate_input_file(input_file_str: str, issue_number: int, project_root: Path) -> str:
    """Validate input-file is in the active issue artifact subtree.

    Returns empty string on success, error message on failure (AC12).
    """
    p = Path(input_file_str)

    # Must be a regular file
    if not p.exists():
        return f"input_file_not_found: {input_file_str!r}"
    if not p.is_file():
        return f"input_file_not_regular: {input_file_str!r}"

    # Must be inside project_root / artifacts / <issue_number>
    try:
        real_input = p.resolve()
    except Exception as exc:
        return f"input_file_resolve_failed: {exc}"

    artifact_subtree = (project_root / "artifacts" / str(issue_number)).resolve()
    try:
        real_input.relative_to(artifact_subtree)
    except ValueError:
        return (
            f"input_file_outside_issue_subtree: {real_input} "
            f"not under {artifact_subtree}"
        )

    return ""


# ── Idempotency marker ────────────────────────────────────────────────────────


def _marker_path(project_root: Path, issue_number: int) -> Path:
    return project_root / "artifacts" / str(issue_number) / "termination_report_published.marker.json"


def _check_idempotency(project_root: Path, issue_number: int) -> dict | None:
    """Return existing marker dict if already published, else None."""
    mp = _marker_path(project_root, issue_number)
    if mp.exists():
        try:
            data = json.loads(mp.read_text())
            if data.get("comment_id") or data.get("comment_url"):
                return data
        except Exception:
            pass
    return None


def _write_idempotency_marker(
    project_root: Path,
    issue_number: int,
    comment_id: str | None,
    comment_url: str | None,
    body_hash: str | None,
) -> None:
    """Write idempotency marker after successful publish."""
    mp = _marker_path(project_root, issue_number)
    mp.parent.mkdir(parents=True, exist_ok=True)
    marker = {
        "schema": "TERMINATION_REPORT_PUBLISH_MARKER_V1",
        "issue_number": issue_number,
        "comment_id": comment_id,
        "comment_url": comment_url,
        "body_sha256": body_hash,
        "published_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    mp.write_text(json.dumps(marker, ensure_ascii=False, indent=2))


# ── Postcondition check ───────────────────────────────────────────────────────


def _check_no_tracked_changes(project_root: Path) -> list[str]:
    """Return list of tracked files changed. Empty = OK (AC14).

    Uses git diff --name-only to detect modifications to tracked files.
    Does NOT check artifacts/ since those are allowed_write_roots.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "diff", "--name-only"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode != 0:
            return [f"git_diff_failed: {out.stderr.strip()[:100]}"]
        changed = [
            f for f in out.stdout.strip().splitlines()
            if f and not any(f.startswith(root) for root in ALLOWED_WRITE_ROOTS)
        ]
        return changed
    except Exception as exc:
        return [f"git_diff_exception: {exc}"]


# ── Publisher invocation ──────────────────────────────────────────────────────


def _invoke_publisher(
    *,
    project_root: Path,
    issue_number: int,
    input_file: str,
    repo: str,
    sanitized_env: dict[str, str],
) -> tuple[int, str, str]:
    """Invoke publish_termination_report.py and return (returncode, stdout, stderr)."""
    publisher = project_root / _PUBLISHER_SCRIPT_REL
    cmd = [
        sys.executable,
        str(publisher),
        "--issue-number", str(issue_number),
        "--input-file", input_file,
        "--repo", repo,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=sanitized_env,
            cwd=str(project_root),
            timeout=60,
            shell=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "publisher_timeout_60s"
    except Exception as exc:
        return -2, "", f"publisher_launch_error: {exc}"


# ── Comment read-back ─────────────────────────────────────────────────────────


def _readback_last_comment(issue_number: int, repo: str) -> dict:
    """Read back the most recent comment on the issue to get id/url/body hash."""
    try:
        out = subprocess.run(
            [
                "gh", "issue", "view", str(issue_number),
                "--repo", repo,
                "--json", "comments",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if out.returncode != 0:
            return {}
        data = json.loads(out.stdout)
        comments = data.get("comments", [])
        if not comments:
            return {}
        last = comments[-1]
        body = last.get("body", "")
        return {
            "comment_id": last.get("id", ""),
            "comment_url": last.get("url", ""),
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        }
    except Exception:
        return {}


# ── Main executor ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Controlled skill mutation executor for termination_report.publish"
    )
    parser.add_argument("--command-id", required=True, help="Command ID")
    parser.add_argument("--issue-number", type=int, required=True, help="GitHub issue number")
    parser.add_argument(
        "--input-file", required=True, help="Path to input JSON file (artifact subtree)"
    )
    parser.add_argument("--repo", required=True, help="GitHub repo slug (owner/repo)")
    parser.add_argument("--json", dest="output_json", action="store_true", help="JSON output")
    parser.add_argument("--dry-run", action="store_true", help="Validate but do not publish")
    args = parser.parse_args(argv)

    def _fail(reason: str, errors: list[str] | None = None, status: str = "error") -> int:
        result = {
            "schema": RESULT_SCHEMA,
            "status": status,
            "command_id": args.command_id,
            "reason": reason,
            "errors": errors or [reason],
        }
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"[controlled_skill_mutation_exec] {status}: {reason}", file=sys.stderr)
        return 2 if status == "error" else 1

    # ── AC8: validate command_id ─────────────────────────────────────────────
    if args.command_id != COMMAND_ID_PUBLISH:
        return _fail(f"unknown_command_id: {args.command_id!r}")

    # ── AC10: validate repo ──────────────────────────────────────────────────
    if args.repo != TRUSTED_REPO:
        return _fail(f"repo_mismatch: {args.repo!r} != {TRUSTED_REPO!r}")

    # ── AC11: issue binding ──────────────────────────────────────────────────
    env_issue = os.environ.get("LOOP_ISSUE_NUMBER", "").strip()
    if env_issue and env_issue.isdigit():
        if int(env_issue) != args.issue_number:
            return _fail(
                f"issue_number_mismatch: --issue-number {args.issue_number} "
                f"!= LOOP_ISSUE_NUMBER {env_issue}"
            )

    # ── AC12: input-file validation ──────────────────────────────────────────
    input_err = _validate_input_file(args.input_file, args.issue_number, PROJECT_ROOT)
    if input_err:
        return _fail(input_err)

    # ── AC16: module realpath inspection ─────────────────────────────────────
    realpath_errors = _check_module_realpaths(PROJECT_ROOT)
    if realpath_errors:
        return _fail("module_shadowing_detected", realpath_errors)

    # ── AC15: idempotency pre-check ──────────────────────────────────────────
    existing_marker = _check_idempotency(PROJECT_ROOT, args.issue_number)
    if existing_marker:
        result = {
            "schema": RESULT_SCHEMA,
            "status": "already_published",
            "command_id": args.command_id,
            "issue_number": args.issue_number,
            "comment_id": existing_marker.get("comment_id"),
            "comment_url": existing_marker.get("comment_url"),
            "body_sha256": existing_marker.get("body_sha256"),
            "idempotency_marker_found": True,
        }
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(
                f"[controlled_skill_mutation_exec] already_published: "
                f"issue #{args.issue_number} idempotency marker found",
                file=sys.stderr,
            )
        return 1  # idempotency block is not an error, but also not a success

    if args.dry_run:
        result = {
            "schema": RESULT_SCHEMA,
            "status": "dry_run_ok",
            "command_id": args.command_id,
            "issue_number": args.issue_number,
        }
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # ── AC13: sanitized environment ──────────────────────────────────────────
    sanitized_env = _build_sanitized_env(PROJECT_ROOT, args.issue_number)

    # ── Invoke publisher ─────────────────────────────────────────────────────
    rc, stdout, stderr = _invoke_publisher(
        project_root=PROJECT_ROOT,
        issue_number=args.issue_number,
        input_file=args.input_file,
        repo=args.repo,
        sanitized_env=sanitized_env,
    )

    if rc != 0:
        errors = [f"publisher_exit_{rc}", stderr[:500] if stderr else "no_stderr"]
        return _fail(f"publisher_failed_rc_{rc}", errors, status="failed")

    # ── AC14: postcondition — no tracked file changes ────────────────────────
    changed = _check_no_tracked_changes(PROJECT_ROOT)
    if changed:
        return _fail(
            "postcondition_tracked_changes_detected",
            [f"changed: {f}" for f in changed[:20]],
            status="failed",
        )

    # ── AC15: comment read-back ──────────────────────────────────────────────
    readback = _readback_last_comment(args.issue_number, args.repo)
    comment_id = readback.get("comment_id") or ""
    comment_url = readback.get("comment_url") or ""
    body_hash = readback.get("body_sha256") or ""

    # Write idempotency marker
    _write_idempotency_marker(
        PROJECT_ROOT, args.issue_number, comment_id, comment_url, body_hash
    )

    result = {
        "schema": RESULT_SCHEMA,
        "status": "ok",
        "command_id": args.command_id,
        "issue_number": args.issue_number,
        "repo": args.repo,
        "comment_id": comment_id,
        "comment_url": comment_url,
        "body_sha256": body_hash,
        "idempotency_marker_written": True,
    }
    if args.output_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"[controlled_skill_mutation_exec] ok: published issue #{args.issue_number}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
