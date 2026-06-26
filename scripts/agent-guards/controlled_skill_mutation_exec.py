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
    TRUSTED_REPO,
    ENV_SANITIZE_KEYS,
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


def _validate_and_resolve_input_file(
    input_file_str: str, issue_number: int, project_root: Path
) -> tuple[Path | None, str]:
    """Validate and resolve the input file path.

    Returns (canonical_path, error_message). canonical_path is None on error.
    Enforces:
    - Lexical: reject absolute paths
    - Lexical: reject '..' components
    - Filesystem: reject symlink components (via lstat)
    - Must be a regular file
    - Must not be a hardlink (st_nlink == 1)
    - Must be under artifacts/{issue_number}/
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

    # Containment check: must be under artifacts/{issue_number}/
    artifact_subtree = (project_root / "artifacts" / str(issue_number)).resolve()
    try:
        canonical.relative_to(artifact_subtree)
    except ValueError:
        return None, (
            f"input_file_outside_issue_subtree: {canonical} "
            f"not under {artifact_subtree}"
        )

    return canonical, ""


# -- Input JSON validation -----------------------------------------------------


def _validate_input_json(
    canonical_input: Path, issue_number: int
) -> str:
    """Read and validate input JSON. Returns error string or empty string."""
    try:
        input_data = json.loads(canonical_input.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"input_json_read_error: {exc}"

    if not isinstance(input_data, dict):
        return "input_json_not_object"

    if input_data.get("schema") != "TERMINATION_REPORT_INPUT_V1":
        schema_val = input_data.get("schema")
        return f"input_schema_mismatch: expected TERMINATION_REPORT_INPUT_V1, got {schema_val!r}"

    input_issue = input_data.get("issue_number")
    if input_issue is None:
        return "input_issue_number_missing"
    if type(input_issue) is not int:
        return f"input_issue_number_not_int: {type(input_issue).__name__}"
    if input_issue != issue_number:
        return f"input_issue_number_mismatch: {input_issue} != {issue_number}"

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
        description="Controlled skill mutation executor for termination_report.publish"
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

    # -- AC8: validate command_id ---------------------------------------------
    if args.command_id != COMMAND_ID_PUBLISH:
        return _fail(f"unknown_command_id: {args.command_id!r}")

    # -- AC10: validate repo --------------------------------------------------
    if args.repo != TRUSTED_REPO:
        return _fail(f"repo_mismatch: {args.repo!r} != {TRUSTED_REPO!r}")

    # -- P1-2: git remote origin binding --------------------------------------
    origin_err = _verify_git_remote_origin(PROJECT_ROOT, TRUSTED_REPO)
    if origin_err:
        return _fail(origin_err)

    # -- P0-6: find gh binary -------------------------------------------------
    gh_bin, gh_err = _find_gh_bin()
    if gh_bin is None:
        return _fail(gh_err)

    # -- AC11: issue binding (LOOP_ISSUE_NUMBER mandatory) --------------------
    env_issue = os.environ.get("LOOP_ISSUE_NUMBER", "").strip()
    if not env_issue:
        return _fail("loop_issue_number_env_missing: LOOP_ISSUE_NUMBER must be set")
    if not env_issue.isdigit():
        return _fail(f"loop_issue_number_env_not_digit: {env_issue!r}")
    if int(env_issue) != args.issue_number:
        return _fail(
            f"issue_number_mismatch: --issue-number {args.issue_number} "
            f"!= LOOP_ISSUE_NUMBER {env_issue}"
        )

    # -- AC12: input-file validation ------------------------------------------
    canonical_input, input_err = _validate_and_resolve_input_file(
        args.input_file, args.issue_number, PROJECT_ROOT
    )
    if input_err:
        return _fail(input_err)

    # -- P0-2: input JSON validation ------------------------------------------
    json_err = _validate_input_json(canonical_input, args.issue_number)
    if json_err:
        return _fail(json_err)

    # -- AC16: module realpath inspection -------------------------------------
    realpath_errors = _check_module_realpaths(PROJECT_ROOT)
    if realpath_errors:
        return _fail("module_shadowing_detected", realpath_errors)

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
