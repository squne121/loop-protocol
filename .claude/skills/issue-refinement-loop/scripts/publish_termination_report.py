#!/usr/bin/env python3
"""
publish_termination_report.py

Thin publisher that calls render_termination_report.py via subprocess
and conditionally posts the rendered body to GitHub as an issue comment.

Usage:
    python3 publish_termination_report.py --issue-number <int> [--input-file <path>]

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
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

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
    """
    timestamp = _now_iso()
    artifact = {
        "timestamp": timestamp,
        "issue_number": issue_number,
        "reason_code": reason_code,
        "renderer_returncode": renderer_returncode,
        "renderer_stderr": renderer_stderr,
    }
    if extra:
        artifact.update(extra)

    # Write to stderr (diagnostic only, no publishable body)
    print(
        f"[publish_termination_report] non-publish artifact: "
        f"reason_code={reason_code!r} issue={issue_number} "
        f"returncode={renderer_returncode} timestamp={timestamp}",
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
    schema = result.get("schema")
    schema_version = result.get("schema_version")
    publishable = result.get("publishable")
    body = result.get("body")

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

    return ""


# ---------------------------------------------------------------------------
# GitHub comment posting (fail-closed: only called on publishable=true)
# ---------------------------------------------------------------------------

def _post_github_comment(*, issue_number: int, body: str) -> int:
    """
    Post body as a GitHub issue comment via gh CLI.

    Uses --body-file only (never --body with direct string).

    Returns gh exit code.
    """
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        prefix="termination_report_",
        delete=False,
        encoding="utf-8",
    ) as f:
        f.write(body)
        body_file = f.name

    try:
        proc = subprocess.run(
            [
                "gh", "issue", "comment",
                str(issue_number),
                "--body-file", body_file,
            ],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
        if proc.returncode != 0:
            print(
                f"[publish_termination_report] gh issue comment failed "
                f"(exit {proc.returncode}): {proc.stderr[:200]}",
                file=sys.stderr,
            )
        return proc.returncode
    finally:
        try:
            Path(body_file).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main publish flow
# ---------------------------------------------------------------------------

def publish(
    *,
    issue_number: int,
    input_data: dict,
    _renderer_override: str | None = None,
) -> int:
    """
    Core publish flow.

    Returns 0 on successful post, 1 on fail-closed (no gh call).
    """
    global RENDERER_SCRIPT
    if _renderer_override is not None:
        RENDERER_SCRIPT = Path(_renderer_override)

    # Invoke renderer
    result, renderer_stderr, returncode = _invoke_renderer(input_data)

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
    gh_exit = _post_github_comment(issue_number=issue_number, body=body)
    if gh_exit != 0:
        _record_artifact(
            issue_number=issue_number,
            reason_code="gh_comment_failed",
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
        "--input-file",
        type=str,
        default=None,
        help="Path to TERMINATION_REPORT_INPUT_V1 JSON file (default: stdin)",
    )
    parser.add_argument(
        "--renderer",
        type=str,
        default=None,
        help="Override path to render_termination_report.py (for testing)",
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
        _renderer_override=args.renderer,
    )


if __name__ == "__main__":
    sys.exit(main())
