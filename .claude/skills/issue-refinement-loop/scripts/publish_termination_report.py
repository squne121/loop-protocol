#!/usr/bin/env python3
"""
publish_termination_report.py

Thin publisher that calls render_termination_report.py via subprocess
and conditionally posts the rendered body to GitHub as an issue comment.

Usage:
    python3 publish_termination_report.py \
        --issue-number <int> \
        --repo <owner/repo> \
        [--input-file <path>]

Note: --renderer CLI flag has been removed. Override RENDERER_SCRIPT module attribute in tests.

Input:
    TERMINATION_REPORT_INPUT_V1 JSON (stdin or --input-file)

Output:
    Artifact logged to stderr / local artifact file on failure.
    On publishable=true: posts GitHub comment via gh issue comment --body-file.
    On publishable=false or any error: does NOT call gh. Fail-closed.

Exit codes:
    0 - comment posted successfully (publishable=true, gh succeeded)
    1 - publishable=false or any failure (fail-closed, gh not called)
    2 - usage error / missing required arguments
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

from render_termination_report import InputValidationError, normalize_input

# ---------------------------------------------------------------------------
# Issue #1633: shared controlled-executor policy import (bounded request
# schema + issue_comment.publish command id / namespace / input schema).
# scripts/agent-guards is a sibling top-level directory, not a package --
# resolved by absolute path from this file, independent of cwd.
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_AGENT_GUARDS_DIR = _PROJECT_ROOT / "scripts" / "agent-guards"
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))

from controlled_skill_mutation_policy import (  # noqa: E402
    COMMAND_ID_ISSUE_COMMENT_PUBLISH,
    INPUT_SCHEMA_BY_COMMAND,
    ISOLATION_ISSUE_COMMENT_REQUEST_SCHEMA,
    ISSUE_METADATA_NAMESPACE_SEGMENT,
    validate_isolation_issue_comment_request,
)

CONTROLLED_SKILL_MUTATION_EXEC_SCRIPT = _AGENT_GUARDS_DIR / "controlled_skill_mutation_exec.py"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXPECTED_SCHEMA = "TERMINATION_REPORT_RENDER_RESULT_V1"
EXPECTED_SCHEMA_VERSION = 1

# Renderer script location (sibling to this script)
_SCRIPTS_DIR = Path(__file__).resolve().parent
RENDERER_SCRIPT = _SCRIPTS_DIR / "render_termination_report.py"

# Artifact directory relative to cwd (or absolute via env var)
ARTIFACT_DIR = Path(os.environ.get("PUBLISH_ARTIFACT_DIR", "artifacts"))

# Timeout for renderer subprocess (seconds)
RENDERER_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Artifact logging (fail-closed: logs to local file, never leaks body to stderr)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_artifact(
    *,
    issue_number: int | None,
    reason_code: str | None,
    renderer_stderr: str,
    renderer_returncode: int | None,
    extra: dict | None = None,
) -> None:
    """
    Record failure/non-publish artifact to local file.

    IMPORTANT: Does NOT write publishable body to stderr or any log.
    Only reason_code, diagnostics, returncode, and issue_number are recorded.
    renderer_stderr is stored only as length and sha256 (never raw content).
    """
    timestamp = _now_iso()
    stderr_bytes = renderer_stderr.encode("utf-8") if renderer_stderr else b""
    artifact = {
        "timestamp": timestamp,
        "issue_number": issue_number,
        "reason_code": reason_code,
        "returncode": renderer_returncode,
        "stderr_len": len(stderr_bytes),
        "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
    }
    if extra:
        artifact.update(extra)

    # Write to stderr (diagnostic only — reason_code / returncode / artifact path only)
    print(
        f"[publish_termination_report] reason_code={reason_code!r} "
        f"returncode={renderer_returncode}",
        file=sys.stderr,
    )

    # Write to local artifact file
    try:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        artifact_path = ARTIFACT_DIR / f"termination_report_publish_{timestamp.replace(':', '-')}.json"
        artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2))
        print(f"[publish_termination_report] artifact written: {artifact_path}", file=sys.stderr)
    except Exception as exc:
        print(f"[publish_termination_report] failed to write artifact: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Renderer invocation
# ---------------------------------------------------------------------------

def _invoke_renderer(input_data: dict) -> tuple[dict | None, str, int]:
    """
    Call render_termination_report.py via subprocess.run (shell=False).

    Returns (result_dict, stderr_text, returncode).
    result_dict is None on JSON decode error or non-zero exit.
    """
    input_json = json.dumps(input_data, ensure_ascii=False)

    try:
        proc = subprocess.run(
            [sys.executable, str(RENDERER_SCRIPT)],
            input=input_json,
            capture_output=True,
            text=True,
            check=False,
            timeout=RENDERER_TIMEOUT,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = f"renderer timeout after {RENDERER_TIMEOUT}s: {exc}"
        print(f"[publish_termination_report] {stderr}", file=sys.stderr)
        return None, stderr, -1

    stderr_text = proc.stderr or ""
    returncode = proc.returncode

    if returncode != 0:
        print(
            f"[publish_termination_report] renderer exited {returncode}: {stderr_text[:200]}",
            file=sys.stderr,
        )
        return None, stderr_text, returncode

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        err = f"renderer stdout JSON decode error: {exc}"
        print(f"[publish_termination_report] {err}", file=sys.stderr)
        return None, stderr_text + "\n" + err, returncode

    return result, stderr_text, returncode


# ---------------------------------------------------------------------------
# Validation of renderer output
# ---------------------------------------------------------------------------

def _validate_render_result(result: dict) -> str:
    """
    Validate TERMINATION_REPORT_RENDER_RESULT_V1 fields.

    Returns empty string on success, error message on failure.
    """
    if not isinstance(result, dict):
        return "render result must be a JSON object"

    schema = result.get("schema")
    schema_version = result.get("schema_version")
    publishable = result.get("publishable")
    body = result.get("body")
    reason_code = result.get("reason_code")

    if schema != EXPECTED_SCHEMA:
        return f"schema mismatch: expected {EXPECTED_SCHEMA!r}, got {schema!r}"

    if schema_version != EXPECTED_SCHEMA_VERSION:
        return (
            f"schema_version mismatch: expected {EXPECTED_SCHEMA_VERSION}, "
            f"got {schema_version!r}"
        )

    if not isinstance(publishable, bool):
        return f"publishable must be bool, got {type(publishable).__name__}"

    # AC4 invariant: publishable=true requires body to be non-null non-empty string
    if publishable is True and (not isinstance(body, str) or not body):
        return "publishable=true but body is null or empty"

    # AC4 invariant: publishable=false requires body to be null
    if publishable is False and body is not None:
        return f"publishable=false but body is non-null: {type(body).__name__}"

    # reason_code invariants
    if publishable:
        if reason_code is not None:
            return "publishable=true must have reason_code=null"
    else:
        if not isinstance(reason_code, str) or not reason_code:
            return "publishable=false requires non-empty reason_code"

    return ""


# ---------------------------------------------------------------------------
# Issue #1633: parent-owned materializer for the isolation worktree agent's
# bounded Issue comment request.
# ---------------------------------------------------------------------------

def materialize_isolation_issue_comment_request(
    *,
    issue_number: int,
    repo: str,
    comment_body: str,
    marker: str,
    project_root: Path | None = None,
) -> tuple[str | None, str]:
    """
    Materialize a bounded ISOLATION_ISSUE_COMMENT_REQUEST_V1 request into the
    issue-scoped input namespace consumed by controlled_skill_mutation_exec.py
    --command-id issue_comment.publish (Issue #1633 / Issue #1608).

    The isolation worktree agent only ever produces the bounded fields
    (issue_number / repo / comment_body / marker) captured as arguments here.
    This function -- run on the canonical main root -- validates those
    bounded fields via validate_isolation_issue_comment_request(), writes an
    ISSUE_COMMENT_PUBLISH_INPUT_V1 JSON file under
    artifacts/{issue_number}/issue-metadata/issue_comment.publish/, and
    returns a project-root-relative POSIX path string so the caller can
    invoke the exact controlled executor argv (the executor rejects absolute
    --input-file paths).

    Returns (relative_input_file_path, error). relative_input_file_path is
    None on validation error.
    """
    root = project_root or _PROJECT_ROOT
    request = {
        "schema": ISOLATION_ISSUE_COMMENT_REQUEST_SCHEMA,
        "issue_number": issue_number,
        "repo": repo,
        "comment_body": comment_body,
        "marker": marker,
    }
    req_err = validate_isolation_issue_comment_request(request, issue_number, repo)
    if req_err:
        return None, req_err

    namespace_dir = (
        root / "artifacts" / str(issue_number)
        / ISSUE_METADATA_NAMESPACE_SEGMENT / COMMAND_ID_ISSUE_COMMENT_PUBLISH
    )
    namespace_dir.mkdir(parents=True, exist_ok=True)

    materialized = {
        "schema": INPUT_SCHEMA_BY_COMMAND[COMMAND_ID_ISSUE_COMMENT_PUBLISH],
        "issue_number": issue_number,
        "comment_body": comment_body,
        "marker": marker,
    }
    out_path = namespace_dir / "issue_comment_publish_input.json"
    out_path.write_text(
        json.dumps(materialized, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out_path.resolve().relative_to(root.resolve()).as_posix(), ""


# ---------------------------------------------------------------------------
# GitHub comment posting (fail-closed: only called on publishable=true)
# ---------------------------------------------------------------------------

def _post_github_comment(*, issue_number: int, body: str, repo: str) -> int:
    """
    Post body as a GitHub issue comment via the issue_comment.publish
    controlled mutation lane (Issue #1633).

    Builds a bounded ISOLATION_ISSUE_COMMENT_REQUEST_V1 request from body
    (embedding CONTROLLED_EXEC_MARKER from env, or a deterministic
    content-hash marker when unset, as the request's marker field -- P0-5),
    materializes it via materialize_isolation_issue_comment_request(), and
    launches controlled_skill_mutation_exec.py --command-id
    issue_comment.publish with the exact argv it accepts (Issue #1166
    AC4/AC17 shared authority -- raw `gh issue comment` is no longer called
    directly from this module).
    Enforces a 30-second timeout; on timeout fails closed.

    Returns the executor's exit code (0 on success, -1 on timeout, or the
    executor's nonzero exit on failure).
    """
    exec_marker = os.environ.get("CONTROLLED_EXEC_MARKER", "")
    if exec_marker:
        marker = f"<!-- CONTROLLED_EXEC_MARKER:{exec_marker} -->"
    else:
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]
        marker = f"<!-- CONTROLLED_EXEC_MARKER:{content_hash} -->"
    comment_body = body + f"\n{marker}"

    materialized_rel_path, materialize_err = materialize_isolation_issue_comment_request(
        issue_number=issue_number, repo=repo, comment_body=comment_body, marker=marker,
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
    input_data: dict,
    repo: str,
) -> int:
    """
    Core publish flow.

    Returns 0 on successful post, 1 on fail-closed (no gh call).
    To override the renderer path in tests, set publish_termination_report.RENDERER_SCRIPT
    directly before calling publish().

    #1311: loop_handoff (optional, TERMINATION_REPORT_INPUT_V1 field) is read
    from input_data by normalize_input() (dict pass-through, no filtering)
    and forwarded unmodified to the renderer subprocess via _invoke_renderer().
    This function does not interpret, derive, or validate loop_handoff itself --
    schema (schemas/loop_handoff_result_v1.json) and Routing Rules policy
    validation are render_termination_report.py's exclusive responsibility.
    """
    try:
        normalized_input = normalize_input(input_data)
    except InputValidationError as exc:
        _record_artifact(
            issue_number=issue_number,
            reason_code="invalid_input",
            renderer_stderr=str(exc),
            renderer_returncode=None,
            extra={"validation_error": str(exc)},
        )
        return 1

    # Invoke renderer
    result, renderer_stderr, returncode = _invoke_renderer(normalized_input)

    # Renderer non-zero exit — fail-closed
    if result is None:
        reason = "renderer_error" if returncode != -1 else "renderer_timeout"
        _record_artifact(
            issue_number=issue_number,
            reason_code=reason,
            renderer_stderr=renderer_stderr,
            renderer_returncode=returncode,
        )
        return 1

    # Validate renderer output
    validation_err = _validate_render_result(result)
    if validation_err:
        print(
            f"[publish_termination_report] render result validation failed: {validation_err}",
            file=sys.stderr,
        )
        _record_artifact(
            issue_number=issue_number,
            reason_code="validation_failed",
            renderer_stderr=renderer_stderr,
            renderer_returncode=returncode,
            extra={"validation_error": validation_err},
        )
        return 1

    publishable = result["publishable"]
    body = result.get("body")
    reason_code = result.get("reason_code")

    # publishable=false — fail-closed, record artifact
    if not publishable:
        _record_artifact(
            issue_number=issue_number,
            reason_code=reason_code or "publishable_false",
            renderer_stderr=renderer_stderr,
            renderer_returncode=returncode,
            # NOTE: Do NOT include body in artifact (body is None here per validation)
        )
        return 1

    # publishable=true and body is non-empty string — post comment
    gh_exit = _post_github_comment(issue_number=issue_number, body=body, repo=repo)
    if gh_exit != 0:
        reason = "gh_comment_timeout" if gh_exit == -1 else "gh_comment_failed"
        _record_artifact(
            issue_number=issue_number,
            reason_code=reason,
            renderer_stderr=renderer_stderr,
            renderer_returncode=returncode,
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
        description="Publish termination report to GitHub issue comment."
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
        "--input-file",
        type=str,
        default=None,
        help="Path to TERMINATION_REPORT_INPUT_V1 JSON file (default: stdin)",
    )
    args = parser.parse_args()

    # Read input
    if args.input_file:
        try:
            input_data = json.loads(Path(args.input_file).read_text())
        except Exception as exc:
            print(f"[publish_termination_report] failed to read input file: {exc}", file=sys.stderr)
            return 2
    else:
        try:
            input_data = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            print(f"[publish_termination_report] stdin JSON decode error: {exc}", file=sys.stderr)
            return 2

    return publish(
        issue_number=args.issue_number,
        input_data=input_data,
        repo=args.repo,
    )


if __name__ == "__main__":
    sys.exit(main())
