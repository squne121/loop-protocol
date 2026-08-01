#!/usr/bin/env python3
"""Deterministic transaction helper for existing-issue body/comment mutation.

Consumes ISSUE_EDIT_TXN_INPUT_V1 and routes mutation through controlled
executor command ids only. Stdout is always a single bounded JSON object.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[4]
CONTROLLED_EXEC = REPO_ROOT / "scripts" / "agent-guards" / "controlled_skill_mutation_exec.py"
GUARD_SCRIPT = SCRIPT_PATH.parent / "guard-issue-body.py"
HYGIENE_SCRIPT = SCRIPT_PATH.parent / "issue_contract_hygiene_autofix.py"
READINESS_SCRIPT = (
    REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts" / "contract_readiness_check.py"
)

# Issue #1883: native relationship (parent / blockedBy / blocking) mutation is
# a fixed controlled-executor operation (issue_relationship.update). The pure,
# side-effect-free graph-invariant validator is imported directly (no gh call,
# no mutation) so this module can reject a malformed desired state before
# ever writing an executor input file. AGENT_GUARDS_IMPORT_ROOT is resolved
# from the real on-disk script location (not the mutable REPO_ROOT module
# attribute tests monkeypatch), since it must always point at the real
# controlled_skill_mutation_policy.py regardless of test fixture overrides.
AGENT_GUARDS_IMPORT_ROOT = SCRIPT_PATH.parents[4] / "scripts" / "agent-guards"
if str(AGENT_GUARDS_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_GUARDS_IMPORT_ROOT))
from controlled_skill_mutation_policy import (  # noqa: E402
    validate_issue_relationship_update_input as _validate_relationship_graph_invariants,
)

INPUT_SCHEMA = "ISSUE_EDIT_TXN_INPUT_V1"
RESULT_SCHEMA = "ISSUE_EDIT_TXN_RESULT_V1"
READINESS_ALLOWED = {"go", "needs_fix", "human_judgment", "input_or_runtime_error"}
CONTROLLED_EXEC_TIMEOUT_SECONDS = 30
TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "issue_number",
        "repo",
        "new_body_file",
        "readiness_forwarding_payload",
        "comment_mode",
        "expected_previous_body_sha256",
        "expected_previous_updated_at",
        "title_update",
        "native_relationships",
    }
)

# Issue #1883: ISSUE_EDIT_TXN_INPUT_V1 additive extension. native_relationships
# is optional -- omitting it entirely preserves the pre-#1883 body/comment-only
# transaction behaviour exactly (backward compatible).
NATIVE_RELATIONSHIPS_KEYS = frozenset(
    {"expected_before", "parent", "add_blocked_by", "remove_blocked_by", "add_blocking", "remove_blocking"}
)
NATIVE_EXPECTED_BEFORE_KEYS = frozenset({"parent", "blocked_by", "blocking"})
NATIVE_PARENT_KEYS = frozenset({"action", "issue_number"})
NATIVE_PARENT_ACTIONS = frozenset({"unchanged", "set", "remove"})
DEFAULT_NATIVE_RESULT: dict[str, Any] = {
    "attempted": False,
    "status": "not_run",
    "before": None,
    "desired": None,
    "after": None,
    "completed_operations": [],
    "pending_operations": [],
    "errors": [],
}
READINESS_KEYS = frozenset({"readiness_result"})
READINESS_RESULT_REQUIRED_KEYS = frozenset(
    {
        "status",
        "body_sha256",
        "source_checks",
        "errors",
        "readiness_result_ref",
    }
)
READINESS_RESULT_KEYS = frozenset(
    {
        "status",
        "body_sha256",
        "source_checks",
        "errors",
        "readiness_result_ref",
        "resolution_evidence",
    }
)
TITLE_UPDATE_KEYS = frozenset({"required", "proposed_title", "reason"})
COMMENT_MODE_KEYS = frozenset({"mode", "comment_body_file", "marker"})
MAX_ERROR_ITEMS = 8
MAX_ERROR_MESSAGE = 240
MAX_CHILD_SNIPPET = 160


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bounded(text: str, limit: int = MAX_ERROR_MESSAGE) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _truncate_errors(errors: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "code": _bounded(str(item.get("code", "error")), 80),
            "message": _bounded(str(item.get("message", ""))),
        }
        for item in errors[:MAX_ERROR_ITEMS]
    ]


def _require_closed_keys(data: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"{label}_unknown_keys: {', '.join(unknown)}")


def _validate_input_payload(data: dict[str, Any]) -> None:
    _require_closed_keys(data, TOP_LEVEL_KEYS, "input")
    if data.get("schema") != INPUT_SCHEMA:
        raise ValueError("input_schema_invalid")
    if not isinstance(data.get("issue_number"), int) or data["issue_number"] <= 0:
        raise ValueError("issue_number_invalid")
    if not isinstance(data.get("repo"), str) or not data["repo"]:
        raise ValueError("repo_invalid")
    if not isinstance(data.get("new_body_file"), str) or not data["new_body_file"]:
        raise ValueError("new_body_file_invalid")
    if not isinstance(data.get("expected_previous_body_sha256"), str) or not data["expected_previous_body_sha256"]:
        raise ValueError("expected_previous_body_sha256_invalid")
    if not isinstance(data.get("expected_previous_updated_at"), str) or not data["expected_previous_updated_at"]:
        raise ValueError("expected_previous_updated_at_invalid")

    readiness = data.get("readiness_forwarding_payload")
    if not isinstance(readiness, dict):
        raise ValueError("readiness_forwarding_payload_invalid")
    _require_closed_keys(readiness, READINESS_KEYS, "readiness_forwarding_payload")
    readiness_result = readiness.get("readiness_result")
    if not isinstance(readiness_result, dict):
        raise ValueError("readiness_result_invalid")
    _require_closed_keys(readiness_result, READINESS_RESULT_KEYS, "readiness_result")
    missing_readiness_required = sorted(READINESS_RESULT_REQUIRED_KEYS - set(readiness_result))
    if missing_readiness_required:
        raise ValueError(f"readiness_result_missing_required_keys: {', '.join(missing_readiness_required)}")
    if readiness_result.get("status") not in READINESS_ALLOWED:
        raise ValueError("readiness_status_invalid")

    title_update = data.get("title_update")
    if title_update is not None:
        if not isinstance(title_update, dict):
            raise ValueError("title_update_invalid")
        _require_closed_keys(title_update, TITLE_UPDATE_KEYS, "title_update")
        if not isinstance(title_update.get("required"), bool):
            raise ValueError("title_update_required_invalid")
        if title_update["required"]:
            proposed_title = title_update.get("proposed_title")
            reason = title_update.get("reason")
            if (
                not isinstance(proposed_title, str)
                or not proposed_title.strip()
                or any(unicodedata.category(char) == "Cc" for char in proposed_title)
            ):
                raise ValueError("title_update_proposed_title_invalid")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("title_update_reason_invalid")
        elif title_update.get("proposed_title") is not None or title_update.get("reason") is not None:
            raise ValueError("title_update_not_required_fields_must_be_null")

    comment_mode = data.get("comment_mode", {"mode": "skip"})
    if not isinstance(comment_mode, dict):
        raise ValueError("comment_mode_invalid")
    _require_closed_keys(comment_mode, COMMENT_MODE_KEYS, "comment_mode")
    if comment_mode.get("mode", "skip") not in {"skip", "publish"}:
        raise ValueError("comment_mode_mode_invalid")
    if comment_mode.get("mode") == "publish":
        if not isinstance(comment_mode.get("comment_body_file"), str) or not comment_mode["comment_body_file"]:
            raise ValueError("comment_body_file_invalid")
        if not isinstance(comment_mode.get("marker"), str) or not comment_mode["marker"]:
            raise ValueError("comment_marker_invalid")

    native_relationships = data.get("native_relationships")
    if native_relationships is not None:
        if not isinstance(native_relationships, dict):
            raise ValueError("native_relationships_invalid")
        _validate_native_relationships_shape(native_relationships)


def _validate_native_relationships_shape(data: dict[str, Any]) -> None:
    """Structural (schema-shape) validation of the ISSUE_EDIT_TXN_INPUT_V1
    additive native_relationships block. This only checks the closed key set
    and basic types the caller must supply; the full AC12 graph-invariant
    check (self-parent, self-dependency, duplicate/unsorted/bool-as-int,
    conflicting blocked_by/blocking targets) is delegated to
    _validate_relationship_graph_invariants (imported from
    controlled_skill_mutation_policy) once the fully-shaped executor payload
    is assembled in _run_native_relationship_step -- this avoids maintaining
    two independent copies of the same invariant logic.
    """
    _require_closed_keys(data, NATIVE_RELATIONSHIPS_KEYS, "native_relationships")

    expected_before = data.get("expected_before")
    if expected_before is not None:
        if not isinstance(expected_before, dict):
            raise ValueError("native_relationships_expected_before_invalid")
        _require_closed_keys(expected_before, NATIVE_EXPECTED_BEFORE_KEYS, "native_relationships_expected_before")

    parent = data.get("parent", {"action": "unchanged", "issue_number": None})
    if not isinstance(parent, dict):
        raise ValueError("native_relationships_parent_invalid")
    _require_closed_keys(parent, NATIVE_PARENT_KEYS, "native_relationships_parent")
    if parent.get("action", "unchanged") not in NATIVE_PARENT_ACTIONS:
        raise ValueError("native_relationships_parent_action_invalid")

    for field in ("add_blocked_by", "remove_blocked_by", "add_blocking", "remove_blocking"):
        value = data.get(field, [])
        if not isinstance(value, list) or not all(isinstance(n, int) and not isinstance(n, bool) for n in value):
            raise ValueError(f"native_relationships_{field}_invalid")


def _relationship_capability_preflight() -> tuple[bool, str]:
    """Read-only environment preflight for native relationship mutation
    (AC10). Distinguishes "cannot even attempt this" (environment blocker --
    binary missing, auth unreachable) from a runtime write-permission
    rejection (403/404), which is classified later by the controlled
    executor's own actor-permission check. Success here is never treated as
    proof of write permission.
    """
    gh = shutil.which("gh")
    if not gh:
        return False, "gh_binary_not_found"
    cp = _run_command([gh, "auth", "status", "--hostname", "github.com"])
    if cp.returncode != 0:
        return False, "gh_auth_status_unreachable"
    return True, ""


def _fetch_native_relationships(issue_number: int, repo: str) -> tuple[dict[str, Any] | None, str]:
    """Best-effort read-only snapshot of {parent, blocked_by, blocking} used
    by this module to determine expected_before (when the caller omits it)
    and to report a pre-mutation snapshot. Fails closed (returns None) rather
    than silently reporting truncated data if a single page of 100 does not
    exhaust blockedBy/blocking -- the authoritative full-pagination readback
    happens inside the controlled executor (issue_relationship.update).
    """
    gh = shutil.which("gh") or "gh"
    owner, _, name = repo.partition("/")
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "repository(owner:$owner,name:$name){"
        "issue(number:$number){"
        "parent{number} "
        "blockedBy(first:100){pageInfo{hasNextPage} totalCount nodes{number}} "
        "blocking(first:100){pageInfo{hasNextPage} totalCount nodes{number}}"
        "}}}"
    )
    cp = _run_command(
        [
            gh,
            "api",
            "graphql",
            "-f",
            "query=\n" + query,
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={issue_number}",
        ]
    )
    if cp.returncode != 0:
        return None, _bounded(cp.stderr.strip() or cp.stdout.strip())
    try:
        payload = json.loads(cp.stdout)
        issue = payload["data"]["repository"]["issue"]
        parent = issue.get("parent")
        parent_number = parent.get("number") if isinstance(parent, dict) else None
        blocked_by_payload = issue.get("blockedBy") or {}
        blocking_payload = issue.get("blocking") or {}
        if (blocked_by_payload.get("pageInfo") or {}).get("hasNextPage"):
            return None, "blocked_by_pagination_incomplete_for_readonly_snapshot"
        if (blocking_payload.get("pageInfo") or {}).get("hasNextPage"):
            return None, "blocking_pagination_incomplete_for_readonly_snapshot"
        blocked_by = sorted(n["number"] for n in blocked_by_payload.get("nodes", []))
        blocking = sorted(n["number"] for n in blocking_payload.get("nodes", []))
        if blocked_by_payload.get("totalCount") not in (None, len(blocked_by)):
            return None, "blocked_by_total_count_mismatch"
        if blocking_payload.get("totalCount") not in (None, len(blocking)):
            return None, "blocking_total_count_mismatch"
        return {"parent": parent_number, "blocked_by": blocked_by, "blocking": blocking}, ""
    except (KeyError, TypeError, json.JSONDecodeError):
        return None, "gh_graphql_non_json_or_missing_fields"


def _run_native_relationship_step(state: "TxnState", relationship_input: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Execute Required Transaction Order steps 1/2/3/4/5/6/7 (native
    relationship capability preflight, pre-readback, expected_before drift
    check, graph invariant validation, and the sequenced mutation+readback
    saga delegated to the issue_relationship.update controlled executor
    command). Returns (proceed_to_content, result_block); proceed_to_content
    is True only for status in {"no_op", "applied"} (AC1/AC2: any other
    outcome must block the subsequent content mutation).
    """
    cap_ok, cap_err = _relationship_capability_preflight()
    if not cap_ok:
        return False, {
            **DEFAULT_NATIVE_RESULT,
            "status": "failed_no_mutation",
            "errors": [{"code": "relationship_capability_preflight_failed", "message": cap_err}],
        }

    live_before, live_err = _fetch_native_relationships(state.issue_number, state.repo)
    if live_before is None:
        return False, {
            **DEFAULT_NATIVE_RESULT,
            "status": "failed_no_mutation",
            "errors": [{"code": "relationship_pre_readback_failed", "message": live_err}],
        }

    expected_before = relationship_input.get("expected_before")
    if expected_before is None:
        expected_before_normalized = live_before
    else:
        expected_before_normalized = {
            "parent": expected_before.get("parent"),
            "blocked_by": sorted(expected_before.get("blocked_by", [])),
            "blocking": sorted(expected_before.get("blocking", [])),
        }
        if expected_before_normalized != live_before:
            return False, {
                **DEFAULT_NATIVE_RESULT,
                "status": "failed_no_mutation",
                "before": live_before,
                "errors": [
                    {
                        "code": "expected_before_drift_detected",
                        "message": "native_relationships.expected_before does not match live pre-readback",
                    }
                ],
            }

    parent_action = relationship_input.get("parent") or {"action": "unchanged", "issue_number": None}
    add_bb = sorted(set(relationship_input.get("add_blocked_by", [])))
    rm_bb = sorted(set(relationship_input.get("remove_blocked_by", [])))
    add_bl = sorted(set(relationship_input.get("add_blocking", [])))
    rm_bl = sorted(set(relationship_input.get("remove_blocking", [])))

    idempotency_key = (
        f"{state.repo}:{state.issue_number}:relationship:"
        f"{parent_action.get('action')}:{parent_action.get('issue_number')}:{add_bb}:{rm_bb}:{add_bl}:{rm_bl}"
    )
    executor_payload = {
        "schema": "ISSUE_RELATIONSHIP_UPDATE_INPUT_V1",
        "issue_number": state.issue_number,
        "repo": state.repo,
        "expected_before": expected_before_normalized,
        "parent": {
            "action": parent_action.get("action", "unchanged"),
            "issue_number": parent_action.get("issue_number"),
        },
        "add_blocked_by": add_bb,
        "remove_blocked_by": rm_bb,
        "add_blocking": add_bl,
        "remove_blocking": rm_bl,
        "idempotency_key": idempotency_key,
    }

    field_err = _validate_relationship_graph_invariants(executor_payload, state.issue_number, state.repo)
    if field_err:
        return False, {
            **DEFAULT_NATIVE_RESULT,
            "status": "failed_no_mutation",
            "before": expected_before_normalized,
            "errors": [{"code": "graph_invariant_violation", "message": field_err}],
        }

    input_ref = _write_issue_metadata_input(state.issue_number, "issue_relationship.update", executor_payload)
    state.relationship_input_ref = input_ref
    cp, result = _invoke_controlled_exec("issue_relationship.update", state.issue_number, state.repo, input_ref)
    parsed = result or {}
    exec_status = parsed.get("status")
    mutation_attempted = bool(parsed.get("mutation_attempted", False))

    if cp.returncode == 0 and exec_status in ("no_op", "applied"):
        return True, {
            "attempted": mutation_attempted,
            "status": exec_status,
            "before": parsed.get("before", expected_before_normalized),
            "desired": parsed.get("desired"),
            "after": parsed.get("after", parsed.get("before", expected_before_normalized)),
            "completed_operations": parsed.get("completed_operations", []),
            "pending_operations": parsed.get("pending_operations", []),
            "errors": [],
        }

    overall_status = "failed_after_mutation" if mutation_attempted else "failed_no_mutation"
    return False, {
        "attempted": mutation_attempted,
        "status": overall_status,
        "before": parsed.get("before", expected_before_normalized),
        "desired": parsed.get("desired"),
        "after": parsed.get("after"),
        "completed_operations": parsed.get("completed_operations", []),
        "pending_operations": parsed.get("pending_operations", []),
        "errors": [_child_error(cp, "issue_relationship_update_failed")],
    }


def _safe_repo_file(relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise ValueError("path_must_be_relative")
    normalized = Path(os.path.normpath(relative_path))
    if not normalized.parts:
        raise ValueError("path_not_found")
    if ".." in normalized.parts:
        raise ValueError("path_must_not_escape_repo")
    repo_root = REPO_ROOT.resolve()
    resolved_cursor = repo_root
    final_lstat = None
    for part in normalized.parts:
        resolved_cursor = resolved_cursor / part
        try:
            st = resolved_cursor.lstat()
        except FileNotFoundError:
            raise ValueError("path_not_found")
        except OSError as exc:
            raise ValueError(f"path_lstat_error: {exc}") from exc
        final_lstat = st
        if resolved_cursor.is_symlink():
            raise ValueError(f"symlink_not_allowed: {resolved_cursor}")
    try:
        resolved = resolved_cursor.resolve()
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError("path_must_resolve_under_repo") from exc
    except OSError as exc:
        raise ValueError(f"path_resolve_error: {exc}") from exc
    if final_lstat is None or not resolved.is_file():
        raise ValueError("path_not_file")
    if final_lstat.st_nlink != 1:
        raise ValueError("hardlink_not_allowed")
    try:
        resolved.stat()
    except OSError:
        raise ValueError("path_not_found")
    return resolved


def _parse_controlled_exec_output(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _read_text_file(relative_path: str) -> str:
    return _safe_repo_file(relative_path).read_text(encoding="utf-8")


def _run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            shell=False,
            cwd=str(REPO_ROOT),
            timeout=CONTROLLED_EXEC_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args,
            124,
            stdout="",
            stderr=(
                f"child command timeout after {int(exc.timeout)}s"
                if exc.timeout is not None
                else "child command timeout"
            ),
        )


def _child_error(cp: subprocess.CompletedProcess[str], code: str) -> dict[str, str]:
    detail = cp.stderr.strip() or cp.stdout.strip()
    return {"code": code, "message": _bounded(detail or f"returncode={cp.returncode}", MAX_CHILD_SNIPPET)}


def _fetch_issue(issue_number: int, repo: str) -> tuple[dict[str, Any] | None, str]:
    gh = shutil.which("gh") or "gh"
    cp = _run_command(
        [gh, "issue", "view", str(issue_number), "--repo", repo, "--json", "title,body,updatedAt"]
    )
    if cp.returncode != 0:
        return None, _bounded(cp.stderr.strip() or cp.stdout.strip())
    try:
        return json.loads(cp.stdout), ""
    except json.JSONDecodeError:
        return None, "gh_issue_view_non_json"


def _render_result(
    *,
    status: str,
    issue_number: int | None,
    repo: str | None,
    mutation_started: bool,
    body_attempted: bool,
    body_status: str,
    comment_attempted: bool,
    comment_status: str,
    comment_id: str | None,
    comment_url: str | None,
    comment_body_sha256: str | None,
    previous_body_sha256: str | None,
    requested_new_body_sha256: str | None,
    remote_current_body_sha256: str | None,
    body_input_ref: str | None,
    comment_input_ref: str | None,
    errors: list[dict[str, str]],
    previous_title: str | None = None,
    requested_title: str | None = None,
    remote_current_title: str | None = None,
    patch_attempted: bool = False,
    mutation_outcome: str = "not_attempted",
    native_relationships: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "status": status,
        "issue_number": issue_number,
        "repo": repo,
        "mutation_started": mutation_started,
        "rollback_attempted": False,
        "body_update": {
            "attempted": body_attempted,
            "status": body_status,
            "previous_body_sha256": previous_body_sha256,
            "new_body_sha256": requested_new_body_sha256,
            "remote_current_body_sha256": remote_current_body_sha256,
            "artifact_ref": body_input_ref,
        },
        "content_update": {
            "previous_title": previous_title,
            "requested_title": requested_title,
            "remote_current_title": remote_current_title,
            "patch_attempted": patch_attempted,
            "mutation_outcome": mutation_outcome,
        },
        "comment_publish": {
            "attempted": comment_attempted,
            "status": comment_status,
            "comment_id": comment_id,
            "comment_url": comment_url,
            "comment_body_sha256": comment_body_sha256,
            "artifact_ref": comment_input_ref,
        },
        "errors": _truncate_errors(errors),
        "native_relationships": dict(native_relationships) if native_relationships else dict(DEFAULT_NATIVE_RESULT),
    }


def _write_issue_metadata_input(issue_number: int, command_id: str, payload: dict[str, Any]) -> str:
    txn_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}"
    relative = Path("artifacts") / str(issue_number) / "issue-metadata" / command_id / f"{txn_id}.input.json"
    absolute = REPO_ROOT / relative
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return str(relative).replace("\\", "/")


def _invoke_controlled_exec(
    command_id: str,
    issue_number: int,
    repo: str,
    input_ref: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    cp = _run_command(
        [
            sys.executable,
            str(CONTROLLED_EXEC),
            "--command-id",
            command_id,
            "--issue-number",
            str(issue_number),
            "--input-file",
            input_ref,
            "--repo",
            repo,
            "--json",
        ]
    )
    return cp, _parse_controlled_exec_output(cp.stdout)


def _extract_comment_publish_result(
    result: dict[str, Any] | None,
) -> tuple[str | None, str | None, str | None]:
    if not result:
        return None, None, None
    comment_id = result.get("comment_id")
    comment_url = result.get("comment_url")
    comment_body_sha256 = result.get("body_sha256")
    normalized_comment_id = comment_id if isinstance(comment_id, str) and comment_id else None
    normalized_comment_url = comment_url if isinstance(comment_url, str) and comment_url else None
    normalized_comment_body_sha256 = (
        comment_body_sha256 if isinstance(comment_body_sha256, str) and comment_body_sha256 else None
    )
    return normalized_comment_id, normalized_comment_url, normalized_comment_body_sha256


def _run_comment_publish(
    state: TxnState,
    comment_mode: dict[str, Any],
) -> bool:
    if comment_mode.get("mode") != "publish":
        return True

    state.comment_attempted = True
    comment_body = _read_text_file(comment_mode["comment_body_file"])
    marker = comment_mode["marker"]
    if marker not in comment_body:
        state.comment_status = "failed"
        state.errors.append(
            {
                "code": "comment_marker_not_embedded_in_body",
                "message": "comment body must contain marker before executor invocation",
            }
        )
        return False

    comment_input = {
        "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
        "issue_number": state.issue_number,
        "comment_body": comment_body,
        "marker": marker,
    }
    state.comment_input_ref = _write_issue_metadata_input(
        state.issue_number, "issue_comment.publish", comment_input
    )
    comment_cp, comment_result = _invoke_controlled_exec(
        "issue_comment.publish", state.issue_number, state.repo, state.comment_input_ref
    )
    state.comment_id, state.comment_url, state.comment_body_sha256 = _extract_comment_publish_result(comment_result)
    if comment_cp.returncode != 0:
        state.comment_status = "failed"
        state.errors.append(_child_error(comment_cp, "issue_comment_publish_failed"))
        return False

    state.comment_status = "ok"
    return True


@dataclass
class TxnState:
    issue_number: int
    repo: str
    previous_body_sha256: str | None = None
    requested_new_body_sha256: str | None = None
    remote_current_body_sha256: str | None = None
    mutation_started: bool = False
    body_attempted: bool = False
    body_status: str = "not_run"
    comment_attempted: bool = False
    comment_status: str = "not_run"
    comment_id: str | None = None
    comment_url: str | None = None
    comment_body_sha256: str | None = None
    body_input_ref: str | None = None
    comment_input_ref: str | None = None
    relationship_input_ref: str | None = None
    errors: list[dict[str, str]] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def run_transaction(input_data: dict[str, Any]) -> dict[str, Any]:
    _validate_input_payload(input_data)
    state = TxnState(issue_number=input_data["issue_number"], repo=input_data["repo"])
    title_update = input_data.get("title_update") or {"required": False}
    readiness_result = input_data["readiness_forwarding_payload"]["readiness_result"]
    forwarded_status = readiness_result["status"]
    if forwarded_status in {"human_judgment", "input_or_runtime_error"}:
        state.errors.append(
            {
                "code": "readiness_forwarding_requires_human_judgment",
                "message": f"forwarded readiness status={forwarded_status}",
            }
        )
        return _render_result(
            status="human_judgment",
            issue_number=state.issue_number,
            repo=state.repo,
            mutation_started=False,
            body_attempted=False,
            body_status="not_run",
            comment_attempted=False,
            comment_status="not_run",
            previous_body_sha256=None,
            requested_new_body_sha256=None,
            remote_current_body_sha256=None,
            body_input_ref=None,
            comment_input_ref=None,
            comment_id=None,
            comment_url=None,
            comment_body_sha256=None,
            errors=state.errors,
        )

    if forwarded_status == "needs_fix" and not readiness_result.get("resolution_evidence"):
        state.errors.append(
            {
                "code": "readiness_needs_fix_without_resolution_evidence",
                "message": "forwarded readiness status=needs_fix without resolution_evidence",
            }
        )
        return _render_result(
            status="failed_no_mutation",
            issue_number=state.issue_number,
            repo=state.repo,
            mutation_started=False,
            body_attempted=False,
            body_status="not_run",
            comment_attempted=False,
            comment_status="not_run",
            previous_body_sha256=None,
            requested_new_body_sha256=None,
            remote_current_body_sha256=None,
            body_input_ref=None,
            comment_input_ref=None,
            comment_id=None,
            comment_url=None,
            comment_body_sha256=None,
            errors=state.errors,
        )

    issue_data, issue_error = _fetch_issue(state.issue_number, state.repo)
    if issue_data is None:
        state.errors.append({"code": "issue_readback_failed", "message": issue_error})
        return _render_result(
            status="failed_no_mutation",
            issue_number=state.issue_number,
            repo=state.repo,
            mutation_started=False,
            body_attempted=False,
            body_status="not_run",
            comment_attempted=False,
            comment_status="not_run",
            previous_body_sha256=None,
            requested_new_body_sha256=None,
            remote_current_body_sha256=None,
            body_input_ref=None,
            comment_input_ref=None,
            comment_id=None,
            comment_url=None,
            comment_body_sha256=None,
            errors=state.errors,
        )

    current_title = issue_data.get("title", "")
    current_body = issue_data.get("body", "")
    current_updated_at = issue_data.get("updatedAt", "")
    current_sha = _sha256_text(current_body)
    state.previous_body_sha256 = current_sha
    state.remote_current_body_sha256 = current_sha
    requested_title = title_update.get("proposed_title") if title_update.get("required") else current_title
    operation_reason = title_update.get("reason") if title_update.get("required") else "issue_body_update"
    if (
        current_sha != input_data["expected_previous_body_sha256"]
        or current_updated_at != input_data["expected_previous_updated_at"]
    ):
        state.errors.append(
            {
                "code": "stale_precondition_before_mutation",
                "message": "remote body sha or updatedAt changed before controlled executor invocation",
            }
        )
        return _render_result(
            status="failed_no_mutation",
            issue_number=state.issue_number,
            repo=state.repo,
            mutation_started=False,
            body_attempted=False,
            body_status="not_run",
            comment_attempted=False,
            comment_status="not_run",
            previous_body_sha256=current_sha,
            requested_new_body_sha256=None,
            remote_current_body_sha256=current_sha,
            body_input_ref=None,
            comment_input_ref=None,
            comment_id=None,
            comment_url=None,
            comment_body_sha256=None,
            errors=state.errors,
        )

    # Issue #1883 Required Transaction Order steps 1-7: native relationship
    # capability preflight, pre-readback, expected_before drift check, graph
    # invariant validation, sequenced mutation, and full post-readback all
    # happen here -- strictly before any title/body content mutation is
    # attempted (AC1/AC2). native_relationships is an additive, optional
    # ISSUE_EDIT_TXN_INPUT_V1 field; omitting it preserves pre-#1883 behaviour
    # exactly.
    relationship_input = input_data.get("native_relationships")
    rel_result_for_output: dict[str, Any] | None = None
    if relationship_input is not None:
        rel_ok, rel_result_for_output = _run_native_relationship_step(state, relationship_input)
        if not rel_ok:
            return _render_result(
                native_relationships=rel_result_for_output,
                status=rel_result_for_output["status"],
                issue_number=state.issue_number,
                repo=state.repo,
                mutation_started=bool(rel_result_for_output.get("attempted")),
                body_attempted=False,
                body_status="not_run",
                comment_attempted=False,
                comment_status="not_run",
                previous_body_sha256=current_sha,
                requested_new_body_sha256=None,
                remote_current_body_sha256=current_sha,
                body_input_ref=None,
                comment_input_ref=None,
                comment_id=None,
                comment_url=None,
                comment_body_sha256=None,
                errors=list(state.errors) + list(rel_result_for_output.get("errors", [])),
            )

    new_body = _read_text_file(input_data["new_body_file"])
    body_change_requested = new_body != current_body
    requested_new_sha = _sha256_text(new_body)
    state.requested_new_body_sha256 = requested_new_sha
    comment_mode = input_data.get("comment_mode", {"mode": "skip"})
    if requested_new_sha == current_sha and requested_title == current_title and comment_mode.get("mode") == "publish":
        if not _run_comment_publish(state, comment_mode):
            return _render_result(
                native_relationships=rel_result_for_output,
                status="failed_after_mutation",
                issue_number=state.issue_number,
                repo=state.repo,
                mutation_started=state.comment_attempted,
                body_attempted=False,
                body_status="not_run",
                comment_attempted=state.comment_attempted,
                comment_status=state.comment_status,
                previous_body_sha256=current_sha,
                requested_new_body_sha256=requested_new_sha,
                remote_current_body_sha256=current_sha,
                body_input_ref=None,
                comment_input_ref=state.comment_input_ref,
                comment_id=state.comment_id,
                comment_url=state.comment_url,
                comment_body_sha256=state.comment_body_sha256,
                errors=state.errors,
            )

        return _render_result(
                native_relationships=rel_result_for_output,
            status="ok",
            issue_number=state.issue_number,
            repo=state.repo,
            mutation_started=True,
            body_attempted=False,
            body_status="not_run",
            comment_attempted=True,
            comment_status="ok",
            previous_body_sha256=current_sha,
            requested_new_body_sha256=requested_new_sha,
            remote_current_body_sha256=current_sha,
            body_input_ref=None,
            comment_input_ref=state.comment_input_ref,
            comment_id=state.comment_id,
            comment_url=state.comment_url,
            comment_body_sha256=state.comment_body_sha256,
            errors=[],
        )

    if (
        requested_new_sha == current_sha
        and requested_title == current_title
        and comment_mode.get("mode", "skip") == "skip"
    ):
        return _render_result(
                native_relationships=rel_result_for_output,
            status="no_change",
            issue_number=state.issue_number,
            repo=state.repo,
            mutation_started=False,
            body_attempted=False,
            body_status="not_run",
            comment_attempted=False,
            comment_status="not_run",
            previous_body_sha256=current_sha,
            requested_new_body_sha256=requested_new_sha,
            remote_current_body_sha256=current_sha,
            body_input_ref=None,
            comment_input_ref=None,
            comment_id=None,
            comment_url=None,
            comment_body_sha256=None,
            errors=[],
        )

    tmp_dir = REPO_ROOT / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        delete=False,
        dir=str(tmp_dir),
        encoding="utf-8",
    ) as tmp_body:
        tmp_body.write(new_body)
        candidate_path = Path(tmp_body.name)

    try:
        if body_change_requested:
            guard_cp = _run_command([sys.executable, str(GUARD_SCRIPT), str(candidate_path), "--format", "json"])
            if guard_cp.returncode != 0:
                state.errors.append(_child_error(guard_cp, "guard_or_readiness_failed_before_mutation"))
                return _render_result(
                native_relationships=rel_result_for_output,
                    status="failed_no_mutation", issue_number=state.issue_number, repo=state.repo,
                    mutation_started=False, body_attempted=False, body_status="not_run",
                    comment_attempted=False, comment_status="not_run", previous_body_sha256=current_sha,
                    requested_new_body_sha256=requested_new_sha, remote_current_body_sha256=current_sha,
                    body_input_ref=None, comment_input_ref=None, comment_id=None, comment_url=None,
                    comment_body_sha256=None, errors=state.errors,
                )
            hygiene_cp = _run_command(
                [
                    sys.executable,
                    str(HYGIENE_SCRIPT),
                    "--body-file",
                    str(candidate_path),
                    "--out-file",
                    str(candidate_path),
                ]
            )
            if hygiene_cp.returncode not in (0, 1, 2):
                state.errors.append(_child_error(hygiene_cp, "issue_contract_hygiene_runtime_error"))
                return _render_result(
                native_relationships=rel_result_for_output,
                    status="failed_no_mutation", issue_number=state.issue_number, repo=state.repo,
                    mutation_started=False, body_attempted=False, body_status="not_run",
                    comment_attempted=False, comment_status="not_run", previous_body_sha256=current_sha,
                    requested_new_body_sha256=requested_new_sha, remote_current_body_sha256=current_sha,
                    body_input_ref=None, comment_input_ref=None, comment_id=None, comment_url=None,
                    comment_body_sha256=None, errors=state.errors,
                )
            mutated_candidate = candidate_path.read_text(encoding="utf-8")
            readiness_cp = _run_command(
                [sys.executable, str(READINESS_SCRIPT), "--body-file", str(candidate_path), "--mode", "static"]
            )
            if readiness_cp.returncode != 0:
                state.errors.append(_child_error(readiness_cp, "guard_or_readiness_failed_before_mutation"))
                return _render_result(
                native_relationships=rel_result_for_output,
                    status="failed_no_mutation", issue_number=state.issue_number, repo=state.repo,
                    mutation_started=False, body_attempted=False, body_status="not_run",
                    comment_attempted=False, comment_status="not_run", previous_body_sha256=current_sha,
                    requested_new_body_sha256=_sha256_text(mutated_candidate), remote_current_body_sha256=current_sha,
                    body_input_ref=None, comment_input_ref=None, comment_id=None, comment_url=None,
                    comment_body_sha256=None, errors=state.errors,
                )
        else:
            # A title-only request must preserve even a noncanonical body exactly.
            mutated_candidate = current_body

        requested_new_sha = _sha256_text(mutated_candidate)
        state.requested_new_body_sha256 = requested_new_sha
        if requested_new_sha == current_sha and requested_title == current_title:
            return _render_result(
                native_relationships=rel_result_for_output,
                status="no_change", issue_number=state.issue_number, repo=state.repo,
                mutation_started=False, body_attempted=False, body_status="not_run",
                comment_attempted=False, comment_status="not_run", previous_body_sha256=current_sha,
                requested_new_body_sha256=requested_new_sha, remote_current_body_sha256=current_sha,
                body_input_ref=None, comment_input_ref=None, comment_id=None, comment_url=None,
                comment_body_sha256=None, errors=[], previous_title=current_title,
                requested_title=requested_title, remote_current_title=current_title,
                patch_attempted=False, mutation_outcome="no_change",
            )

        body_input = {
            "schema": "ISSUE_CONTENT_UPDATE_INPUT_V1",
            "issue_number": state.issue_number,
            "repo": state.repo,
            "expected_previous_title": current_title,
            "expected_previous_body_sha256": current_sha,
            "expected_previous_updated_at": current_updated_at,
            "new_title": requested_title,
            "new_body": mutated_candidate,
            "new_body_sha256": requested_new_sha,
            "operation_reason": operation_reason,
            "idempotency_key": f"{state.repo}:{state.issue_number}:{current_sha}:{requested_new_sha}:{requested_title}",
        }
        state.body_input_ref = _write_issue_metadata_input(state.issue_number, "issue_content.update", body_input)
        state.body_attempted = True
        body_cp, body_result = _invoke_controlled_exec(
            "issue_content.update", state.issue_number, state.repo, state.body_input_ref
        )
        if body_cp.returncode != 0:
            refreshed_issue, _ = _fetch_issue(state.issue_number, state.repo)
            refreshed_body = (refreshed_issue or {}).get("body", current_body)
            refreshed_title = (refreshed_issue or {}).get("title", current_title)
            refreshed_sha = _sha256_text(refreshed_body)
            state.remote_current_body_sha256 = refreshed_sha
            state.body_status = "failed"
            state.errors.append(_child_error(body_cp, "issue_body_update_failed"))
            if refreshed_sha == requested_new_sha and refreshed_title == requested_title:
                state.mutation_started = True
                return _render_result(
                native_relationships=rel_result_for_output,
                    status="failed_after_mutation",
                    issue_number=state.issue_number,
                    repo=state.repo,
                    mutation_started=True,
                    body_attempted=True,
                    body_status="failed",
                    comment_attempted=False,
                    comment_status="not_run",
                    previous_body_sha256=current_sha,
                    requested_new_body_sha256=requested_new_sha,
                    remote_current_body_sha256=refreshed_sha,
                    body_input_ref=state.body_input_ref,
                    comment_input_ref=None,
                    comment_id=None,
                    comment_url=None,
                    comment_body_sha256=None,
                    errors=state.errors,
                )
            return _render_result(
                native_relationships=rel_result_for_output,
                status="mutation_outcome_unknown",
                issue_number=state.issue_number,
                repo=state.repo,
                mutation_started=False,
                body_attempted=True,
                body_status="failed",
                comment_attempted=False,
                comment_status="not_run",
                previous_body_sha256=current_sha,
                requested_new_body_sha256=requested_new_sha,
                remote_current_body_sha256=refreshed_sha,
                body_input_ref=state.body_input_ref,
                comment_input_ref=None,
                comment_id=None,
                comment_url=None,
                comment_body_sha256=None,
                errors=state.errors,
                previous_title=current_title,
                requested_title=requested_title,
                remote_current_title=refreshed_title if isinstance(refreshed_title, str) else None,
                patch_attempted=True,
                mutation_outcome="unknown",
            )

        state.mutation_started = True
        state.body_status = "ok"
        body_result_sha = requested_new_sha
        if body_result is not None:
            parsed_sha = body_result.get("new_body_sha256")
            if isinstance(parsed_sha, str) and parsed_sha:
                body_result_sha = parsed_sha
                state.requested_new_body_sha256 = parsed_sha

        final_issue, final_error = _fetch_issue(state.issue_number, state.repo)
        if final_issue is None:
            state.errors.append({"code": "final_readback_failed", "message": final_error})
            return _render_result(
                native_relationships=rel_result_for_output,
                status="failed_after_mutation",
                issue_number=state.issue_number,
                repo=state.repo,
                mutation_started=True,
                body_attempted=True,
                body_status="ok",
                comment_attempted=False,
                comment_status="not_run",
                previous_body_sha256=current_sha,
                requested_new_body_sha256=body_result_sha,
                remote_current_body_sha256=None,
                body_input_ref=state.body_input_ref,
                comment_input_ref=None,
                comment_id=None,
                comment_url=None,
                comment_body_sha256=None,
                errors=state.errors,
            )

        final_sha = _sha256_text(final_issue.get("body", ""))
        final_title = final_issue.get("title", "")
        state.remote_current_body_sha256 = final_sha
        if final_sha != requested_new_sha or final_title != requested_title:
            state.errors.append(
                {
                    "code": "final_readback_content_mismatch",
                    "message": (
                        "controlled content update completed but final readback "
                        "did not match requested title/body"
                    ),
                }
            )
            return _render_result(
                native_relationships=rel_result_for_output,
                status="failed_after_mutation",
                issue_number=state.issue_number,
                repo=state.repo,
                mutation_started=True,
                body_attempted=True,
                body_status="ok",
                comment_attempted=False,
                comment_status="not_run",
                previous_body_sha256=current_sha,
                requested_new_body_sha256=body_result_sha,
                remote_current_body_sha256=final_sha,
                body_input_ref=state.body_input_ref,
                comment_input_ref=None,
                comment_id=None,
                comment_url=None,
                comment_body_sha256=None,
                errors=state.errors,
            )

        if comment_mode.get("mode") == "publish" and not _run_comment_publish(state, comment_mode):
            return _render_result(
                native_relationships=rel_result_for_output,
                status="failed_after_mutation",
                issue_number=state.issue_number,
                repo=state.repo,
                mutation_started=True,
                body_attempted=True,
                body_status="ok",
                comment_attempted=state.comment_attempted,
                comment_status=state.comment_status,
                previous_body_sha256=current_sha,
                requested_new_body_sha256=body_result_sha,
                remote_current_body_sha256=final_sha,
                body_input_ref=state.body_input_ref,
                comment_input_ref=state.comment_input_ref,
                comment_id=state.comment_id,
                comment_url=state.comment_url,
                comment_body_sha256=state.comment_body_sha256,
                errors=state.errors,
            )

        return _render_result(
                native_relationships=rel_result_for_output,
            status="ok",
            issue_number=state.issue_number,
            repo=state.repo,
            mutation_started=True,
            body_attempted=True,
            body_status=state.body_status,
            comment_attempted=state.comment_attempted,
            comment_status=state.comment_status,
            previous_body_sha256=current_sha,
            requested_new_body_sha256=body_result_sha,
            remote_current_body_sha256=state.remote_current_body_sha256,
            body_input_ref=state.body_input_ref,
            comment_input_ref=state.comment_input_ref,
            comment_id=state.comment_id,
            comment_url=state.comment_url,
            comment_body_sha256=state.comment_body_sha256,
            errors=[],
            previous_title=current_title,
            requested_title=requested_title,
            remote_current_title=final_title if isinstance(final_title, str) else None,
            patch_attempted=True,
            mutation_outcome="applied",
        )
    finally:
        try:
            candidate_path.unlink()
        except OSError:
            pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run issue edit transaction helper")
    parser.add_argument("--input-file", required=True, help="repo-relative ISSUE_EDIT_TXN_INPUT_V1 JSON path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        input_text = _read_text_file(args.input_file)
        input_data = json.loads(input_text)
        if not isinstance(input_data, dict):
            raise ValueError("input_json_must_be_object")
        result = run_transaction(input_data)
        exit_code = 0 if result["status"] in {"ok", "no_change"} else 1
    except Exception as exc:
        result = _render_result(
            status="failed_no_mutation",
            issue_number=None,
            repo=None,
            mutation_started=False,
            body_attempted=False,
            body_status="not_run",
            comment_attempted=False,
            comment_status="not_run",
            previous_body_sha256=None,
            requested_new_body_sha256=None,
            remote_current_body_sha256=None,
            body_input_ref=None,
            comment_input_ref=None,
            comment_id=None,
            comment_url=None,
            comment_body_sha256=None,
            errors=[{"code": "txn_input_or_runtime_error", "message": str(exc)}],
        )
        exit_code = 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
