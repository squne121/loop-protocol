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

# Issue #1908: this is a mode of the existing controlled publisher, not a new
# GitHub transport.  The identity and marker are deliberately self-contained
# in the new human-history comment so legacy machine-readable comments retain
# their target, marker, payload, and consumers unchanged.
_HUMAN_HISTORY_FIELDS = (
    "loop_kind", "phase", "source_issue_number", "target_kind", "target_number",
    "route_or_termination_reason", "reviewed_ref",
)
_HUMAN_HISTORY_REASONS = frozenset({
    "completed", "needs_fix", "human_judgment", "binding_missing", "binding_ambiguous",
    "binding_wrong_repo", "binding_gone", "head_drift", "human_escalation",
})
_HUMAN_HISTORY_MARKER_PREFIX = "<!-- loop-protocol/human-history:v1:sha256:"
_HUMAN_HISTORY_MARKER_RE = __import__("re").compile(
    r"^<!-- loop-protocol/human-history:v1:sha256:[0-9a-f]{64} -->$"
)
_HUMAN_HISTORY_ISSUE_REF_RE = __import__("re").compile(r"^[0-9a-f]{64}$")
_HUMAN_HISTORY_PR_REF_RE = __import__("re").compile(r"^refs/pull/([1-9][0-9]*)/head@([0-9a-f]{40})$")
_SECRET_OR_UNSAFE_RE = __import__("re").compile(
    r"(?:gh[porsu]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|authorization:\s*bearer|(?:^|\s)/[^\s]+)",
    __import__("re").I,
)


def _human_history_jcs(identity: dict) -> bytes:
    """The identity value domain is ASCII-only, so sorted compact JSON is the
    RFC 8785 JCS representation for this fixed object shape."""
    return json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validate_human_history_identity(identity: object) -> tuple[dict | None, str]:
    if not isinstance(identity, dict) or set(identity) != set(_HUMAN_HISTORY_FIELDS):
        return None, "human_history_identity_key_set_invalid"
    normalized = {name: identity[name] for name in _HUMAN_HISTORY_FIELDS}
    for field in ("loop_kind", "phase", "target_kind", "route_or_termination_reason", "reviewed_ref"):
        if not isinstance(normalized[field], str):
            return None, f"human_history_identity_type_invalid:{field}"
    for field in ("source_issue_number", "target_number"):
        if type(normalized[field]) is not int or normalized[field] <= 0:
            return None, f"human_history_identity_type_invalid:{field}"
    loop = normalized["loop_kind"]
    phase = normalized["phase"]
    target_kind = normalized["target_kind"]
    reason = normalized["route_or_termination_reason"]
    if loop not in {"issue-refinement-loop", "impl-review-loop"} or reason not in _HUMAN_HISTORY_REASONS:
        return None, "human_history_identity_literal_invalid"
    valid = {
        ("issue-refinement-loop", "review-complete", "issue"): {"completed", "needs_fix", "human_judgment"},
        ("impl-review-loop", "pre-PR-binding", "issue"): {"completed", "needs_fix", "human_judgment"},
        ("impl-review-loop", "binding-validation", "issue"): {"binding_missing", "binding_ambiguous", "binding_wrong_repo", "binding_gone"},
        ("impl-review-loop", "post-PR-binding", "pull_request"): {"completed", "needs_fix", "human_judgment"},
        ("impl-review-loop", "post-PR-head-drift", "pull_request"): {"head_drift"},
        ("impl-review-loop", "conflict-resolution", "issue"): {"human_escalation"},
        ("impl-review-loop", "conflict-resolution", "pull_request"): {"human_escalation"},
    }
    if reason not in valid.get((loop, phase, target_kind), set()):
        return None, "human_history_identity_matrix_combination_invalid"
    if target_kind == "issue":
        if normalized["target_number"] != normalized["source_issue_number"]:
            return None, "human_history_issue_target_number_mismatch"
        if not _HUMAN_HISTORY_ISSUE_REF_RE.fullmatch(normalized["reviewed_ref"]):
            return None, "human_history_issue_reviewed_ref_invalid"
    else:
        reference = _HUMAN_HISTORY_PR_REF_RE.fullmatch(normalized["reviewed_ref"])
        if reference is None or int(reference.group(1)) != normalized["target_number"]:
            return None, "human_history_pr_reviewed_ref_invalid"
    return normalized, ""


def _validate_public_safe_human_text(value: object, field: str) -> tuple[str | None, str]:
    if not isinstance(value, str) or not value.strip() or "```" in value or _SECRET_OR_UNSAFE_RE.search(value):
        return None, f"human_history_public_safe_text_invalid:{field}"
    return value.strip(), ""


def render_human_history_comment(
    *, identity: object, result: object, evidence_refs: object, recommended_action: object,
    recommended_reason: object, impact_if_unaddressed: object, stale_evidence: object = None,
) -> tuple[dict | None, str]:
    """Build a public-safe Japanese human-history body and stable marker."""
    value, error = _validate_human_history_identity(identity)
    if error:
        return None, error
    assert value is not None
    text_values: dict[str, str] = {}
    for name, raw in {
        "result": result, "recommended_action": recommended_action,
        "recommended_reason": recommended_reason, "impact_if_unaddressed": impact_if_unaddressed,
    }.items():
        clean, text_error = _validate_public_safe_human_text(raw, name)
        if text_error:
            return None, text_error
        assert clean is not None
        text_values[name] = clean
    if not isinstance(evidence_refs, list) or not evidence_refs:
        return None, "human_history_evidence_refs_invalid"
    evidence: list[str] = []
    for item in evidence_refs:
        clean, text_error = _validate_public_safe_human_text(item, "evidence_ref")
        if text_error:
            return None, text_error
        assert clean is not None
        evidence.append(clean)
    stale_line = ""
    if stale_evidence is not None:
        clean, text_error = _validate_public_safe_human_text(stale_evidence, "stale_evidence")
        if text_error:
            return None, text_error
        stale_line = f"\n- stale evidence: {clean}"
    identity_sha256 = hashlib.sha256(_human_history_jcs(value)).hexdigest()
    marker = f"{_HUMAN_HISTORY_MARKER_PREFIX}{identity_sha256} -->"
    body = (
        "## review loop の作業履歴\n\n"
        f"- 実施内容: {text_values['result']}\n"
        f"- phase: {value['phase']}\n"
        f"- reviewed_ref: {value['reviewed_ref']}\n"
        f"- 推奨アクション: {text_values['recommended_action']}\n"
        f"- 推奨する理由: {text_values['recommended_reason']}\n"
        f"- 対応しない場合の影響: {text_values['impact_if_unaddressed']}\n"
        f"- evidence refs: {'; '.join(evidence)}{stale_line}\n\n{marker}\n"
    )
    return {
        "body": body,
        "marker": marker,
        "identity": value,
        "identity_sha256": identity_sha256,
    }, ""


def publish_human_history(
    *, target_number: int, repo: str, identity: object, result: object, evidence_refs: object,
    recommended_action: object, recommended_reason: object, impact_if_unaddressed: object,
    stale_evidence: object = None,
) -> int:
    """Publish one human-history event through existing issue_comment.publish."""
    rendered, error = render_human_history_comment(
        identity=identity, result=result, evidence_refs=evidence_refs,
        recommended_action=recommended_action, recommended_reason=recommended_reason,
        impact_if_unaddressed=impact_if_unaddressed, stale_evidence=stale_evidence,
    )
    if error or rendered is None:
        _record_artifact(issue_number=target_number, reason_code=error or "human_history_render_failed")
        return 1
    if type(target_number) is not int or target_number <= 0 or rendered["identity"]["target_number"] != target_number:
        _record_artifact(issue_number=target_number, reason_code="human_history_target_binding_invalid")
        return 1
    return _post_github_comment(
        issue_number=target_number, body=rendered["body"], repo=repo, marker=rendered["marker"],
    )


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

def _post_github_comment(*, issue_number: int, body: str, repo: str, marker: str | None = None) -> int:
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
    if marker is None:
        exec_marker = os.environ.get("CONTROLLED_EXEC_MARKER", "")
        if exec_marker:
            marker = f"<!-- CONTROLLED_EXEC_MARKER:{exec_marker} -->"
        else:
            # Issue #1639 fix_delta P1-2: the fallback marker must not collide
            # across different repos/issues that happen to share identical body.
            fallback_seed = f"{repo}\x00{issue_number}\x00{body}".encode("utf-8")
            content_hash = hashlib.sha256(fallback_seed).hexdigest()[:32]
            marker = f"<!-- CONTROLLED_EXEC_MARKER:{content_hash} -->"
        comment_body = body + f"\n{marker}"
    else:
        # #1908 passes its stable identity marker intact to the existing bridge;
        # adding a legacy marker would alter the new comment's digest semantics.
        if marker not in body:
            return 1
        comment_body = body

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
    # The controlled lane binds its CLI number to the *comment target*.  A PR
    # history event legitimately carries a distinct source Issue identity, so
    # an inherited source-issue session binding must not mis-bind this target.
    if marker.startswith(_HUMAN_HISTORY_MARKER_PREFIX):
        env.pop("LOOP_ISSUE_NUMBER", None)
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
