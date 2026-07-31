#!/usr/bin/env python3
"""
publish_termination_report.py

Thin publisher that posts a plain-markdown termination summary body to a
GitHub issue as a comment, via the issue_comment.publish controlled
mutation lane.

#1873 (bounded review loops): the previous version of this module invoked
render_termination_report.py (a TERMINATION_REPORT_INPUT_V1 -> rendered-body
pipeline layered on top of PARENT_REPLAY_* / ISSUE_EXECUTION_DECISION_V1
routing state) as a subprocess, then posted its output. That renderer and
the routing state it depended on have been removed. The orchestrator
(`plan_refinement_loop.py` / SKILL.md Step 5) now assembles a short plain
markdown summary directly and passes it to this module as-is -- there is no
intermediate render/validate step here.

Usage:
    python3 publish_termination_report.py \
        --issue-number <int> \
        --repo <owner/repo> \
        [--body-file <path>]

Input:
    The comment body (plain markdown text), from stdin or --body-file.

Output:
    Artifact logged to stderr / local artifact file on failure.
    On success: posts GitHub comment via the issue_comment.publish
    controlled mutation lane (never a raw `gh issue comment` call).

Exit codes:
    0 - comment posted successfully
    1 - failure (fail-closed, gh not called, or gh call failed)
    2 - usage error / missing required arguments / empty body
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

# ---------------------------------------------------------------------------
# Issue #1633 / #1639 / #1873 Tranche 3: the isolation worktree agent's
# bounded Issue comment request producer/consumer (build/materialize) and
# the controlled-executor invocation helper now live in
# isolation_issue_comment_bridge.py, a standalone module shared by any
# production caller of the issue_comment.publish lane -- not duplicated
# here. This module imports them rather than redefining them.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR_FOR_BRIDGE = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR_FOR_BRIDGE) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR_FOR_BRIDGE))

from isolation_issue_comment_bridge import (  # noqa: E402
    build_isolation_issue_comment_request,
    materialize_isolation_issue_comment_request,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_AGENT_GUARDS_DIR = _PROJECT_ROOT / "scripts" / "agent-guards"
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))

from controlled_skill_mutation_policy import (  # noqa: E402
    COMMAND_ID_ISSUE_COMMENT_PUBLISH,
)

CONTROLLED_SKILL_MUTATION_EXEC_SCRIPT = _AGENT_GUARDS_DIR / "controlled_skill_mutation_exec.py"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Artifact directory relative to cwd (or absolute via env var)
ARTIFACT_DIR = Path(os.environ.get("PUBLISH_ARTIFACT_DIR", "artifacts"))


# ---------------------------------------------------------------------------
# Artifact logging (fail-closed: logs to local file, never leaks body to stderr)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_artifact(
    *,
    issue_number: int | None,
    reason_code: str | None,
    extra: dict | None = None,
) -> None:
    """
    Record failure artifact to local file.

    IMPORTANT: Does NOT write the comment body to stderr or any log.
    Only reason_code and issue_number (and non-body extras) are recorded.
    """
    timestamp = _now_iso()
    artifact = {
        "timestamp": timestamp,
        "issue_number": issue_number,
        "reason_code": reason_code,
    }
    if extra:
        artifact.update(extra)

    print(
        f"[publish_termination_report] reason_code={reason_code!r}",
        file=sys.stderr,
    )

    try:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        artifact_path = ARTIFACT_DIR / f"termination_report_publish_{timestamp.replace(':', '-')}.json"
        artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2))
        print(f"[publish_termination_report] artifact written: {artifact_path}", file=sys.stderr)
    except Exception as exc:
        print(f"[publish_termination_report] failed to write artifact: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# GitHub comment posting (fail-closed)
# ---------------------------------------------------------------------------

def _post_github_comment(*, issue_number: int, body: str, repo: str) -> int:
    """
    Post body as a GitHub issue comment via the issue_comment.publish
    controlled mutation lane (Issue #1633).

    Builds a bounded ISOLATION_ISSUE_COMMENT_REQUEST_V1 request (via
    build_isolation_issue_comment_request(), Issue #1639 fix_delta P1-1)
    from body (embedding CONTROLLED_EXEC_MARKER from env, or a deterministic
    marker derived from repo + issue_number + body when unset -- Issue #1639
    fix_delta P1-2 -- as the request's marker field), materializes it via
    materialize_isolation_issue_comment_request(), and launches
    controlled_skill_mutation_exec.py --command-id
    issue_comment.publish with the exact argv it accepts (Issue #1166
    AC4/AC17 shared authority -- raw `gh issue comment` is never called
    directly from this module).
    Enforces a 30-second timeout; on timeout fails closed.

    Returns the executor's exit code (0 on success, -1 on timeout, or the
    executor's nonzero exit on failure).
    """
    exec_marker = os.environ.get("CONTROLLED_EXEC_MARKER", "")
    if exec_marker:
        marker = f"<!-- CONTROLLED_EXEC_MARKER:{exec_marker} -->"
    else:
        # Issue #1639 fix_delta P1-2: the fallback marker must not collide
        # across different repos/issues that happen to share identical body
        # content -- hash repo + issue_number + body (NUL-separated to avoid
        # ambiguous concatenation), not body alone.
        fallback_seed = f"{repo}\x00{issue_number}\x00{body}".encode("utf-8")
        content_hash = hashlib.sha256(fallback_seed).hexdigest()[:32]
        marker = f"<!-- CONTROLLED_EXEC_MARKER:{content_hash} -->"
    comment_body = body + f"\n{marker}"

    request = build_isolation_issue_comment_request(
        issue_number=issue_number, repo=repo, comment_body=comment_body, marker=marker,
    )
    materialized_rel_path, materialize_err = materialize_isolation_issue_comment_request(
        request=request, expected_issue_number=issue_number, expected_repo=repo,
        project_root=_PROJECT_ROOT,
    )
    if materialize_err:
        print(
            f"[publish_termination_report] materialize_isolation_issue_comment_request "
            f"failed: {materialize_err}",
            file=sys.stderr,
        )
        return 1

    cmd = [
        sys.executable, str(CONTROLLED_SKILL_MUTATION_EXEC_SCRIPT),
        "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
        "--issue-number", str(issue_number),
        "--input-file", materialized_rel_path,
        "--repo", repo,
    ]
    env = os.environ.copy()
    env["GH_PROMPT_DISABLED"] = "1"
    env.setdefault("GH_NO_UPDATE_NOTIFIER", "1")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=30,
            env=env,
        )
    except subprocess.TimeoutExpired:
        print(
            "[publish_termination_report] controlled_skill_mutation_exec issue_comment.publish "
            "timed out (30s) — fail-closed",
            file=sys.stderr,
        )
        return -1

    if proc.returncode != 0:
        print(
            f"[publish_termination_report] controlled_skill_mutation_exec "
            f"issue_comment.publish failed (exit {proc.returncode}): {proc.stderr[:200]}",
            file=sys.stderr,
        )
    return proc.returncode


# ---------------------------------------------------------------------------
# Main publish flow
# ---------------------------------------------------------------------------

def publish(
    *,
    issue_number: int,
    body: str,
    repo: str,
) -> int:
    """
    Core publish flow: post `body` (already-assembled plain markdown) as a
    GitHub issue comment. Returns 0 on successful post, 1 on fail-closed
    (no gh call, or gh call failed).
    """
    if not isinstance(body, str) or not body.strip():
        _record_artifact(issue_number=issue_number, reason_code="empty_body")
        return 1

    gh_exit = _post_github_comment(issue_number=issue_number, body=body, repo=repo)
    if gh_exit != 0:
        reason = "gh_comment_timeout" if gh_exit == -1 else "gh_comment_failed"
        _record_artifact(
            issue_number=issue_number,
            reason_code=reason,
            extra={"gh_exit_code": gh_exit},
        )
        return 1

    print(
        f"[publish_termination_report] comment posted for issue #{issue_number}",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish a plain-markdown termination summary to a GitHub issue comment."
    )
    parser.add_argument(
        "--issue-number",
        type=int,
        required=True,
        help="GitHub issue number to comment on",
    )
    parser.add_argument(
        "--repo",
        type=str,
        required=True,
        help="GitHub repository slug (owner/repo) for canonical repo binding",
    )
    parser.add_argument(
        "--body-file",
        type=str,
        default=None,
        help="Path to a plain markdown body file (default: stdin)",
    )
    args = parser.parse_args()

    # Read input
    if args.body_file:
        try:
            body = Path(args.body_file).read_text(encoding="utf-8")
        except Exception as exc:
            print(f"[publish_termination_report] failed to read body file: {exc}", file=sys.stderr)
            return 2
    else:
        body = sys.stdin.read()

    if not body.strip():
        print("[publish_termination_report] body is empty", file=sys.stderr)
        return 2

    return publish(
        issue_number=args.issue_number,
        body=body,
        repo=args.repo,
    )


if __name__ == "__main__":
    sys.exit(main())
