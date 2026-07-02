#!/usr/bin/env python3
"""
controlled_skill_mutation_exec.py

Single executor for CONTROLLED_SKILL_MUTATION_COMMAND_POLICY entries.
Invoked by agents via the exact argv form defined in controlled_skill_mutation_policy.py.

Design: Direct script allow for publish_termination_report.py is denied. Only this
executor is allow-listed in settings.json. The executor enforces:
  - command_id whitelist (termination_report.publish only)
  - repo binding (--repo must be TRUSTED_REPO)
  - git remote origin binding (must match TRUSTED_REPO)
  - issue binding (--issue-number must match LOOP_ISSUE_NUMBER env -- mandatory)
  - input-file binding (must be in active issue artifact subtree, no symlinks, no hardlinks)
  - input-file JSON validation (schema + issue_number field cross-check)
  - gh binary discovery (trusted path only)
  - environment sanitization (PUBLISH_ARTIFACT_DIR / PYTHONPATH / PYTHONHOME /
    GH_EDITOR / EDITOR / VISUAL / BROWSER overridden/removed)
  - module realpath inspection (publisher / renderer / prose_boundary canonical path check,
    missing=deny, import origin check)
  - idempotency (marker file pre-check; no double-post)
  - exec marker injection (deterministic marker for comment read-back)
  - postcondition (git status --porcelain=v1 must show no changes outside artifacts/)
  - comment read-back by marker (comment id / url / body hash recorded)

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
import re as _re
import shutil
import stat as _stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

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
    ALL_COMMAND_IDS,
    INPUT_SCHEMA_BY_COMMAND,
    ENV_BINDING_MANDATORY_COMMAND_IDS,
    ISSUE_METADATA_NAMESPACE_SEGMENT,
    TRUSTED_REPO,
    ENV_SANITIZE_KEYS,
)

_ENSURE_CONTRACT_SNAPSHOT_REL = (
    ".claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py"
)

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


def _verify_git_remote_origin(project_root: Path, trusted_repo: str) -> str:
    """Return empty string if origin matches trusted_repo, else error."""
    try:
        out = subprocess.run(
            ["git", "-C", str(project_root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return f"git_remote_origin_failed: {out.stderr.strip()[:100]}"
        url = out.stdout.strip()
        m = _re.search(r'[:/]([^/]+/[^/]+?)(?:\.git)?$', url)
        if not m:
            return f"git_remote_origin_not_parseable: {url!r}"
        normalized = m.group(1)
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


def _validate_contract_snapshot_publish_fields(data: dict, repo: str) -> str:
    declared_repo = data.get("repo", repo)
    if declared_repo != repo:
        return f"contract_snapshot_publish_repo_mismatch: {declared_repo!r} != {repo!r}"
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


# -- Issue #1284: issue body / comment mutation helpers ------------------------


def _fetch_issue_body_and_updated_at(
    issue_number: int, repo: str, gh_bin: str
) -> tuple[str | None, str | None, str]:
    """Fetch (body, updatedAt, error) via gh api (argv-list, shell=False)."""
    try:
        out = subprocess.run(
            [gh_bin, "issue", "view", str(issue_number), "--repo", repo,
             "--json", "body,updatedAt"],
            capture_output=True, text=True, timeout=15, shell=False,
        )
        if out.returncode != 0:
            return None, None, f"gh_issue_view_failed_rc_{out.returncode}"
        data = json.loads(out.stdout)
        return data.get("body", ""), data.get("updatedAt", ""), ""
    except Exception as exc:
        return None, None, f"gh_issue_view_exception: {exc}"


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
            return f"gh_api_patch_failed: {out.stderr.strip()[:200]}"
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
            return "", "", f"gh_api_post_comment_failed: {out.stderr.strip()[:200]}"
        try:
            resp = json.loads(out.stdout)
        except Exception as exc:
            return "", "", f"gh_api_post_comment_response_parse_error: {exc}"
        return str(resp.get("html_url", "")), str(resp.get("id", "")), ""
    except Exception as exc:
        return "", "", f"gh_api_post_comment_exception: {exc}"


# -- Postcondition check -------------------------------------------------------


def _check_no_tracked_changes(project_root: Path, issue_number: int) -> list[str]:
    """Return list of violations (staged, unstaged, untracked source files). Empty = OK (AC14).

    Uses git status --porcelain=v1 --untracked-files=all.
    Allows writes inside artifacts/{issue_number}/.
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
        "--input-file", required=True, help="Relative path to input JSON file (artifact subtree)"
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

    # -- input-file binding -------------------------------------------------------
    canonical_input, input_err = _validate_and_resolve_input_file(
        args.input_file, args.issue_number, PROJECT_ROOT, command_id=args.command_id
    )
    if input_err:
        return _fail(input_err)

    # -- AC10: per-command-id input schema validation ----------------------------
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
        return _run_contract_snapshot_publish(args, input_data, _fail, _ok)

    return _fail(f"unhandled_command_id: {args.command_id!r}")  # pragma: no cover — defensive


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
    if marker_path.exists():
        try:
            existing = json.loads(marker_path.read_text())
        except Exception:
            existing = {}
        if existing.get("new_body_sha256") == input_data["new_body_sha256"]:
            return _ok({
                "status_detail": "already_applied",
                "new_body_sha256": existing.get("new_body_sha256"),
                "idempotency_marker_found": True,
            })

    if args.dry_run:
        result = {"schema": RESULT_SCHEMA, "status": "dry_run_ok", "command_id": args.command_id,
                   "issue_number": args.issue_number}
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # -- AC9: stale-write precondition — readback must match previous_* --------
    body, updated_at, err = _fetch_issue_body_and_updated_at(args.issue_number, args.repo, gh_bin)
    if err:
        return _fail(err, status="failed")
    current_body_sha256 = "sha256:" + hashlib.sha256((body or "").encode("utf-8")).hexdigest()
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

    # -- AC14: postcondition -- no tracked/staged/untracked source file changes
    changed = _check_no_tracked_changes(PROJECT_ROOT, args.issue_number)
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
        "new_body_sha256": actual_new_sha256,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }, ensure_ascii=False, indent=2))

    return _ok({
        "new_body_sha256": actual_new_sha256,
        "idempotency_marker_written": True,
    })


def _run_issue_comment_publish(args, canonical_input, input_data, gh_bin, _fail, _ok) -> int:
    field_err = _validate_issue_comment_publish_fields(input_data)
    if field_err:
        return _fail(field_err)

    marker = input_data["marker"]
    marker_path = _issue_metadata_marker_path(
        PROJECT_ROOT, args.issue_number, args.command_id, "issue_comment_publish.marker.json"
    )
    if marker_path.exists():
        try:
            existing = json.loads(marker_path.read_text())
        except Exception:
            existing = {}
        if existing.get("marker") == marker:
            return _ok({
                "status_detail": "already_published",
                "comment_id": existing.get("comment_id"),
                "comment_url": existing.get("comment_url"),
                "idempotency_marker_found": True,
            })

    if args.dry_run:
        result = {"schema": RESULT_SCHEMA, "status": "dry_run_ok", "command_id": args.command_id,
                   "issue_number": args.issue_number}
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    comment_url, comment_id, post_err = _post_gh_comment(
        args.issue_number, args.repo, input_data["comment_body"], gh_bin
    )
    if post_err:
        return _fail(post_err, status="failed")

    # -- AC4/AC14: readback by marker — false success not allowed ---------------
    readback = _readback_by_marker_literal(marker, args.issue_number, args.repo, gh_bin)
    if "error" in readback:
        return _fail(f"readback_failed: {readback['error']}", status="failed")

    changed = _check_no_tracked_changes(PROJECT_ROOT, args.issue_number)
    if changed:
        return _fail(
            "postcondition_tracked_changes_detected",
            [f"changed: {f}" for f in changed[:20]],
            status="failed",
        )

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps({
        "schema": "ISSUE_COMMENT_PUBLISH_MARKER_V1",
        "issue_number": args.issue_number,
        "marker": marker,
        "comment_id": readback.get("comment_id"),
        "comment_url": readback.get("comment_url"),
        "published_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }, ensure_ascii=False, indent=2))

    return _ok({
        "comment_id": readback.get("comment_id"),
        "comment_url": readback.get("comment_url"),
        "body_sha256": readback.get("body_sha256"),
        "idempotency_marker_written": True,
    })


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


def _run_contract_snapshot_publish(args, input_data, _fail, _ok) -> int:
    field_err = _validate_contract_snapshot_publish_fields(input_data, args.repo)
    if field_err:
        return _fail(field_err)

    if args.dry_run:
        result = {"schema": RESULT_SCHEMA, "status": "dry_run_ok", "command_id": args.command_id,
                   "issue_number": args.issue_number}
        if args.output_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    publisher = PROJECT_ROOT / _ENSURE_CONTRACT_SNAPSHOT_REL
    if not publisher.exists():
        return _fail(f"publisher_missing: {publisher}", status="failed")

    artifact_dir = _issue_metadata_marker_path(
        PROJECT_ROOT, args.issue_number, args.command_id, ""
    ).parent
    cmd = [
        sys.executable,
        str(publisher),
        "--issue-number", str(args.issue_number),
        "--repo", args.repo,
        "--mode", "auto",
        "--post",
        "--artifact-dir", str(artifact_dir),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180, cwd=str(PROJECT_ROOT), shell=False,
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

    changed = _check_no_tracked_changes(PROJECT_ROOT, args.issue_number)
    if changed:
        return _fail(
            "postcondition_tracked_changes_detected",
            [f"changed: {f}" for f in changed[:20]],
            status="failed",
        )

    return _ok({
        "contract_snapshot_url": pub_result["contract_snapshot_url"],
        "post_status": pub_result.get("post_status"),
        "idempotency_marker_written": True,
    })


if __name__ == "__main__":
    sys.exit(main())
