#!/usr/bin/env python3
"""
controlled_skill_mutation_exec.py

Single executor for CONTROLLED_SKILL_MUTATION_COMMAND_POLICY entries.
Invoked by agents via the exact argv form defined in controlled_skill_mutation_policy.py.

Design: Direct script allow for publish_termination_report.py / ensure_contract_snapshot.py
is denied. Only this executor is allow-listed in settings.json. It handles four
command ids: termination_report.publish (legacy, Issue #1166), and issue_body.update /
issue_comment.publish / contract_snapshot.publish (Issue #1284 issue metadata mutation
lane). The executor enforces:
  - command_id whitelist (ALL_COMMAND_IDS)
  - repo binding (--repo must be TRUSTED_REPO)
  - git remote origin binding (must match TRUSTED_REPO)
  - issue binding (--issue-number must match LOOP_ISSUE_NUMBER env -- mandatory for
    termination_report.publish, optional-but-matching for the Issue #1284 command ids)
  - input-file binding (must be in the active issue/command-id artifact subtree,
    no symlinks, no hardlinks)
  - input-file JSON validation (schema + issue_number field cross-check, plus
    per-command-id field schemas for the Issue #1284 command ids)
  - gh binary discovery (trusted path only)
  - environment sanitization (PUBLISH_ARTIFACT_DIR / PYTHONPATH / PYTHONHOME /
    GH_EDITOR / EDITOR / VISUAL / BROWSER overridden/removed)
  - module realpath inspection (publisher / renderer / prose_boundary canonical path
    check for termination_report.publish; ensure_contract_snapshot.py /
    run_contract_review_once.py / contract_review_result_parser.py canonical path
    check for contract_snapshot.publish -- missing=deny)
  - remote-state-is-authority idempotency: local marker files are cache/audit only.
    issue_body.update and issue_comment.publish always readback GitHub before
    declaring success; a local marker never substitutes for a remote check.
  - exec marker injection (deterministic marker for comment read-back, legacy command)
  - pre-mutation marker precheck for issue_comment.publish (no POST before remote
    marker state is known -- a failed transaction must not leave a side effect)
  - postcondition (git status --porcelain=v1 must show no changes outside the
    command-id-scoped artifact write root)
  - comment read-back by marker (comment id / url / body hash recorded)

Exit codes:
  0 - publish succeeded
  1 - publish failed, stale/mismatched state detected, or idempotency marker already set
  2 - validation error (wrong args, wrong issue, wrong file, missing schema fields, etc.)

Issue #1166 / Issue #1284.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re as _re
import shutil
import stat as _stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

# -- Path resolution -----------------------------------------------------------

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
    ".claude/skills/create-issue/scripts/prose_boundary_policy.py"
)

# -- Import shared policy ------------------------------------------------------

sys.path.insert(0, str(_THIS_FILE.parent))
from controlled_skill_mutation_policy import (
    COMMAND_ID_PUBLISH,
    COMMAND_ID_ISSUE_BODY_UPDATE,
    COMMAND_ID_ISSUE_COMMENT_PUBLISH,
    COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
    COMMAND_ID_PR_REVIEW_PUBLISH,
    COMMAND_ID_ISSUE_SCOPE_SNAPSHOT_MATERIALIZE,
    COMMAND_ID_ISSUE_DEPENDENCY_REMOVE,
    ALL_COMMAND_IDS,
    INPUT_SCHEMA_BY_COMMAND,
    ENV_BINDING_MANDATORY_COMMAND_IDS,
    ISSUE_METADATA_NAMESPACE_SEGMENT,
    ISSUE_DEPENDENCY_REMOVE_MAX_BLOCKED_BY_NUMBERS,
    TRUSTED_REPO,
    ENV_SANITIZE_KEYS,
    validate_issue_dependency_remove_input,
)

_ENSURE_CONTRACT_SNAPSHOT_REL = (
    ".claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py"
)
_RUN_CONTRACT_REVIEW_ONCE_REL = (
    ".claude/skills/issue-contract-review/scripts/run_contract_review_once.py"
)
_EVALUATE_PRODUCT_SPEC_GATE_REL = (
    ".claude/skills/impl-review-loop/scripts/evaluate_product_spec_gate.py"
)
_CONTRACT_REVIEW_RESULT_PARSER_REL = (
    ".claude/skills/issue-contract-review/scripts/contract_review_result_parser.py"
)
_ISSUE_SCOPE_SNAPSHOT_MATERIALIZER_REL = "scripts/agent-guards/materialize_issue_scope_snapshot.py"

# -- Result schema -------------------------------------------------------------

RESULT_SCHEMA = "CONTROLLED_SKILL_MUTATION_RESULT_V1"

# -- gh binary discovery -------------------------------------------------------

_GH_TRUSTED_PATHS = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


def _find_gh_bin() -> tuple[str | None, str]:
    """Find gh binary in trusted PATH. Returns (path, error)."""
    gh = shutil.which("gh", path=_GH_TRUSTED_PATHS)
    if not gh:
        return None, "gh_not_found_in_trusted_path"
    return gh, ""


# -- Git remote origin verification --------------------------------------------


# Issue #1539 fix_delta Blocker 2: the only trusted remote host. Structural
# scheme/host validation replaces the previous "grab the last owner/repo-shaped
# path segment" regex, which ignored host/scheme entirely and would treat
# `https://attacker.example/squne121/loop-protocol.git` as trusted.
_TRUSTED_GITHUB_HOST = "github.com"
_OWNER_REPO_RE = _re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _normalize_owner_repo(path: str) -> str | None:
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not path or not _OWNER_REPO_RE.match(path):
        return None
    return path


def _parse_trusted_github_remote(url: str) -> str | None:
    """Return the normalized ``owner/repo`` iff url is a canonical HTTPS/SSH
    github.com remote. Returns None for any other host, scheme, port, or
    non-``git``/anonymous userinfo (evil host, file://, other-host SSH, etc.).
    """
    url = (url or "").strip()
    if not url or "\x00" in url:
        return None
    if "://" in url:
        try:
            parsed = urlsplit(url)
        except ValueError:
            return None
        if parsed.scheme.lower() not in ("https", "ssh"):
            return None
        host = (parsed.hostname or "").lower()
        if host != _TRUSTED_GITHUB_HOST:
            return None
        if parsed.port not in (None, 443, 22):
            return None
        if parsed.username not in (None, "git"):
            return None
        return _normalize_owner_repo(parsed.path)
    # scp-like syntax: [user@]host:path (e.g. git@github.com:owner/repo.git)
    m = _re.match(r"^(?:([A-Za-z0-9_.-]+)@)?([A-Za-z0-9_.-]+):(.+)$", url)
    if not m:
        return None
    user, host, path = m.group(1), m.group(2), m.group(3)
    if user not in (None, "git"):
        return None
    if host.lower() != _TRUSTED_GITHUB_HOST:
        return None
    return _normalize_owner_repo(path)


def _verify_git_remote_origin(
    project_root: Path, trusted_repo: str, env: dict[str, str] | None = None
) -> str:
    """Return empty string if origin is a canonical github.com/trusted_repo
    HTTPS or SSH remote, else a descriptive error string."""
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        if out.returncode != 0:
            return f"git_remote_origin_failed: {out.stderr.strip()[:100]}"
        url = out.stdout.strip()
        normalized = _parse_trusted_github_remote(url)
        if normalized is None:
            return f"git_remote_origin_untrusted_host_or_scheme: {url!r}"
        if normalized != trusted_repo:
            return f"git_remote_origin_mismatch: {normalized!r} != {trusted_repo!r}"
        return ""
    except Exception as exc:
        return f"git_remote_origin_exception: {exc}"


# -- Environment sanitization --------------------------------------------------


def _build_sanitized_env(
    project_root: Path, issue_number: int, exec_marker: str = ""
) -> dict[str, str]:
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

    # Inject exec marker for comment read-back
    if exec_marker:
        env["CONTROLLED_EXEC_MARKER"] = exec_marker

    return env


# -- Issue #1284 Blocker 5: generic metadata-command env sanitizer -------------


def _build_metadata_sanitized_env() -> dict[str, str]:
    """Build a sanitized environment for issue-metadata publisher subprocesses
    (contract_snapshot.publish). Equivalent boundary to _build_sanitized_env()
    minus the PUBLISH_ARTIFACT_DIR override, which is legacy-publisher-specific.
    """
    env = os.environ.copy()
    for key in ENV_SANITIZE_KEYS:
        env.pop(key, None)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    return env


# -- Issue #1284 Blocker 5: contract_snapshot.publish module realpath check ----


def _check_contract_snapshot_module_realpaths(project_root: Path) -> list[str]:
    """Return list of realpath violations for the contract_snapshot.publish
    publisher module chain. Missing modules are treated as errors (missing=deny),
    mirroring _check_module_realpaths() for the legacy termination_report.publish
    command.

    Issue #1459 review Blocker (evaluator_missing_from_module_trust_chain):
    evaluate_product_spec_gate.py is imported by ensure_contract_snapshot.py at
    module load time, so it is part of the trusted publisher module chain and
    must be realpath-checked here too -- otherwise a repo-external symlink
    shadowing that evaluator would run unchecked before the publisher even
    starts. Path ancestry is decided with Path.is_relative_to() against the
    resolved project root rather than a raw str.startswith() prefix check,
    which would also treat a sibling directory such as
    "/repo-evil/..." as "under" "/repo" purely by string-prefix coincidence.
    """
    errors = []
    resolved_project_root = project_root.resolve()
    for rel in (
        _ENSURE_CONTRACT_SNAPSHOT_REL,
        _RUN_CONTRACT_REVIEW_ONCE_REL,
        _EVALUATE_PRODUCT_SPEC_GATE_REL,
        _CONTRACT_REVIEW_RESULT_PARSER_REL,
    ):
        canonical = (project_root / rel).resolve()
        if not canonical.exists():
            errors.append(f"module_missing: {rel} not found at {canonical}")
            continue
        if not canonical.is_relative_to(resolved_project_root):
            errors.append(
                f"module_shadowing: {rel} resolved to {canonical}, "
                f"expected under {resolved_project_root}"
            )
    return errors


# -- Module realpath inspection ------------------------------------------------


def _check_module_realpaths(project_root: Path) -> list[str]:
    """Return list of realpath violations. Empty list = all OK.

    Checks that publisher / renderer / prose_boundary_policy resolve to canonical
    paths under project_root. Prevents module shadowing (AC16).
    Missing modules are treated as errors (missing=deny).
    """
    errors = []
    for rel in (_PUBLISHER_SCRIPT_REL, _RENDERER_SCRIPT_REL, _PROSE_BOUNDARY_REL):
        canonical = (project_root / rel).resolve()
        if not canonical.exists():
            errors.append(f"module_missing: {rel} not found at {canonical}")
            continue
        if not str(canonical).startswith(str(project_root)):
            errors.append(
                f"module_shadowing: {rel} resolved to {canonical}, "
                f"expected under {project_root}"
            )

    # Import origin check for prose_boundary_policy via subprocess probe
    prose_canonical = (project_root / _PROSE_BOUNDARY_REL).resolve()
    if prose_canonical.exists():
        try:
            probe_code = (
                "import sys; sys.path.insert(0, '"
                + str(prose_canonical.parent).replace("'", "\\'")
                + "'); "
                "import prose_boundary_policy; "
                "import pathlib; "
                "print(pathlib.Path(prose_boundary_policy.__file__).resolve())"
            )
            probe = subprocess.run(
                [sys.executable, "-c", probe_code],
                capture_output=True, text=True, timeout=10,
                cwd=str(project_root),
            )
            if probe.returncode == 0:
                imported_origin = Path(probe.stdout.strip())
                if imported_origin != prose_canonical:
                    errors.append(
                        f"module_import_origin_mismatch: prose_boundary_policy "
                        f"imported from {imported_origin}, expected {prose_canonical}"
                    )
            else:
                errors.append(f"module_import_probe_failed: {probe.stderr[:200]}")
        except Exception as exc:
            errors.append(f"module_import_probe_error: {exc}")

    return errors


# -- Input file validation -----------------------------------------------------


def _issue_metadata_subtree(project_root: Path, issue_number: int, command_id: str) -> Path:
    """Return the canonical allowed input-file subtree for a new-style command id.

    Issue #1284: namespace is unified under
    artifacts/{issue_number}/issue-metadata/{command-id}/
    """
    return (
        project_root
        / "artifacts"
        / str(issue_number)
        / ISSUE_METADATA_NAMESPACE_SEGMENT
        / command_id
    ).resolve()


def _validate_and_resolve_input_file(
    input_file_str: str,
    issue_number: int,
    project_root: Path,
    command_id: str = COMMAND_ID_PUBLISH,
) -> tuple[Path | None, str]:
    """Validate and resolve the input file path.

    Returns (canonical_path, error_message). canonical_path is None on error.
    Enforces:
    - Lexical: reject absolute paths
    - Lexical: reject '..' components
    - Filesystem: reject symlink components (via lstat)
    - Must be a regular file
    - Must not be a hardlink (st_nlink == 1)
    - Must be under artifacts/{issue_number}/ (legacy termination_report.publish)
      or artifacts/{issue_number}/issue-metadata/{command_id}/ (Issue #1284 command ids)
    """
    raw = PurePosixPath(input_file_str)

    # Lexical: reject absolute paths
    if raw.is_absolute():
        return None, f"input_file_absolute_path_denied: {input_file_str!r}"

    # Lexical: reject '..' components
    if ".." in raw.parts:
        return None, f"input_file_dotdot_denied: {input_file_str!r}"

    # Filesystem: check each component for symlinks via lstat
    cursor = project_root
    for part in raw.parts:
        cursor = cursor / part
        try:
            lstat = cursor.lstat()
        except FileNotFoundError:
            return None, f"input_file_not_found: {input_file_str!r}"
        except Exception as exc:
            return None, f"input_file_lstat_error: {exc}"
        if _stat.S_ISLNK(lstat.st_mode):
            return None, f"input_file_symlink_denied: {cursor}"

    # Resolve canonical path (no symlinks remain after lstat check above)
    try:
        canonical = cursor.resolve()
    except Exception as exc:
        return None, f"input_file_resolve_error: {exc}"

    # Must be a regular file
    try:
        st = canonical.stat()
    except Exception as exc:
        return None, f"input_file_stat_error: {exc}"

    if not _stat.S_ISREG(st.st_mode):
        return None, f"input_file_not_regular: {input_file_str!r}"

    # Hardlink check
    if st.st_nlink != 1:
        return None, f"input_file_hardlink_denied: st_nlink={st.st_nlink}"

    # Containment check.
    if command_id == COMMAND_ID_PUBLISH:
        artifact_subtree = (project_root / "artifacts" / str(issue_number)).resolve()
    else:
        artifact_subtree = _issue_metadata_subtree(project_root, issue_number, command_id)
    try:
        canonical.relative_to(artifact_subtree)
    except ValueError:
        return None, (
            f"input_file_outside_issue_subtree: {canonical} "
            f"not under {artifact_subtree}"
        )

    return canonical, ""


# -- Input JSON validation -----------------------------------------------------


def _load_and_validate_input_json(
    canonical_input: Path, issue_number: int, command_id: str
) -> tuple[dict | None, str]:
    """Read and validate input JSON against the per-command-id schema (AC10).

    Returns (input_data, error_message). input_data is None on error.
    """
    try:
        input_data = json.loads(canonical_input.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"input_json_read_error: {exc}"

    if not isinstance(input_data, dict):
        return None, "input_json_not_object"

    expected_schema = INPUT_SCHEMA_BY_COMMAND.get(command_id)
    if input_data.get("schema") != expected_schema:
        schema_val = input_data.get("schema")
        return None, (
            f"input_schema_mismatch: expected {expected_schema}, got {schema_val!r}"
        )

    input_issue = input_data.get("issue_number")
    if input_issue is None:
        return None, "input_issue_number_missing"
    if type(input_issue) is not int:
        return None, f"input_issue_number_not_int: {type(input_issue).__name__}"
    if input_issue != issue_number:
        return None, f"input_issue_number_mismatch: {input_issue} != {issue_number}"

    return input_data, ""


def _validate_input_json(
    canonical_input: Path, issue_number: int
) -> str:
    """Backward-compatible wrapper for termination_report.publish (AC10 legacy)."""
    _, err = _load_and_validate_input_json(canonical_input, issue_number, COMMAND_ID_PUBLISH)
    return err


# -- Issue #1284: per-command input field validation ---------------------------


def _validate_issue_body_update_fields(data: dict) -> str:
    for field, typ in (
        ("previous_body_sha256", str),
        ("previous_updated_at", str),
        ("new_body", str),
        ("new_body_sha256", str),
    ):
        val = data.get(field)
        if not isinstance(val, typ) or (typ is str and not val):
            return f"issue_body_update_field_invalid: {field!r}"
    computed = "sha256:" + hashlib.sha256(data["new_body"].encode("utf-8")).hexdigest()
    if computed != data["new_body_sha256"]:
        return (
            f"issue_body_update_new_body_sha256_mismatch: computed={computed} "
            f"declared={data['new_body_sha256']}"
        )
    return ""


def _validate_issue_comment_publish_fields(data: dict) -> str:
    for field in ("comment_body", "marker"):
        val = data.get(field)
        if not isinstance(val, str) or not val:
            return f"issue_comment_publish_field_invalid: {field!r}"
    if data["marker"] not in data["comment_body"]:
        return "issue_comment_publish_marker_not_embedded_in_body"
    return ""


_PR_HEAD_SHA_RE = _re.compile(r"^[0-9a-f]{40}$")


# Issue #1539 fix_delta High 2: exact-key schema -- an input JSON with any key
# outside this set is rejected before any mutation. Applies to the
# --input-file code path (the --render-body-file code path never accepts an
# arbitrary dict at all -- see _render_pr_review_publish_request()).
_PR_REVIEW_PUBLISH_ALLOWED_KEYS = frozenset({
    "schema", "issue_number", "repo", "pr_number", "expected_head_sha",
    "event", "producer_role", "body", "body_sha256", "idempotency_key",
})
_PR_REVIEW_BODY_MAX_BYTES = 60000


def _validate_pr_review_publish_fields(data: dict, repo: str, issue_number: int) -> str:
    """Issue #1536 AC1/AC2/AC5/AC6: PR_REVIEW_PUBLISH_REQUEST_V1 field validation.

    All checks below run before any GitHub API call (AC2/AC3/AC5 require
    fail-closed rejection with zero remote side effect for malformed input).
    """
    unknown_keys = set(data.keys()) - _PR_REVIEW_PUBLISH_ALLOWED_KEYS
    if unknown_keys:
        return f"pr_review_publish_unknown_fields: {sorted(unknown_keys)}"

    declared_repo = data.get("repo")
    if declared_repo != repo:
        return f"pr_review_publish_repo_mismatch: {declared_repo!r} != {repo!r}"

    pr_number = data.get("pr_number")
    if type(pr_number) is not int or pr_number <= 0:
        return f"pr_review_publish_pr_number_invalid: {pr_number!r}"
    if pr_number != issue_number:
        return (
            f"pr_review_publish_pr_number_mismatch: pr_number={pr_number} "
            f"!= --issue-number={issue_number}"
        )

    expected_head_sha = data.get("expected_head_sha")
    if not isinstance(expected_head_sha, str) or not _PR_HEAD_SHA_RE.match(expected_head_sha):
        return f"pr_review_publish_expected_head_sha_invalid: {expected_head_sha!r}"

    # AC2: event is fixed to COMMENT. Any alias (approve/-a/-r/APPROVE/
    # REQUEST_CHANGES/lowercase "comment"/empty/missing) is rejected before
    # any mutation -- the executor never negotiates event type with the API.
    if data.get("event") != "COMMENT":
        return f"pr_review_publish_event_not_comment: {data.get('event')!r}"

    producer_role = data.get("producer_role")
    if producer_role != "pr-reviewer":
        return f"pr_review_publish_producer_role_invalid: {producer_role!r}"

    body = data.get("body")
    if not isinstance(body, str) or not body:
        return "pr_review_publish_body_invalid"
    if len(body.encode("utf-8")) > _PR_REVIEW_BODY_MAX_BYTES:
        return f"pr_review_publish_body_too_large: {len(body.encode('utf-8'))} bytes"
    body_sha256 = data.get("body_sha256")
    computed_body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if body_sha256 != computed_body_sha256:
        return (
            f"pr_review_publish_body_sha256_mismatch: computed={computed_body_sha256} "
            f"declared={body_sha256!r}"
        )

    idempotency_key = data.get("idempotency_key")
    expected_idempotency_key = f"{repo}:{pr_number}:{expected_head_sha}:{body_sha256}"
    if idempotency_key != expected_idempotency_key:
        return (
            f"pr_review_publish_idempotency_key_mismatch: expected="
            f"{expected_idempotency_key!r} declared={idempotency_key!r}"
        )

    return ""


def _validate_contract_snapshot_publish_fields(data: dict, repo: str) -> str:
    """Issue #1284 Blocker 4: CONTRACT_SNAPSHOT_PUBLISH_INPUT_V1 must bind repo /
    target_issue_body_sha256 / expected_latest_contract_review_status /
    expected_contract_marker / operation_reason. An input file with only
    {schema, issue_number} is no longer sufficient to launch
    ensure_contract_snapshot.py --mode auto --post.
    """
    declared_repo = data.get("repo")
    if declared_repo != repo:
        return f"contract_snapshot_publish_repo_mismatch: {declared_repo!r} != {repo!r}"
    for field in (
        "target_issue_body_sha256",
        "expected_latest_contract_review_status",
        "expected_contract_marker",
        "operation_reason",
    ):
        val = data.get(field)
        if not isinstance(val, str) or not val:
            return f"contract_snapshot_publish_field_invalid: {field!r}"
    return ""


def _validate_issue_scope_snapshot_materialize_fields(data: dict, repo: str) -> str:
    if data.get("repo") != repo:
        return "issue_scope_snapshot_materialize_repo_mismatch"
    for field in ("contract_snapshot_url", "base_ref", "branch_name", "worktree_path", "output_path"):
        if not isinstance(data.get(field), str) or not data[field]:
            return f"issue_scope_snapshot_materialize_field_invalid: {field!r}"
    return ""


# -- Issue #1284: env binding (AC15) --------------------------------------------


def _check_issue_env_binding(command_id: str, issue_number: int) -> str:
    """Return error string, or empty string when binding is satisfied.

    Legacy termination_report.publish: LOOP_ISSUE_NUMBER is mandatory (Issue #1166).
    New command ids: LOOP_ISSUE_NUMBER is optional; when present it must match
    --issue-number (Issue #1284 AC15).
    """
    env_issue = os.environ.get("LOOP_ISSUE_NUMBER", "").strip()
    mandatory = command_id in ENV_BINDING_MANDATORY_COMMAND_IDS
    if not env_issue:
        if mandatory:
            return "loop_issue_number_env_missing: LOOP_ISSUE_NUMBER must be set"
        return ""
    if not env_issue.isdigit():
        return f"loop_issue_number_env_not_digit: {env_issue!r}"
    if int(env_issue) != issue_number:
        return (
            f"issue_number_mismatch: --issue-number {issue_number} "
            f"!= LOOP_ISSUE_NUMBER {env_issue}"
        )
    return ""


# -- Idempotency marker --------------------------------------------------------


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


# -- Exec marker (idempotency read-back by marker) ----------------------------

EXEC_MARKER_PREFIX = "<!-- CONTROLLED_EXEC_MARKER:"
EXEC_MARKER_SUFFIX = " -->"


def _compute_exec_marker(
    command_id: str, repo: str, issue_number: int, canonical_input: Path
) -> str:
    """Compute deterministic exec marker for comment injection."""
    input_sha = hashlib.sha256(canonical_input.read_bytes()).hexdigest()
    marker_src = f"{command_id}:{repo}:{issue_number}:{input_sha}"
    return hashlib.sha256(marker_src.encode()).hexdigest()[:32]


def _readback_by_marker(
    exec_marker: str, issue_number: int, repo: str, gh_bin: str
) -> dict:
    """Search comments for exec_marker and return comment metadata."""
    marker_str = f"{EXEC_MARKER_PREFIX}{exec_marker}{EXEC_MARKER_SUFFIX}"
    try:
        out = subprocess.run(
            [gh_bin, "issue", "view", str(issue_number),
             "--repo", repo, "--json", "comments"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            return {"error": f"gh_failed_rc_{out.returncode}"}
        data = json.loads(out.stdout)
        comments = data.get("comments", [])
        matches = [c for c in comments if marker_str in c.get("body", "")]
        if len(matches) == 0:
            return {"error": "marker_not_found"}
        if len(matches) > 1:
            return {"error": f"marker_found_{len(matches)}_times"}
        c = matches[0]
        body = c.get("body", "")
        return {
            "comment_id": c.get("id", ""),
            "comment_url": c.get("url", ""),
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        }
    except Exception as exc:
        return {"error": f"readback_exception:{exc}"}


# -- Issue #1284: HTTP error classification (same granularity as
# ensure_contract_snapshot.py's classify_post_http_error / _extract_http_status)


def _extract_http_status(stderr: str) -> int | None:
    """Extract HTTP status code from gh CLI stderr."""
    m = _re.search(r"HTTP (\d{3})", stderr or "")
    if m:
        return int(m.group(1))
    for code in (403, 404, 410, 422, 429, 503):
        if str(code) in (stderr or ""):
            return code
    return None


def _classify_gh_error(prefix: str, stderr: str) -> str:
    """Classify a gh api failure into a deterministic error code.

    403 -> permission_denied, 404/410 -> ambiguous_no_retry, 422 -> validation_failed,
    429/503 -> rate_limited, unknown -> the raw truncated stderr.
    """
    status = _extract_http_status(stderr)
    if status == 403:
        return f"{prefix}_permission_denied_http_403"
    if status in (404, 410):
        return f"{prefix}_ambiguous_no_retry_http_{status}"
    if status == 422:
        return f"{prefix}_validation_failed_http_422"
    if status in (429, 503):
        return f"{prefix}_rate_limited_http_{status}"
    return f"{prefix}: {stderr.strip()[:200]}"


# -- Issue #1284: issue body / comment mutation helpers ------------------------


def _fetch_issue_body_and_updated_at(
    issue_number: int, repo: str, gh_bin: str
) -> tuple[str | None, str | None, str]:
    """Fetch live Issue state from the trusted GitHub host only.

    ``contract_snapshot.publish`` uses this helper for both its stale-write
    precondition and its post-publish live-body revalidation.  Those reads are
    part of the authoritative success boundary, so they must not inherit a
    caller-controlled GH_HOST/GH_REPO/GH_CONFIG_DIR setting.
    """
    try:
        out = subprocess.run(
            [
                gh_bin,
                "api",
                "--hostname",
                _TRUSTED_GITHUB_HOST,
                f"repos/{repo}/issues/{issue_number}",
                "--jq",
                "{body, updatedAt: .updated_at}",
            ],
            capture_output=True, text=True, timeout=15, shell=False,
            env=_build_metadata_sanitized_env(),
        )
        if out.returncode != 0:
            return None, None, f"gh_issue_fetch_failed_rc_{out.returncode}"
        data = json.loads(out.stdout)
        return data.get("body", ""), data.get("updatedAt", ""), ""
    except Exception as exc:
        return None, None, f"gh_issue_fetch_exception: {exc}"


def _patch_issue_body(
    issue_number: int, repo: str, new_body: str, gh_bin: str
) -> str:
    """PATCH issue body via gh api argv-list (no gh issue edit CLI). Returns error or ''."""
    import tempfile

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(new_body)
            tmp_path = tmp.name
        try:
            out = subprocess.run(
                [gh_bin, "api", "--method", "PATCH",
                 f"repos/{repo}/issues/{issue_number}",
                 "--field", f"body=@{tmp_path}"],
                capture_output=True, text=True, timeout=15, shell=False,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if out.returncode != 0:
            return _classify_gh_error("gh_api_patch_failed", out.stderr or "")
        return ""
    except Exception as exc:
        return f"gh_api_patch_exception: {exc}"


def _post_gh_comment(
    issue_number: int, repo: str, body: str, gh_bin: str
) -> tuple[str, str, str]:
    """POST a comment via gh api argv-list. Returns (comment_url, comment_id, error)."""
    import tempfile

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(body)
            tmp_path = tmp.name
        try:
            out = subprocess.run(
                [gh_bin, "api", "--method", "POST",
                 f"repos/{repo}/issues/{issue_number}/comments",
                 "--field", f"body=@{tmp_path}"],
                capture_output=True, text=True, timeout=15, shell=False,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if out.returncode != 0:
            return "", "", _classify_gh_error("gh_api_post_comment_failed", out.stderr or "")
        try:
            resp = json.loads(out.stdout)
        except Exception as exc:
            return "", "", f"gh_api_post_comment_response_parse_error: {exc}"
        return str(resp.get("html_url", "")), str(resp.get("id", "")), ""
    except Exception as exc:
        return "", "", f"gh_api_post_comment_exception: {exc}"


# -- Postcondition check -------------------------------------------------------


def _check_no_tracked_changes(
    project_root: Path, issue_number: int, allowed_prefix: str | None = None
) -> list[str]:
    """Return list of violations (staged, unstaged, untracked source files). Empty = OK (AC14).

    Uses git status --porcelain=v1 --untracked-files=all.
    Allows writes inside allowed_prefix. Defaults to artifacts/{issue_number}/
    (legacy termination_report.publish write root). Issue #1284 Blocker 6: new
    command ids pass a command-id-scoped prefix
    (artifacts/{issue_number}/issue-metadata/{command_id}/) so the postcondition
    cannot be satisfied by writes to a sibling command's namespace.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "status", "--porcelain=v1",
             "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode != 0:
            return [f"git_status_failed: {out.stderr.strip()[:100]}"]

        if allowed_prefix is None:
            allowed_prefix = f"artifacts/{issue_number}/"
        violations = []
        for line in out.stdout.splitlines():
            if len(line) < 4:
                continue
            xy = line[:2]
            path = line[3:]
            # Allow writes inside artifacts/{issue_number}/
            if path.startswith(allowed_prefix):
                continue
            # Block staged (xy[0] != ' ' and != '?'), unstaged (xy[1] != ' '),
            # untracked ('??')
            if xy.strip() or xy == "??":
                violations.append(f"{xy}:{path}")
        return violations
    except Exception as exc:
        return [f"git_status_exception: {exc}"]


# -- Publisher invocation ------------------------------------------------------


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


# -- Main executor -------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Controlled skill mutation executor (Issue #1166 / #1284)"
    )
    parser.add_argument("--command-id", required=True, help="Command ID")
    parser.add_argument("--issue-number", type=int, required=True, help="GitHub issue number")
    parser.add_argument(
        "--input-file", default=None,
        help="Relative path to input JSON file (artifact subtree)",
    )
    parser.add_argument("--repo", required=True, help="GitHub repo slug (owner/repo)")
    parser.add_argument("--json", dest="output_json", action="store_true", help="JSON output")
    parser.add_argument("--dry-run", action="store_true", help="Validate but do not publish")
    # Issue #1539 fix_delta Blocker 1: pr_review.publish "render mode". A
    # trusted caller (NOT the pr-reviewer SubAgent, which has no Write/Edit
    # tool and may not write files via Bash either) supplies the raw verdict
    # body TEXT via --render-body-file plus the structured verdict metadata as
    # CLI flags; the executor independently computes body_sha256 /
    # idempotency_key and hardcodes producer_role="pr-reviewer" / event=
    # "COMMENT" itself instead of trusting a pre-built, self-hashed JSON.
    parser.add_argument(
        "--render-body-file", default=None,
        help="Relative path to a raw review body TEXT file (artifact subtree, "
             "pr_review.publish render mode only)",
    )
    parser.add_argument(
        "--verdict", default=None, choices=["APPROVE", "REQUEST_CHANGES", "COMMENT"],
        help="Declared verdict (render mode only)",
    )
    parser.add_argument(
        "--reviewed-head-sha", default=None,
        help="Head SHA the reviewer actually inspected (render mode only)",
    )
    parser.add_argument(
        "--expected-head-sha", default=None,
        help="Head SHA the review must be commit_id-bound to (render mode only)",
    )
    parser.add_argument(
        "--merge-ready", action="store_true",
        help="Declared merge_ready flag (render mode only; requires --verdict APPROVE)",
    )
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

    def _ok(extra: dict) -> int:
        result = {
            "schema": RESULT_SCHEMA,
            "status": "ok",
            "command_id": args.command_id,
            "issue_number": args.issue_number,
            "repo": args.repo,
        }
        result.update(extra)
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"[controlled_skill_mutation_exec] ok: {args.command_id} issue #{args.issue_number}", file=sys.stderr)
        return 0

    # -- AC10 / AC8: validate command_id (unknown command id → exit 2) --------
    if args.command_id not in ALL_COMMAND_IDS:
        return _fail(f"unknown_command_id: {args.command_id!r}")

    # -- validate repo ----------------------------------------------------------
    if args.repo != TRUSTED_REPO:
        return _fail(f"repo_mismatch: {args.repo!r} != {TRUSTED_REPO!r}")

    # -- git remote origin binding ----------------------------------------------
    origin_err = _verify_git_remote_origin(PROJECT_ROOT, TRUSTED_REPO)
    if origin_err:
        return _fail(origin_err)

    # -- find gh binary -----------------------------------------------------------
    gh_bin, gh_err = _find_gh_bin()
    if gh_bin is None:
        return _fail(gh_err)

    # -- AC15: issue binding (mandatory for legacy, optional-but-matching for new)
    env_err = _check_issue_env_binding(args.command_id, args.issue_number)
    if env_err:
        return _fail(env_err)

    # -- Issue #1539 fix_delta Blocker 1: pr_review.publish render mode -------
    # Mutually exclusive with --input-file. Only pr_review.publish supports it.
    render_mode = args.render_body_file is not None
    if render_mode and args.command_id != COMMAND_ID_PR_REVIEW_PUBLISH:
        return _fail("render_mode_not_supported_for_command_id")
    if render_mode and args.input_file is not None:
        return _fail("render_mode_and_input_file_mutually_exclusive")
    if not render_mode and args.input_file is None:
        return _fail("missing_input_source: neither --input-file nor --render-body-file given")

    if render_mode:
        canonical_input = None
        input_data, render_err = _render_pr_review_publish_request(args, PROJECT_ROOT)
        if render_err:
            return _fail(render_err)
    else:
        # -- input-file binding ---------------------------------------------------
        canonical_input, input_err = _validate_and_resolve_input_file(
            args.input_file, args.issue_number, PROJECT_ROOT, command_id=args.command_id
        )
        if input_err:
            return _fail(input_err)

        # -- AC10: per-command-id input schema validation ------------------------
        input_data, json_err = _load_and_validate_input_json(
            canonical_input, args.issue_number, args.command_id
        )
        if json_err:
            return _fail(json_err)

    # -- AC16: module realpath inspection (legacy publisher path only) ----------
    if args.command_id == COMMAND_ID_PUBLISH:
        realpath_errors = _check_module_realpaths(PROJECT_ROOT)
        if realpath_errors:
            return _fail("module_shadowing_detected", realpath_errors)

    if args.command_id == COMMAND_ID_PUBLISH:
        return _run_termination_report_publish(args, canonical_input, gh_bin, _fail, _ok)
    if args.command_id == COMMAND_ID_ISSUE_BODY_UPDATE:
        return _run_issue_body_update(args, input_data, gh_bin, _fail, _ok)
    if args.command_id == COMMAND_ID_ISSUE_COMMENT_PUBLISH:
        return _run_issue_comment_publish(args, canonical_input, input_data, gh_bin, _fail, _ok)
    if args.command_id == COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH:
        return _run_contract_snapshot_publish(args, input_data, gh_bin, _fail, _ok)
    if args.command_id == COMMAND_ID_ISSUE_SCOPE_SNAPSHOT_MATERIALIZE:
        return _run_issue_scope_snapshot_materialize(args, input_data, gh_bin, _fail, _ok)
    if args.command_id == COMMAND_ID_PR_REVIEW_PUBLISH:
        return _run_pr_review_publish(args, input_data, gh_bin, _fail, _ok)
    if args.command_id == COMMAND_ID_ISSUE_DEPENDENCY_REMOVE:
        return _run_issue_dependency_remove(args, input_data, gh_bin, _fail, _ok)

    return _fail(f"unhandled_command_id: {args.command_id!r}")  # pragma: no cover — defensive


def _run_issue_scope_snapshot_materialize(args, input_data, gh_bin, _fail, _ok) -> int:
    field_err = _validate_issue_scope_snapshot_materialize_fields(input_data, args.repo)
    if field_err:
        return _fail(field_err)
    if input_data["worktree_path"] != str(PROJECT_ROOT.resolve()):
        return _fail("issue_scope_snapshot_materialize_worktree_binding_mismatch")
    expected_output = (
        f"artifacts/{args.issue_number}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/"
        f"{args.command_id}/issue_scope_snapshot.json"
    )
    if input_data["output_path"] != expected_output:
        return _fail("issue_scope_snapshot_materialize_output_binding_mismatch")
    materializer_path = (PROJECT_ROOT / _ISSUE_SCOPE_SNAPSHOT_MATERIALIZER_REL).resolve()
    if not materializer_path.exists() or not materializer_path.is_relative_to(PROJECT_ROOT.resolve()):
        return _fail("issue_scope_snapshot_materializer_module_shadowing")
    if args.dry_run:
        return _ok({"status_detail": "dry_run_ok"})
    try:
        from materialize_issue_scope_snapshot import materialize

        # Issue #1629 fix_delta P1 (untrusted_gh_git_env): a resolved, trusted
        # gh_bin and a sanitized subprocess env are threaded into the
        # materializer explicitly, the same way every other controlled
        # mutation command id in this executor does -- the materializer must
        # never fall back to an ambient "gh"/"git" on PATH with an
        # unsanitized environment.
        result = materialize(
            issue_number=args.issue_number,
            repo=args.repo,
            contract_snapshot_url=input_data["contract_snapshot_url"],
            base_ref=input_data["base_ref"],
            branch_name=input_data["branch_name"],
            worktree_path=input_data["worktree_path"],
            output=input_data["output_path"],
            gh_bin=gh_bin,
            env=_build_metadata_sanitized_env(),
            project_root=PROJECT_ROOT,
        )
    except Exception as exc:
        return _fail(f"issue_scope_snapshot_materialize_failed: {exc}", status="failed")
    write_root = f"artifacts/{args.issue_number}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{args.command_id}/"
    changed = _check_no_tracked_changes(PROJECT_ROOT, args.issue_number, write_root)
    if changed:
        return _fail("postcondition_tracked_changes_detected", changed, status="failed")
    return _ok({"materializer_result": result})


def _run_termination_report_publish(args, canonical_input, gh_bin, _fail, _ok) -> int:
    # -- AC15: idempotency pre-check ------------------------------------------
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

    # -- Compute exec marker --------------------------------------------------
    exec_marker = _compute_exec_marker(
        args.command_id, args.repo, args.issue_number, canonical_input
    )

    # -- AC13: sanitized environment ------------------------------------------
    sanitized_env = _build_sanitized_env(PROJECT_ROOT, args.issue_number, exec_marker)

    # -- Invoke publisher ------------------------------------------------------
    rc, stdout, stderr = _invoke_publisher(
        project_root=PROJECT_ROOT,
        issue_number=args.issue_number,
        input_file=str(canonical_input),
        repo=args.repo,
        sanitized_env=sanitized_env,
    )

    if rc != 0:
        errors = [f"publisher_exit_{rc}", stderr[:500] if stderr else "no_stderr"]
        return _fail(f"publisher_failed_rc_{rc}", errors, status="failed")

    # -- AC14: postcondition -- no tracked/staged/untracked source file changes
    changed = _check_no_tracked_changes(PROJECT_ROOT, args.issue_number)
    if changed:
        return _fail(
            "postcondition_tracked_changes_detected",
            [f"changed: {f}" for f in changed[:20]],
            status="failed",
        )

    # -- AC15: comment read-back by exec marker -------------------------------
    readback = _readback_by_marker(exec_marker, args.issue_number, args.repo, gh_bin)
    if "error" in readback:
        return _fail(
            f"readback_failed: {readback['error']}",
            status="failed",
        )
    comment_id = readback.get("comment_id") or ""
    comment_url = readback.get("comment_url") or ""
    body_hash = readback.get("body_sha256") or ""

    # Write idempotency marker only after successful read-back
    _write_idempotency_marker(
        PROJECT_ROOT, args.issue_number, comment_id, comment_url, body_hash
    )

    return _ok({
        "comment_id": comment_id,
        "comment_url": comment_url,
        "body_sha256": body_hash,
        "idempotency_marker_written": True,
    })


def _issue_metadata_marker_path(project_root: Path, issue_number: int, command_id: str, name: str) -> Path:
    return project_root / "artifacts" / str(issue_number) / ISSUE_METADATA_NAMESPACE_SEGMENT / command_id / name


def _run_issue_body_update(args, input_data, gh_bin, _fail, _ok) -> int:
    # -- AC9: per-field schema validation (includes new_body_sha256 self-check)
    field_err = _validate_issue_body_update_fields(input_data)
    if field_err:
        return _fail(field_err)

    marker_path = _issue_metadata_marker_path(
        PROJECT_ROOT, args.issue_number, args.command_id, "issue_body_update.marker.json"
    )
    write_root = f"artifacts/{args.issue_number}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{args.command_id}/"

    if args.dry_run:
        result = {"schema": RESULT_SCHEMA, "status": "dry_run_ok", "command_id": args.command_id,
                   "issue_number": args.issue_number}
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # -- Blocker 1: local marker is cache/audit only, never remote-mutation
    # authority. Marker metadata is checked for consistency, but success or
    # failure is decided by a fresh remote readback below.
    marker_data = None
    marker_state = "absent"
    if marker_path.exists():
        try:
            marker_data = json.loads(marker_path.read_text())
        except Exception:
            return _fail("issue_body_update_marker_corrupt")
        if (
            marker_data.get("issue_number") != args.issue_number
            or marker_data.get("repo") != args.repo
        ):
            return _fail("issue_body_update_marker_metadata_mismatch")

    # -- Readback current remote state (authority for both the marker-hit path
    # and the normal stale-write precondition path).
    body, updated_at, err = _fetch_issue_body_and_updated_at(args.issue_number, args.repo, gh_bin)
    if err:
        return _fail(err, status="failed")
    current_body_sha256 = "sha256:" + hashlib.sha256((body or "").encode("utf-8")).hexdigest()

    if marker_data is not None:
        if current_body_sha256 == input_data["new_body_sha256"]:
            return _ok({
                "status_detail": "already_applied",
                "marker_state": "already_applied_remote_authority",
                "new_body_sha256": current_body_sha256,
                "idempotency_marker_found": True,
            })
        marker_state = "stale_local_marker_recovered"

    # -- AC9: stale-write precondition — readback must match previous_* --------
    if current_body_sha256 != input_data["previous_body_sha256"]:
        return _fail(
            f"stale_precondition_body_sha256_mismatch: current={current_body_sha256} "
            f"expected={input_data['previous_body_sha256']}",
            status="failed",
        )
    if updated_at != input_data["previous_updated_at"]:
        return _fail(
            f"stale_precondition_updated_at_mismatch: current={updated_at} "
            f"expected={input_data['previous_updated_at']}",
            status="failed",
        )

    # -- Mutate ------------------------------------------------------------------
    patch_err = _patch_issue_body(args.issue_number, args.repo, input_data["new_body"], gh_bin)
    if patch_err:
        return _fail(patch_err, status="failed")

    # -- AC4/AC9: postcondition readback — new_body_sha256 must match ------------
    body_after, _updated_at_after, err_after = _fetch_issue_body_and_updated_at(
        args.issue_number, args.repo, gh_bin
    )
    if err_after:
        return _fail(err_after, status="failed")
    actual_new_sha256 = "sha256:" + hashlib.sha256((body_after or "").encode("utf-8")).hexdigest()
    if actual_new_sha256 != input_data["new_body_sha256"]:
        return _fail(
            f"postcondition_new_body_sha256_mismatch: actual={actual_new_sha256} "
            f"expected={input_data['new_body_sha256']}",
            status="failed",
        )

    # -- AC14 / Blocker 6: postcondition -- no changes outside this command's
    # own write root (artifacts/{issue}/issue-metadata/issue_body.update/).
    changed = _check_no_tracked_changes(PROJECT_ROOT, args.issue_number, write_root)
    if changed:
        return _fail(
            "postcondition_tracked_changes_detected",
            [f"changed: {f}" for f in changed[:20]],
            status="failed",
        )

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps({
        "schema": "ISSUE_BODY_UPDATE_MARKER_V1",
        "issue_number": args.issue_number,
        "repo": args.repo,
        "new_body_sha256": actual_new_sha256,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }, ensure_ascii=False, indent=2))

    return _ok({
        "new_body_sha256": actual_new_sha256,
        "idempotency_marker_written": True,
        "marker_state": marker_state,
    })


def _run_issue_comment_publish(args, canonical_input, input_data, gh_bin, _fail, _ok) -> int:
    field_err = _validate_issue_comment_publish_fields(input_data)
    if field_err:
        return _fail(field_err)

    marker = input_data["marker"]
    comment_body = input_data["comment_body"]
    expected_body_sha256 = hashlib.sha256(comment_body.encode()).hexdigest()
    marker_path = _issue_metadata_marker_path(
        PROJECT_ROOT, args.issue_number, args.command_id, "issue_comment_publish.marker.json"
    )
    write_root = f"artifacts/{args.issue_number}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{args.command_id}/"

    if args.dry_run:
        result = {"schema": RESULT_SCHEMA, "status": "dry_run_ok", "command_id": args.command_id,
                   "issue_number": args.issue_number}
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    def _write_marker(comment_id, comment_url) -> None:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps({
            "schema": "ISSUE_COMMENT_PUBLISH_MARKER_V1",
            "issue_number": args.issue_number,
            "repo": args.repo,
            "marker": marker,
            "comment_id": comment_id,
            "comment_url": comment_url,
            "published_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }, ensure_ascii=False, indent=2))

    # -- Blocker 2/3: pre-mutation remote marker precheck. A local marker file
    # is never authority by itself; remote GitHub state decides no-op vs. post
    # vs. conflict, and this check runs BEFORE any POST so a failed transaction
    # never leaves a remote side effect.
    matches, list_err = _find_marker_matches(marker, args.issue_number, args.repo, gh_bin)
    if list_err:
        return _fail(f"marker_precheck_failed: {list_err}", status="failed")

    if len(matches) > 1:
        return _fail("duplicate_marker_conflict_pre_mutation", status="failed")

    if len(matches) == 1:
        c = matches[0]
        remote_body_sha256 = hashlib.sha256(c.get("body", "").encode()).hexdigest()
        if remote_body_sha256 != expected_body_sha256:
            return _fail("remote_marker_identity_conflict_pre_mutation", status="failed")
        # No-op: already published by a prior run (or another agent). Refresh
        # the local cache/audit marker but do not POST again.
        _write_marker(c.get("id", ""), c.get("url", ""))
        return _ok({
            "status_detail": "already_published",
            "comment_id": c.get("id", ""),
            "comment_url": c.get("url", ""),
            "body_sha256": remote_body_sha256,
            "idempotency_marker_written": True,
        })

    # -- matches == 0: no remote marker yet, proceed to post --------------------
    comment_url, comment_id, post_err = _post_gh_comment(
        args.issue_number, args.repo, comment_body, gh_bin
    )
    if post_err:
        return _fail(post_err, status="failed")

    # -- AC4/AC14: postcondition readback by marker — false success not allowed -
    readback = _readback_by_marker_literal(marker, args.issue_number, args.repo, gh_bin)
    if "error" in readback:
        return _fail(f"readback_failed: {readback['error']}", status="failed")
    if readback.get("body_sha256") != expected_body_sha256:
        return _fail("postcondition_body_sha256_mismatch", status="failed")

    # -- AC14 / Blocker 6: postcondition -- no changes outside this command's
    # own write root (artifacts/{issue}/issue-metadata/issue_comment.publish/).
    changed = _check_no_tracked_changes(PROJECT_ROOT, args.issue_number, write_root)
    if changed:
        return _fail(
            "postcondition_tracked_changes_detected",
            [f"changed: {f}" for f in changed[:20]],
            status="failed",
        )

    _write_marker(readback.get("comment_id"), readback.get("comment_url"))

    return _ok({
        "comment_id": readback.get("comment_id"),
        "comment_url": readback.get("comment_url"),
        "body_sha256": readback.get("body_sha256"),
        "idempotency_marker_written": True,
    })


def _find_marker_matches(marker_literal: str, issue_number: int, repo: str, gh_bin: str) -> tuple[list[dict], str]:
    """List all remote comments containing marker_literal. Returns (matches, error).

    Used as the pre-mutation precheck for issue_comment.publish (Blocker 3):
    the caller must know remote marker count/identity BEFORE deciding whether
    to POST, so that a failed transaction never leaves a remote side effect.
    """
    try:
        out = subprocess.run(
            [gh_bin, "issue", "view", str(issue_number), "--repo", repo, "--json", "comments"],
            capture_output=True, text=True, timeout=15, shell=False,
        )
        if out.returncode != 0:
            return [], f"gh_failed_rc_{out.returncode}"
        data = json.loads(out.stdout)
        comments = data.get("comments", [])
        matches = [c for c in comments if marker_literal in c.get("body", "")]
        return matches, ""
    except Exception as exc:
        return [], f"marker_list_exception:{exc}"


def _readback_by_marker_literal(marker_literal: str, issue_number: int, repo: str, gh_bin: str) -> dict:
    """Search comments for a literal marker string (issue_comment.publish uses
    caller-provided markers rather than the EXEC_MARKER_PREFIX wrapping used by
    termination_report.publish)."""
    try:
        out = subprocess.run(
            [gh_bin, "issue", "view", str(issue_number), "--repo", repo, "--json", "comments"],
            capture_output=True, text=True, timeout=15, shell=False,
        )
        if out.returncode != 0:
            return {"error": f"gh_failed_rc_{out.returncode}"}
        data = json.loads(out.stdout)
        comments = data.get("comments", [])
        matches = [c for c in comments if marker_literal in c.get("body", "")]
        if len(matches) == 0:
            return {"error": "marker_not_found"}
        if len(matches) > 1:
            return {"error": f"marker_found_{len(matches)}_times"}
        c = matches[0]
        body = c.get("body", "")
        return {
            "comment_id": c.get("id", ""),
            "comment_url": c.get("url", ""),
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        }
    except Exception as exc:
        return {"error": f"readback_exception:{exc}"}


# -- Issue #1536: controlled PR review publisher (pr_review.publish) -----------

PR_REVIEW_MARKER_PREFIX = "<!-- PR_REVIEW_PUBLISH_MARKER:"
PR_REVIEW_MARKER_SUFFIX = " -->"

# Issue #1539 fix_delta Blocker 2: env vars that must never reach the `gh`
# subprocess for pr_review.publish, beyond the generic ENV_SANITIZE_KEYS
# (GH_HOST / GH_REPO / GH_CONFIG_DIR / GH_DEBUG / DEBUG can silently redirect
# `gh` to a different host/config or leak debug output; an inherited parent
# env is never trusted here).
_PR_REVIEW_GH_ENV_STRIP_KEYS = frozenset(ENV_SANITIZE_KEYS) | frozenset({
    "GH_HOST", "GH_REPO", "GH_CONFIG_DIR", "GH_DEBUG", "DEBUG",
})


def _build_pr_review_gh_env() -> dict[str, str]:
    """Sanitized environment for every `gh` subprocess call made while
    publishing a PR review. Built fresh (not memoized) so each call gets an
    independent copy that later mutation cannot cross-contaminate."""
    env = os.environ.copy()
    for key in _PR_REVIEW_GH_ENV_STRIP_KEYS:
        env.pop(key, None)
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    return env


def _pr_review_marker_str(idempotency_key: str) -> str:
    marker_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
    return f"{PR_REVIEW_MARKER_PREFIX}{marker_hash}{PR_REVIEW_MARKER_SUFFIX}"


def _marker_at_expected_position(body: str, marker_str: str) -> bool:
    """True iff marker_str occurs exactly once in body AND that occurrence is
    the trailing content (i.e. the publisher's own appended marker, not an
    unrelated mid-body substring match -- Issue #1539 fix_delta Blocker 3)."""
    if not body or body.count(marker_str) != 1:
        return False
    return body.rstrip("\n").endswith(marker_str)


def _fetch_pr_head_sha(
    pr_number: int, repo: str, gh_bin: str, env: dict[str, str] | None = None
) -> tuple[str | None, str]:
    """Fetch the current remote PR head commit SHA. Returns (sha, error)."""
    try:
        out = subprocess.run(
            [gh_bin, "api", "--hostname", _TRUSTED_GITHUB_HOST,
             f"repos/{repo}/pulls/{pr_number}", "--jq", ".head.sha"],
            capture_output=True, text=True, timeout=15, shell=False, env=env,
        )
        if out.returncode != 0:
            return None, _classify_gh_error("gh_api_pr_head_fetch_failed", out.stderr or "")
        sha = out.stdout.strip()
        if not _PR_HEAD_SHA_RE.match(sha):
            return None, f"gh_api_pr_head_unexpected_output: {sha!r}"
        return sha, ""
    except Exception as exc:
        return None, f"gh_api_pr_head_fetch_exception: {exc}"


def _fetch_authenticated_login(
    gh_bin: str, env: dict[str, str] | None = None
) -> tuple[str | None, str]:
    """Fetch the authenticated gh identity's login. Used as a postcondition
    identity binding when re-verifying an idempotent-retry review (Issue #1539
    fix_delta Blocker 3): the review author must be the SAME identity this
    process is authenticated as, not an unrelated/spoofed account."""
    try:
        out = subprocess.run(
            [gh_bin, "api", "--hostname", _TRUSTED_GITHUB_HOST, "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=15, shell=False, env=env,
        )
        if out.returncode != 0:
            return None, _classify_gh_error("gh_api_authenticated_user_failed", out.stderr or "")
        login = out.stdout.strip()
        if not login:
            return None, "gh_api_authenticated_user_empty"
        return login, ""
    except Exception as exc:
        return None, f"gh_api_authenticated_user_exception: {exc}"


def _find_pr_review_marker_matches(
    marker_literal: str, pr_number: int, repo: str, gh_bin: str,
    env: dict[str, str] | None = None,
) -> tuple[list[dict], str]:
    """List all remote reviews on the PR whose body embeds marker_literal AT
    THE EXPECTED TRAILING POSITION (not merely as a substring anywhere in the
    body -- Issue #1539 fix_delta Blocker 3). Mirrors _find_marker_matches()
    for issue_comment.publish (Blocker 3 pattern): the caller must know remote
    marker count/identity BEFORE deciding whether to POST, so a failed
    transaction never leaves a remote side effect (AC7)."""
    try:
        out = subprocess.run(
            [gh_bin, "api", "--hostname", _TRUSTED_GITHUB_HOST,
             f"repos/{repo}/pulls/{pr_number}/reviews", "--paginate", "--jq", "."],
            capture_output=True, text=True, timeout=15, shell=False, env=env,
        )
        if out.returncode != 0:
            return [], _classify_gh_error("gh_api_pr_reviews_list_failed", out.stderr or "")
        reviews: list[dict] = []
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parsed = json.loads(line)
            if isinstance(parsed, list):
                reviews.extend(parsed)
            else:
                reviews.append(parsed)
        matches = [
            r for r in reviews
            if _marker_at_expected_position(r.get("body") or "", marker_literal)
        ]
        return matches, ""
    except Exception as exc:
        return [], f"pr_review_marker_list_exception: {exc}"


def _post_pr_review(
    pr_number: int, repo: str, commit_id: str, event: str, body: str, gh_bin: str,
    env: dict[str, str] | None = None,
) -> tuple[dict | None, str]:
    """POST a PR review via gh api with JSON stdin (--input -), never via
    shell-interpolated -f/-F flags, so an arbitrary body (backticks, pipes,
    `$(...)`, quotes) round-trips byte-for-byte (AC6)."""
    payload = json.dumps({"commit_id": commit_id, "event": event, "body": body})
    try:
        out = subprocess.run(
            [gh_bin, "api", "--hostname", _TRUSTED_GITHUB_HOST, "--method", "POST",
             f"repos/{repo}/pulls/{pr_number}/reviews", "--input", "-"],
            input=payload, capture_output=True, text=True, timeout=20, shell=False, env=env,
        )
        if out.returncode != 0:
            return None, _classify_gh_error("gh_api_post_review_failed", out.stderr or "")
        try:
            return json.loads(out.stdout), ""
        except Exception as exc:
            return None, f"gh_api_post_review_response_parse_error: {exc}"
    except Exception as exc:
        return None, f"gh_api_post_review_exception: {exc}"


def _readback_pr_review(
    review_id, pr_number: int, repo: str, gh_bin: str, env: dict[str, str] | None = None
) -> dict:
    """Fetch exactly one review by id (not a marker search) for postcondition
    verification (AC4: state == COMMENTED, commit_id == expected_head_sha)."""
    try:
        out = subprocess.run(
            [gh_bin, "api", "--hostname", _TRUSTED_GITHUB_HOST,
             f"repos/{repo}/pulls/{pr_number}/reviews/{review_id}"],
            capture_output=True, text=True, timeout=15, shell=False, env=env,
        )
        if out.returncode != 0:
            return {"error": _classify_gh_error("gh_api_pr_review_readback_failed", out.stderr or "")}
        return {"review": json.loads(out.stdout)}
    except Exception as exc:
        return {"error": f"pr_review_readback_exception: {exc}"}


def _validate_pr_review_postcondition(
    review: dict,
    expected_head_sha: str,
    marker_str: str,
    body_sha256: str,
    authenticated_login: str | None,
) -> tuple[bool, str, str]:
    """Shared postcondition validator used by BOTH the fresh-post path and the
    idempotent-retry path (Issue #1539 fix_delta Blocker 3): state ==
    COMMENTED, commit_id == expected_head_sha, submitted_at present, marker at
    the expected trailing position (not a mid-body substring), rendered body
    hash matches, and (when an authenticated_login is supplied) the review
    author identity matches. Returns (ok, error_reason, stripped_body_sha256).
    """
    if review.get("state") != "COMMENTED":
        return False, f"postcondition_review_state_mismatch: {review.get('state')!r}", ""
    if review.get("commit_id") != expected_head_sha:
        return False, (
            f"postcondition_review_commit_id_mismatch: {review.get('commit_id')!r} "
            f"!= {expected_head_sha!r}"
        ), ""
    if not review.get("submitted_at"):
        return False, "postcondition_review_submitted_at_missing", ""
    body = review.get("body") or ""
    if not _marker_at_expected_position(body, marker_str):
        return False, "postcondition_marker_not_at_expected_position", ""
    # AC6: strip the trailing marker before hashing so the round-tripped
    # UTF-8 body content (not the executor-appended marker) must hash to
    # the caller-declared body_sha256.
    #
    # Issue #1539 fix_delta: rendered_body is constructed as exactly
    # f"{raw_body}\n\n{marker_str}\n" -- the only bytes the executor adds
    # between raw_body and the marker are the fixed 2-char separator "\n\n".
    # Strip exactly that fixed separator, not an open-ended rstrip, so the
    # input-side and readback-side hashes apply the identical normalization
    # rule (i.e. none) to raw_body.
    marker_idx = body.rfind(marker_str)
    pre_marker = body[:marker_idx]
    if pre_marker.endswith("\n\n"):
        stripped_body = pre_marker[: -len("\n\n")]
    else:
        stripped_body = pre_marker
    stripped_body_sha256 = hashlib.sha256(stripped_body.encode("utf-8")).hexdigest()
    if stripped_body_sha256 != body_sha256:
        return False, (
            f"postcondition_body_sha256_mismatch: readback={stripped_body_sha256} "
            f"expected={body_sha256}"
        ), ""
    if authenticated_login is not None:
        actual_login = (review.get("user") or {}).get("login")
        if actual_login != authenticated_login:
            return False, (
                f"postcondition_review_author_identity_mismatch: {actual_login!r} "
                f"!= {authenticated_login!r}"
            ), ""
    return True, "", stripped_body_sha256


# -- Issue #1539 fix_delta Blocker 1: pr_review.publish render mode -----------
# This is the "trusted bridge": a trusted caller (the impl-review-loop
# control-plane, NOT the sandboxed pr-reviewer SubAgent) provides only a raw
# body TEXT file plus structured verdict metadata as CLI flags. This function
# independently computes body_sha256 / idempotency_key, hardcodes
# producer_role="pr-reviewer" and event="COMMENT" (never read from any input),
# and cross-checks the body's embedded LOOP_VERDICT_V2 fenced-YAML block
# against the CLI-declared --verdict/--merge-ready so the two can never
# silently diverge (High 2: no self-reported hash/schema/producer_role).

_LOOP_VERDICT_V2_BLOCK_RE = _re.compile(
    r"```ya?ml\s*\n\s*LOOP_VERDICT_V2\s*:\s*\n(?P<block>.*?)```", _re.DOTALL
)
_LOOP_VERDICT_V2_VERDICT_FIELD_RE = _re.compile(
    r"^\s*verdict\s*:\s*([A-Za-z_]+)\s*$", _re.MULTILINE
)
_LOOP_VERDICT_V2_MERGE_READY_FIELD_RE = _re.compile(
    r"^\s*merge_ready\s*:\s*(true|false)\s*$", _re.MULTILINE | _re.IGNORECASE
)


def _extract_loop_verdict_v2_fields(body: str) -> tuple[str | None, bool | None, str]:
    """Extract (verdict, merge_ready, error) from a LOOP_VERDICT_V2 fenced YAML
    block embedded in body. error is non-empty iff the block, or either
    required field within it, could not be found."""
    m = _LOOP_VERDICT_V2_BLOCK_RE.search(body)
    if not m:
        return None, None, "pr_review_render_body_missing_loop_verdict_v2_block"
    block = m.group("block")
    vm = _LOOP_VERDICT_V2_VERDICT_FIELD_RE.search(block)
    if not vm:
        return None, None, "pr_review_render_body_loop_verdict_v2_missing_verdict_field"
    mm = _LOOP_VERDICT_V2_MERGE_READY_FIELD_RE.search(block)
    if not mm:
        return None, None, "pr_review_render_body_loop_verdict_v2_missing_merge_ready_field"
    return vm.group(1), mm.group(1).lower() == "true", ""


def _render_pr_review_publish_request(args, project_root: Path) -> tuple[dict | None, str]:
    """Build a PR_REVIEW_PUBLISH_REQUEST_V1 dict from render-mode CLI flags,
    without ever trusting a caller-declared hash, schema, producer_role, or
    event. Returns (input_data, error)."""
    if args.verdict is None:
        return None, "pr_review_render_missing_verdict"
    if args.reviewed_head_sha is None:
        return None, "pr_review_render_missing_reviewed_head_sha"
    if args.expected_head_sha is None:
        return None, "pr_review_render_missing_expected_head_sha"

    if not _PR_HEAD_SHA_RE.match(args.expected_head_sha):
        return None, f"pr_review_render_expected_head_sha_invalid: {args.expected_head_sha!r}"
    if not _PR_HEAD_SHA_RE.match(args.reviewed_head_sha):
        return None, f"pr_review_render_reviewed_head_sha_invalid: {args.reviewed_head_sha!r}"
    # High 2: reviewed_head_sha and expected_head_sha must be semantically
    # consistent -- the reviewer's own inspection target must be the same
    # commit the publish transaction will bind to.
    if args.reviewed_head_sha != args.expected_head_sha:
        return None, (
            f"pr_review_render_reviewed_head_sha_mismatch: "
            f"reviewed={args.reviewed_head_sha!r} expected={args.expected_head_sha!r}"
        )

    # High 2: merge_ready may only be declared true alongside verdict APPROVE.
    if args.merge_ready and args.verdict != "APPROVE":
        return None, (
            f"pr_review_render_merge_ready_requires_approve: verdict={args.verdict!r}"
        )

    # -- render-body-file path safety: same lexical/symlink/hardlink/subtree
    # containment checks as --input-file, scoped to the pr_review.publish
    # artifact subtree.
    canonical_body_file, path_err = _validate_and_resolve_input_file(
        args.render_body_file, args.issue_number, project_root,
        command_id=COMMAND_ID_PR_REVIEW_PUBLISH,
    )
    if path_err:
        return None, f"pr_review_render_body_file_invalid: {path_err}"

    try:
        raw_bytes = canonical_body_file.read_bytes()
    except Exception as exc:
        return None, f"pr_review_render_body_read_error: {exc}"

    if len(raw_bytes) > _PR_REVIEW_BODY_MAX_BYTES:
        return None, f"pr_review_render_body_too_large: {len(raw_bytes)} bytes"
    if not raw_bytes:
        return None, "pr_review_render_body_empty"

    try:
        body = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, f"pr_review_render_body_not_utf8: {exc}"

    # High 2: cross-check the body's own embedded LOOP_VERDICT_V2 block against
    # the CLI-declared verdict/merge_ready so the rendered comment text and the
    # structured metadata used to build the publish request can never diverge.
    body_verdict, body_merge_ready, extract_err = _extract_loop_verdict_v2_fields(body)
    if extract_err:
        return None, extract_err
    if body_verdict != args.verdict:
        return None, (
            f"pr_review_render_body_verdict_mismatch: body={body_verdict!r} "
            f"declared={args.verdict!r}"
        )
    if body_merge_ready != bool(args.merge_ready):
        return None, (
            f"pr_review_render_body_merge_ready_mismatch: body={body_merge_ready!r} "
            f"declared={bool(args.merge_ready)!r}"
        )

    body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    idempotency_key = f"{args.repo}:{args.issue_number}:{args.expected_head_sha}:{body_sha256}"

    return {
        "schema": "PR_REVIEW_PUBLISH_REQUEST_V1",
        "issue_number": args.issue_number,
        "repo": args.repo,
        "pr_number": args.issue_number,
        "expected_head_sha": args.expected_head_sha,
        "event": "COMMENT",
        "producer_role": "pr-reviewer",
        "body": body,
        "body_sha256": body_sha256,
        "idempotency_key": idempotency_key,
    }, ""


def _run_pr_review_publish(args, input_data, gh_bin, _fail, _ok) -> int:
    field_err = _validate_pr_review_publish_fields(input_data, args.repo, args.issue_number)
    if field_err:
        return _fail(field_err)

    pr_number = input_data["pr_number"]
    expected_head_sha = input_data["expected_head_sha"]
    raw_body = input_data["body"]
    body_sha256 = input_data["body_sha256"]
    idempotency_key = input_data["idempotency_key"]
    marker_str = _pr_review_marker_str(idempotency_key)
    rendered_body = f"{raw_body}\n\n{marker_str}\n"

    marker_path = _issue_metadata_marker_path(
        PROJECT_ROOT, args.issue_number, args.command_id, "pr_review_publish.marker.json"
    )
    write_root = f"artifacts/{args.issue_number}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{args.command_id}/"

    if args.dry_run:
        result = {"schema": RESULT_SCHEMA, "status": "dry_run_ok", "command_id": args.command_id,
                   "issue_number": args.issue_number}
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    gh_env = _build_pr_review_gh_env()

    def _write_marker(review_id, review_url) -> None:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps({
            "schema": "PR_REVIEW_PUBLISH_MARKER_V1",
            "pr_number": pr_number,
            "repo": args.repo,
            "idempotency_key": idempotency_key,
            "expected_head_sha": expected_head_sha,
            "review_id": review_id,
            "review_url": review_url,
            "published_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }, ensure_ascii=False, indent=2))

    # -- AC7: pre-mutation idempotency precheck. Remote review list is the
    # authority; a local marker file never substitutes for it. Runs BEFORE
    # any POST so a failed/ambiguous transaction never leaves a remote
    # side effect. _find_pr_review_marker_matches() itself now only returns
    # reviews where the marker is at the expected trailing position (Blocker 3).
    matches, list_err = _find_pr_review_marker_matches(
        marker_str, pr_number, args.repo, gh_bin, env=gh_env
    )
    if list_err:
        return _fail(f"pr_review_marker_precheck_failed: {list_err}", status="failed")

    if len(matches) > 1:
        return _fail("pr_review_duplicate_marker_conflict_pre_mutation", status="failed")

    if len(matches) == 1:
        # -- Issue #1539 fix_delta Blocker 3: idempotent-retry hardening. The
        # previous implementation trusted the LIST entry's state/commit_id and
        # returned success immediately -- it never re-verified body hash,
        # marker uniqueness/position, current PR head, author identity, or
        # tracked-changes postcondition. Retry now runs the SAME postcondition
        # validator as the fresh-post path against a FRESH single-review GET
        # (not the list entry), plus a fresh current-head fetch and an
        # authenticated-identity check.
        candidate_id = matches[0].get("id")
        fresh_readback = _readback_pr_review(candidate_id, pr_number, args.repo, gh_bin, env=gh_env)
        if "error" in fresh_readback:
            return _fail(
                f"pr_review_retry_readback_failed: {fresh_readback['error']}",
                [f"posted_review_id: {candidate_id}"],
                status="failed",
            )
        review = fresh_readback["review"]

        current_head_sha, head_err = _fetch_pr_head_sha(pr_number, args.repo, gh_bin, env=gh_env)
        if head_err:
            return _fail(
                f"pr_review_retry_current_head_fetch_failed: {head_err}",
                [f"posted_review_id: {candidate_id}"],
                status="failed",
            )

        authenticated_login, login_err = _fetch_authenticated_login(gh_bin, env=gh_env)
        if login_err:
            return _fail(
                f"pr_review_retry_authenticated_identity_fetch_failed: {login_err}",
                [f"posted_review_id: {candidate_id}"],
                status="failed",
            )

        ok, post_err, stripped_body_sha256 = _validate_pr_review_postcondition(
            review, expected_head_sha, marker_str, body_sha256, authenticated_login
        )
        if not ok:
            return _fail(
                f"pr_review_remote_marker_identity_conflict_pre_mutation: {post_err}",
                [f"posted_review_id: {candidate_id}"],
                status="failed",
            )

        if current_head_sha != expected_head_sha:
            # The review that already exists is valid for expected_head_sha,
            # but the PR has moved on since -- do not report success for a
            # verdict that is no longer current (High 1 TOCTOU symmetry).
            _write_marker(review.get("id"), review.get("html_url", ""))
            return _fail(
                f"published_but_stale: current_head={current_head_sha} "
                f"expected_head={expected_head_sha}",
                [f"posted_review_id: {review.get('id')}"],
                status="published_but_stale",
            )

        changed = _check_no_tracked_changes(PROJECT_ROOT, args.issue_number, write_root)
        if changed:
            return _fail(
                "postcondition_tracked_changes_detected",
                [f"changed: {f}" for f in changed[:20]],
                status="failed",
            )

        _write_marker(review.get("id"), review.get("html_url", ""))
        return _ok({
            "status_detail": "already_published",
            "review_id": review.get("id"),
            "review_url": review.get("html_url", ""),
            "commit_id": review.get("commit_id"),
            "body_sha256": stripped_body_sha256,
            "idempotency_marker_written": True,
        })

    # -- AC3: stale-head precondition. GitHub review must never be created
    # against a PR head that has moved since the reviewer inspected it.
    current_head_sha, head_err = _fetch_pr_head_sha(pr_number, args.repo, gh_bin, env=gh_env)
    if head_err:
        return _fail(head_err, status="failed")
    if current_head_sha != expected_head_sha:
        return _fail(
            f"stale_review_request: current_head={current_head_sha} "
            f"expected_head={expected_head_sha}",
            status="failed",
        )

    # -- matches == 0 and head is fresh: proceed to post -----------------------
    post_result, post_err = _post_pr_review(
        pr_number, args.repo, expected_head_sha, "COMMENT", rendered_body, gh_bin, env=gh_env
    )
    if post_err:
        return _fail(post_err, status="failed")

    review_id = post_result.get("id") if isinstance(post_result, dict) else None
    if review_id is None:
        return _fail("pr_review_post_response_missing_id", status="failed")

    # -- AC4: readback -- POST response fields are not trusted on their own;
    # a fresh GET readback by review id is the postcondition authority.
    readback = _readback_pr_review(review_id, pr_number, args.repo, gh_bin, env=gh_env)
    if "error" in readback:
        return _fail(
            readback["error"],
            [f"posted_review_id: {review_id}"],
            status="failed",
        )
    review = readback["review"]

    ok, post_check_err, readback_body_sha256 = _validate_pr_review_postcondition(
        review, expected_head_sha, marker_str, body_sha256, None
    )
    if not ok:
        return _fail(
            post_check_err,
            [f"posted_review_id: {review_id}"],
            status="failed",
        )

    # -- AC14 postcondition: no changes outside this command's own write root.
    changed = _check_no_tracked_changes(PROJECT_ROOT, args.issue_number, write_root)
    if changed:
        return _fail(
            "postcondition_tracked_changes_detected",
            [f"changed: {f}" for f in changed[:20]],
            status="failed",
        )

    # -- Issue #1539 fix_delta High 1: TOCTOU close-out. commit_id binding
    # proves the review is ATTACHED to expected_head_sha; it is not an atomic
    # guarantee that expected_head_sha was STILL current PR head at POST time.
    # Re-fetch current head one more time after the review is durably posted
    # and postcondition-verified; if it has moved, the review evidence is kept
    # (a GitHub review cannot be un-posted) but success is NOT reported --
    # callers must route to a fresh review against the new head.
    post_publish_head_sha, post_head_err = _fetch_pr_head_sha(
        pr_number, args.repo, gh_bin, env=gh_env
    )
    if post_head_err:
        _write_marker(review.get("id"), review.get("html_url", ""))
        return _fail(
            f"published_but_unverified_current_head: {post_head_err}",
            [f"posted_review_id: {review_id}"],
            status="published_but_unverified",
        )
    if post_publish_head_sha != expected_head_sha:
        _write_marker(review.get("id"), review.get("html_url", ""))
        return _fail(
            f"published_but_stale: current_head={post_publish_head_sha} "
            f"expected_head={expected_head_sha}",
            [f"posted_review_id: {review_id}"],
            status="published_but_stale",
        )

    _write_marker(review.get("id"), review.get("html_url", ""))

    return _ok({
        "review_id": review.get("id"),
        "review_url": review.get("html_url", ""),
        "commit_id": review.get("commit_id"),
        "state": review.get("state"),
        "body_sha256": readback_body_sha256,
        "idempotency_marker_written": True,
    })


_ISSUECOMMENT_ID_RE = _re.compile(r"#issuecomment-(\d+)$")
_CANONICAL_SINGLE_COMMENT_PROJECTION = (
    "{id, html_url, created_at, updated_at, body, "
    "author: .user.login, author_id: .user.id, "
    "author_type: .user.type, author_association}"
)


def _extract_comment_id_from_url(url: str) -> str | None:
    """Extract the numeric comment id from a GitHub `#issuecomment-<id>` URL."""
    if not url:
        return None
    m = _ISSUECOMMENT_ID_RE.search(url)
    if not m:
        return None
    return m.group(1)


def _fetch_single_comment_by_id(comment_id: str, repo: str, gh_bin: str) -> dict:
    """Fetch exactly one comment by id (not a marker search across all comments)."""
    try:
        out = subprocess.run(
            [
                gh_bin,
                "api",
                "--hostname",
                _TRUSTED_GITHUB_HOST,
                f"repos/{repo}/issues/comments/{comment_id}",
                "--jq",
                _CANONICAL_SINGLE_COMMENT_PROJECTION,
            ],
            capture_output=True, text=True, timeout=15, shell=False,
            env=_build_metadata_sanitized_env(),
        )
        if out.returncode != 0:
            return {"error": f"comment_fetch_failed_rc_{out.returncode}"}
        return {"comment": json.loads(out.stdout)}
    except Exception as exc:
        return {"error": f"comment_fetch_exception:{exc}"}


def _readback_contract_snapshot(
    marker_literal: str,
    issue_number: int,
    repo: str,
    gh_bin: str,
    expected_url: str,
    expected_body_sha256: str,
) -> dict:
    """Verify the posted snapshot against remote comment state, not child stdout.

    Issue #1459 review Blocker (legacy_refresh_duplicate_marker_deadlock): the
    idempotency marker is derived from (issue, body_sha256, schema) only, so a
    stale legacy go comment can share the exact same marker text as the fresh
    comment the publisher just posted. Searching all comments for a *unique*
    marker match therefore deadlocks permanently once both comments coexist.
    This function instead selects the single comment the publisher itself
    reported posting (by the comment id parsed from expected_url / html_url)
    and verifies marker/YAML/is_go_current against that one comment only.
    Global marker uniqueness across the whole comment list is not required.
    """
    try:
        comment_id = _extract_comment_id_from_url(expected_url)
        if not comment_id:
            return {"error": "contract_snapshot_url_missing_comment_id"}

        fetched = _fetch_single_comment_by_id(comment_id, repo, gh_bin)
        if "error" in fetched:
            return {"error": fetched["error"]}
        comment = fetched["comment"]
        if comment.get("html_url") != expected_url:
            return {"error": "remote_contract_snapshot_url_mismatch"}
        body = comment.get("body", "") or ""
        if marker_literal not in body:
            return {"error": "expected_contract_marker_not_embedded_in_selected_comment"}

        import importlib.util

        parser_path = PROJECT_ROOT / ".claude/skills/issue-contract-review/scripts/contract_review_result_parser.py"
        parser_spec = importlib.util.spec_from_file_location("contract_review_result_parser", parser_path)
        ensure_path = PROJECT_ROOT / _ENSURE_CONTRACT_SNAPSHOT_REL
        ensure_spec = importlib.util.spec_from_file_location("ensure_contract_snapshot", ensure_path)
        if not parser_spec or not parser_spec.loader or not ensure_spec or not ensure_spec.loader:
            return {"error": "contract_snapshot_readback_import_error"}
        parser_mod = importlib.util.module_from_spec(parser_spec)
        parser_spec.loader.exec_module(parser_mod)
        ensure_mod = importlib.util.module_from_spec(ensure_spec)
        ensure_spec.loader.exec_module(ensure_mod)
        issue_url = f"https://github.com/{repo}/issues/{issue_number}"
        results = parser_mod.parse_contract_review_results([comment], issue_url)
        # #1475 fix_delta P1 item 3: this is the actual controlled mutation
        # boundary. It must apply the same trusted_only=True gate as every
        # other consumer -- an untrusted comment must never be treated as an
        # authoritative snapshot readback here either.
        authoritative_go = getattr(parser_mod, "find_latest_authoritative_go", None)
        if callable(authoritative_go):
            snapshot = authoritative_go(results)
        else:
            try:
                snapshot = parser_mod.find_latest_go(
                    results, trusted_only=True, fingerprint_ready_only=True
                )
            except TypeError:  # legacy test-double only; production parser has the predicate
                snapshot = parser_mod.find_latest_go(results, trusted_only=True)
        if snapshot is None or not ensure_mod.is_go_current(snapshot, expected_body_sha256):
            return {"error": "remote_contract_snapshot_not_current"}

        # -- Issue #1459 review Blocker (post_publish_live_body_not_revalidated) --
        # The checks above only prove the *posted comment* is bound to
        # expected_body_sha256. They do not prove the *live* Issue body still
        # matches that hash at readback time -- a concurrent body edit between
        # the pre-publish check and this readback must not be reported as
        # success. Re-fetch the live body and require it to match the input
        # hash, the outer (comment-bound) hash, and the nested product-spec
        # hash carried inside the just-verified snapshot -- all three must
        # agree, not just the outer one.
        live_body, _live_updated_at, live_body_err = _fetch_issue_body_and_updated_at(
            issue_number, repo, gh_bin
        )
        if live_body_err:
            return {
                "error": f"failed_after_mutation:live_body_refetch_error:{live_body_err}",
                "comment_id": comment.get("id", ""),
                "comment_url": comment.get("html_url", ""),
            }
        live_body_sha256 = "sha256:" + hashlib.sha256(
            (live_body or "").encode("utf-8")
        ).hexdigest()
        inner = snapshot.get("inner") if isinstance(snapshot, dict) else None
        checks = inner.get("checks") if isinstance(inner, dict) else None
        product_spec_check = (
            checks.get("product_spec_check") if isinstance(checks, dict) else None
        )
        nested_product_spec_sha256 = (
            product_spec_check.get("body_sha256")
            if isinstance(product_spec_check, dict)
            else None
        )
        hashes_to_check = {
            "expected_body_sha256": expected_body_sha256,
            "live_body_sha256": live_body_sha256,
            "nested_product_spec_body_sha256": nested_product_spec_sha256,
        }
        if len(set(hashes_to_check.values())) != 1:
            return {
                "error": (
                    "failed_after_mutation:live_body_hash_mismatch:"
                    f"{json.dumps(hashes_to_check, sort_keys=True)}"
                ),
                "comment_id": comment.get("id", ""),
                "comment_url": comment.get("html_url", ""),
            }

        return {
            "comment_id": comment.get("id", ""),
            "comment_url": comment.get("html_url", ""),
            "remote_postcondition_verified": True,
        }
    except Exception as exc:
        return {"error": f"remote_contract_snapshot_readback_exception:{exc}"}


def _run_contract_snapshot_publish(args, input_data, gh_bin, _fail, _ok) -> int:
    # -- Blocker 4: input schema binding (repo / target_issue_body_sha256 /
    # expected_latest_contract_review_status / expected_contract_marker /
    # operation_reason) — an under-specified input can no longer launch
    # ensure_contract_snapshot.py --mode auto --post.
    field_err = _validate_contract_snapshot_publish_fields(input_data, args.repo)
    if field_err:
        return _fail(field_err)

    # -- Blocker 5: publisher module chain realpath / shadowing check, same
    # rigor as the legacy termination_report.publish command.
    realpath_errors = _check_contract_snapshot_module_realpaths(PROJECT_ROOT)
    if realpath_errors:
        return _fail("module_shadowing_detected", realpath_errors)

    if args.dry_run:
        result = {"schema": RESULT_SCHEMA, "status": "dry_run_ok", "command_id": args.command_id,
                   "issue_number": args.issue_number}
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # -- target_issue_body_sha256 precondition: refuse to publish a contract
    # snapshot against an Issue body that has moved since the caller computed
    # its expected state.
    body, _updated_at, body_err = _fetch_issue_body_and_updated_at(args.issue_number, args.repo, gh_bin)
    if body_err:
        return _fail(body_err, status="failed")
    current_body_sha256 = "sha256:" + hashlib.sha256((body or "").encode("utf-8")).hexdigest()
    if current_body_sha256 != input_data["target_issue_body_sha256"]:
        return _fail(
            f"target_issue_body_sha256_mismatch: current={current_body_sha256} "
            f"expected={input_data['target_issue_body_sha256']}",
            status="failed",
        )

    publisher = PROJECT_ROOT / _ENSURE_CONTRACT_SNAPSHOT_REL
    if not publisher.exists():
        return _fail(f"publisher_missing: {publisher}", status="failed")

    artifact_dir = _issue_metadata_marker_path(
        PROJECT_ROOT, args.issue_number, args.command_id, ""
    ).parent
    write_root = f"artifacts/{args.issue_number}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{args.command_id}/"
    cmd = [
        sys.executable,
        str(publisher),
        "--issue-number", str(args.issue_number),
        "--repo", args.repo,
        "--mode", "auto",
        "--post",
        "--artifact-dir", str(artifact_dir),
    ]
    # -- Blocker 5: sanitized env (PYTHONPATH / PYTHONHOME / editor / browser /
    # prompt overrides removed), same boundary as _build_sanitized_env().
    sanitized_env = _build_metadata_sanitized_env()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180, cwd=str(PROJECT_ROOT),
            shell=False, env=sanitized_env,
        )
    except subprocess.TimeoutExpired:
        return _fail("publisher_timeout_180s", status="failed")
    except Exception as exc:
        return _fail(f"publisher_launch_error: {exc}", status="failed")

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return _fail("publisher_no_stdout", [proc.stderr[:500]], status="failed")
    try:
        pub_result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return _fail(f"publisher_json_parse_error: {exc}", status="failed")

    pub_status = pub_result.get("status")
    if pub_status != "ok" or not pub_result.get("contract_snapshot_url"):
        return _fail(
            f"publisher_did_not_succeed: status={pub_status!r}",
            pub_result.get("errors") or [f"post_status={pub_result.get('post_status')}"],
            status="failed",
        )

    readback = _readback_contract_snapshot(
        input_data["expected_contract_marker"],
        args.issue_number,
        args.repo,
        gh_bin,
        pub_result["contract_snapshot_url"],
        input_data["target_issue_body_sha256"],
    )
    if "error" in readback:
        # The publisher's POST already happened (a remote side effect exists).
        # Preserve the posted URL/comment id as evidence even on failure so the
        # caller can locate and reconcile the mutation instead of only seeing
        # an opaque error string.
        evidence = [readback["error"]]
        if readback.get("comment_url"):
            evidence.append(f"posted_comment_url: {readback['comment_url']}")
        if readback.get("comment_id"):
            evidence.append(f"posted_comment_id: {readback['comment_id']}")
        return _fail(readback["error"], evidence, status="failed")

    # -- AC14 / Blocker 6: postcondition -- no changes outside this command's
    # own write root (artifacts/{issue}/issue-metadata/contract_snapshot.publish/).
    changed = _check_no_tracked_changes(PROJECT_ROOT, args.issue_number, write_root)
    if changed:
        return _fail(
            "postcondition_tracked_changes_detected",
            [f"changed: {f}" for f in changed[:20]],
            status="failed",
        )

    return _ok({
        "contract_snapshot_url": readback["comment_url"],
        "post_status": pub_result.get("post_status"),
        "remote_postcondition_verified": True,
        "idempotency_marker_written": False,
    })


# -- Issue #1632: controlled removal of a stale closed-blocker GitHub native
# `blockedBy` relationship (issue_dependency.remove) -----------------------
#
# Fixed GraphQL host/query/mutation. No caller-supplied query, host, argv,
# credential, or response path. Every read is an exhaustive all-page
# readback (pageInfo.hasNextPage must reach false); mutation happens at
# most once per invocation (no automatic retry on transport/GraphQL error);
# and a fresh all-page readback is required both BEFORE (precondition) and
# AFTER (postcondition) the single removeBlockedBy call.

_ISSUE_DEPENDENCY_REMOVE_BLOCKED_BY_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id
      number
      state
      blockedBy(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id number state repository { nameWithOwner } }
      }
    }
  }
}
"""

_ISSUE_DEPENDENCY_REMOVE_MUTATION = """
mutation($issueId: ID!, $blockedByIssueId: ID!) {
  removeBlockedBy(input: {issueId: $issueId, blockedByIssueId: $blockedByIssueId}) {
    issue { id number }
  }
}
"""

_ISSUE_DEPENDENCY_REMOVE_TRUSTED_PERMISSIONS = frozenset({"admin", "write", "maintain"})

# Hard bound on pagination loop iterations, independent of the caller-declared
# expected_blocked_by_numbers size cap -- prevents a runaway loop even if a
# malformed/adversarial response never sets hasNextPage to false.
_ISSUE_DEPENDENCY_REMOVE_MAX_PAGES = 50


def _build_issue_dependency_remove_gh_env() -> dict[str, str]:
    """Sanitized environment for every `gh` subprocess call made while
    removing an issue dependency relationship. Strips the generic
    ENV_SANITIZE_KEYS plus GH_HOST/GH_REPO/GH_CONFIG_DIR/GH_DEBUG/DEBUG,
    the same boundary already used for pr_review.publish."""
    env = os.environ.copy()
    for key in ENV_SANITIZE_KEYS:
        env.pop(key, None)
    for key in ("GH_HOST", "GH_REPO", "GH_CONFIG_DIR", "GH_DEBUG", "DEBUG"):
        env.pop(key, None)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    return env


def _graphql_call(
    gh_bin: str, env: dict[str, str], query: str, variables: dict
) -> tuple[dict | None, str]:
    """Execute a single fixed-host GraphQL call via `gh api graphql --input -`.

    Never uses shell-interpolated -f/-F flags for the query text (mirrors
    _post_pr_review's --input - pattern) so query/variables round-trip as an
    exact JSON POST body. Returns (data, error); data is the `data` object of
    the parsed GraphQL response, or None on any transport/schema/GraphQL
    `errors` failure.
    """
    payload = json.dumps({"query": query, "variables": variables})
    try:
        out = subprocess.run(
            [gh_bin, "api", "--hostname", _TRUSTED_GITHUB_HOST, "graphql", "--input", "-"],
            input=payload, capture_output=True, text=True, timeout=20, shell=False, env=env,
        )
        if out.returncode != 0:
            return None, _classify_gh_error("gh_api_graphql_failed", out.stderr or "")
        try:
            parsed = json.loads(out.stdout)
        except Exception as exc:
            return None, f"gh_api_graphql_response_parse_error: {exc}"
        if not isinstance(parsed, dict):
            return None, "gh_api_graphql_response_not_object"
        if parsed.get("errors"):
            return None, f"gh_api_graphql_errors: {json.dumps(parsed['errors'])[:300]}"
        data = parsed.get("data")
        if not isinstance(data, dict):
            return None, "gh_api_graphql_response_missing_data"
        return data, ""
    except Exception as exc:
        return None, f"gh_api_graphql_exception: {exc}"


def _fetch_issue_dependency_remove_actor(
    gh_bin: str, env: dict[str, str], repo: str
) -> tuple[str | None, str | None, str]:
    """Fetch (login, permission, error) for the authenticated gh identity
    against `repo`. Never records the token/credential itself -- only the
    login and the coarse permission string are ever returned/recorded."""
    login, err = _fetch_authenticated_login(gh_bin, env=env)
    if err:
        return None, None, err
    try:
        out = subprocess.run(
            [gh_bin, "api", "--hostname", _TRUSTED_GITHUB_HOST,
             f"repos/{repo}/collaborators/{login}/permission", "--jq", ".permission"],
            capture_output=True, text=True, timeout=15, shell=False, env=env,
        )
        if out.returncode != 0:
            return login, None, _classify_gh_error(
                "gh_api_permission_fetch_failed", out.stderr or ""
            )
        permission = out.stdout.strip()
        if not permission:
            return login, None, "gh_api_permission_empty"
        return login, permission, ""
    except Exception as exc:
        return login, None, f"gh_api_permission_exception: {exc}"


def _fetch_blocked_by_all_pages(
    issue_number: int, repo: str, gh_bin: str, env: dict[str, str]
) -> tuple[dict | None, str]:
    """Exhaustive cursor-paginated readback of Issue.blockedBy.

    Returns (result, error). result = {blocked_issue_id, blocked_issue_number,
    blocked_issue_state, nodes: [{id, number, state}], page_count}. Fail-closed
    on: GraphQL errors, missing/malformed response shape, cross-page identity
    drift, non-repo nodes, duplicate node ids/numbers across pages, a cursor
    that does not progress while hasNextPage is true, and the caller-declared
    size cap being exceeded.
    """
    owner, sep, name = repo.partition("/")
    if not sep or not owner or not name:
        return None, "repo_slug_malformed"

    nodes: list[dict] = []
    seen_numbers: set[int] = set()
    seen_ids: set[str] = set()
    cursor = None
    page_count = 0
    blocked_issue_id = None
    blocked_issue_number = None
    blocked_issue_state = None

    while True:
        data, err = _graphql_call(
            gh_bin, env, _ISSUE_DEPENDENCY_REMOVE_BLOCKED_BY_QUERY,
            {"owner": owner, "name": name, "number": issue_number, "cursor": cursor},
        )
        if err:
            return None, err

        repository = data.get("repository")
        if not isinstance(repository, dict):
            return None, "graphql_response_missing_repository"
        issue = repository.get("issue")
        if not isinstance(issue, dict):
            return None, "graphql_response_missing_issue"

        if blocked_issue_id is None:
            blocked_issue_id = issue.get("id")
            blocked_issue_number = issue.get("number")
            blocked_issue_state = issue.get("state")
            if not isinstance(blocked_issue_id, str) or not blocked_issue_id:
                return None, "graphql_response_blocked_issue_id_invalid"
            if blocked_issue_number != issue_number:
                return None, "graphql_response_blocked_issue_number_mismatch"
        elif issue.get("id") != blocked_issue_id or issue.get("number") != blocked_issue_number:
            return None, "graphql_response_blocked_issue_identity_drift_mid_pagination"

        blocked_by = issue.get("blockedBy")
        if not isinstance(blocked_by, dict):
            return None, "graphql_response_missing_blocked_by"
        page_info = blocked_by.get("pageInfo")
        if not isinstance(page_info, dict):
            return None, "graphql_response_missing_page_info"
        page_nodes = blocked_by.get("nodes")
        if not isinstance(page_nodes, list):
            return None, "graphql_response_missing_nodes"

        page_count += 1
        if page_count > _ISSUE_DEPENDENCY_REMOVE_MAX_PAGES:
            return None, "graphql_pagination_runaway"

        for node in page_nodes:
            if not isinstance(node, dict):
                return None, "graphql_response_node_not_object"
            node_id = node.get("id")
            node_number = node.get("number")
            node_state = node.get("state")
            node_repo = (node.get("repository") or {}).get("nameWithOwner")
            if not isinstance(node_id, str) or not node_id:
                return None, "graphql_response_node_id_invalid"
            if type(node_number) is not int or node_number <= 0:
                return None, "graphql_response_node_number_invalid"
            if node_state not in ("OPEN", "CLOSED"):
                return None, f"graphql_response_node_state_invalid: {node_state!r}"
            if node_repo != repo:
                return None, f"graphql_response_node_repo_mismatch: {node_repo!r}"
            if node_number in seen_numbers or node_id in seen_ids:
                return None, "graphql_response_duplicate_node_across_pages"
            seen_numbers.add(node_number)
            seen_ids.add(node_id)
            nodes.append({"id": node_id, "number": node_number, "state": node_state})
            if len(nodes) > ISSUE_DEPENDENCY_REMOVE_MAX_BLOCKED_BY_NUMBERS:
                return None, "graphql_response_blocked_by_size_cap_exceeded"

        has_next = page_info.get("hasNextPage")
        end_cursor = page_info.get("endCursor")
        if not isinstance(has_next, bool):
            return None, "graphql_response_has_next_page_not_bool"
        if has_next:
            if not isinstance(end_cursor, str) or not end_cursor or end_cursor == cursor:
                return None, "graphql_response_cursor_invalid_or_not_progressing"
            cursor = end_cursor
            continue
        break

    return {
        "blocked_issue_id": blocked_issue_id,
        "blocked_issue_number": blocked_issue_number,
        "blocked_issue_state": blocked_issue_state,
        "nodes": nodes,
        "page_count": page_count,
    }, ""


def _compute_blocked_by_snapshot_sha256(
    blocked_issue_id: str, blocked_issue_number: int, nodes: list[dict]
) -> str:
    """Deterministic hash binding blocked-issue identity + the full sorted
    (number, id, state) set of its blockedBy relationships."""
    canonical_nodes = sorted(
        (
            {"id": n["id"], "number": n["number"], "state": n["state"]}
            for n in nodes
        ),
        key=lambda n: n["number"],
    )
    payload = {
        "blocked_issue_id": blocked_issue_id,
        "blocked_issue_number": blocked_issue_number,
        "blocked_by": canonical_nodes,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _run_issue_dependency_remove(args, input_data, gh_bin, _fail, _ok) -> int:
    field_err = validate_issue_dependency_remove_input(input_data, args.issue_number, args.repo)
    if field_err:
        return _fail(field_err)

    target_blocker_number = input_data["target_blocker_number"]
    expected_blocked_issue_node_id = input_data["expected_blocked_issue_node_id"]
    expected_blocker_node_id = input_data["expected_blocker_node_id"]
    expected_numbers = input_data["expected_blocked_by_numbers"]
    expected_pre_hash = input_data["expected_pre_mutation_snapshot_sha256"]
    idempotency_key = input_data["idempotency_key"]

    marker_path = _issue_metadata_marker_path(
        PROJECT_ROOT, args.issue_number, args.command_id, "issue_dependency_remove.marker.json"
    )
    write_root = f"artifacts/{args.issue_number}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{args.command_id}/"

    if args.dry_run:
        result = {"schema": RESULT_SCHEMA, "status": "dry_run_ok", "command_id": args.command_id,
                   "issue_number": args.issue_number}
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    gh_env = _build_issue_dependency_remove_gh_env()

    # -- AC3: trusted credential actor readback. Runs before any relationship
    # read/mutation. Only login + coarse permission are ever recorded -- never
    # a token/credential.
    login, permission, actor_err = _fetch_issue_dependency_remove_actor(gh_bin, gh_env, args.repo)
    if actor_err:
        return _fail(actor_err, status="transport_or_schema_error")
    if permission not in _ISSUE_DEPENDENCY_REMOVE_TRUSTED_PERMISSIONS:
        return _fail(
            f"credential_actor_not_authorized: login={login!r} permission={permission!r}",
            status="precondition_rejected",
        )

    def _write_marker(status_detail: str, pre_hash: str, post_hash: str | None = None) -> None:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps({
            "schema": "ISSUE_DEPENDENCY_REMOVE_MARKER_V1",
            "issue_number": args.issue_number,
            "repo": args.repo,
            "target_blocker_number": target_blocker_number,
            "idempotency_key": idempotency_key,
            "actor_login": login,
            "pre_mutation_snapshot_sha256": pre_hash,
            "post_mutation_snapshot_sha256": post_hash,
            "status_detail": status_detail,
            "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }, ensure_ascii=False, indent=2))

    existing_marker = None
    if marker_path.exists():
        try:
            existing_marker = json.loads(marker_path.read_text())
        except Exception:
            existing_marker = None

    # -- AC2 / precondition: all-page pre-mutation readback. Remote state is
    # the sole authority; a local marker never substitutes for it.
    pre_state, pre_err = _fetch_blocked_by_all_pages(args.issue_number, args.repo, gh_bin, gh_env)
    if pre_err:
        return _fail(pre_err, status="transport_or_schema_error")

    if pre_state["blocked_issue_id"] != expected_blocked_issue_node_id:
        return _fail("precondition_blocked_issue_node_id_mismatch", status="precondition_rejected")

    pre_numbers = sorted(n["number"] for n in pre_state["nodes"])

    # -- Idempotency: a marker for this exact idempotency_key plus a FRESH
    # remote readback showing the target relationship already absent is the
    # only path to already_completed. This never substitutes a cached/local
    # decision for the remote check above.
    if (
        existing_marker is not None
        and existing_marker.get("idempotency_key") == idempotency_key
        and target_blocker_number not in pre_numbers
    ):
        computed_pre_hash = _compute_blocked_by_snapshot_sha256(
            pre_state["blocked_issue_id"], pre_state["blocked_issue_number"], pre_state["nodes"]
        )
        return _ok({
            "status": "already_completed",
            "actor_login": login,
            "pre_mutation_snapshot_sha256": computed_pre_hash,
            "idempotency_marker_found": True,
        })

    if pre_numbers != expected_numbers:
        return _fail(
            f"precondition_blocked_by_set_mismatch: current={pre_numbers} expected={expected_numbers}",
            status="precondition_rejected",
        )

    target_nodes = [n for n in pre_state["nodes"] if n["number"] == target_blocker_number]
    if len(target_nodes) != 1:
        return _fail(
            "precondition_target_blocker_not_found_exactly_once", status="precondition_rejected"
        )
    target_node = target_nodes[0]
    if target_node["id"] != expected_blocker_node_id:
        return _fail("precondition_target_blocker_node_id_mismatch", status="precondition_rejected")
    if target_node["state"] != "CLOSED":
        return _fail("precondition_target_blocker_not_closed", status="precondition_rejected")

    computed_pre_hash = _compute_blocked_by_snapshot_sha256(
        pre_state["blocked_issue_id"], pre_state["blocked_issue_number"], pre_state["nodes"]
    )
    if computed_pre_hash != expected_pre_hash:
        return _fail(
            f"precondition_pre_mutation_snapshot_sha256_mismatch: computed={computed_pre_hash} "
            f"expected={expected_pre_hash}",
            status="precondition_rejected",
        )

    # -- AC4: single mutation attempt. No automatic retry on transport/GraphQL
    # error -- a failed call is recorded and reported, never retried here.
    _mutation_data, mutation_err = _graphql_call(
        gh_bin, gh_env, _ISSUE_DEPENDENCY_REMOVE_MUTATION,
        {"issueId": expected_blocked_issue_node_id, "blockedByIssueId": expected_blocker_node_id},
    )
    if mutation_err:
        _write_marker("transport_or_schema_error", computed_pre_hash)
        return _fail(mutation_err, status="transport_or_schema_error")

    # -- AC5: all-page post-mutation readback (TOCTOU close-out).
    post_state, post_err = _fetch_blocked_by_all_pages(args.issue_number, args.repo, gh_bin, gh_env)
    if post_err:
        return _fail(post_err, status="transport_or_schema_error")

    if post_state["blocked_issue_id"] != expected_blocked_issue_node_id:
        return _fail("postcondition_blocked_issue_node_id_mismatch", status="postcondition_rejected")

    post_numbers = sorted(n["number"] for n in post_state["nodes"])
    expected_post_numbers = sorted(n for n in expected_numbers if n != target_blocker_number)
    if target_blocker_number in post_numbers:
        return _fail(
            "postcondition_target_relationship_still_present", status="postcondition_rejected"
        )
    if post_numbers != expected_post_numbers:
        return _fail(
            f"postcondition_non_target_set_changed: current={post_numbers} "
            f"expected={expected_post_numbers}",
            status="postcondition_rejected",
        )

    pre_by_number = {n["number"]: n["id"] for n in pre_state["nodes"]}
    for n in post_state["nodes"]:
        if pre_by_number.get(n["number"]) != n["id"]:
            return _fail(
                "postcondition_non_target_node_id_drift", status="postcondition_rejected"
            )

    computed_post_hash = _compute_blocked_by_snapshot_sha256(
        post_state["blocked_issue_id"], post_state["blocked_issue_number"], post_state["nodes"]
    )

    # -- AC14-equivalent postcondition: no changes outside this command's own
    # write root.
    changed = _check_no_tracked_changes(PROJECT_ROOT, args.issue_number, write_root)
    if changed:
        return _fail(
            "postcondition_tracked_changes_detected",
            [f"changed: {f}" for f in changed[:20]],
            status="failed",
        )

    _write_marker("removed", computed_pre_hash, computed_post_hash)

    return _ok({
        "status": "removed",
        "actor_login": login,
        "pre_mutation_snapshot_sha256": computed_pre_hash,
        "post_mutation_snapshot_sha256": computed_post_hash,
        "idempotency_marker_written": True,
    })


if __name__ == "__main__":
    sys.exit(main())
