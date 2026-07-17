#!/usr/bin/env python3
"""Build and emit IMPL_REVIEW_INTAKE_CAPSULE_V1 for impl-review-loop."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[3]
_DEFAULT_REPO = "squne121/loop-protocol"
_DEFAULT_MAX_STDOUT_BYTES = 4096
_DEFAULT_SAMPLE_PATHS = 10
_SCHEMA_NAME = "IMPL_REVIEW_INTAKE_CAPSULE_V1"
_SCHEMA_VERSION = 1
_DEFAULT_ARTIFACT_DIR = _REPO_ROOT / "artifacts" / "impl-review-loop"
_TRIAGE_SCHEMA = "CONTRACT_BLOCKER_TRIAGE_V1"
_TRIAGE_PATH = _SCRIPT_DIR / "triage_contract_blockers.py"
# #1475: shared GitHub provenance trust policy (single source of truth).
_CRP_PATH = (
    _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"
    / "contract_review_result_parser.py"
)
_BASELINE_PREFLIGHT_PATH = (
    _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"
    / "baseline_vc_preflight.py"
)
_ALLOWED_PATHS_GATE_PATH = (
    _REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "scripts"
    / "allowed_paths_review_gate.py"
)
_FENCED_YAML_RE = re.compile(r"```ya?ml[ \t]*\n(.*?)```", re.DOTALL)
_CONTRACT_REVIEW_MARKER = "CONTRACT_REVIEW_RESULT_V1"


def _now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _run_command(cmd: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(
        cmd,
        cwd=str(_REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def _record_command(
    command_log: list[dict[str, Any]],
    name: str,
    argv: list[str],
    rc: int,
    stdout: str,
    stderr: str,
) -> None:
    command_log.append(
        {
            "name": name,
            "argv_sha256": _sha256(json.dumps(argv, ensure_ascii=False)),
            "exit_code": rc,
            "stdout_sha256": _sha256(stdout),
            "stderr_sha256": _sha256(stderr),
            "collected_at": _now_utc(),
        }
    )


def _safe_load_json(payload: str) -> dict[str, Any] | None:
    try:
        loaded = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if isinstance(loaded, dict):
        return loaded
    return None


def _slugify(text: str) -> str:
    normalized = text.lower()
    parts = re.split(r"[:：]", normalized, maxsplit=1)
    body = parts[1] if len(parts) == 2 else parts[0]
    body = re.sub(r"[^a-z0-9]+", "-", body.strip())
    while "--" in body:
        body = body.replace("--", "-")
    return body.strip("-")[:40] or "issue"


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot_load_module:{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _parse_simple_yaml_block(block: str) -> dict[str, Any]:
    try:
        import yaml

        parsed = yaml.safe_load(block)
        if isinstance(parsed, dict):
            return parsed
        return {}
    except Exception:
        pass

    result: dict[str, Any] = {}
    lines = block.splitlines()
    current_key: str | None = None

    for line in lines:
        stripped = line.rstrip()
        if not stripped or stripped.lstrip().startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())
        if indent == 0:
            current_key = None
            match = re.match(r"^(\S[^:]*?):\s*(.*)", stripped)
            if not match:
                continue
            key = match.group(1).strip()
            value = match.group(2).strip()
            if value:
                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]
                result[key] = value
            else:
                result[key] = None
                current_key = key
            continue

        if current_key is None:
            continue
        submatch = re.match(r"^\s+(\S[^:]*?):\s*(.*)", stripped)
        if not submatch:
            continue
        subkey = submatch.group(1).strip()
        subvalue = submatch.group(2).strip()
        if (subvalue.startswith('"') and subvalue.endswith('"')) or (
            subvalue.startswith("'") and subvalue.endswith("'")
        ):
            subvalue = subvalue[1:-1]
        current = result.get(current_key)
        if not isinstance(current, dict):
            current = {}
            result[current_key] = current
        current[subkey] = subvalue or None

    return result


def _extract_yaml_blocks(body: str) -> list[str]:
    return [match.group(1) for match in _FENCED_YAML_RE.finditer(body)]


def _is_trusted_snapshot_author(
    author: Any,
    author_association: Any,
    author_id: Any = None,
    author_type: Any = None,
) -> bool:
    """#1475: delegate to the shared trust policy (single source of truth)."""
    module = _load_module(_CRP_PATH, "contract_review_result_parser_for_intake_capsule")
    return bool(
        module.is_trusted_snapshot_author(
            author,
            author_association,
            author_id=author_id,
            author_type=author_type,
        )
    )


def _is_fingerprint_ready(
    inner: dict[str, Any],
    comment_id: Any,
    issue_number: int | None,
) -> bool:
    """#1537: delegate to the shared fingerprint-readiness policy (single
    source of truth) -- same rule contract_review_result_parser.py and
    ensure_contract_snapshot.py use for the same expected_contract_fingerprint
    schema."""
    module = _load_module(_CRP_PATH, "contract_review_result_parser_for_intake_capsule")
    return bool(module.is_fingerprint_ready_go(inner, comment_id, issue_number))


def _live_allowed_paths_hash(body: str) -> str | None:
    """Compute the reviewer-compatible hash from the live Issue body."""
    baseline = _load_module(_BASELINE_PREFLIGHT_PATH, "baseline_allowed_paths_for_intake")
    gate = _load_module(_ALLOWED_PATHS_GATE_PATH, "allowed_paths_gate_for_intake")
    paths = baseline.extract_allowed_paths(body)
    if not isinstance(paths, list) or not paths:
        return None
    canonicalized: list[str] = []
    for path in paths:
        normalized = gate.AllowedPathsMatcher.normalize_allowed_pattern(path)
        if normalized is None:
            return None
        canonicalized.append(normalized)
    if not canonicalized:
        return None
    return hashlib.sha256(
        json.dumps(sorted(set(canonicalized)), separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


_ISSUE_NUMBER_FROM_URL_RE = re.compile(r"/issues/(\d+)\Z")


def _issue_number_from_url(issue_url: str) -> int | None:
    m = _ISSUE_NUMBER_FROM_URL_RE.search(issue_url or "")
    return int(m.group(1)) if m else None


def _is_valid_contract_review_result(block: dict[str, Any], expected_issue_url: str) -> bool:
    inner = block.get(_CONTRACT_REVIEW_MARKER)
    if not isinstance(inner, dict):
        return False
    if inner.get("status") not in {"go", "blocked"}:
        return False
    if inner.get("generated_by") != "issue-contract-review":
        return False
    if not inner.get("generated_at"):
        return False
    return inner.get("issue_url") == expected_issue_url


def _collect_issue_metadata(
    issue_number: int,
    repo: str,
    command_log: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    argv = [
        "gh",
        "issue",
        "view",
        str(issue_number),
        "--repo",
        repo,
        "--json",
        "title,state,labels,body,updatedAt",
    ]
    rc, stdout, stderr = _run_command(argv)
    _record_command(command_log, "issue_view", argv, rc, stdout, stderr)
    if rc != 0:
        return {"error": "gh_issue_view_failed", "stderr": stderr.strip()}, ["issue_view_failed"]

    payload = _safe_load_json(stdout)
    if payload is None:
        return {"error": "gh_issue_view_invalid_json", "stderr": stderr.strip()}, ["issue_view_invalid_json"]

    title = str(payload.get("title") or "")
    labels = payload.get("labels") or []
    label_names = [str(item.get("name") or "").lower() for item in labels if isinstance(item, dict)]
    body = str(payload.get("body") or "")
    updated_at = str(payload.get("updatedAt") or "")
    title_prefix_ok = title.startswith("実装:") or title.startswith("implement:")
    phase_label_ok = "phase/implementation" in label_names
    issue_state_open = str(payload.get("state") or "").lower() == "open"
    ready_status = "pass" if all([title_prefix_ok, phase_label_ok, issue_state_open]) else "failed"

    return {
        "title": title,
        "issue_url": f"https://github.com/{repo}/issues/{issue_number}",
        "body_sha256": _sha256(body),
        "body": body,
        "updated_at": updated_at,
        "ready_tuple": {
            "title_prefix_ok": title_prefix_ok,
            "phase_label_present": phase_label_ok,
            "issue_state_open": issue_state_open,
            "status": ready_status,
        },
    }, []


def _collect_issue_comments(
    issue_number: int,
    repo: str,
    command_log: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    argv = [
        "gh",
        "api",
        "--paginate",
        f"repos/{repo}/issues/{issue_number}/comments?per_page=100",
        "--jq",
        ".[] | {id, html_url, created_at, updated_at, body, "
        "author: .user.login, author_id: .user.id, "
        "author_type: .user.type, author_association}",
    ]
    rc, stdout, stderr = _run_command(argv)
    _record_command(command_log, "issue_comments", argv, rc, stdout, stderr)
    if rc != 0:
        return [], {"invalid_json_lines_count": 0}, ["comments_fetch_error"]

    comments: list[dict[str, Any]] = []
    invalid_json_lines_count = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError:
            invalid_json_lines_count += 1
            continue
        if isinstance(loaded, dict):
            comments.append(loaded)
        else:
            invalid_json_lines_count += 1

    return comments, {"invalid_json_lines_count": invalid_json_lines_count}, []


def _parse_contract_results(
    comments: list[dict[str, Any]],
    expected_issue_url: str,
    issue_number: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if issue_number is None:
        issue_number = _issue_number_from_url(expected_issue_url)
    results: list[dict[str, Any]] = []
    invalid_contract_blocks_count = 0
    ambiguous_contract_blocks_count = 0

    for comment in comments:
        body = str(comment.get("body") or "")
        if _CONTRACT_REVIEW_MARKER not in body:
            continue

        valid_blocks: list[dict[str, Any]] = []
        matched_blocks = 0
        for raw_block in _extract_yaml_blocks(body):
            if _CONTRACT_REVIEW_MARKER not in raw_block:
                continue
            matched_blocks += 1
            parsed = _parse_simple_yaml_block(raw_block)
            if _is_valid_contract_review_result(parsed, expected_issue_url):
                inner = parsed[_CONTRACT_REVIEW_MARKER]
                author = comment.get("author")
                author_association = comment.get("author_association")
                author_id = comment.get("author_id")
                author_type = comment.get("author_type")
                valid_blocks.append(
                    {
                        "comment_id": comment.get("id"),
                        "html_url": comment.get("html_url", ""),
                        "created_at": comment.get("created_at", ""),
                        "updated_at": comment.get("updated_at", ""),
                        "status": inner.get("status"),
                        "inner": inner,
                        "author": author,
                        "author_association": author_association,
                        "author_id": author_id,
                        "author_type": author_type,
                        "is_trusted_author": _is_trusted_snapshot_author(
                            author,
                            author_association,
                            author_id=author_id,
                            author_type=author_type,
                        ),
                        "is_fingerprint_ready": _is_fingerprint_ready(
                            inner,
                            comment.get("id"),
                            issue_number,
                        ),
                    }
                )
            else:
                invalid_contract_blocks_count += 1

        if len(valid_blocks) > 1:
            ambiguous_contract_blocks_count += 1
        if valid_blocks:
            results.append(valid_blocks[0])

    results.sort(key=lambda item: (str(item.get("created_at") or ""), int(item.get("comment_id") or 0)))
    return results, {
        "invalid_contract_blocks_count": invalid_contract_blocks_count,
        "ambiguous_contract_blocks_count": ambiguous_contract_blocks_count,
    }


def _find_latest_result(
    results: list[dict[str, Any]],
    *,
    trusted_only: bool = False,
) -> dict[str, Any] | None:
    """#1475 fix_delta P1 item 1: trusted_only=True filters to authoritative
    (trusted-author) candidates BEFORE selecting the latest entry, so an
    untrusted comment can never pre-empt a trusted go/blocked snapshot.
    Every caller that decides go/blocked precedence must pass
    trusted_only=True.
    """
    candidates = (
        [item for item in results if item.get("is_trusted_author") is True]
        if trusted_only
        else results
    )
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (str(item.get("created_at") or ""), int(item.get("comment_id") or 0)),
        reverse=True,
    )[0]


def _find_latest_go(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    # #1537: every consumer of an intake capsule requires a fully bound GO.
    go_results = [
        item
        for item in results
        if item.get("status") == "go"
        and item.get("is_trusted_author") is True
        and item.get("is_fingerprint_ready") is True
    ]
    return _find_latest_result(go_results)


def _build_triage(payload: dict[str, Any]) -> dict[str, Any]:
    triage_module = _load_module(_TRIAGE_PATH, "triage_contract_blockers")
    return triage_module.build_triage_result(payload)


def _top_blocked_categories(triage_result: dict[str, Any]) -> list[str]:
    categories: list[str] = []
    for item in triage_result.get("per_ac") or []:
        if not isinstance(item, dict):
            continue
        category = item.get("category")
        if isinstance(category, str) and category not in categories:
            categories.append(category)
    return categories


def _normalize_contract_snapshot_from_ensure(
    ensure_payload: dict[str, Any],
) -> dict[str, Any]:
    upstream_status = str(ensure_payload.get("status") or "")
    triage_result = (
        _build_triage(ensure_payload)
        if upstream_status in {"blocked_needs_refinement", "dry_run_would_post"}
        else {"schema": _TRIAGE_SCHEMA, "status": "not_run"}
    )
    top_categories = _top_blocked_categories(triage_result)

    normalized_status = "runtime_error"
    if upstream_status == "ok":
        normalized_status = "go"
    elif upstream_status in {"blocked_needs_refinement", "dry_run_would_post"}:
        normalized_status = "missing_go"
    elif upstream_status == "human_judgment":
        normalized_status = "human_judgment"
    elif upstream_status == "stale_or_conflicting_snapshot":
        normalized_status = "stale"

    return {
        "normalized_status": normalized_status,
        "upstream_schema": str(ensure_payload.get("schema") or "none"),
        "upstream_status": upstream_status or None,
        "source": str(ensure_payload.get("source") or "ensure_contract_snapshot_result"),
        "contract_snapshot_url": ensure_payload.get("contract_snapshot_url"),
        "top_blocked_categories": top_categories,
        "contract_blocker_triage": triage_result,
    }


def _normalize_contract_snapshot_live(
    issue_url: str,
    issue_body: str,
    issue_body_sha256: str,
    issue_updated_at: str,
    comments: list[dict[str, Any]],
    parse_warning_counts: dict[str, int],
) -> tuple[dict[str, Any], bool]:
    parsed_results, parser_counts = _parse_contract_results(
        comments, issue_url, _issue_number_from_url(issue_url)
    )
    parse_warning_counts.update(parser_counts)

    # #1475 fix_delta P1 item 1: trust filtering before precedence -- an
    # untrusted comment must never pre-empt a trusted go via the
    # "latest blocked wins" branch below.
    latest = _find_latest_result(parsed_results, trusted_only=True)
    latest_go = _find_latest_go(parsed_results)
    # #1537: a trusted go lacking a well-formed source-bound
    # expected_contract_fingerprint must never be routed as the
    # loop-consumable latest go -- treat it as if no go were found so the
    # caller re-materializes instead of adopting a fingerprint-incomplete
    # snapshot.
    if latest_go is not None and not latest_go.get("is_fingerprint_ready"):
        latest_go = None
    evidence_complete = not any(parse_warning_counts.values())

    # #1475 fix_delta P2 item 4: incomplete comment evidence (malformed
    # NDJSON lines, ambiguous duplicate contract blocks, invalid contract
    # blocks) must never be silently treated as "no conflicting evidence" --
    # a "go" adoption based on a partial comment fetch is fail-open. When
    # evidence is incomplete, do not report normalized_status: "go" even if
    # a trusted go candidate was found in the partial data; route to
    # missing_go so the caller re-fetches / escalates instead of proceeding.
    if not evidence_complete and latest_go and not (latest and latest.get("status") == "blocked"):
        return {
            "normalized_status": "missing_go",
            "upstream_schema": _CONTRACT_REVIEW_MARKER,
            "upstream_status": "go",
            "source": "incomplete_evidence",
            "contract_snapshot_url": latest_go.get("html_url"),
            "top_blocked_categories": [],
            "contract_blocker_triage": {"schema": _TRIAGE_SCHEMA, "status": "not_run"},
        }, evidence_complete

    if latest and latest.get("status") == "blocked":
        return {
            "normalized_status": "latest_blocked",
            "upstream_schema": _CONTRACT_REVIEW_MARKER,
            "upstream_status": "blocked",
            "source": "latest_blocked",
            "contract_snapshot_url": latest.get("html_url"),
            "top_blocked_categories": [],
            "contract_blocker_triage": {"schema": _TRIAGE_SCHEMA, "status": "not_run"},
        }, evidence_complete

    if latest_go:
        inner = latest_go.get("inner") or {}
        fingerprint = inner.get("expected_contract_fingerprint")
        live_paths_hash = _live_allowed_paths_hash(issue_body)
        if (
            not isinstance(fingerprint, dict)
            or live_paths_hash is None
            or fingerprint.get("allowed_paths_normalized_sha256") != live_paths_hash
        ):
            return {
                "normalized_status": "missing_go",
                "upstream_schema": _CONTRACT_REVIEW_MARKER,
                "upstream_status": "go",
                "source": "fingerprint_live_allowed_paths_mismatch",
                "contract_snapshot_url": latest_go.get("html_url"),
                "top_blocked_categories": [],
                "contract_blocker_triage": {"schema": _TRIAGE_SCHEMA, "status": "not_run"},
            }, evidence_complete
        body_sha256 = inner.get("body_sha256")
        generated_at = str(inner.get("generated_at") or "")
        if body_sha256 and body_sha256 != issue_body_sha256:
            return {
                "normalized_status": "stale",
                "upstream_schema": _CONTRACT_REVIEW_MARKER,
                "upstream_status": "go",
                "source": "existing_go",
                "contract_snapshot_url": latest_go.get("html_url"),
                "top_blocked_categories": [],
                "contract_blocker_triage": {"schema": _TRIAGE_SCHEMA, "status": "not_run"},
            }, evidence_complete
        if not body_sha256 and issue_updated_at and generated_at and generated_at < issue_updated_at:
            return {
                "normalized_status": "stale",
                "upstream_schema": _CONTRACT_REVIEW_MARKER,
                "upstream_status": "go",
                "source": "existing_go",
                "contract_snapshot_url": latest_go.get("html_url"),
                "top_blocked_categories": [],
                "contract_blocker_triage": {"schema": _TRIAGE_SCHEMA, "status": "not_run"},
            }, evidence_complete
        return {
            "normalized_status": "go",
            "upstream_schema": _CONTRACT_REVIEW_MARKER,
            "upstream_status": "go",
            "source": "existing_go",
            "contract_snapshot_url": latest_go.get("html_url"),
            "top_blocked_categories": [],
            "contract_blocker_triage": {"schema": _TRIAGE_SCHEMA, "status": "not_run"},
        }, evidence_complete

    return {
        "normalized_status": "missing_go",
        "upstream_schema": "none",
        "upstream_status": None,
        "source": "none",
        "contract_snapshot_url": None,
        "top_blocked_categories": [],
        "contract_blocker_triage": {"schema": _TRIAGE_SCHEMA, "status": "not_run"},
    }, evidence_complete


def _collect_repo_state(command_log: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []

    head_argv = ["git", "rev-parse", "HEAD"]
    head_rc, head_stdout, head_stderr = _run_command(head_argv)
    _record_command(command_log, "git_head", head_argv, head_rc, head_stdout, head_stderr)

    branch_argv = ["git", "branch", "--show-current"]
    branch_rc, branch_stdout, branch_stderr = _run_command(branch_argv)
    _record_command(command_log, "git_branch", branch_argv, branch_rc, branch_stdout, branch_stderr)

    status_argv = ["git", "status", "--short", "--porcelain=v1"]
    status_rc, status_stdout, status_stderr = _run_command(status_argv)
    _record_command(command_log, "git_status", status_argv, status_rc, status_stdout, status_stderr)

    if status_rc != 0:
        errors.append("git_status_failed")
        return {
            "worktree_status": "unavailable",
            "head_sha": head_stdout.strip() if head_rc == 0 else "",
            "current_branch": branch_stdout.strip() if branch_rc == 0 else "",
            "dirty": False,
            "dirty_paths_summary": {"count": 0, "sample_paths": [], "truncated": False},
        }, errors

    dirty_paths: list[str] = []
    for line in status_stdout.splitlines():
        if not line.strip():
            continue
        dirty_paths.append(line[3:].strip())

    sample_paths = sorted(dict.fromkeys(dirty_paths))
    return {
        "worktree_status": "dirty" if dirty_paths else "clean",
        "head_sha": head_stdout.strip() if head_rc == 0 else "",
        "current_branch": branch_stdout.strip() if branch_rc == 0 else "",
        "dirty": bool(dirty_paths),
        "dirty_paths_summary": {
            "count": len(dirty_paths),
            "sample_paths": sample_paths[:_DEFAULT_SAMPLE_PATHS],
            "truncated": len(sample_paths) > _DEFAULT_SAMPLE_PATHS,
        },
    }, errors


def _next_action_route(
    issue_ready_status: str,
    contract_snapshot: dict[str, Any],
) -> str:
    if issue_ready_status != "pass":
        return "request_readiness_check"

    normalized_status = contract_snapshot.get("normalized_status")
    triage_status = (contract_snapshot.get("contract_blocker_triage") or {}).get("status")
    if normalized_status == "go":
        return "proceed_to_step_1"
    if normalized_status == "missing_go":
        return "run_contract_blocker_triage" if triage_status == "ok" else "ensure_contract_snapshot"
    if normalized_status == "latest_blocked":
        return "run_contract_blocker_triage"
    if normalized_status == "stale":
        return "refresh_contract_snapshot"
    return "human_review_required"


def build_intake_capsule(
    issue_number: int,
    repo: str = _DEFAULT_REPO,
    ensure_contract_snapshot_result: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    command_log: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []

    issue_meta, issue_errors = _collect_issue_metadata(issue_number, repo, command_log)
    errors.extend(issue_errors)

    repo_state, repo_errors = _collect_repo_state(command_log)
    errors.extend(repo_errors)

    if "error" in issue_meta:
        capsule = {
            "schema": _SCHEMA_NAME,
            "schema_version": _SCHEMA_VERSION,
            "generated_at": _now_utc(),
            "issue_number": issue_number,
            "repo": repo,
            "errors": errors,
            "warnings": warnings,
        }
        artifact = {
            "schema": _SCHEMA_NAME,
            "schema_version": _SCHEMA_VERSION,
            "generated_at": capsule["generated_at"],
            "commands": command_log,
            "input_error": issue_meta,
            "repo_state": repo_state,
        }
        return capsule, artifact, 1

    parse_warning_counts = {
        "invalid_json_lines_count": 0,
        "invalid_contract_blocks_count": 0,
        "ambiguous_contract_blocks_count": 0,
    }

    comments_for_digest: list[dict[str, Any]] = []
    if ensure_contract_snapshot_result:
        try:
            ensure_payload = json.loads(
                Path(ensure_contract_snapshot_result).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            errors.append("ensure_contract_snapshot_result_parse_error")
            contract_snapshot = {
                "normalized_status": "runtime_error",
                "upstream_schema": "none",
                "upstream_status": None,
                "source": "ensure_contract_snapshot_result",
                "contract_snapshot_url": None,
                "top_blocked_categories": [],
                "contract_blocker_triage": {"schema": _TRIAGE_SCHEMA, "status": "not_run"},
                "errors": [str(exc)],
            }
            evidence_complete = False
        else:
            contract_snapshot = _normalize_contract_snapshot_from_ensure(ensure_payload)
            evidence_complete = True
    else:
        comments, comment_counts, comment_errors = _collect_issue_comments(issue_number, repo, command_log)
        comments_for_digest = comments
        parse_warning_counts.update(comment_counts)
        errors.extend(comment_errors)
        contract_snapshot, evidence_complete = _normalize_contract_snapshot_live(
            issue_meta["issue_url"],
            issue_meta["body"],
            issue_meta["body_sha256"],
            issue_meta["updated_at"],
            comments,
            parse_warning_counts,
        )

    for key, count in parse_warning_counts.items():
        if count:
            warnings.append(f"{key}:{count}")

    title = str(issue_meta["title"])
    slug = _slugify(title)
    worktree = {
        "status": "deferred_to_concurrent_work_ledger",
        "path": f".claude/worktrees/issue-{issue_number}-{slug}",
        "branch": f"worktree-issue-{issue_number}-{slug}",
        "slug": slug,
    }

    source_integrity = {
        "git_head_sha": repo_state.get("head_sha", ""),
        "issue_body_sha256": issue_meta["body_sha256"],
        "issue_updated_at": issue_meta["updated_at"],
        "comments_digest": None,
        "commands": command_log,
        "parse_warnings": parse_warning_counts,
        "evidence_complete": evidence_complete and not errors,
    }

    if not ensure_contract_snapshot_result and "comments_fetch_error" not in errors:
        comment_digest_material = json.dumps(
            [
                {
                    "id": item.get("id"),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "body_sha256": _sha256(str(item.get("body") or "")),
                }
                for item in comments_for_digest
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        source_integrity["comments_digest"] = _sha256(comment_digest_material)

    next_action = {
        "route": _next_action_route(issue_meta["ready_tuple"]["status"], contract_snapshot),
        "reason_codes": warnings + errors,
    }

    capsule = {
        "schema": _SCHEMA_NAME,
        "schema_version": _SCHEMA_VERSION,
        "generated_at": _now_utc(),
        "issue_number": issue_number,
        "repo": repo,
        "issue_ready_tuple": {
            **issue_meta["ready_tuple"],
            "title": title,
            "issue_url": issue_meta["issue_url"],
        },
        "contract_snapshot": contract_snapshot,
        "source_integrity": source_integrity,
        "worktree": worktree,
        "repo_state": repo_state,
        "agent_runtime": {
            "status": "deferred_to_capability_router",
            "runner": "wsl2-ubuntu",
            "collector": "build_intake_capsule.py",
        },
        "next_action": next_action,
        "warnings": warnings,
        "errors": errors,
    }

    artifact_payload = {
        "schema": _SCHEMA_NAME,
        "schema_version": _SCHEMA_VERSION,
        "generated_at": capsule["generated_at"],
        "input": {
            "issue_number": issue_number,
            "repo": repo,
            "ensure_contract_snapshot_result": ensure_contract_snapshot_result,
        },
        "issue_metadata": {
            "title": title,
            "issue_url": issue_meta["issue_url"],
            "body_sha256": issue_meta["body_sha256"],
            "updated_at": issue_meta["updated_at"],
        },
        "contract_snapshot": contract_snapshot,
        "source_integrity": source_integrity,
        "worktree": worktree,
        "repo_state": repo_state,
        "agent_runtime": capsule["agent_runtime"],
        "next_action": next_action,
        "warnings": warnings,
        "errors": errors,
    }

    exit_code = 0 if not errors else 1
    return capsule, artifact_payload, exit_code


def _render_stdout(payload: dict[str, Any], max_stdout_bytes: int) -> str:
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(compact.encode("utf-8")) <= max_stdout_bytes:
        return compact

    fallback = {
        "schema": _SCHEMA_NAME,
        "schema_version": _SCHEMA_VERSION,
        "issue_number": payload.get("issue_number"),
        "repo": payload.get("repo"),
        "status": "stdout_truncated",
        "next_action": payload.get("next_action"),
        "artifact_path": payload.get("artifact_path"),
    }
    fallback_json = json.dumps(fallback, ensure_ascii=False, separators=(",", ":"))
    if len(fallback_json.encode("utf-8")) <= max_stdout_bytes:
        return fallback_json

    hard = {
        "schema": _SCHEMA_NAME,
        "schema_version": _SCHEMA_VERSION,
        "status": "stdout_too_large",
    }
    return json.dumps(hard, ensure_ascii=False, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build IMPL_REVIEW_INTAKE_CAPSULE_V1 for impl-review-loop",
        allow_abbrev=False,
    )
    parser.add_argument("--issue-number", "-i", type=int, required=True)
    parser.add_argument("--repo", default=_DEFAULT_REPO)
    parser.add_argument("--max-stdout-bytes", type=int, default=_DEFAULT_MAX_STDOUT_BYTES)
    parser.add_argument("--ensure-contract-snapshot-result")
    parser.add_argument("--artifact-dir", default=str(_DEFAULT_ARTIFACT_DIR))
    args = parser.parse_args()

    if args.max_stdout_bytes <= 0:
        print(
            json.dumps(
                {
                    "schema": _SCHEMA_NAME,
                    "schema_version": _SCHEMA_VERSION,
                    "status": "runtime_error",
                    "errors": ["max-stdout-bytes must be > 0"],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 1

    capsule, artifact_payload, exit_code = build_intake_capsule(
        issue_number=args.issue_number,
        repo=args.repo,
        ensure_contract_snapshot_result=args.ensure_contract_snapshot_result,
    )

    artifact_dir = Path(args.artifact_dir)
    artifact_path = artifact_dir / f"intake-capsule-{args.issue_number}.json"
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_payload["artifact_path"] = str(artifact_path)
        artifact_payload["artifact_settings"] = {
            "max_stdout_bytes": args.max_stdout_bytes,
            "generated_at": _now_utc(),
        }
        with artifact_path.open("w", encoding="utf-8") as file_obj:
            json.dump(artifact_payload, file_obj, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(
            json.dumps(
                {
                    "schema": _SCHEMA_NAME,
                    "schema_version": _SCHEMA_VERSION,
                    "status": "artifact_write_failed",
                    "errors": [str(exc)],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 1

    capsule["artifact_path"] = str(artifact_path)
    print(_render_stdout(capsule, args.max_stdout_bytes))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
