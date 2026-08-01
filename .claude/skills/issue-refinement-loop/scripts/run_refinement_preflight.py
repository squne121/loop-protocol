#!/usr/bin/env python3
"""
run_refinement_preflight.py

Deterministic entrypoint that assembles planner input from GitHub API data,
validates anchor comments structurally, invokes plan_refinement_loop.py with
correctly-formed stdin JSON, and writes a compact result artifact.

Usage:
    uv run python3 run_refinement_preflight.py \\
        --issue-number <N> \\
        --repo <owner/name> \\
        [--anchor-comment-url <URL> ...] \\
        [--fixture <path>]

Output (stdout): compact projection of refinement_preflight_result/v1 artifact.

Canonical stdout fields:
    STATUS       - pass | warn | blocked | environment_failure (always present)
    NEXT_ACTION  - routing instruction (always present)
    MUST_READ    - files/paths to read before proceeding (omitted if empty)
    COMMANDS     - argv-only command templates (omitted if empty)
    BLOCKERS     - blocker reason codes (omitted if empty)
    ARTIFACT     - artifact key: absolute_path pairs (omitted if empty)
    REQUIRED_SECTIONS - required sections from rewrite constraints (planner-derived)
    REQUIRED_CONTRACT_KEYS - required contract keys from rewrite constraints (planner-derived)
    REWRITE_CONSTRAINTS - planner rewrite constraints payload when fail_closed=true

Non-canonical / suppressed fields:
    SUMMARY      - human-only prose, not consumed by orchestrators
    DO_NOT_READ  - reserved, currently empty; consumers MUST NOT rely on absence
    EVIDENCE     - raw issue body / comments; NEVER emitted to stdout

Artifact (file):  .claude/artifacts/issue-refinement-loop/<issue_number>/
                  refinement_preflight_result_v1.json  (canonical result)
                  raw_issue_snapshot.json              (raw issue + comments)
                  planner_input.json                   (planner stdin, byte-stable)

Exit codes:
    0 - pass (planner succeeded, fail_closed.required == false, no unknown confidence)
    1 - warn (planner exit 0, fail_closed.required == false, >=1 decision with
              confidence: unknown — human note needed but not blocking)
    2 - blocked (anchor mismatch, planner exit 2, or planner fail_closed.required == true)
    3 - environment_failure (gh not found / auth / API / timeout / non-JSON)

Planner ↔ Wrapper Exit Code Mapping:
    anchor comment not in issue                    → blocked  / 2
    gh not found / auth / API fail / timeout / JSON → environment_failure / 3
    planner exit 2 (invalid input)                  → blocked  / 2
    planner exit 3 (internal error)                 → environment_failure / 3
    planner exit 0 + fail_closed.required == true   → blocked  / 2
    planner exit 0, fail_closed=false, no unknown   → pass     / 0
    planner exit 0, fail_closed=false, >=1 unknown  → warn     / 1

warn (exit 1) definition:
    planner exit 0 AND fail_closed.required == false
    AND decisions.*.confidence contains at least one "unknown"
    → status: warn / exit 1 (human note needed, but not fully blocking)
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Runtime schema validation (jsonschema >= 4.0, already in pyproject.toml)
# ---------------------------------------------------------------------------

try:
    import jsonschema as _jsonschema

    _JSONSCHEMA_AVAILABLE = True
except ImportError:
    _JSONSCHEMA_AVAILABLE = False


# ---------------------------------------------------------------------------
# #1677 AC4/AC12: reuse plan_refinement_loop.py's normative semantic
# validator instead of re-implementing ISSUE_EXECUTION_DECISION_V1
# invariants here. Import is best-effort (subprocess/CLI callers of this
# module do not require it; only _join_scope_rollup_into_planner_input's
# self-check below uses it).
# ---------------------------------------------------------------------------

import sys as _sys_for_import

_sys_for_import.path.insert(0, str(Path(__file__).resolve().parent))
try:
    # PR #1767 owner review (P0-4/AC12 Scope Delta): import the standalone
    # canonical module directly rather than re-exporting through
    # plan_refinement_loop.py, so every consumer shares one authority.
    from validate_issue_execution_decision import validate_issue_execution_decision
except ImportError:  # pragma: no cover - defensive fallback
    validate_issue_execution_decision = None

try:
    # #1891: pure analyzer for anchor comment multi-turn segmentation and
    # candidate extraction. anchor_context.py has no GitHub API client of its
    # own; it only consumes the already-fetched anchor_comment.snapshot body
    # that this module builds below (AC8).
    import anchor_context
except ImportError:  # pragma: no cover - defensive fallback
    anchor_context = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent
_SCHEMAS_DIR = _SCRIPTS_DIR.parent / "schemas"
PLANNER_SCRIPT = _SCRIPTS_DIR / "plan_refinement_loop.py"
REPAIR_SCRIPT = _SCRIPTS_DIR / "repair_issue_contract.py"

SCHEMA_VERSION_RESULT = "refinement_preflight_result/v1"
SCHEMA_VERSION_PLANNER_INPUT = "refinement_loop_planner_input/v1"
SCHEMA_VERSION_INPUT_FIXTURE = "refinement_preflight_input/v1"

# Timeout constants (seconds)
GH_API_TIMEOUT = 30
PLANNER_TIMEOUT = 60

# Exit codes
EXIT_PASS = 0
EXIT_WARN = 1
EXIT_BLOCKED = 2
EXIT_ENVIRONMENT_FAILURE = 3

# Blocker reason codes
BLOCKER_ANCHOR_NOT_IN_ISSUE = "ANCHOR_NOT_IN_ISSUE"
BLOCKER_ANCHOR_IS_PR_REVIEW = "ANCHOR_IS_PR_REVIEW_COMMENT"
BLOCKER_GH_FAILURE = "GH_API_FAILURE"
BLOCKER_PLANNER_INVALID_INPUT = "PLANNER_INVALID_INPUT"
BLOCKER_PLANNER_INTERNAL_ERROR = "PLANNER_INTERNAL_ERROR"
BLOCKER_FAIL_CLOSED = "PLANNER_FAIL_CLOSED"
BLOCKER_ANCHOR_REPO_MISMATCH = "ANCHOR_REPO_MISMATCH"
BLOCKER_ANCHOR_ISSUE_NUMBER_MISMATCH = "ANCHOR_ISSUE_NUMBER_MISMATCH"
BLOCKER_ANCHOR_COMMENT_NOT_FOUND = "ANCHOR_COMMENT_NOT_FOUND"
BLOCKER_ANCHOR_ISSUE_URL_MISMATCH = "ANCHOR_ISSUE_URL_MISMATCH"
BLOCKER_ANCHOR_COMMENT_SCHEMA_INVALID = "ANCHOR_COMMENT_SCHEMA_INVALID"
BLOCKER_ANCHOR_COMMENT_MULTIPLE_UNSUPPORTED = "ANCHOR_COMMENT_MULTIPLE_UNSUPPORTED"
BLOCKER_INPUT_SCHEMA_INVALID = "INPUT_SCHEMA_INVALID"
BLOCKER_RESULT_SCHEMA_INVALID = "RESULT_SCHEMA_INVALID"
BLOCKER_INVALID_ARGS = "INVALID_ARGS"
BLOCKER_REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD = "REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD"
BLOCKER_REWRITE_CONSTRAINTS_NOT_JSON_SERIALIZABLE = "REWRITE_CONSTRAINTS_NOT_JSON_SERIALIZABLE"
BLOCKER_REWRITE_CONSTRAINTS_INVARIANT_VIOLATION = "REWRITE_CONSTRAINTS_INVARIANT_VIOLATION"
BLOCKER_PLANNER_FAIL_CLOSED_PAYLOAD_INVALID = "planner_fail_closed_payload_invalid"
BLOCKER_ARTIFACT_PROJECTION_MISMATCH = "ARTIFACT_PROJECTION_MISMATCH"
BLOCKER_ISSUE_EXECUTION_DECISION_INVALID = "ISSUE_EXECUTION_DECISION_INVALID"
BLOCKER_ISSUE_EXECUTION_DECISION_VALIDATOR_UNAVAILABLE = "ISSUE_EXECUTION_DECISION_VALIDATOR_UNAVAILABLE"
# #1891 iteration 2 (PR #1923 OWNER REQUEST_CHANGES): the multi-turn-candidate
# route and the heavy-mutation gate must actually reach the `blockers` list
# consumed by `_apply_exit_code_mapping()`, not just live inside
# `known_context` where the planner never inspects them.
BLOCKER_ANCHOR_MULTI_TURN_FAIL_CLOSED = "ANCHOR_MULTI_TURN_FAIL_CLOSED"
BLOCKER_HEAVY_MUTATION_FAIL_CLOSED = "HEAVY_MUTATION_FAIL_CLOSED"


def _render_artifact_projection_lines(artifacts: dict[str, str]) -> list[str]:
    lines: list[str] = ["ARTIFACT:"]
    for key, value in sorted(artifacts.items()):
        lines.append(f"  {key}: {value}")
    return lines


def _validate_artifact_projection(*, repo_root: Path, issue_number: int, artifacts: dict[str, str]) -> list[str]:
    if not artifacts:
        return []

    expected_root = (repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)).resolve()
    failures: list[str] = []

    for key, raw_path in artifacts.items():
        if not isinstance(raw_path, str) or not raw_path:
            failures.append(f"artifact_path_invalid: {key!r}:{raw_path!r}")
            continue

        try:
            resolved = Path(raw_path).resolve()
        except Exception:
            failures.append(f"artifact_path_unresolvable: {key} -> {raw_path!r}")
            continue

        if resolved != expected_root and not resolved.is_relative_to(expected_root):
            normalized = (
                resolved.relative_to(repo_root).as_posix() if resolved.is_relative_to(repo_root) else str(resolved)
            )
            failures.append(f"artifact_path_outside_issue_root: {key} -> {normalized}")
            continue

        if not resolved.exists():
            normalized = (
                resolved.relative_to(repo_root).as_posix() if resolved.is_relative_to(repo_root) else str(resolved)
            )
            failures.append(f"artifact_path_missing: {key} -> {normalized}")

    return failures


# Trusted author associations for ANCHOR_SCOPE_REFRAME_V1
TRUSTED_ANCHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    """Compute SHA256 hex digest of UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _as_string_list(
    value: Any,
    field_name: str,
    blockers: list[str],
) -> tuple[list[str], bool]:
    """Extract a list[str] payload or record a blocker and fail closed."""
    if not isinstance(value, list):
        blockers.append(f"{BLOCKER_REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD}: {field_name} must be a list")
        return [], False

    for item in value:
        if not isinstance(item, str):
            blockers.append(f"{BLOCKER_REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD}: {field_name} contains non-string item")
            return [], False

    return value, True


def _build_safe_rewrite_constraints(
    required_sections: list[str],
    required_contract_keys: list[str],
) -> dict[str, Any]:
    """Build a schema-safe rewrite constraints payload for fail-closed payload violations."""
    return {
        "schema_version": "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1",
        "required_sections": required_sections,
        "required_contract_keys": required_contract_keys,
        "rewrite_constraints": {
            "must_add_sections": required_sections,
            "must_add_contract_keys": required_contract_keys,
            "freeform_rewrite_forbidden": True,
        },
        "override_policy": {
            "allowed_reason_codes": [
                "missing_required_section",
                "missing_required_contract_key",
            ],
            "never_override_reason_codes": [
                "unknown_issue_kind",
                "issue_kind_policy_load_error",
                "contract_schema_parse_error",
                "template_resolution_error",
                "checker_internal_error",
            ],
            "overridable_in_current_result": [],
            "non_overridable_in_current_result": [],
        },
        "max_rewrite_attempts": 2,
        "no_progress_route": "human_judgment_required",
    }


def _ensure_json_serializable(value: Any, field_name: str, blockers: list[str]) -> bool:
    """Validate JSON serializability for deterministic stdout/hashing artifacts."""
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return True
    except (TypeError, ValueError) as exc:
        blockers.append(f"{BLOCKER_REWRITE_CONSTRAINTS_NOT_JSON_SERIALIZABLE}: {field_name} serialization error: {exc}")
        return False


def _find_repo_root() -> Path:
    """Walk up from this script to find the .git root."""
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    # Fallback: assume .claude/skills/issue-refinement-loop/scripts/
    return Path(__file__).resolve().parent.parent.parent.parent.parent


def _load_schema(schema_filename: str) -> dict | None:
    """Load a JSON schema file from the schemas directory. Returns None if not found."""
    schema_path = _SCHEMAS_DIR / schema_filename
    if not schema_path.exists():
        return None
    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _validate_with_schema(data: dict, schema: dict) -> tuple[bool, list[str]]:
    """
    Validate data against schema using jsonschema.

    Returns (is_valid, error_messages).
    If jsonschema not available, skips validation (returns True, []).
    """
    if not _JSONSCHEMA_AVAILABLE:
        return True, []
    try:
        validator_cls = _jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)
        validator = validator_cls(
            schema,
            format_checker=validator_cls.FORMAT_CHECKER,
        )
        errors = sorted(validator.iter_errors(data), key=lambda exc: list(exc.path))
        if errors:
            return False, [f"schema_validation_error: {errors[0].message}"]
        format_errors = _validate_date_time_formats(data, schema)
        if format_errors:
            return False, format_errors
        return True, []
    except _jsonschema.ValidationError as exc:
        return False, [f"schema_validation_error: {exc.message}"]
    except Exception as exc:
        return False, [f"schema_validation_unexpected: {exc}"]


def _validate_date_time_formats(data: Any, schema: dict, path: str = "$") -> list[str]:
    schema_type = schema.get("type")
    if schema.get("format") == "date-time" and isinstance(data, str):
        candidate = data.replace("Z", "+00:00")
        try:
            datetime.fromisoformat(candidate)
        except ValueError:
            return [f"schema_validation_error: {path} must be a valid date-time"]
        return []

    if schema_type == "object" and isinstance(data, dict):
        errors: list[str] = []
        for key, value in data.items():
            child_schema = schema.get("properties", {}).get(key)
            if isinstance(child_schema, dict):
                errors.extend(_validate_date_time_formats(value, child_schema, f"{path}.{key}"))
        return errors

    if schema_type == "array" and isinstance(data, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            errors: list[str] = []
            for index, item in enumerate(data):
                errors.extend(_validate_date_time_formats(item, item_schema, f"{path}[{index}]"))
            return errors

    return []


# ---------------------------------------------------------------------------
# URL parsing for anchor comment structural validation
# ---------------------------------------------------------------------------

# Pattern: https://github.com/<owner>/<repo>/issues/<number>#issuecomment-<id>
_ISSUE_COMMENT_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
    r"/issues/(?P<issue_number>\d+)#issuecomment-(?P<comment_id>\d+)$"
)

# PR review comment pattern (different from issue comment)
_PR_REVIEW_COMMENT_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/\d+#issuecomment-\d+$")
_PR_REVIEW_DISCUSSION_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/\d+#discussion_r\d+$")

# Valid --repo pattern
_REPO_PATTERN = re.compile(r"^[^/]+/[^/]+$")

# Valid GitHub comment URL prefix
_GITHUB_URL_PREFIX = "https://github.com/"


def _parse_anchor_comment_url(url: str) -> dict[str, Any]:
    """
    Parse an anchor comment URL into its structural components.

    Returns dict with: owner, repo, issue_number (int), comment_id (int), valid (bool)
    Does NOT use substring matching — validates URL structure via regex only.
    """
    # Reject PR review comment URLs (different endpoint from issue comments)
    if _PR_REVIEW_DISCUSSION_RE.match(url):
        return {"valid": False, "error": "pr_review_comment_url"}

    m = _ISSUE_COMMENT_RE.match(url)
    if not m:
        return {"valid": False, "error": "url_parse_failure"}

    return {
        "valid": True,
        "owner": m.group("owner"),
        "repo": m.group("repo"),
        "issue_number": int(m.group("issue_number")),
        "comment_id": int(m.group("comment_id")),
    }


# ---------------------------------------------------------------------------
# gh CLI wrappers
# ---------------------------------------------------------------------------


def _run_gh(argv: list[str], timeout: int = GH_API_TIMEOUT) -> tuple[dict | list | None, str]:
    """
    Run a gh command and return (parsed_json, error_message).

    Uses subprocess.run([...], shell=False) — never shell=True.
    Returns (None, error_message) on timeout, non-zero exit, or JSON parse failure.
    """
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None, "gh_not_found"
    except subprocess.TimeoutExpired:
        return None, f"gh_timeout after {timeout}s"
    except Exception as exc:
        return None, f"gh_unexpected_error: {exc}"

    if proc.returncode != 0:
        stderr_snip = (proc.stderr or "")[:300]
        return None, f"gh_exit_{proc.returncode}: {stderr_snip}"

    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return None, f"gh_json_decode_error: {exc}"

    return parsed, ""


def _fetch_issue(repo: str, issue_number: int) -> tuple[dict | None, str]:
    """Fetch issue data via gh issue view --json."""
    data, err = _run_gh(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            repo,
            "--json",
            "number,title,body,labels,url,updatedAt",
        ]
    )
    return data, err


def _fetch_issue_comments(repo: str, issue_number: int) -> tuple[list | None, str]:
    """Fetch all issue comments via gh api with --paginate --slurp.

    gh 2.88.1+ --slurp returns [[...page1...], [...page2...]] which must be
    flattened to a single list.  Single-page results are also wrapped as [[...]].
    """
    try:
        from command_registry import render_command as _render_command

        _argv = _render_command("gh.issue.comments.list", {"repo": repo, "issue_number": issue_number})
    except Exception as exc:
        raise RuntimeError(f"BLOCKER_COMMAND_REGISTRY_UNAVAILABLE: gh.issue.comments.list failed: {exc}") from exc
    data, err = _run_gh(_argv)
    if data is None:
        return None, err
    # --slurp wraps each page as an element: [[page1_comments...], [page2_comments...]]
    # Flatten one level regardless of page count.
    if isinstance(data, list):
        if len(data) == 0:
            return [], ""
        # Check if it's a slurp-wrapped list-of-lists
        if all(isinstance(item, list) for item in data):
            flattened: list[dict] = []
            for page in data:
                flattened.extend(page)
            return flattened, ""
        # Already a flat list (e.g. non-paginated gh or mock returning flat list)
        return data, ""
    return None, f"gh_comments_unexpected_type: {type(data).__name__}"


def _fetch_single_comment(repo: str, comment_id: int) -> tuple[dict | None, str]:
    """Fetch a single issue comment via gh api to validate issue_url field."""
    data, err = _run_gh(["gh", "api", f"repos/{repo}/issues/comments/{comment_id}"])
    return data, err


def _load_anchor_comment_schema() -> dict[str, Any]:
    schema_path = _SCHEMAS_DIR / "anchor_comment.schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# ANCHOR_SCOPE_REFRAME_V1 parsing and classification (AC2-AC5)
# ---------------------------------------------------------------------------


def _parse_anchor_scope_reframe_body(comment_body: str) -> "dict | None":
    """
    Parse ANCHOR_SCOPE_REFRAME_V1 payload from a comment body.

    Only top-level fenced yaml blocks are canonical.
    Fail-closed: blockquote-embedded fenced blocks and raw-text markers are rejected.
    Returns None if not found or malformed.
    """
    import re

    fenced_pattern = re.compile(r"^```yaml\s*\n(.*?)^```", re.MULTILINE | re.DOTALL)
    for match in fenced_pattern.finditer(comment_body):
        yaml_content = match.group(1)
        # Fail-closed: reject if this fence is inside a blockquote
        start = match.start()
        before = comment_body[:start]
        if before.rstrip().endswith(">"):
            continue
        try:
            import yaml as _yaml

            data = _yaml.safe_load(yaml_content)
        except Exception:
            return None
        if isinstance(data, dict) and data.get("schema_version") == "ANCHOR_SCOPE_REFRAME_V1":
            return data
    return None


def _classify_anchor_scope_reframe(
    *,
    comment_payload: "dict",
    anchor_body: str,
    repo: str,
    issue_number: int,
    anchor_url: str,
) -> dict:
    """
    Classify anchor comment for ANCHOR_SCOPE_REFRAME_V1 trust and generate scope_delta_decision.

    Trusted if ALL of:
    - author_association in TRUSTED_ANCHOR_ASSOCIATIONS
    - Payload has ANCHOR_SCOPE_REFRAME_V1 schema_version
    - target.repo == repo
    - target.issue_number == issue_number
    - Payload passes anchor_scope_reframe_v1.schema.json validation

    Always returns a scope_delta_decision dict.
    """
    import hashlib as _hashlib

    author_assoc = comment_payload.get("author_association", "")
    anchor_hash = _hashlib.sha256(
        anchor_body.encode("utf-8") if isinstance(anchor_body, str) else anchor_body
    ).hexdigest()

    # Check author trust
    if author_assoc not in TRUSTED_ANCHOR_ASSOCIATIONS:
        return {
            "status": "fail_closed",
            "reason": f"untrusted_author_association: {author_assoc!r}",
            "implementation_go": False,
            "anchor_author_association": author_assoc or None,
            "anchor_comment_url": anchor_url,
            "anchor_comment_hash": anchor_hash,
            "allowed_path_deltas": [],
            "required_rerun": [],
        }

    # Parse ANCHOR_SCOPE_REFRAME_V1 payload from body
    payload = _parse_anchor_scope_reframe_body(anchor_body)
    if payload is None:
        return {
            "status": "fail_closed",
            "reason": "no_anchor_scope_reframe_v1_payload",
            "implementation_go": False,
            "anchor_author_association": author_assoc,
            "anchor_comment_url": anchor_url,
            "anchor_comment_hash": anchor_hash,
            "allowed_path_deltas": [],
            "required_rerun": [],
        }

    # Validate against schema (fail-closed on schema error)
    schema = _load_schema("anchor_scope_reframe_v1.schema.json")
    if schema is not None:
        valid, errors = _validate_with_schema(payload, schema)
        if not valid:
            return {
                "status": "fail_closed",
                "reason": f"schema_invalid: {errors[:3]}",
                "implementation_go": False,
                "anchor_author_association": author_assoc,
                "anchor_comment_url": anchor_url,
                "anchor_comment_hash": anchor_hash,
                "allowed_path_deltas": [],
                "required_rerun": [],
            }

    # Check target.repo
    target = payload.get("target", {})
    if target.get("repo") != repo:
        return {
            "status": "fail_closed",
            "reason": f"wrong_repo: expected {repo!r}, got {target.get('repo')!r}",
            "implementation_go": False,
            "anchor_author_association": author_assoc,
            "anchor_comment_url": anchor_url,
            "anchor_comment_hash": anchor_hash,
            "allowed_path_deltas": [],
            "required_rerun": [],
        }

    # Check target.issue_number
    if target.get("issue_number") != issue_number:
        return {
            "status": "fail_closed",
            "reason": f"wrong_issue_number: expected {issue_number}, got {target.get('issue_number')!r}",
            "implementation_go": False,
            "anchor_author_association": author_assoc,
            "anchor_comment_url": anchor_url,
            "anchor_comment_hash": anchor_hash,
            "allowed_path_deltas": [],
            "required_rerun": [],
        }

    # All checks pass — trusted anchor
    return {
        "status": "approved_by_trusted_anchor",
        "implementation_go": False,
        "anchor_author_association": author_assoc,
        "anchor_comment_url": anchor_url,
        "anchor_comment_hash": anchor_hash,
        "allowed_path_deltas": payload.get("allowed_path_deltas", []),
        "required_rerun": payload.get("required_rerun", []),
    }


def _build_anchor_comment_state(
    *,
    anchor_url: str,
    comment: dict[str, Any],
    issue_number: int,
    captured_at: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    issue_url = comment.get("issue_url")
    if not isinstance(issue_url, str) or not issue_url:
        return None, [BLOCKER_ANCHOR_NOT_IN_ISSUE]

    parsed_url = urlparse(issue_url)
    path_parts = [part for part in parsed_url.path.split("/") if part]
    if len(path_parts) < 4 or path_parts[-2] != "issues" or path_parts[-1] != str(issue_number):
        return None, [BLOCKER_ANCHOR_NOT_IN_ISSUE]

    state = {
        "url": anchor_url,
        "id": comment.get("id"),
        "issue_number": issue_number,
        "html_url": comment.get("html_url"),
        "api_url": comment.get("url"),
        "user_login": ((comment.get("user") or {}).get("login")),
        "author_association": comment.get("author_association"),
        "snapshot": comment.get("body", ""),
        "captured_at": captured_at,
        "fetched_at": captured_at,
        "comment_created_at": comment.get("created_at"),
        "comment_updated_at": comment.get("updated_at"),
        "preliminary_classification": "feedback_update_required",
        "final_classification": None,
        (
            "classification_reason"
        ): "defaulted_by_preflight_schema_normalization; semantic classification deferred to #1008/#1011",
        "verified_claims": [],
        "unresolved_claims": [],
        "scope_impact": None,
        "requires_fact_check": False,
    }

    schema = _load_anchor_comment_schema()
    valid, errors = _validate_with_schema(state, schema)
    if not valid:
        return None, [BLOCKER_ANCHOR_COMMENT_SCHEMA_INVALID, *errors]
    return state, []


# ---------------------------------------------------------------------------
# Anchor comment structural validation
# ---------------------------------------------------------------------------


def _validate_anchor_comment_url(
    url: str,
    repo: str,
    issue_number: int,
    fixture_comments: Optional[list[dict]] = None,
) -> tuple[bool, list[str]]:
    """
    Validate a single anchor comment URL structurally.

    Checks (all must pass):
    1. URL owner/repo matches --repo
    2. URL issue_number matches --issue-number
    3. Comment id exists (via gh api or fixture)
    4. Comment's issue_url REST field points to same issue (must be present and non-empty)
    5. Not a PR review comment (different endpoint)

    Returns (is_valid, list_of_blocker_codes).
    Uses structural URL parsing only — no substring checks.
    """
    parsed = _parse_anchor_comment_url(url)

    if not parsed.get("valid"):
        error = parsed.get("error", "unknown")
        if error == "pr_review_comment_url":
            return False, [BLOCKER_ANCHOR_IS_PR_REVIEW, BLOCKER_ANCHOR_NOT_IN_ISSUE]
        return False, [BLOCKER_ANCHOR_NOT_IN_ISSUE]

    # Check 1: owner/repo match
    url_owner = parsed["owner"].lower()
    url_repo_name = parsed["repo"].lower()
    parts = repo.lower().split("/", 1)
    if len(parts) != 2:
        return False, [BLOCKER_ANCHOR_REPO_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]

    expected_owner, expected_repo_name = parts
    if url_owner != expected_owner or url_repo_name != expected_repo_name:
        return False, [BLOCKER_ANCHOR_REPO_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]

    # Check 2: issue number match
    if parsed["issue_number"] != issue_number:
        return False, [BLOCKER_ANCHOR_ISSUE_NUMBER_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]

    comment_id = parsed["comment_id"]

    # Check 3 & 4: comment exists and issue_url field matches
    if fixture_comments is not None:
        # Fixture mode: look up comment from pre-fetched data
        comment_data = None
        for c in fixture_comments:
            if isinstance(c, dict) and str(c.get("id")) == str(comment_id):
                comment_data = c
                break
        if comment_data is None:
            return False, [BLOCKER_ANCHOR_COMMENT_NOT_FOUND, BLOCKER_ANCHOR_NOT_IN_ISSUE]
    else:
        # Live mode: fetch via gh api
        comment_data, err = _fetch_single_comment(repo, comment_id)
        if comment_data is None:
            return False, [BLOCKER_ANCHOR_COMMENT_NOT_FOUND, BLOCKER_ANCHOR_NOT_IN_ISSUE]

    # Check 4: issue_url field validation — must be present and non-empty
    issue_url_field = comment_data.get("issue_url")

    # Missing or empty issue_url → blocked (fail-closed)
    if not issue_url_field:
        return False, [BLOCKER_ANCHOR_ISSUE_URL_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]

    # Expected format: https://api.github.com/repos/<owner>/<repo>/issues/<number>
    # Also accept: https://github.com/<owner>/<repo>/issues/<number>
    expected_api_url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
    expected_html_url = f"https://github.com/{repo}/issues/{issue_number}"

    if issue_url_field in (expected_api_url, expected_html_url):
        return True, []

    # Structural check via urlparse (not substring)
    parsed_url = urlparse(issue_url_field)
    path_parts = parsed_url.path.rstrip("/").split("/")
    if len(path_parts) >= 4 and path_parts[-2] == "issues" and path_parts[-1] == str(issue_number):
        # Repo path should be /<owner>/<repo>/
        if (
            len(path_parts) >= 5
            and path_parts[-4].lower() == expected_owner
            and path_parts[-3].lower() == expected_repo_name
        ):
            return True, []
        else:
            return False, [BLOCKER_ANCHOR_ISSUE_URL_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]
    else:
        return False, [BLOCKER_ANCHOR_ISSUE_URL_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]


def _validate_anchor_comments_batch(
    anchor_comment_urls: list[str],
    repo: str,
    issue_number: int,
    fixture_comments: Optional[list[dict]] = None,
) -> tuple[list[str], list[str]]:
    """
    Validate all anchor comment URLs. Returns (stable_sorted_unique_valid_urls, all_blockers).

    Stable sort + dedupe per spec. One invalid URL blocks all.
    ANCHOR_NOT_IN_ISSUE is always included as canonical blocker when any URL fails.
    """
    if not anchor_comment_urls:
        return [], []

    all_blockers: list[str] = []
    seen_urls: set[str] = set()
    deduped_urls: list[str] = []

    for url in anchor_comment_urls:
        if url not in seen_urls:
            seen_urls.add(url)
            deduped_urls.append(url)

    # Stable sort
    sorted_urls = sorted(deduped_urls)
    if len(sorted_urls) > 1:
        return [], [BLOCKER_ANCHOR_COMMENT_MULTIPLE_UNSUPPORTED]

    for url in sorted_urls:
        valid, blockers = _validate_anchor_comment_url(url, repo, issue_number, fixture_comments=fixture_comments)
        if not valid:
            all_blockers.extend(blockers)

    # Deduplicate blockers while preserving order
    seen_b: set[str] = set()
    deduped_blockers: list[str] = []
    for b in all_blockers:
        if b not in seen_b:
            seen_b.add(b)
            deduped_blockers.append(b)

    return sorted_urls, deduped_blockers


# ---------------------------------------------------------------------------
# Planner invocation
# ---------------------------------------------------------------------------


def _load_scope_rollup_artifact(repo_root: Path, issue_number: int) -> Optional[dict]:
    """
    Load a previously-persisted ISSUE_SCOPE_ROLLUP_PLAN_V2 artifact for this
    Issue, if one exists (#1677 AC4 join). Rerunning plan_issue_scope_rollup.py
    itself is out of scope here (#1677 Out of Scope); this function only
    consumes an artifact that a prior preflight step already produced.

    Returns None (non-blocking) when no artifact is present or it fails to
    parse -- absence of scope-rollup evidence must not block the refinement
    preflight, it only means the planner falls back to a minimal
    ISSUE_EXECUTION_DECISION_V1 ('selected', no relations).
    """
    artifact_path = _issue_artifact_dir(repo_root, issue_number) / "issue_scope_rollup_plan_v2.json"
    if not artifact_path.exists():
        return None
    try:
        return json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _join_scope_rollup_into_planner_input(
    planner_input: dict[str, Any],
    scope_rollup_plan: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """
    Join an ISSUE_SCOPE_ROLLUP_PLAN_V2 artifact into planner_input's
    known_context.scope_rollup_result (#1677 AC4).

    plan_refinement_loop.py's build_issue_execution_decision() reads
    known_context['scope_rollup_result'] to derive ISSUE_EXECUTION_DECISION_V1
    relations/execution state. Without this join, the planner always emits
    the minimal 'selected' shape regardless of known collisions.

    Pure function: does not mutate the input dict in place.
    """
    if not scope_rollup_plan:
        return planner_input
    joined = dict(planner_input)
    known_context = dict(joined.get("known_context") or {})
    known_context["scope_rollup_result"] = scope_rollup_plan
    joined["known_context"] = known_context
    return joined


def _build_planner_input(
    issue: dict,
    comments: list[dict],
    known_context: Optional[dict],
    anchor_comment_feedback: Optional[dict] = None,
    anchor_comment_ids: Optional[set[str]] = None,
    now: Optional[str] = None,
) -> dict:
    """Build REFINEMENT_LOOP_PLANNER_INPUT_V1 from issue/comments data."""
    labels = []
    raw_labels = issue.get("labels", [])
    for lbl in raw_labels:
        if isinstance(lbl, dict):
            labels.append(lbl.get("name", ""))
        elif isinstance(lbl, str):
            labels.append(lbl)

    planner_comments = comments
    if anchor_comment_ids:
        planner_comments = []
        for comment in comments:
            comment_id = comment.get("id")
            if comment_id is not None and str(comment_id) in anchor_comment_ids:
                sanitized = dict(comment)
                sanitized["body"] = "[redacted: anchor comment snapshot stored in artifact]"
                planner_comments.append(sanitized)
            else:
                planner_comments.append(comment)

    planner_input: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION_PLANNER_INPUT,
        "issue": {
            "number": issue.get("number"),
            "title": issue.get("title", ""),
            "body": issue.get("body", ""),
            "labels": labels,
        },
        "comments": planner_comments,
    }
    if known_context is not None:
        planner_input["known_context"] = known_context
    if anchor_comment_feedback is not None:
        planner_input["anchor_comment_feedback"] = anchor_comment_feedback
    if now is not None:
        planner_input["now"] = now

    return planner_input


def _issue_body_source_ref(issue: dict[str, Any], issue_number: int, repo: str) -> str:
    html_url = issue.get("html_url")
    if isinstance(html_url, str) and html_url:
        return html_url
    url = issue.get("url")
    if isinstance(url, str) and url:
        return url
    return f"https://github.com/{repo}/issues/{issue_number}"


def _issue_artifact_dir(repo_root: Path, issue_number: int) -> Path:
    return repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)


def _snapshot_archive_dir(repo_root: Path, issue_number: int) -> Path:
    return _issue_artifact_dir(repo_root, issue_number) / "snapshots"


def _snapshot_body(snapshot: dict[str, Any]) -> str:
    issue = snapshot.get("issue")
    if not isinstance(issue, dict):
        return ""
    body = issue.get("body", "")
    return body if isinstance(body, str) else ""


def _snapshot_fetched_at(snapshot: dict[str, Any]) -> str:
    value = snapshot.get("fetched_at", "")
    return value if isinstance(value, str) else ""


def _snapshot_archive_path(repo_root: Path, issue_number: int, snapshot: dict[str, Any]) -> Path:
    fetched_at = _snapshot_fetched_at(snapshot) or "unknown"
    safe_fetched_at = re.sub(r"[^0-9A-Za-z_-]+", "_", fetched_at)
    body_sha = _sha256(_snapshot_body(snapshot))
    return _snapshot_archive_dir(repo_root, issue_number) / (f"raw_issue_snapshot_{safe_fetched_at}_{body_sha}.json")


def _snapshot_source_ref(snapshot_path: Path, snapshot: dict[str, Any]) -> str:
    repo = snapshot.get("repo")
    issue_number = snapshot.get("issue_number")
    body_sha = _sha256(_snapshot_body(snapshot))
    fetched_at = _snapshot_fetched_at(snapshot)
    return (
        f"artifact:{snapshot_path.resolve()}"
        f"#repo={repo}&issue_number={issue_number}"
        f"&body_sha256={body_sha}&fetched_at={fetched_at}"
    )


def _load_valid_issue_snapshot(path: Path, issue_number: int, repo: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != "raw_issue_snapshot/v1":
        return None
    if payload.get("issue_number") != issue_number or payload.get("repo") != repo:
        return None
    issue = payload.get("issue")
    if not isinstance(issue, dict) or not isinstance(issue.get("body"), str):
        return None
    return payload


def _materialize_immutable_snapshot(
    repo_root: Path,
    issue_number: int,
    snapshot: dict[str, Any],
) -> Path:
    archive_dir = _snapshot_archive_dir(repo_root, issue_number)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = _snapshot_archive_path(repo_root, issue_number, snapshot)
    if not archive_path.exists():
        archive_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
    return archive_path


def _find_previous_immutable_snapshot(
    repo_root: Path,
    issue_number: int,
    repo: str,
    current_snapshot: dict[str, Any],
) -> tuple[dict[str, Any], str] | None:
    archive_dir = _snapshot_archive_dir(repo_root, issue_number)
    candidates: list[tuple[datetime, dict[str, Any], Path]] = []
    current_fetched_at = _snapshot_fetched_at(current_snapshot)
    try:
        current_sort_key = datetime.fromisoformat(current_fetched_at.replace("Z", "+00:00"))
    except ValueError:
        current_sort_key = None
    if archive_dir.exists():
        for path in archive_dir.glob("raw_issue_snapshot_*.json"):
            snapshot = _load_valid_issue_snapshot(path, issue_number, repo)
            if snapshot is None:
                continue
            fetched_at = _snapshot_fetched_at(snapshot)
            try:
                sort_key = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            except ValueError:
                sort_key = datetime.fromtimestamp(0, tz=timezone.utc)
            if current_sort_key is not None and sort_key >= current_sort_key:
                continue
            candidates.append((sort_key, snapshot, path))

    if not candidates:
        legacy_path = _issue_artifact_dir(repo_root, issue_number) / "raw_issue_snapshot.json"
        snapshot = _load_valid_issue_snapshot(legacy_path, issue_number, repo)
        if snapshot is not None:
            path = _materialize_immutable_snapshot(repo_root, issue_number, snapshot)
            candidates.append(
                (
                    datetime.fromisoformat(_snapshot_fetched_at(snapshot).replace("Z", "+00:00"))
                    if _snapshot_fetched_at(snapshot)
                    else datetime.fromtimestamp(0, tz=timezone.utc),
                    snapshot,
                    path,
                )
            )

    if not candidates:
        return None

    _, snapshot, path = max(candidates, key=lambda item: item[0])
    return snapshot, _snapshot_source_ref(path, snapshot)


def _ensure_scope_signal_delta_input(
    *,
    repo_root: Path,
    issue: dict[str, Any],
    raw_snapshot: dict[str, Any],
    known_context: Optional[dict[str, Any]],
    issue_number: int,
    repo: str,
) -> dict[str, Any]:
    merged = dict(known_context) if known_context else {}
    merged.setdefault("current_phase", "preflight")
    if "scope_signal_delta_input" in merged:
        return merged

    previous_snapshot = _find_previous_immutable_snapshot(
        repo_root,
        issue_number,
        repo,
        raw_snapshot,
    )
    if previous_snapshot is None:
        return merged

    previous_raw_snapshot, previous_source_ref = previous_snapshot
    current_body = issue.get("body", "") or ""
    current_archive_path = _snapshot_archive_path(repo_root, issue_number, raw_snapshot)
    current_source_ref = _snapshot_source_ref(current_archive_path, raw_snapshot)
    merged["scope_signal_delta_input"] = {
        "before_body": _snapshot_body(previous_raw_snapshot),
        "current_body": current_body,
        "after_body": current_body,
        "source_refs": {
            "before": previous_source_ref,
            "current": current_source_ref,
            "after": current_source_ref,
        },
    }
    return merged


def _invoke_repair(body: str) -> dict:
    """
    Invoke repair_issue_contract.py (dry-run) to pre-process the Issue body
    before feeding it to the planner.

    Returns the repair result dict (schema: repair_issue_contract/v1).
    Never raises; on failure returns a minimal dict with error key.
    """
    import tempfile
    import os as _os
    import sys as _sys
    import subprocess as _sp

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as tf:
        tf.write(body)
        tmp_path = tf.name

    try:
        proc = _sp.run(
            [_sys.executable, str(REPAIR_SCRIPT), "--body-file", tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        import json as _json

        if proc.stdout:
            return _json.loads(proc.stdout)
        return {
            "schema": "repair_issue_contract/v1",
            "changed": False,
            "repairs": [],
            "error": proc.stderr or "no output",
        }
    except Exception as exc:
        return {"schema": "repair_issue_contract/v1", "changed": False, "repairs": [], "error": str(exc)}
    finally:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass


def _invoke_planner(planner_input: dict) -> tuple[dict | None, int, str, str]:
    """
    Invoke plan_refinement_loop.py via subprocess.run([sys.executable, ...], shell=False).

    Returns (plan_dict, exit_code, stderr_text, raw_stdout).
    plan_dict is None on JSON parse failure.
    """
    input_json = json.dumps(planner_input, ensure_ascii=False, allow_nan=False)

    try:
        proc = subprocess.run(
            [sys.executable, str(PLANNER_SCRIPT)],
            input=input_json,
            shell=False,
            timeout=PLANNER_TIMEOUT,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return None, 3, f"planner timeout after {PLANNER_TIMEOUT}s", ""
    except FileNotFoundError:
        return None, 3, f"planner script not found: {PLANNER_SCRIPT}", ""
    except Exception as exc:
        return None, 3, f"planner unexpected error: {exc}", ""

    stderr_text = proc.stderr or ""
    exit_code = proc.returncode
    raw_stdout = proc.stdout or ""

    if exit_code not in (0, 2, 3):
        return None, exit_code, stderr_text, raw_stdout

    try:
        plan = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return None, exit_code, f"planner stdout JSON decode error: {exc}", raw_stdout

    return plan, exit_code, stderr_text, raw_stdout


# ---------------------------------------------------------------------------
# warn condition detection
# ---------------------------------------------------------------------------


def _has_unknown_confidence(plan: dict) -> bool:
    """
    Return True if any decision in plan.decisions.*.confidence == "unknown".

    This determines the warn condition:
    planner exit 0 + fail_closed.required == false + >=1 unknown confidence → warn/1.
    """
    decisions = plan.get("decisions", {})
    for _key, policy in decisions.items():
        if isinstance(policy, dict) and policy.get("confidence") == "unknown":
            return True
    return False


# ---------------------------------------------------------------------------
# Exit code mapping
# ---------------------------------------------------------------------------


def _apply_exit_code_mapping(
    planner_exit_code: Optional[int],
    planner_fail_closed: Optional[bool],
    blockers: list[str],
    plan: Optional[dict] = None,
) -> tuple[str, int]:
    """
    Apply the Planner ↔ Wrapper Exit Code Mapping table.

    Returns (status_str, exit_code_int).

    warn condition: planner exit 0 AND fail_closed=false AND >=1 unknown confidence
    → status: warn / exit 1
    """
    # Pre-planner blockers (anchor mismatch, gh failure)
    if blockers:
        anchor_blockers = {
            BLOCKER_ANCHOR_NOT_IN_ISSUE,
            BLOCKER_ANCHOR_REPO_MISMATCH,
            BLOCKER_ANCHOR_ISSUE_NUMBER_MISMATCH,
            BLOCKER_ANCHOR_COMMENT_NOT_FOUND,
            BLOCKER_ANCHOR_ISSUE_URL_MISMATCH,
            BLOCKER_ANCHOR_IS_PR_REVIEW,
            BLOCKER_INPUT_SCHEMA_INVALID,
            BLOCKER_INVALID_ARGS,
        }
        env_blockers = {
            BLOCKER_GH_FAILURE,
            BLOCKER_RESULT_SCHEMA_INVALID,
        }
        has_env = any(b in env_blockers for b in blockers)
        has_anchor = any(b in anchor_blockers for b in blockers)
        # AC6: REWRITE_CONSTRAINTS_* and planner_fail_closed_payload_invalid
        # are environment failures (payload integrity), not issue blockers.
        rewrite_env_blockers = {
            BLOCKER_REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD,
            BLOCKER_REWRITE_CONSTRAINTS_NOT_JSON_SERIALIZABLE,
            BLOCKER_REWRITE_CONSTRAINTS_INVARIANT_VIOLATION,
            BLOCKER_PLANNER_FAIL_CLOSED_PAYLOAD_INVALID,
            BLOCKER_ARTIFACT_PROJECTION_MISMATCH,
        }
        if any(any(b.split(":", 1)[0] == rb for rb in rewrite_env_blockers) for b in blockers):
            has_env = True

        if has_env:
            return "environment_failure", EXIT_ENVIRONMENT_FAILURE
        if has_anchor:
            return "blocked", EXIT_BLOCKED
        # Other pre-planner blockers → blocked
        return "blocked", EXIT_BLOCKED

    if planner_exit_code is None:
        return "environment_failure", EXIT_ENVIRONMENT_FAILURE

    if planner_exit_code == 2:
        return "blocked", EXIT_BLOCKED

    if planner_exit_code == 3:
        return "environment_failure", EXIT_ENVIRONMENT_FAILURE

    if planner_exit_code == 0:
        if planner_fail_closed is True:
            return "blocked", EXIT_BLOCKED
        # Check warn condition: >=1 decision has confidence: unknown
        if plan is not None and _has_unknown_confidence(plan):
            return "warn", EXIT_WARN
        return "pass", EXIT_PASS

    # Unknown exit code
    return "environment_failure", EXIT_ENVIRONMENT_FAILURE


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------


def _write_artifacts(
    repo_root: Path,
    issue_number: int,
    raw_snapshot: dict,
    planner_input: dict,
    result: dict,
) -> dict[str, str]:
    """
    Write artifacts to .claude/artifacts/issue-refinement-loop/<issue_number>/.

    Returns {artifact_key: absolute_path_str}.
    issue_number is int-normalized; path is NOT generated from repo name or URL.

    Writes:
      - raw_issue_snapshot.json  (raw issue + comments)
      - planner_input.json       (planner stdin JSON, byte-stable)
      - refinement_preflight_result_v1.json (canonical result)
    """
    artifact_dir = _issue_artifact_dir(repo_root, issue_number)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    snapshot_path = artifact_dir / "raw_issue_snapshot.json"
    planner_input_path = artifact_dir / "planner_input.json"
    result_path = artifact_dir / "refinement_preflight_result_v1.json"
    artifacts = {
        "raw_issue_snapshot": str(snapshot_path),
        "planner_input": str(planner_input_path),
        "refinement_preflight_result_v1": str(result_path),
    }
    if result.get("artifacts") != artifacts:
        raise ValueError("final_result_artifact_projection_mismatch")
    schema_errors = _validate_result_artifact(result)
    if schema_errors:
        raise ValueError("final_result_schema_invalid: " + "; ".join(schema_errors))
    _atomic_write_json(snapshot_path, raw_snapshot)
    _materialize_immutable_snapshot(repo_root, issue_number, raw_snapshot)
    _atomic_write_json(planner_input_path, planner_input)
    _atomic_write_json(result_path, result)
    try:
        readback = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"result_artifact_readback_error:{type(exc).__name__}:{exc}") from exc
    readback_errors = _validate_result_artifact(readback)
    if readback_errors:
        raise ValueError("result_artifact_readback_schema_invalid: " + "; ".join(readback_errors))
    return artifacts


def _atomic_write_json(path: Path, value: Any) -> None:
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _validate_result_artifact(result: Any) -> list[str]:
    schema = _load_schema("refinement_preflight_result_v1.schema.json")
    if schema is None:
        return ["result_schema_unavailable"]
    valid, errors = _validate_with_schema(result, schema)
    return [] if valid else errors


# ---------------------------------------------------------------------------
# Compact stdout projection
# ---------------------------------------------------------------------------


def _build_compact_stdout(result: dict) -> str:
    """
    Build agent-friendly compact projection of result for stdout.

    Canonical fields (always emitted if present):
      STATUS, NEXT_ACTION, MUST_READ, COMMANDS, BLOCKERS, ARTIFACT

    MUST NOT include raw issue body, raw comments, or any sentinel-containing fields.
    DO_NOT_READ is a reserved field and is intentionally not emitted here.
    EVIDENCE (raw body/comments) is never emitted to stdout.
    """
    lines = [
        f"STATUS: {result['status']}",
        f"NEXT_ACTION: {result['next_action']}",
    ]

    must_read = result.get("must_read", [])
    if must_read:
        lines.append("MUST_READ:")
        for p in must_read:
            lines.append(f"  - {p}")

    commands = result.get("commands", [])
    if commands:
        try:
            from command_registry import REGISTRY as _REG

            spec_objects = []
            for cmd in commands:
                cmd_id = cmd.get("id") or cmd.get("kind", "?")
                entry = _REG.get(cmd_id, {})
                spec_objects.append(
                    {
                        "id": cmd_id,
                        "argv": cmd.get("argv", []),
                        "shell": cmd.get("shell", False),
                        "cwd_policy": entry.get("cwd_policy", "repo_root"),
                        "stdin_contract": entry.get("stdin_contract", "none"),
                        "stdout_contract": entry.get("stdout_contract", "unknown"),
                        "timeout_seconds": entry.get("timeout_seconds", 120),
                        "mutation": entry.get("mutation", False),
                    }
                )
            lines.append("COMMANDS_JSON: " + json.dumps(spec_objects, ensure_ascii=False, separators=(",", ":")))
            lines.append("COMMANDS_DISPLAY:")
            for cmd in commands:
                argv_str = " ".join(str(a) for a in cmd.get("argv", []))
                lines.append(f"  display: [{cmd.get('id') or cmd.get('kind', '?')}] {argv_str}")
        except ImportError:
            lines.append("COMMANDS:")
            for cmd in commands:
                argv_str = " ".join(cmd.get("argv", []))
                lines.append(f"  - [{cmd.get('kind', '?')}] {argv_str}")

    blockers = result.get("blockers", [])
    if blockers:
        lines.append("BLOCKERS:")
        for b in blockers:
            lines.append(f"  - {b}")

    required_sections = result.get("required_sections", [])
    if required_sections:
        lines.append("REQUIRED_SECTIONS:")
        for section in required_sections:
            lines.append(f"  - {section}")

    required_contract_keys = result.get("required_contract_keys", [])
    if required_contract_keys:
        lines.append("REQUIRED_CONTRACT_KEYS:")
        for key in required_contract_keys:
            lines.append(f"  - {key}")

    rewrite_constraints = result.get("rewrite_constraints")
    if rewrite_constraints:
        lines.append("REWRITE_CONSTRAINTS:")
        rewritten = json.dumps(
            rewrite_constraints,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        lines.append(f"  {rewritten}")

    artifacts = result.get("artifacts", {})
    if artifacts:
        lines.extend(_render_artifact_projection_lines(artifacts))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def _build_result(
    *,
    status: str,
    issue_number: int,
    repo: str,
    planner_exit_code: Optional[int],
    planner_fail_closed: Optional[bool],
    next_action: str,
    must_read: list[str],
    do_not_read: list[str],
    commands: list[dict],
    blockers: list[str],
    planner_fail_closed_reason_codes: list[str],
    required_sections: list[str],
    required_contract_keys: list[str],
    rewrite_constraints: Optional[dict[str, Any]],
    artifacts: dict[str, str],
    hashes: dict[str, str],
    contract_update: Optional[dict[str, Any]] = None,
) -> dict:
    """Build a refinement_preflight_result/v1 compliant dict."""
    result = {
        "schema_version": SCHEMA_VERSION_RESULT,
        "status": status,
        "issue_number": issue_number,
        "repo": repo,
        "planner_exit_code": planner_exit_code,
        "planner_fail_closed": planner_fail_closed,
        "next_action": next_action,
        "must_read": must_read,
        "do_not_read": do_not_read,
        "commands": commands,
        "blockers": blockers,
        "planner_fail_closed_reason_codes": planner_fail_closed_reason_codes,
        "required_sections": required_sections,
        "required_contract_keys": required_contract_keys,
        "artifacts": artifacts,
        "hashes": hashes,
    }
    if rewrite_constraints is not None:
        result["rewrite_constraints"] = rewrite_constraints
    if contract_update is not None:
        result["contract_update"] = contract_update
    return result


def _bounded_contract_update_handoff(consumer_result: dict[str, Any]) -> dict[str, Any]:
    """Project the transaction-local consumer result into the existing result.

    This intentionally retains only the phase outcome needed by the parent
    orchestrator.  Source bodies, candidate bodies, operation identities, and
    transaction payloads remain local to the controlled phase.
    """
    raw_status = consumer_result.get("status")
    states = consumer_result.get("states")
    state = states.get("contract_update", {}).get("status") if isinstance(states, dict) else None
    iterations = consumer_result.get("iterations", 0)
    if raw_status == "applied" and iterations == 1:
        status = "rebased"
    else:
        status = state if state in {"applied", "no_change", "rebased"} else "failed"
    fresh = consumer_result.get("fresh_checks")
    if not isinstance(fresh, dict):
        fresh = {}
    return {
        "status": status,
        "writes": int(consumer_result.get("writes", 0)) if isinstance(consumer_result.get("writes", 0), int) else 0,
        "iterations": int(iterations) if isinstance(iterations, int) else 0,
        "final_readback": "verified" if raw_status in {"applied", "no_change"} else "failed",
        "fresh_preflight": str(fresh.get("preflight", "unavailable")),
        "fresh_review": str(fresh.get("review", "unavailable")),
        "fresh_readiness": str(fresh.get("readiness", "unavailable")),
    }


def _commands_from_plan(plan: dict, issue_number: int, repo: str) -> list[dict]:
    """Build commands[] from ISSUE_REFINEMENT_COMMAND_REGISTRY_V1 preflight.run entry."""
    try:
        from command_registry import render_command as _render_command, REGISTRY as _REGISTRY

        _entry = _REGISTRY.get("preflight.run", {})
        _argv = _render_command("preflight.run", {"issue_number": issue_number, "repo": repo})
    except Exception as exc:
        raise RuntimeError(f"BLOCKER_COMMAND_REGISTRY_UNAVAILABLE: command_registry render failed: {exc}") from exc
    return [
        {
            "kind": "run_preflight",
            "argv": _argv,
            "shell": False,
            "source": "registry",
        }
    ]


def _emit_failure_result(
    *,
    repo_root: Path,
    issue_number: int,
    repo: str,
    status: str,
    next_action: str,
    blockers: list[str],
    planner_exit_code: Optional[int] = None,
    planner_fail_closed: Optional[bool] = None,
    planner_input: Optional[dict] = None,
    raw_snapshot: Optional[dict] = None,
    planner_fail_closed_reason_codes: Optional[list[str]] = None,
    required_sections: Optional[list[str]] = None,
    required_contract_keys: Optional[list[str]] = None,
    rewrite_constraints: Optional[dict[str, Any]] = None,
) -> tuple[dict, int]:
    """
    Build a failure/blocked/environment_failure result, write artifacts if available,
    print compact stdout, and return (result, exit_code).

    This helper ensures stdout and disk are written from the same final result dict
    (no post-write mutation).
    """
    # Compute hashes if raw_snapshot available
    hashes: dict[str, str] = {}
    if raw_snapshot is not None:
        snapshot_text = json.dumps(raw_snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False)
        hashes["raw_issue_snapshot_sha256"] = _sha256(snapshot_text)
    if planner_input is not None:
        planner_input_text = json.dumps(planner_input, sort_keys=True, ensure_ascii=False, allow_nan=False)
        hashes["planner_input_sha256"] = _sha256(planner_input_text)

    artifacts: dict[str, str] = {}
    if raw_snapshot is not None and planner_input is not None:
        artifact_dir = _issue_artifact_dir(repo_root, issue_number)
        artifacts = {
            "raw_issue_snapshot": str(artifact_dir / "raw_issue_snapshot.json"),
            "planner_input": str(artifact_dir / "planner_input.json"),
            "refinement_preflight_result_v1": str(artifact_dir / "refinement_preflight_result_v1.json"),
        }

    result = _build_result(
        status=status,
        issue_number=issue_number,
        repo=repo,
        planner_exit_code=planner_exit_code,
        planner_fail_closed=planner_fail_closed,
        next_action=next_action,
        must_read=[],
        do_not_read=[],
        commands=[],
        blockers=blockers,
        planner_fail_closed_reason_codes=planner_fail_closed_reason_codes or [],
        required_sections=required_sections or [],
        required_contract_keys=required_contract_keys or [],
        rewrite_constraints=rewrite_constraints,
        artifacts=artifacts,
        hashes=hashes,
    )

    if raw_snapshot is not None and planner_input is not None:
        try:
            _write_artifacts(repo_root, issue_number, raw_snapshot, planner_input, result)
        except Exception as exc:
            result["blockers"] = [
                *result["blockers"],
                BLOCKER_RESULT_SCHEMA_INVALID,
                f"failure_artifact_write_error:{type(exc).__name__}:{str(exc)[:500]}",
            ]
            result["status"] = "environment_failure"
            result["next_action"] = "fix_environment"
            result["artifacts"] = {}

    _, exit_code = _apply_exit_code_mapping(planner_exit_code, planner_fail_closed, result["blockers"])
    if result["status"] == "environment_failure":
        exit_code = EXIT_ENVIRONMENT_FAILURE
    print(_build_compact_stdout(result))
    return result, exit_code


# ---------------------------------------------------------------------------
# SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 (#1323) -- freeform human review comment
# evidence builder (independent of the structured ANCHOR_SCOPE_REFRAME_V1
# payload consumed by _classify_anchor_scope_reframe above).
# ---------------------------------------------------------------------------

# Heavy mutation categories (#1891 AC6): mutation intents that require an
# explicit owner-sourced decision before they may proceed. Non-heavy
# categories continue with a warning instead of a hard stop.
HEAVY_MUTATION_CATEGORIES = frozenset(
    {
        "close",
        "not_planned",
        "replacement_issue_creation",
        "dependency_removal",
        "parent_child_change",
    }
)


def _classify_heavy_mutation_gate(
    *,
    mutation_category: "str | None",
    scope_delta_decision: "dict | None",
) -> dict:
    """#1891 AC6: fail-closed gate for heavy mutation categories.

    Heavy mutation categories (close / not planned / replacement Issue
    creation / dependency removal / parent-child relationship change) are
    blocked unless `scope_delta_decision` reflects an explicit owner-sourced
    decision (`status == "approved_by_trusted_anchor"` and
    `anchor_author_association == "OWNER"`). Non-heavy mutation categories
    (ordinary body improvement / additional investigation / review
    continuation) are never blocked here -- they continue with a `warn`
    status, matching the pre-existing advisory-only behavior.
    """
    decision = scope_delta_decision or {}
    owner_explicit = (
        decision.get("status") == "approved_by_trusted_anchor"
        and decision.get("anchor_author_association") == "OWNER"
    )
    is_heavy = mutation_category in HEAVY_MUTATION_CATEGORIES

    if owner_explicit:
        return {
            "mutation_category": mutation_category,
            "is_heavy_mutation": is_heavy,
            "status": "allowed",
            "fail_closed": False,
            "reason": "owner_explicit_decision_present",
        }

    if is_heavy:
        return {
            "mutation_category": mutation_category,
            "is_heavy_mutation": True,
            "status": "blocked",
            "fail_closed": True,
            "reason": "heavy_mutation_requires_owner_explicit_decision",
        }

    return {
        "mutation_category": mutation_category,
        "is_heavy_mutation": False,
        "status": "warn",
        "fail_closed": False,
        "reason": "non_heavy_mutation_warning_continue",
    }


def _apply_multi_turn_candidate_route(
    scope_delta_decision: dict,
    segments_result: "dict | None",
    candidates_result: "dict | None",
) -> dict:
    """#1891 AC4: route to human judgment when anchor_context.py finds
    multiple unclassified candidates spread across a genuine multi-turn
    transcript (>=2 marker-delimited segments).

    This does not fire for an ordinary single-turn review comment that
    happens to contain several bullet points -- it is scoped to the
    multi-turn/supersession scenario this Issue targets, so it never
    auto-selects a single winning candidate and never silently overrides an
    existing fail-closed decision with a weaker one.
    """
    if not isinstance(segments_result, dict) or not isinstance(candidates_result, dict):
        return scope_delta_decision

    marked_segments = [seg for seg in segments_result.get("segments", []) if seg.get("marker")]
    candidates = candidates_result.get("candidates", [])

    if len(marked_segments) >= 2 and len(candidates) >= 2:
        updated = dict(scope_delta_decision)
        updated["status"] = "fail_closed"
        updated["reason"] = "multi_turn_anchor_context_requires_human_judgment"
        updated["anchor_context_candidate_count"] = len(candidates)
        updated["anchor_context_marked_segment_count"] = len(marked_segments)
        updated["implementation_go"] = False
        return updated

    return scope_delta_decision


def _build_scope_delta_authority_evidence(
    *,
    comment_payload: dict,
    comment_body: str,
    repo: str,
    issue_number: int,
    anchor_url: str,
    captured_at: str,
    segments_result: "dict | None" = None,
    candidates_result: "dict | None" = None,
):
    """#1323: build SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 from an anchor comment.

    Unlike _classify_anchor_scope_reframe (which requires a structured
    ANCHOR_SCOPE_REFRAME_V1 fenced yaml payload), this works on freeform
    human review comments (e.g. Issue #1270's Revised Acceptance Criteria
    comment) so explicit human-review directives are not silently dropped
    just because they are not machine-formatted.

    Returns None (fail-closed) when the anchor URL does not structurally
    resolve to an issue-comment on `issue_number` in `repo` (AC16). Never
    forwards the raw comment body -- only sha256 + extracted markers /
    directives / boundary flags (AC14).

    #1891 AC4 (iteration 2, PR #1923 OWNER REQUEST_CHANGES): `segments_result`
    / `candidates_result` are the anchor_context.py pure-analyzer outputs for
    this same `comment_body`. They are accepted here (instead of only being
    threaded through a disconnected downstream call) so the multi-turn
    candidate route is wired at the same call site that builds the scope
    delta authority evidence, per the Issue's In Scope wiring requirement.
    They are intentionally NOT added to the returned
    SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 dict (its schema is `additionalProperties:
    false`); the caller applies `_apply_multi_turn_candidate_route()` to
    `scope_delta_decision` immediately after this call using the same
    `segments_result` / `candidates_result` values.
    """
    try:
        from scope_signal_delta import (
            classify_directive_confidence,
            detect_boundary_flags,
            extract_directive_items,
            extract_directive_markers,
            parse_issue_comment_url,
        )
    except ImportError:
        return None

    parsed = parse_issue_comment_url(anchor_url)
    if parsed is None:
        return None
    if f"{parsed['owner']}/{parsed['repo']}".lower() != repo.lower():
        return None
    if parsed["issue_number"] != issue_number:
        return None

    author_association = comment_payload.get("author_association")
    user = comment_payload.get("user")
    if isinstance(user, dict):
        author_login = user.get("login")
        author_type = user.get("type") or "unknown"
    else:
        author_login = comment_payload.get("author_login")
        author_type = comment_payload.get("author_type") or "unknown"

    markers = extract_directive_markers(comment_body)
    directives = extract_directive_items(comment_body)
    confidence = classify_directive_confidence(comment_body, markers)
    boundary_flags_map = detect_boundary_flags(comment_body)
    boundary_flag_names = [name for name, value in boundary_flags_map.items() if value]

    issue_url = f"https://github.com/{repo}/issues/{issue_number}"

    return {
        "schema_version": "SCOPE_DELTA_AUTHORITY_EVIDENCE_V1",
        "source_kind": "issue_comment",
        "source_ref": anchor_url,
        "source_issue_number": issue_number,
        "comment_id": comment_payload.get("id"),
        "comment_url": anchor_url,
        "issue_url": issue_url,
        "body_sha256": _sha256(comment_body),
        "author_login": author_login,
        "author_type": author_type,
        "author_association": author_association,
        "captured_at": captured_at,
        "directive_markers": markers,
        "extracted_directives": directives,
        "ambiguity_flags": [] if confidence != "ambiguous" else ["structured_list_missing"],
        "boundary_flags": boundary_flag_names,
        "confidence": confidence,
    }


def consume_trusted_anchor_contract_patch_plan(
    *,
    repo: str,
    issue_number: int,
    issue: dict,
    anchor_url: str,
    anchor_payload: dict,
    anchor_body: str,
    contract_patch_plan: dict,
    callbacks: Optional[dict[str, Any]] = None,
) -> dict:
    """Connect an approved patch plan to the controlled transaction lane.

    The planner remains read-only.  This explicit consumer is called only by
    the opt-in preflight execution path after a trusted directive has produced
    an existing ``CONTRACT_PATCH_PLAN_V1``.  Its default callbacks use the
    existing readiness checker and ``edit_issue_txn.py``; tests can inject
    fixture callbacks without a GitHub mutation.
    """
    from scope_signal_delta import run_trusted_anchor_iteration_zero

    callbacks = callbacks or {}
    temporary_paths: list[Path] = []

    def fetch_current() -> tuple[dict, dict]:
        injected = callbacks.get("fetch_current")
        if injected is not None:
            return injected()
        current_issue, issue_error = _fetch_issue(repo, issue_number)
        if current_issue is None:
            raise RuntimeError(f"issue_readback_failed:{issue_error}")
        comment_id = anchor_payload.get("id")
        current_anchor, anchor_error = _fetch_single_comment(repo, int(comment_id))
        if current_anchor is None:
            raise RuntimeError(f"anchor_readback_failed:{anchor_error}")
        current_anchor = dict(current_anchor)
        current_anchor["html_url"] = anchor_url
        current_anchor["source_body_sha256"] = f"sha256:{_sha256(current_anchor.get('body', ''))}"
        return current_issue, current_anchor

    def candidate_readiness(candidate_body: str) -> dict:
        injected = callbacks.get("candidate_readiness")
        if injected is not None:
            return injected(candidate_body)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8", delete=False) as handle:
            handle.write(candidate_body)
            candidate_path = Path(handle.name)
        temporary_paths.append(candidate_path)
        readiness_script = (
            _SCRIPTS_DIR.parent.parent / "issue-contract-review" / "scripts" / "contract_readiness_check.py"
        )
        completed = subprocess.run(
            [sys.executable, str(readiness_script), "--body-file", str(candidate_path), "--mode", "static"],
            capture_output=True,
            text=True,
            shell=False,
            timeout=30,
        )
        try:
            readiness = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return {
                "status": "input_or_runtime_error",
                "body_sha256": f"sha256:{_sha256(candidate_body)}",
                "source_checks": [],
                "errors": [],
                "readiness_result_ref": "transaction-local",
            }
        if not isinstance(readiness, dict):
            return {
                "status": "input_or_runtime_error",
                "body_sha256": f"sha256:{_sha256(candidate_body)}",
                "source_checks": [],
                "errors": [],
                "readiness_result_ref": "transaction-local",
            }
        readiness["readiness_result_ref"] = "transaction-local"
        return readiness

    def apply_transaction(current_issue: dict, candidate_body: str, readiness: dict) -> dict:
        injected = callbacks.get("apply_transaction")
        if injected is not None:
            return injected(current_issue, candidate_body, readiness)
        repo_root = _find_repo_root()
        candidate_path = repo_root / "tmp" / f"issue_{issue_number}_trusted_anchor_candidate.md"
        input_path = repo_root / "tmp" / f"issue_{issue_number}_trusted_anchor_txn.json"
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_text(candidate_body, encoding="utf-8")
        temporary_paths.extend([candidate_path, input_path])
        from scope_signal_delta import build_issue_edit_txn_input

        # The readiness checker exposes a richer result for callers, while
        # edit_issue_txn.py intentionally accepts only its closed forwarding
        # payload. Keep this narrowing transaction-local so the controlled
        # executor's input validation remains strict.
        readiness_forwarding = {
            key: readiness[key]
            for key in (
                "status",
                "body_sha256",
                "source_checks",
                "errors",
                "readiness_result_ref",
                "resolution_evidence",
            )
            if key in readiness
        }
        transaction_input = build_issue_edit_txn_input(
            issue_number=issue_number,
            repo=repo,
            previous_body_sha256=f"sha256:{_sha256(current_issue.get('body', ''))}",
            previous_updated_at=current_issue["updatedAt"],
            new_body_file=str(candidate_path.relative_to(repo_root)),
            readiness_result=readiness_forwarding,
        )
        input_path.write_text(json.dumps(transaction_input, ensure_ascii=False), encoding="utf-8")
        transaction_script = _SCRIPTS_DIR.parent.parent / "edit-issue" / "scripts" / "edit_issue_txn.py"
        completed = subprocess.run(
            [sys.executable, str(transaction_script), "--input-file", str(input_path.relative_to(repo_root))],
            capture_output=True,
            text=True,
            shell=False,
            cwd=str(repo_root),
            timeout=60,
        )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return {"status": "failed_no_mutation"}
        return result if isinstance(result, dict) else {"status": "failed_no_mutation"}

    def fresh_checks(current_issue: dict) -> dict:
        injected = callbacks.get("fresh_checks")
        if injected is not None:
            return injected(current_issue)
        readiness = candidate_readiness(current_issue.get("body", ""))
        with contextlib.redirect_stdout(io.StringIO()):
            preflight_result, _ = run_preflight(
                issue_number=issue_number,
                repo=repo,
                anchor_comment_urls=[anchor_url],
                consume_contract_patch_plan=False,
            )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8", delete=False) as handle:
            handle.write(current_issue.get("body", ""))
            review_body_path = Path(handle.name)
        temporary_paths.append(review_body_path)
        review_script = _SCRIPTS_DIR.parent.parent / "review-issue" / "scripts" / "check_issue_contract.py"
        review = subprocess.run(
            [sys.executable, str(review_script), "--file", str(review_body_path), "--json"],
            capture_output=True,
            text=True,
            shell=False,
            timeout=30,
        )
        return {
            "preflight": preflight_result.get("status"),
            "review": "approve" if review.returncode == 0 else "needs_fix" if review.returncode == 1 else "unavailable",
            "readiness": readiness.get("status"),
        }

    normalized_anchor = dict(anchor_payload)
    normalized_anchor["html_url"] = anchor_url
    normalized_anchor["source_body_sha256"] = f"sha256:{_sha256(anchor_body)}"
    try:
        return run_trusted_anchor_iteration_zero(
            repo=repo,
            issue_number=issue_number,
            issue=issue,
            anchor=normalized_anchor,
            anchor_body=anchor_body,
            patch_plan=contract_patch_plan,
            candidate_readiness=candidate_readiness,
            fetch_current=fetch_current,
            apply_transaction=apply_transaction,
            fresh_checks=fresh_checks,
        )
    finally:
        for path in temporary_paths:
            try:
                path.unlink()
            except OSError:
                pass


def _satisfied_trusted_directive_noop_patch_plan(
    *,
    plan: dict[str, Any],
    issue: dict[str, Any],
    repo: str,
    issue_number: int,
    anchor_url: str,
    anchor_payload: dict[str, Any],
    anchor_body: str,
) -> dict[str, Any] | None:
    """Reconstruct only a proven-satisfied trusted directive as a no-op.

    The planner intentionally omits a patch plan after it sees that a
    previously applied directive is already present.  Contract-update mode
    must not turn that safe replay into a failed mutation phase.  This helper
    is deliberately narrower than a generic missing-plan fallback: it accepts
    only the planner's ``no_scope_signal`` outcome, then independently derives
    explicit trusted operations and requires the section-aware builder to
    report no body change.
    """
    sidecar = plan.get("scope_signal_guard_decision_v2")
    if not isinstance(sidecar, dict):
        return None
    raw_signal = sidecar.get("raw_signal")
    if not isinstance(raw_signal, dict) or raw_signal.get("triggered") is not False:
        return None
    if raw_signal.get("reason_code") != "no_scope_signal":
        return None

    try:
        from scope_signal_delta import (
            build_contract_patch_plan_v1,
            build_section_aware_candidate_body,
            derive_contract_patch_operations,
            normalize_trusted_anchor_iteration_zero,
        )
    except ImportError:
        return None

    evidence = _build_scope_delta_authority_evidence(
        comment_payload=anchor_payload,
        comment_body=anchor_body,
        repo=repo,
        issue_number=issue_number,
        anchor_url=anchor_url,
        captured_at=_now_iso(),
    )
    if not isinstance(evidence, dict) or evidence.get("confidence") != "explicit":
        return None
    if evidence.get("boundary_flags"):
        return None
    normalized = normalize_trusted_anchor_iteration_zero(
        repo=repo,
        issue_number=issue_number,
        anchor=anchor_payload,
        source_body=anchor_body,
    )
    if not normalized.get("accepted"):
        return None
    operations = derive_contract_patch_operations([evidence])
    if not operations:
        return None
    candidate = build_section_aware_candidate_body(
        body=issue.get("body", ""),
        operations=operations,
        source_identity=normalized["source_identity"],
    )
    if candidate.get("changed"):
        return None
    return build_contract_patch_plan_v1(
        target_issue_number=issue_number,
        base_issue_body_sha256=_sha256(issue.get("body", "")),
        source_evidence=[evidence],
        operations=operations,
    )


def run_preflight(
    issue_number: int,
    repo: str,
    anchor_comment_urls: list[str],
    fixture_path: Optional[Path] = None,
    known_context: Optional[dict] = None,
    now: Optional[str] = None,
    consume_contract_patch_plan: bool = False,
    contract_update_callbacks: Optional[dict[str, Any]] = None,
) -> tuple[dict, int]:
    """
    Main preflight logic.

    Returns (result_dict, exit_code).
    Writes artifacts and prints compact stdout.

    Artifact write guarantee: stdout and disk are written from the same final
    result dict (no post-write mutation). This ensures AC6 failure-path consistency.
    """
    repo_root = _find_repo_root()
    blockers: list[str] = []
    planner_exit_code: Optional[int] = None
    planner_fail_closed: Optional[bool] = None
    planner_fail_closed_reason_codes: list[str] = []
    required_sections: list[str] = []
    required_contract_keys: list[str] = []
    rewrite_constraints: Optional[dict[str, Any]] = None
    planner_input_dict: Optional[dict] = None
    raw_snapshot: Optional[dict] = None
    anchor_payload_for_consumer: Optional[dict] = None
    anchor_body_for_consumer: Optional[str] = None
    anchor_url_for_consumer: Optional[str] = None
    contract_update_handoff: Optional[dict[str, Any]] = None

    # --- Load data (fixture or live gh) ---
    if fixture_path is not None:
        # Fixture mode: load pre-fetched snapshot
        try:
            fixture_raw = fixture_path.read_text(encoding="utf-8")
            fixture_data = json.loads(fixture_raw)
        except Exception as exc:
            result = _build_result(
                status="environment_failure",
                issue_number=issue_number,
                repo=repo,
                planner_exit_code=None,
                planner_fail_closed=None,
                next_action="fix_environment",
                must_read=[],
                do_not_read=[],
                commands=[],
                blockers=[f"FIXTURE_LOAD_ERROR: {exc}"],
                planner_fail_closed_reason_codes=[],
                required_sections=[],
                required_contract_keys=[],
                rewrite_constraints=None,
                artifacts={},
                hashes={},
            )
            print(_build_compact_stdout(result))
            return result, EXIT_ENVIRONMENT_FAILURE

        # Validate fixture input against input schema (fail-closed on unknown input)
        input_schema = _load_schema("refinement_preflight_input.schema.json")
        if input_schema is not None:
            is_valid, schema_errors = _validate_with_schema(fixture_data, input_schema)
            if not is_valid:
                err_detail = "; ".join(schema_errors)
                return _emit_failure_result(
                    repo_root=repo_root,
                    issue_number=issue_number,
                    repo=repo,
                    status="blocked",
                    next_action="human_judgment_required",
                    blockers=[BLOCKER_INPUT_SCHEMA_INVALID, f"input_schema_errors: {err_detail}"],
                    planner_fail_closed_reason_codes=[],
                    required_sections=[],
                    required_contract_keys=[],
                    rewrite_constraints=None,
                )

        issue = fixture_data.get("issue", {})
        comments = fixture_data.get("comments", [])
        fixture_anchor_comments = fixture_data.get("anchor_comments", [])
        fixture_anchor_urls = fixture_data.get("anchor_comment_urls", anchor_comment_urls)

        # Use fixture anchor data for structural validation
        active_anchor_urls = fixture_anchor_urls or anchor_comment_urls
        fixture_comment_lookup = fixture_anchor_comments if fixture_anchor_comments else comments
        known_context = known_context or fixture_data.get("known_context")
        now = now or fixture_data.get("now")
    else:
        # Live mode: fetch from GitHub
        issue, err = _fetch_issue(repo, issue_number)
        if issue is None:
            blockers.append(BLOCKER_GH_FAILURE)
            return _emit_failure_result(
                repo_root=repo_root,
                issue_number=issue_number,
                repo=repo,
                status="environment_failure",
                next_action="fix_environment",
                blockers=blockers,
                planner_fail_closed_reason_codes=[],
                required_sections=[],
                required_contract_keys=[],
                rewrite_constraints=None,
            )

        comments, err = _fetch_issue_comments(repo, issue_number)
        if comments is None:
            blockers.append(BLOCKER_GH_FAILURE)
            return _emit_failure_result(
                repo_root=repo_root,
                issue_number=issue_number,
                repo=repo,
                status="environment_failure",
                next_action="fix_environment",
                blockers=blockers,
                planner_fail_closed_reason_codes=[],
                required_sections=[],
                required_contract_keys=[],
                rewrite_constraints=None,
            )

        active_anchor_urls = anchor_comment_urls
        fixture_comment_lookup = None

    anchor_comment_state: Optional[dict[str, Any]] = None
    anchor_comment_feedback: Optional[dict[str, Any]] = None
    anchor_comment_ids: set[str] = set()

    # --- Anchor comment structural validation ---
    if active_anchor_urls:
        sorted_urls, anchor_blockers = _validate_anchor_comments_batch(
            active_anchor_urls,
            repo,
            issue_number,
            fixture_comments=fixture_comment_lookup,
        )
        if anchor_blockers:
            blockers.extend(anchor_blockers)
            return _emit_failure_result(
                repo_root=repo_root,
                issue_number=issue_number,
                repo=repo,
                status="blocked",
                next_action="human_judgment_required",
                blockers=blockers,
                planner_fail_closed_reason_codes=[],
                required_sections=[],
                required_contract_keys=[],
                rewrite_constraints=None,
            )

        if sorted_urls:
            anchor_url = sorted_urls[0]
            parsed_anchor = _parse_anchor_comment_url(anchor_url)
            comment_id = parsed_anchor.get("comment_id")
            comment_payload = None
            if fixture_comment_lookup is not None:
                for item in fixture_comment_lookup:
                    if str(item.get("id")) == str(comment_id):
                        comment_payload = item
                        break
            else:
                comment_payload, err = _fetch_single_comment(repo, comment_id)
                if comment_payload is None:
                    blockers.append(BLOCKER_GH_FAILURE)
                    return _emit_failure_result(
                        repo_root=repo_root,
                        issue_number=issue_number,
                        repo=repo,
                        status="environment_failure",
                        next_action="fix_environment",
                        blockers=blockers,
                        planner_fail_closed_reason_codes=[],
                        required_sections=[],
                        required_contract_keys=[],
                        rewrite_constraints=None,
                    )

            anchor_comment_state, anchor_errors = _build_anchor_comment_state(
                anchor_url=anchor_url,
                comment=comment_payload,
                issue_number=issue_number,
                captured_at=now or _now_iso(),
            )
            if anchor_errors:
                blockers.extend(anchor_errors)
                return _emit_failure_result(
                    repo_root=repo_root,
                    issue_number=issue_number,
                    repo=repo,
                    status="blocked",
                    next_action="human_judgment_required",
                    blockers=blockers,
                    planner_fail_closed_reason_codes=[],
                    required_sections=[],
                    required_contract_keys=[],
                    rewrite_constraints=None,
                )

            anchor_comment_ids.add(str(comment_payload["id"]))
            anchor_payload_for_consumer = dict(comment_payload)
            anchor_body_for_consumer = anchor_comment_state["snapshot"]
            anchor_url_for_consumer = anchor_url
            anchor_comment_feedback = {
                "url": anchor_comment_state["url"],
                "preliminary_classification": anchor_comment_state["preliminary_classification"],
                "final_classification": anchor_comment_state["final_classification"],
                "classification_reason": anchor_comment_state["classification_reason"],
                "verified_claims": anchor_comment_state["verified_claims"],
                "unresolved_claims": anchor_comment_state["unresolved_claims"],
                "scope_impact": anchor_comment_state["scope_impact"],
                "requires_fact_check": anchor_comment_state["requires_fact_check"],
            }

            # --- Classify ANCHOR_SCOPE_REFRAME_V1 and build scope_delta_decision ---
            scope_delta_decision = _classify_anchor_scope_reframe(
                comment_payload=comment_payload,
                anchor_body=anchor_comment_state["snapshot"],
                repo=repo,
                issue_number=issue_number,
                anchor_url=anchor_url,
            )
            # Propagate to known_context so planner sees anchor_reframe context
            _kc = dict(known_context) if known_context else {}
            # The iteration-zero consumer must cross-check normalized anchor
            # evidence against the live repository even though planner input
            # intentionally omits raw issue URLs.
            _kc["repo"] = repo
            _kc["anchor_reframe"] = scope_delta_decision["status"] == "approved_by_trusted_anchor"
            _kc["anchor_comment_url"] = anchor_url
            _kc["anchor_comment_hash"] = scope_delta_decision.get("anchor_comment_hash", "")
            _kc["scope_delta_decision"] = scope_delta_decision

            # --- #1891: anchor_context.py pure analyzer (segment + candidates) ---
            # anchor_context.py has no GitHub API client of its own (AC8); it
            # only consumes the already-fetched anchor_comment.snapshot body
            # built above. Computed here -- immediately before the
            # _build_scope_delta_authority_evidence() call below -- so both
            # this call and the multi-turn candidate route that follows it
            # share the same segment/candidate outputs at a single call site
            # (Issue #1891 In Scope: "_build_scope_delta_authority_evidence()
            # に anchor_context.py segment + candidates の出力を渡す").
            _anchor_body_for_context = anchor_comment_state["snapshot"]
            _segments_result = None
            _candidates_result = None
            if anchor_context is not None:
                _segments_result = anchor_context.segment_body(_anchor_body_for_context)
                _candidates_result = anchor_context.extract_candidates(_anchor_body_for_context)
                _kc["source_fetch_complete"] = bool(_anchor_body_for_context)
                _kc["source_hash_verified"] = _sha256(_anchor_body_for_context) == scope_delta_decision.get(
                    "anchor_comment_hash"
                )
                _kc["source_ranges_covered"] = anchor_context.compute_source_ranges_covered(
                    _segments_result["segments"], _segments_result["line_count"]
                )
            else:  # pragma: no cover - defensive fallback when import fails
                _kc["source_fetch_complete"] = bool(_anchor_body_for_context)
                _kc["source_hash_verified"] = False
                _kc["source_ranges_covered"] = False

            # --- #1323: build freeform SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 ---
            # (independent of the structured ANCHOR_SCOPE_REFRAME_V1 payload
            # above, so explicit human-review directives in freeform review
            # comments -- e.g. Issue #1270 -- are not dropped just because
            # they carry no machine-formatted reframe marker.)
            _scope_delta_authority_evidence = _build_scope_delta_authority_evidence(
                comment_payload=comment_payload,
                comment_body=anchor_comment_state["snapshot"],
                repo=repo,
                issue_number=issue_number,
                anchor_url=anchor_url,
                captured_at=now or _now_iso(),
                segments_result=_segments_result,
                candidates_result=_candidates_result,
            )
            if _scope_delta_authority_evidence is not None:
                _kc["scope_delta_authority_evidence"] = [_scope_delta_authority_evidence]

            # --- #1891 AC4: route to human judgment on genuine multi-turn
            # ambiguity, using the same _segments_result / _candidates_result
            # this call site just fetched and forwarded above. ---
            _kc["scope_delta_decision"] = _apply_multi_turn_candidate_route(
                _kc["scope_delta_decision"], _segments_result, _candidates_result
            )

            # --- #1891 iteration 2 (PR #1923 OWNER REQUEST_CHANGES): the
            # multi-turn fail_closed route must actually reach `blockers`
            # (the list `_apply_exit_code_mapping()` consumes), not merely
            # live inside known_context where the planner never reads it. ---
            _routed_scope_delta_decision = _kc["scope_delta_decision"]
            if (
                isinstance(_routed_scope_delta_decision, dict)
                and _routed_scope_delta_decision.get("status") == "fail_closed"
                and _routed_scope_delta_decision.get("reason")
                == "multi_turn_anchor_context_requires_human_judgment"
            ):
                blockers.append(BLOCKER_ANCHOR_MULTI_TURN_FAIL_CLOSED)

            known_context = _kc

    # --- Build raw snapshot (for artifact) ---
    raw_snapshot = {
        "schema_version": "raw_issue_snapshot/v1",
        "fetched_at": now or _now_iso(),
        "issue_number": issue_number,
        "repo": repo,
        "issue": issue,
        "comments": comments,
    }
    if anchor_comment_state is not None:
        raw_snapshot["anchor_comment"] = anchor_comment_state

    # --- Run repair pass before planner (Issue #889) ---
    # repair_issue_contract runs dry-run to report defects; the repaired body is
    # NOT fed to the planner (the planner always receives the original Issue body).
    # repair_result is included in the preflight output as repair_diagnostics (BLOCKER 1 fix).
    _repair_result = _invoke_repair(issue.get("body", "") or "")

    # --- #1891 AC6: heavy mutation gate ---
    # Only evaluated when the caller (main thread / orchestrator) has stated
    # an intended mutation_category in known_context; this never fires for
    # ordinary body-improvement preflight runs that do not carry one.
    if known_context and known_context.get("mutation_category"):
        known_context = dict(known_context)
        _heavy_mutation_gate = _classify_heavy_mutation_gate(
            mutation_category=known_context.get("mutation_category"),
            scope_delta_decision=known_context.get("scope_delta_decision"),
        )
        known_context["heavy_mutation_gate"] = _heavy_mutation_gate
        # #1891 iteration 2 (PR #1923 OWNER REQUEST_CHANGES): the heavy
        # mutation gate must actually reach `blockers`, not merely live
        # inside known_context where the planner never reads it.
        if _heavy_mutation_gate.get("fail_closed") is True:
            blockers.append(BLOCKER_HEAVY_MUTATION_FAIL_CLOSED)

    # --- Invoke planner ---
    known_context = _ensure_scope_signal_delta_input(
        repo_root=repo_root,
        issue=issue,
        raw_snapshot=raw_snapshot,
        known_context=known_context,
        issue_number=issue_number,
        repo=repo,
    )
    planner_input_dict = _build_planner_input(
        issue,
        comments,
        known_context,
        anchor_comment_feedback=anchor_comment_feedback,
        anchor_comment_ids=anchor_comment_ids,
        now=now,
    )
    # #1677 AC4: join a previously-persisted scope-rollup artifact (if any)
    # into the planner input so ISSUE_EXECUTION_DECISION_V1 reflects known
    # collisions instead of always defaulting to 'selected'.
    _scope_rollup_plan = _load_scope_rollup_artifact(repo_root, issue_number)
    planner_input_dict = _join_scope_rollup_into_planner_input(planner_input_dict, _scope_rollup_plan)
    plan, planner_exit_code, planner_stderr, planner_stdout_raw = _invoke_planner(planner_input_dict)

    if plan is None:
        # Planner invocation failed
        if planner_exit_code == 2:
            blockers.append(BLOCKER_PLANNER_INVALID_INPUT)
        else:
            blockers.append(BLOCKER_PLANNER_INTERNAL_ERROR)

        # --- Blocker 3: failure classification sidecar ---
        failure_cls = classify_planner_failure(
            exit_code=planner_exit_code,
            stdout=planner_stdout_raw,
            stderr=planner_stderr,
            script_path=PLANNER_SCRIPT,
            python_executable=sys.executable,
        )

        try:
            _cls_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
            _cls_dir.mkdir(parents=True, exist_ok=True)
            (_cls_dir / "planner_failure_classification_v1.json").write_text(
                json.dumps(failure_cls, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
            )
        except Exception:
            pass

        # --- Blocker 1 (failure path): provenance sidecar ---
        try:
            _anchor_url = anchor_comment_urls[0] if anchor_comment_urls else ""
            _status_str_prov, _ = _apply_exit_code_mapping(planner_exit_code, None, blockers)
            _prov = build_provenance(
                repo=repo,
                issue_number=issue_number,
                anchor_comment_url=_anchor_url,
                planner_input=planner_input_dict,
                raw_snapshot=raw_snapshot,
                wrapper_exit_code=EXIT_ENVIRONMENT_FAILURE,
                wrapper_status=_status_str_prov,
                blockers=blockers,
                stderr=planner_stderr or "",
                repo_root=repo_root,
            )
            write_provenance_artifact(repo_root, issue_number, _prov)
        except Exception:
            pass

        status_str, _ = _apply_exit_code_mapping(planner_exit_code, None, blockers)
        if planner_exit_code == 3:
            status_str = "environment_failure"
        return _emit_failure_result(
            repo_root=repo_root,
            issue_number=issue_number,
            repo=repo,
            status=status_str,
            next_action="fix_environment" if status_str == "environment_failure" else "human_judgment_required",
            blockers=blockers,
            planner_fail_closed_reason_codes=[BLOCKER_PLANNER_INTERNAL_ERROR],
            required_sections=[],
            required_contract_keys=[],
            rewrite_constraints=_build_safe_rewrite_constraints([], []),
            planner_exit_code=planner_exit_code,
            planner_fail_closed=True,
            planner_input=planner_input_dict,
            raw_snapshot=raw_snapshot,
        )

    if consume_contract_patch_plan:
        sidecar = plan.get("scope_signal_guard_decision_v2")
        authority = sidecar.get("scope_delta_authority") if isinstance(sidecar, dict) else None
        patch_plan = authority.get("contract_patch_plan") if isinstance(authority, dict) else None
        if (
            not isinstance(patch_plan, dict)
            and anchor_payload_for_consumer is not None
            and anchor_body_for_consumer is not None
            and anchor_url_for_consumer is not None
        ):
            patch_plan = _satisfied_trusted_directive_noop_patch_plan(
                plan=plan,
                issue=issue,
                repo=repo,
                issue_number=issue_number,
                anchor_url=anchor_url_for_consumer,
                anchor_payload=anchor_payload_for_consumer,
                anchor_body=anchor_body_for_consumer,
            )
        if (
            isinstance(patch_plan, dict)
            and anchor_payload_for_consumer is not None
            and anchor_body_for_consumer is not None
            and anchor_url_for_consumer is not None
        ):
            consumer_result = consume_trusted_anchor_contract_patch_plan(
                repo=repo,
                issue_number=issue_number,
                issue=issue,
                anchor_url=anchor_url_for_consumer,
                anchor_payload=anchor_payload_for_consumer,
                anchor_body=anchor_body_for_consumer,
                contract_patch_plan=patch_plan,
                callbacks=contract_update_callbacks,
            )
            contract_update_handoff = _bounded_contract_update_handoff(consumer_result)
            if consumer_result.get("status") not in {"applied", "no_change"}:
                blockers.append(BLOCKER_FAIL_CLOSED)
        else:
            # The explicit mutation phase has no safe action without a
            # planner-produced and provenance-bound patch plan.
            contract_update_handoff = {
                "status": "failed",
                "writes": 0,
                "iterations": 0,
                "final_readback": "failed",
                "fresh_preflight": "unavailable",
                "fresh_review": "unavailable",
                "fresh_readiness": "unavailable",
            }
            blockers.append(BLOCKER_FAIL_CLOSED)

    # --- Extract planner output fields ---
    fail_closed = plan.get("fail_closed", {})
    planner_fail_closed = fail_closed.get("required", False)

    # Build must_read / do_not_read from planner decisions
    must_read: list[str] = []
    do_not_read: list[str] = []

    decisions = plan.get("decisions", {})
    investigation_policy = decisions.get("investigation_policy", {})
    if investigation_policy.get("required"):
        target_paths = investigation_policy.get("target_paths", [])
        must_read.extend(target_paths)

    # Build commands
    commands = _commands_from_plan(plan, issue_number, repo)

    # Planner blockers
    # #1677 AC12 (PR #1767 owner review, P0-4): actually invoke the shared
    # semantic validator here instead of importing it unused. A missing
    # validator import or a semantically invalid issue_execution_decision
    # blocks the preflight rather than silently passing plan.
    _issue_execution_decision = plan.get("issue_execution_decision")
    if isinstance(_issue_execution_decision, dict):
        if validate_issue_execution_decision is None:
            blockers.append(BLOCKER_ISSUE_EXECUTION_DECISION_VALIDATOR_UNAVAILABLE)
        else:
            _ied_violations = validate_issue_execution_decision(_issue_execution_decision)
            if _ied_violations:
                blockers.append(f"{BLOCKER_ISSUE_EXECUTION_DECISION_INVALID}: " + ", ".join(_ied_violations))

    if planner_exit_code == 2:
        blockers.append(BLOCKER_PLANNER_INVALID_INPUT)
    elif planner_exit_code == 3:
        blockers.append(BLOCKER_PLANNER_INTERNAL_ERROR)
    elif planner_exit_code == 0 and planner_fail_closed:
        blockers.append(BLOCKER_FAIL_CLOSED)
        reason_codes, reason_codes_ok = _as_string_list(
            fail_closed.get("reason_codes", []),
            "planner.fail_closed.reason_codes",
            blockers,
        )
        planner_fail_closed_reason_codes = reason_codes
        blockers.extend(reason_codes)
        if not reason_codes_ok:
            rewrite_constraints = _build_safe_rewrite_constraints([], [])

        rc = fail_closed.get("rewrite_constraints", {})
        if not isinstance(rc, dict):
            blockers.append(f"{BLOCKER_REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD}: rewrite_constraints must be an object")
            rewrite_constraints = _build_safe_rewrite_constraints([], [])
            reason_codes_ok = False
        elif not rc.get("schema_version"):
            rewrite_constraints = _build_safe_rewrite_constraints([], [])
        elif reason_codes_ok:
            if not _ensure_json_serializable(rc, "planner.fail_closed.rewrite_constraints", blockers):
                rewrite_constraints = _build_safe_rewrite_constraints([], [])
                reason_codes_ok = False
            else:
                required_sections, sections_ok = _as_string_list(
                    rc.get("required_sections", []),
                    "planner.fail_closed.rewrite_constraints.required_sections",
                    blockers,
                )
                required_contract_keys, keys_ok = _as_string_list(
                    rc.get("required_contract_keys", []),
                    "planner.fail_closed.rewrite_constraints.required_contract_keys",
                    blockers,
                )
                if sections_ok and keys_ok:
                    rewrite_constraints = rc
                else:
                    rewrite_constraints = _build_safe_rewrite_constraints([], [])
                    reason_codes_ok = False

        # AC7: Invariant check — required_sections/required_contract_keys must match
        # the nested must_add_sections/must_add_contract_keys in rewrite_constraints.
        if rewrite_constraints is not None and reason_codes_ok:
            rc_inner = rewrite_constraints.get("rewrite_constraints", {})
            must_add_sections = rc_inner.get("must_add_sections", [])
            must_add_keys = rc_inner.get("must_add_contract_keys", [])
            if list(required_sections) != list(must_add_sections):
                blockers.append(
                    f"{BLOCKER_REWRITE_CONSTRAINTS_INVARIANT_VIOLATION}: "
                    f"required_sections {required_sections!r} != "
                    f"must_add_sections {must_add_sections!r}"
                )
                rewrite_constraints = _build_safe_rewrite_constraints([], [])
                required_sections = []
                required_contract_keys = []
                reason_codes_ok = False
            elif list(required_contract_keys) != list(must_add_keys):
                blockers.append(
                    f"{BLOCKER_REWRITE_CONSTRAINTS_INVARIANT_VIOLATION}: "
                    f"required_contract_keys {required_contract_keys!r} != "
                    f"must_add_contract_keys {must_add_keys!r}"
                )
                rewrite_constraints = _build_safe_rewrite_constraints([], [])
                required_sections = []
                required_contract_keys = []
                reason_codes_ok = False

        if not reason_codes_ok:
            # Schema-safe deterministic forwarding requires aligned payloads.
            # Non-string / non-list fields are treated as schema violation.
            blockers.append(BLOCKER_PLANNER_FAIL_CLOSED_PAYLOAD_INVALID)

    # --- Write repair artifact and update blockers ---
    # BLOCKER 1 fix: repair_diagnostics is exposed via artifact file (not as a top-level result key,
    # which would violate schema additionalProperties: false).
    repair_artifact_path: Optional[str] = None
    try:
        artifact_dir_repair = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
        artifact_dir_repair.mkdir(parents=True, exist_ok=True)
        repair_artifact_file = artifact_dir_repair / "repair_diagnostics.json"
        repair_artifact_file.write_text(
            json.dumps(_repair_result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
        )
        repair_artifact_path = str(repair_artifact_file)
    except Exception:
        pass  # Non-fatal: repair artifact write failure does not block preflight

    # If repair detected changes, add a blocker so orchestrator is informed
    if _repair_result.get("changed") is True and repair_artifact_path is not None:
        blockers.append(
            json.dumps(
                {
                    "kind": "repair_diagnostics",
                    "message": "repair_issue_contract detected changes: see repair artifact for details",
                    "artifact_path": repair_artifact_path,
                }
            )
        )

    # --- Apply exit code mapping (with plan for warn detection, after all blockers finalized) ---
    status, exit_code = _apply_exit_code_mapping(planner_exit_code, planner_fail_closed, blockers, plan=plan)

    # Determine next_action
    if status == "pass":
        next_action = "proceed"
    elif status == "warn":
        next_action = "proceed_with_notes"
    elif status == "blocked":
        next_action = "human_judgment_required"
    else:
        next_action = "fix_environment"

    # --- Compute hashes for byte-stability (after all blockers finalized) ---
    snapshot_text = json.dumps(raw_snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False)
    planner_input_text = json.dumps(planner_input_dict, sort_keys=True, ensure_ascii=False, allow_nan=False)

    # Core result (without artifacts/hashes) for hash computation
    result_core_for_hash = {
        "schema_version": SCHEMA_VERSION_RESULT,
        "status": status,
        "issue_number": issue_number,
        "repo": repo,
        "planner_exit_code": planner_exit_code,
        "planner_fail_closed": planner_fail_closed,
        "next_action": next_action,
        "must_read": sorted(set(must_read)),
        "do_not_read": do_not_read,
        "commands": commands,
        "blockers": blockers,
        "planner_fail_closed_reason_codes": planner_fail_closed_reason_codes,
        "required_sections": required_sections,
        "required_contract_keys": required_contract_keys,
        "rewrite_constraints": rewrite_constraints,
    }
    if contract_update_handoff is not None:
        result_core_for_hash["contract_update"] = contract_update_handoff
    result_core_text = json.dumps(result_core_for_hash, sort_keys=True, ensure_ascii=False, allow_nan=False)

    hashes = {
        "raw_issue_snapshot_sha256": _sha256(snapshot_text),
        "planner_input_sha256": _sha256(planner_input_text),
        "result_core_sha256": _sha256(result_core_text),
    }

    artifact_dir = _issue_artifact_dir(repo_root, issue_number)
    artifacts = {
        "raw_issue_snapshot": str(artifact_dir / "raw_issue_snapshot.json"),
        "planner_input": str(artifact_dir / "planner_input.json"),
        "refinement_preflight_result_v1": str(artifact_dir / "refinement_preflight_result_v1.json"),
    }
    result = _build_result(
        status=status,
        issue_number=issue_number,
        repo=repo,
        planner_exit_code=planner_exit_code,
        planner_fail_closed=planner_fail_closed,
        next_action=next_action,
        must_read=sorted(set(must_read)),
        do_not_read=do_not_read,
        commands=commands,
        blockers=blockers,
        planner_fail_closed_reason_codes=planner_fail_closed_reason_codes,
        required_sections=required_sections,
        required_contract_keys=required_contract_keys,
        rewrite_constraints=rewrite_constraints,
        artifacts=artifacts,
        hashes=hashes,
        contract_update=contract_update_handoff,
    )
    try:
        _write_artifacts(repo_root, issue_number, raw_snapshot, planner_input_dict, result)
    except Exception as exc:
        result = _build_result(
            status="environment_failure",
            issue_number=issue_number,
            repo=repo,
            planner_exit_code=planner_exit_code,
            planner_fail_closed=planner_fail_closed,
            next_action="fix_environment",
            must_read=sorted(set(must_read)),
            do_not_read=do_not_read,
            commands=commands,
            blockers=[
                *blockers,
                BLOCKER_RESULT_SCHEMA_INVALID,
                f"result_artifact_write_error:{type(exc).__name__}:{str(exc)[:500]}",
            ],
            planner_fail_closed_reason_codes=planner_fail_closed_reason_codes,
            required_sections=required_sections,
            required_contract_keys=required_contract_keys,
            rewrite_constraints=rewrite_constraints,
            artifacts={},
            hashes=hashes,
        )
        exit_code = EXIT_ENVIRONMENT_FAILURE

    # --- Blocker 1 (success path): provenance sidecar ---
    try:
        _anchor_url_prov = anchor_comment_urls[0] if anchor_comment_urls else ""
        _provenance = build_provenance(
            repo=repo,
            issue_number=issue_number,
            anchor_comment_url=_anchor_url_prov,
            planner_input=planner_input_dict,
            raw_snapshot=raw_snapshot,
            wrapper_exit_code=exit_code,
            wrapper_status=result.get("status", "unknown"),
            blockers=blockers,
            stderr=planner_stderr or "",
            repo_root=repo_root,
        )
        write_provenance_artifact(repo_root, issue_number, _provenance)
    except Exception:
        pass

    # Print compact stdout (no raw body/comments/sentinels) — same result dict
    print(_build_compact_stdout(result))

    return result, exit_code


# ---------------------------------------------------------------------------
# Provenance and failure classification (Issue #1035)
# ---------------------------------------------------------------------------


def _git_head_sha(repo_root: Path) -> str:
    """Return git HEAD SHA or 'unknown' on failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _git_blob_sha(file_path: Path, repo_root: Path) -> str:
    """Return git blob SHA of a file or 'unknown' on failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "hash-object", str(file_path.resolve())],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _git_head_tree_blob_sha(file_path: Path, repo_root: Path) -> str:
    """Return blob SHA from HEAD tree (git rev-parse HEAD:<relpath>) or 'unknown'."""
    try:
        relpath = file_path.resolve().relative_to(repo_root.resolve())
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", f"HEAD:{relpath}"],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _git_worktree_status(file_path: Path, repo_root: Path) -> str:
    """Return 'git status --short' for a file or 'unknown' on failure."""
    try:
        relpath = file_path.resolve().relative_to(repo_root.resolve())
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--short", "--", str(relpath)],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def build_py_compile_proof(script_path: Path, repo_root: Path) -> dict:
    """Generate PY_SYNTAX_COMPILE_PROOF_V2 artifact for a Python script.

    Compiles the source in-process without materializing a bytecode cache.
    ``python -m py_compile`` is intentionally not used here: it writes a
    ``__pycache__`` entry next to the source even when descendant imports have
    ``PYTHONDONTWRITEBYTECODE`` enabled.  The privileged executor must retain
    source-tree write attribution, so provenance generation cannot create an
    otherwise unauthorized cache file.

    The source is read and compiled as raw ``bytes`` (not a decoded ``str``)
    so that CPython's own PEP 263 encoding-declaration handling (UTF-8 BOM,
    ``# -*- coding: ... -*-`` cookie, and BOM/cookie contradictions) applies
    exactly as it would for a normally-imported module, instead of this
    wrapper silently assuming UTF-8. ``dont_inherit=True`` and ``flags=0``
    ensure this module's own ``from __future__ import annotations`` (and any
    other wrapper-side future statement) is never inherited by the checked
    script. There is no executable ``command`` for this proof: the operation
    is in-process and is described by ``operation_kind`` instead.
    """
    script_realpath = str(script_path.resolve())
    operation_kind = "in_process_compile"
    source_mode = "bytes"
    flags = 0
    dont_inherit = True
    optimize = -1

    try:
        source_bytes = script_path.read_bytes()
        compile(
            source_bytes,
            script_realpath,
            "exec",
            flags=flags,
            dont_inherit=dont_inherit,
            optimize=optimize,
        )
        py_compile_status = "pass"
        stderr_text = ""
    except Exception as exc:
        py_compile_status = "fail"
        stderr_text = str(exc)

    stderr_excerpt = stderr_text[:500]

    return {
        "schema_version": "PY_SYNTAX_COMPILE_PROOF_V2",
        "operation_kind": operation_kind,
        "source_mode": source_mode,
        "flags": flags,
        "dont_inherit": dont_inherit,
        "optimize": optimize,
        "cache_write_expected": False,
        "py_compile_status": py_compile_status,
        "python_version": sys.version,
        "python_executable": sys.executable,
        "git_head_sha": _git_head_sha(repo_root),
        "planner_script_path": str(script_path),
        "planner_script_realpath": script_realpath,
        "planner_script_blob_sha": _git_blob_sha(script_path, repo_root),
        "cwd": str(Path.cwd()),
        "stderr_sha256": _sha256(stderr_text),
        "stderr_excerpt": stderr_excerpt,
    }


def classify_planner_failure(
    exit_code: int,
    stdout: str,
    stderr: str,
    script_path: Optional[Path] = None,
    python_executable: Optional[str] = None,
) -> dict:
    """Classify a planner failure into PLANNER_FAILURE_CLASSIFICATION_V1 taxonomy.

    Categories (mutually exclusive, evaluated in priority order):
      syntax_compile_failure         - SyntaxError detected in stderr / exit != 0
      anchor_or_input_blocked        - exit 2 (invalid input / schema error)
      planner_stdout_non_json        - exit 0 but stdout is not valid JSON
      wrapper_environment_failure    - gh not found / auth / timeout
      planner_runtime_internal_error - exit 3 without SyntaxError
    """
    stderr_str = stderr or ""
    stdout_str = stdout or ""
    is_syntax_error = bool(re.search(r"SyntaxError|py_compile\b", stderr_str))

    if is_syntax_error and exit_code != 0:
        category = "syntax_compile_failure"
    elif exit_code == 2:
        category = "anchor_or_input_blocked"
    elif exit_code == 0:
        try:
            json.loads(stdout_str)
            category = "planner_runtime_internal_error"
        except (json.JSONDecodeError, ValueError):
            category = "planner_stdout_non_json"
    elif exit_code == 3:
        env_keywords = ("not found", "FileNotFoundError", "timeout", "gh_", "gh not")
        if any(kw in stderr_str for kw in env_keywords):
            category = "wrapper_environment_failure"
        else:
            category = "planner_runtime_internal_error"
    else:
        category = "wrapper_environment_failure"

    # Traceback excerpt (SyntaxError only)
    traceback_excerpt = ""
    if category == "syntax_compile_failure":
        lines = stderr_str.splitlines()
        relevant = [
            ln for ln in lines if "SyntaxError" in ln or ln.strip().startswith("File ") or "line " in ln.lower()
        ]
        traceback_excerpt = "\n".join(relevant[:10])

    # JSON decode error (non-JSON stdout only)
    json_decode_error = ""
    if category == "planner_stdout_non_json":
        try:
            json.loads(stdout_str)
        except (json.JSONDecodeError, ValueError) as exc:
            json_decode_error = str(exc)

    script_realpath = str(script_path.resolve()) if script_path else ""

    return {
        "schema_version": "PLANNER_FAILURE_CLASSIFICATION_V1",
        "category": category,
        "exit_code": exit_code,
        "stdout_sha256": _sha256(stdout_str),
        "stderr_sha256": _sha256(stderr_str),
        "stderr_excerpt": stderr_str[:500],
        "json_decode_error": json_decode_error,
        "traceback_excerpt": traceback_excerpt,
        "script_path": str(script_path) if script_path else "",
        "script_realpath": script_realpath,
        "python_executable": python_executable or sys.executable,
        "python_version": sys.version,
    }


def build_provenance(
    repo: str,
    issue_number: int,
    anchor_comment_url: str,
    planner_input: dict,
    raw_snapshot: dict,
    wrapper_exit_code: int,
    wrapper_status: str,
    blockers: list,
    stderr: str,
    repo_root: Path,
) -> dict:
    """Generate REFINEMENT_PREFLIGHT_PROVENANCE_V1 sidecar artifact.

    Captures the full execution context of a preflight run so that a later
    replay or audit can verify which file/interpreter/commit was used.
    Written to the same artifact directory as the main result but as a
    separate file (``refinement_preflight_provenance_v1.json``) to avoid
    violating the strict ``additionalProperties: false`` result schema.
    """
    planner_script = PLANNER_SCRIPT
    wrapper_script = Path(__file__).resolve()

    py_compile_proof = build_py_compile_proof(planner_script, repo_root)

    planner_input_text = _canonical_json(planner_input)
    raw_snapshot_text = _canonical_json(raw_snapshot)
    stderr_str = stderr or ""

    def _dependency_version(name: str) -> str:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return "not_installed"

    return {
        "schema_version": "REFINEMENT_PREFLIGHT_PROVENANCE_V1",
        "repo": repo,
        "issue_number": issue_number,
        "anchor_comment_url": anchor_comment_url,
        "git_head_sha": _git_head_sha(repo_root),
        "planner_invocation_command": [sys.executable, str(planner_script)],
        "planner_script_path": str(planner_script),
        "planner_script_realpath": str(planner_script.resolve()),
        "planner_script_blob_sha": _git_blob_sha(planner_script, repo_root),
        "planner_head_tree_blob_sha": _git_head_tree_blob_sha(planner_script, repo_root),
        "planner_worktree_status": _git_worktree_status(planner_script, repo_root),
        "wrapper_script_blob_sha": _git_blob_sha(wrapper_script, repo_root),
        "python_executable": sys.executable,
        "python_version": sys.version,
        "dependency_versions": {
            "jsonschema": _dependency_version("jsonschema"),
            "referencing": _dependency_version("referencing"),
        },
        "cwd": str(Path.cwd()),
        "py_compile_status": py_compile_proof["py_compile_status"],
        # Issue #1439 AC12: persist the full PY_SYNTAX_COMPILE_PROOF_V2 proof
        # (schema_version + all required fields), not just the collapsed
        # py_compile_status summary above, so a later reader of the
        # refinement_preflight_provenance_v1.json artifact can verify the
        # full V2 semantics without re-deriving them.
        "python_syntax_compile_proof": py_compile_proof,
        "wrapper_exit_code": wrapper_exit_code,
        "wrapper_status": wrapper_status,
        "blockers": list(blockers),
        "planner_input_sha256": _sha256(planner_input_text),
        "raw_snapshot_sha256": _sha256(raw_snapshot_text),
        "stderr_sha256": _sha256(stderr_str),
        "stderr_excerpt": stderr_str[:500],
    }


def build_replay_proof(
    live_input: dict,
    fixture_input: dict,
    live_result_status: str,
    fixture_result_status: str,
) -> dict:
    """Generate REFINEMENT_PREFLIGHT_REPLAY_PROOF_V1.

    Compares the SHA256 of the canonical JSON of ``live_input`` (fetched
    from GitHub) against ``fixture_input`` (a saved snapshot).  Identical
    hashes guarantee the classification is deterministic; a mismatch is
    classified as ``input_drift`` so that the caller cannot prematurely
    declare the issue resolved.
    """
    live_sha = _sha256(_canonical_json(live_input))
    fixture_sha = _sha256(_canonical_json(fixture_input))
    input_drift_detected = live_sha != fixture_sha
    results_consistent = live_result_status == fixture_result_status

    if input_drift_detected:
        classification = "input_drift"
    elif results_consistent:
        classification = "replay_consistent"
    else:
        classification = "classification_mismatch"

    return {
        "schema_version": "REFINEMENT_PREFLIGHT_REPLAY_PROOF_V1",
        "live_input_sha256": live_sha,
        "fixture_input_sha256": fixture_sha,
        "input_drift_detected": input_drift_detected,
        "live_result_status": live_result_status,
        "fixture_result_status": fixture_result_status,
        "results_consistent": results_consistent,
        "classification": classification,
    }


def write_provenance_artifact(
    repo_root: Path,
    issue_number: int,
    provenance: dict,
) -> str:
    """Write provenance dict to the artifacts directory and return the path."""
    artifact_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prov_path = artifact_dir / "refinement_preflight_provenance_v1.json"
    prov_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return str(prov_path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Deterministic preflight wrapper for issue-refinement-loop.",
        allow_abbrev=False,
    )
    parser.add_argument("--issue-number", type=int, required=True, help="GitHub Issue number (positive int).")
    parser.add_argument("--repo", required=True, help="owner/repo string (must match ^[^/]+/[^/]+$).")
    parser.add_argument(
        "--anchor-comment-url",
        dest="anchor_comment_urls",
        action="append",
        default=[],
        help=(
            "Anchor comment URL to validate. The CLI flag is repeatable, but only "
            "0 or 1 distinct URL is actually supported: passing 2+ distinct "
            "anchor-comment URLs is rejected as ANCHOR_COMMENT_MULTIPLE_UNSUPPORTED "
            "(repeating the identical URL is deduplicated to 1 and is not an "
            "error)."
        ),
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help="Path to fixture JSON (bypasses gh CLI calls).",
    )
    parser.add_argument(
        "--consume-contract-patch-plan",
        action="store_true",
        help="Execute a trusted CONTRACT_PATCH_PLAN_V1 through edit_issue_txn.py.",
    )

    args = parser.parse_args(argv)

    # --- argparse input validation (blocked / exit 2 on contract violation) ---
    input_errors: list[str] = []

    if args.issue_number is not None and args.issue_number <= 0:
        input_errors.append(f"--issue-number must be a positive int, got {args.issue_number}")

    if not _REPO_PATTERN.match(args.repo):
        input_errors.append(f"--repo must match ^[^/]+/[^/]+$, got {args.repo!r}")

    for url in args.anchor_comment_urls:
        if not url.startswith(_GITHUB_URL_PREFIX):
            input_errors.append(f"--anchor-comment-url must start with {_GITHUB_URL_PREFIX!r}, got {url!r}")

    if input_errors:
        # Build minimal blocked result for argparse validation failure
        _repo_root = _find_repo_root()
        err_detail = "; ".join(input_errors)
        result = _build_result(
            status="blocked",
            issue_number=args.issue_number or 0,
            repo=args.repo or "",
            planner_exit_code=None,
            planner_fail_closed=None,
            next_action="human_judgment_required",
            must_read=[],
            do_not_read=[],
            commands=[],
            blockers=[BLOCKER_INVALID_ARGS, f"arg_errors: {err_detail}"],
            planner_fail_closed_reason_codes=[],
            required_sections=[],
            required_contract_keys=[],
            rewrite_constraints=None,
            artifacts={},
            hashes={},
        )
        print(_build_compact_stdout(result))
        sys.exit(EXIT_BLOCKED)

    _, exit_code = run_preflight(
        issue_number=args.issue_number,
        repo=args.repo,
        anchor_comment_urls=args.anchor_comment_urls,
        fixture_path=args.fixture,
        consume_contract_patch_plan=args.consume_contract_patch_plan,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
