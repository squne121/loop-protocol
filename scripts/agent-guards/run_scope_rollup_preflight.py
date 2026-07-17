#!/usr/bin/env python3
"""run_scope_rollup_preflight.py

Exact single-transaction executor for the ``scope_rollup.run`` command
(Issue #1547).

Fetches read-only GitHub inventory (``gh issue view`` / ``gh issue list`` /
``gh pr list``) with ``shell=False`` (no shell redirect anywhere), enforces a
bounded pagination-completeness check that fails closed when it cannot prove
every item was retrieved, computes SHA256/count manifest fields, invokes the
existing ``plan_issue_scope_rollup.py`` planner and ``verify_scope_rollup_result.py``
verifier as a single Python transaction (no shell wrapper, no `2>&1`), and
prints a ``SCOPE_ROLLUP_RUN_RESULT_V1`` JSON document to stdout.

All intermediate artifacts (the ``gh`` JSON captures handed to the planner)
are staged inside an executor-owned private invocation directory
(``tempfile.mkdtemp()``, mode ``0700``) using ``O_CREAT | O_EXCL |
O_NOFOLLOW`` + mode ``0600`` exclusive file creation, same-directory atomic
rename, flush + ``fsync``. The entire directory is removed in a ``finally``
block on every exit path (success / failure / timeout) so no residue is ever
left on disk (AC5 / AC6) -- callers only ever see the JSON emitted on stdout,
never a retained artifact path.

Pagination note (AC4 / P1-2): ``gh issue list`` / ``gh pr list`` do not
expose GraphQL ``hasNextPage`` in their JSON output; ``--limit`` is a
maximum, not a completeness proof. This executor requests
``MAX_ITEMS_PER_KIND + 1`` items and treats a returned count greater than
``MAX_ITEMS_PER_KIND`` as "completeness cannot be proven" -- the whole
transaction fails closed (``truncated: true``) rather than silently handing
a partial inventory to the planner as though it were the full set.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from skill_runtime_command_policy import (  # noqa: E402
    TRUSTED_REPO_SLUG,
    current_branch,
    resolve_default_branch,
    resolve_repo_slug,
)

SCHEMA = "SCOPE_ROLLUP_RUN_RESULT_V1"
QUERY_SCHEMA_VERSION = 1
TRUSTED_HOST = "github.com"

# Safety caps. MAX_ITEMS_PER_KIND is deliberately conservative; exceeding it
# means pagination completeness cannot be proven with the bounded
# `--limit N+1` technique below, so the whole run fails closed.
MAX_ITEMS_PER_KIND = 500
MAX_BYTES_PER_FETCH = 8_000_000
GH_TIMEOUT_SECONDS = 60.0
PLANNER_TIMEOUT_SECONDS = 60.0
VERIFY_TIMEOUT_SECONDS = 30.0

_SKILL_SCRIPTS = _ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
PLAN_SCRIPT = _SKILL_SCRIPTS / "plan_issue_scope_rollup.py"
VERIFY_SCRIPT = _SKILL_SCRIPTS / "verify_scope_rollup_result.py"

# Env vars that gh honors and could redirect/alter its behavior. These must
# never be inherited from the caller's environment (Issue #1547 P1-1).
_GH_SANITIZED_DROP_KEYS = (
    "GH_HOST",
    "GH_REPO",
    "GH_FORCE_TTY",
    "GH_PAGER",
    "PAGER",
    "GH_CONFIG_DIR",
    "GH_DEBUG",
    "GH_PATH",
    "GH_PROMPT_DISABLED",
)
_GH_SANITIZED_ALLOWED_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    }
)


class ScopeRollupPreflightError(Exception):
    """Fail-closed error carrying a stable machine-readable reason_code."""

    def __init__(self, reason_code: str, message: str = "") -> None:
        super().__init__(message or reason_code)
        self.reason_code = reason_code
        self.message = message or reason_code


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


class _PrivateInvocationDir:
    """Executor-owned private invocation directory (mode 0700).

    Provides exclusive (``O_CREAT | O_EXCL | O_NOFOLLOW``, mode ``0600``)
    ``.part`` file creation, same-directory atomic rename, flush + fsync,
    and guaranteed cleanup on every exit path via :meth:`cleanup`.
    """

    def __init__(self) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="scope_rollup_"))
        os.chmod(self.path, 0o700)

    def write_exclusive(self, name: str, data: bytes) -> Path:
        final_path = self.path / name
        part_path = self.path / f"{name}.part"

        if os.path.lexists(part_path):
            raise ScopeRollupPreflightError(
                "artifact_collision", f"part file already exists: {part_path}"
            )
        if os.path.lexists(final_path):
            raise ScopeRollupPreflightError(
                "artifact_collision", f"final file already exists: {final_path}"
            )

        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(part_path, flags, 0o600)
        except FileExistsError as exc:
            raise ScopeRollupPreflightError("artifact_collision", str(exc)) from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ScopeRollupPreflightError("artifact_symlink_rejected", str(exc)) from exc
            raise ScopeRollupPreflightError("artifact_write_failed", str(exc)) from exc

        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            _safe_unlink(part_path)
            raise

        if os.path.lexists(final_path):
            _safe_unlink(part_path)
            raise ScopeRollupPreflightError(
                "artifact_collision", f"final file appeared concurrently: {final_path}"
            )
        os.rename(part_path, final_path)

        dir_fd = os.open(self.path, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return final_path

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Trusted gh binary / environment sanitation
# ---------------------------------------------------------------------------


def _resolve_trusted_gh_binary(project_root: str) -> str:
    resolved = shutil.which("gh")
    if not resolved:
        raise ScopeRollupPreflightError("gh_not_found", "gh executable not found on PATH")
    real = os.path.realpath(resolved)
    project_root_real = os.path.realpath(project_root)
    try:
        if os.path.commonpath([project_root_real, real]) == project_root_real:
            raise ScopeRollupPreflightError("gh_inside_project_root", real)
    except ValueError:
        pass
    if not os.access(real, os.X_OK):
        raise ScopeRollupPreflightError("gh_not_executable", real)
    return real


def _sanitized_gh_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _GH_SANITIZED_ALLOWED_KEYS and value
    }
    for key in _GH_SANITIZED_DROP_KEYS:
        env.pop(key, None)
    env["GH_HOST"] = TRUSTED_HOST
    env["GH_PROMPT_DISABLED"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _sanitized_child_env(project_root: str) -> dict[str, str]:
    allowed = {"HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH"}
    env = {key: value for key, value in os.environ.items() if key in allowed and value}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CLAUDE_PROJECT_DIR"] = project_root
    return env


def _run_gh(gh_bin: str, args: list[str], *, timeout: float = GH_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            [gh_bin, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
            env=_sanitized_gh_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ScopeRollupPreflightError("gh_timeout", str(exc)) from exc
    except OSError as exc:
        raise ScopeRollupPreflightError("gh_exec_failed", str(exc)) from exc
    if len(result.stdout.encode("utf-8", "surrogatepass")) > MAX_BYTES_PER_FETCH:
        raise ScopeRollupPreflightError("gh_output_too_large", args[0] if args else "gh")
    return result.returncode, result.stdout, result.stderr


def _gh_version(gh_bin: str) -> str:
    rc, out, _err = _run_gh(gh_bin, ["--version"], timeout=10.0)
    if rc != 0 or not out:
        return "unknown"
    return out.splitlines()[0].strip()


# ---------------------------------------------------------------------------
# GitHub inventory fetch
# ---------------------------------------------------------------------------


def _fetch_issue_view(gh_bin: str, repo: str, issue_number: int) -> tuple[dict[str, Any], str]:
    rc, out, err = _run_gh(
        gh_bin,
        [
            "issue",
            "view",
            str(issue_number),
            "--repo",
            repo,
            "--json",
            "number,title,body,labels,state,stateReason,url",
        ],
    )
    if rc != 0:
        raise ScopeRollupPreflightError("gh_issue_view_failed", err.strip()[:500])
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ScopeRollupPreflightError("gh_issue_view_malformed_json", str(exc)) from exc
    if not isinstance(data, dict):
        raise ScopeRollupPreflightError("gh_issue_view_malformed_json", "expected a JSON object")
    return data, out


def _fetch_list(
    gh_bin: str,
    resource: str,
    repo: str,
    json_fields: str,
) -> tuple[list[dict[str, Any]], str, bool]:
    """Fetch a bounded list for ``resource`` in {"issue", "pr"}.

    Returns ``(items, raw_text, truncated)``. Requests
    ``MAX_ITEMS_PER_KIND + 1`` items; if the actual returned count exceeds
    ``MAX_ITEMS_PER_KIND``, pagination completeness cannot be proven and
    ``truncated`` is ``True`` -- the caller must fail closed rather than
    silently treating the bounded result as the full inventory (AC4).
    """
    request_limit = MAX_ITEMS_PER_KIND + 1
    rc, out, err = _run_gh(
        gh_bin,
        [
            resource,
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--limit",
            str(request_limit),
            "--json",
            json_fields,
        ],
        timeout=GH_TIMEOUT_SECONDS,
    )
    if rc != 0:
        raise ScopeRollupPreflightError(f"gh_{resource}_list_failed", err.strip()[:500])
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ScopeRollupPreflightError(f"gh_{resource}_list_malformed_json", str(exc)) from exc
    if not isinstance(data, list):
        raise ScopeRollupPreflightError(f"gh_{resource}_list_malformed_json", "expected a JSON array")
    truncated = len(data) > MAX_ITEMS_PER_KIND
    if truncated:
        data = data[:MAX_ITEMS_PER_KIND]
        # Re-derive raw_text from the (bounded) parsed data so callers never
        # see more than MAX_ITEMS_PER_KIND items in the persisted artifact,
        # even though `truncated=True` will fail the whole transaction closed.
        out = json.dumps(data, ensure_ascii=False)
    return data, out, truncated


# ---------------------------------------------------------------------------
# Runtime context validation (defense in depth; hooks are not a security
# boundary -- see docs/dev/hook-boundaries.md).
# ---------------------------------------------------------------------------


def _validate_runtime_context(project_root: str, repo: str) -> None:
    if os.path.realpath(os.getcwd()) != os.path.realpath(project_root):
        raise ScopeRollupPreflightError("cwd_not_canonical_main_root")
    branch = current_branch(project_root)
    default_branch = resolve_default_branch(project_root)
    if not branch or branch != default_branch:
        raise ScopeRollupPreflightError("root_not_default_branch")
    repo_slug = resolve_repo_slug(project_root)
    if repo_slug != TRUSTED_REPO_SLUG or repo != TRUSTED_REPO_SLUG:
        raise ScopeRollupPreflightError("repo_binding_mismatch")


# ---------------------------------------------------------------------------
# Planner / verifier subprocess transaction
# ---------------------------------------------------------------------------


def _run_planner(
    project_root: str,
    issues_path: Path,
    prs_path: Path,
    issue_number: int,
    repo: str,
    invocation_id: str,
    result_path: Path,
) -> None:
    if PLAN_SCRIPT.is_symlink() or not PLAN_SCRIPT.is_file():
        raise ScopeRollupPreflightError("planner_script_invalid")
    argv = [
        os.path.realpath(sys.executable),
        str(PLAN_SCRIPT),
        "--issues-json",
        str(issues_path),
        "--prs-json",
        str(prs_path),
        "--current-issue",
        str(issue_number),
        "--repo",
        repo,
        "--invocation-id",
        invocation_id,
        "--output",
        str(result_path),
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=PLANNER_TIMEOUT_SECONDS,
            shell=False,
            check=False,
            cwd=project_root,
            env=_sanitized_child_env(project_root),
        )
    except subprocess.TimeoutExpired as exc:
        raise ScopeRollupPreflightError("planner_timeout", str(exc)) from exc
    # Exit code 0 = full success, 2 = current_issue not found (documented,
    # non-fatal "partial" completeness per plan_issue_scope_rollup.py).
    if proc.returncode not in (0, 2):
        raise ScopeRollupPreflightError("planner_failed", proc.stderr.strip()[:500])
    if not result_path.is_file() or result_path.is_symlink():
        raise ScopeRollupPreflightError("planner_output_missing")


def _run_verifier(project_root: str, result_path: Path) -> None:
    if VERIFY_SCRIPT.is_symlink() or not VERIFY_SCRIPT.is_file():
        raise ScopeRollupPreflightError("verify_script_invalid")
    argv = [
        os.path.realpath(sys.executable),
        str(VERIFY_SCRIPT),
        "--result-json",
        str(result_path),
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=VERIFY_TIMEOUT_SECONDS,
            shell=False,
            check=False,
            cwd=project_root,
            env=_sanitized_child_env(project_root),
        )
    except subprocess.TimeoutExpired as exc:
        raise ScopeRollupPreflightError("verify_timeout", str(exc)) from exc
    if proc.returncode != 0:
        raise ScopeRollupPreflightError("verify_failed", proc.stdout.strip()[:500])


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------


def _run_transaction(
    project_root: str,
    issue_number: int,
    repo: str,
    private_dir: _PrivateInvocationDir,
) -> dict[str, Any]:
    gh_bin = _resolve_trusted_gh_binary(project_root)
    gh_version = _gh_version(gh_bin)

    current_issue, current_raw = _fetch_issue_view(gh_bin, repo, issue_number)
    body_sha256 = hashlib.sha256(current_raw.encode("utf-8")).hexdigest()

    issues, issues_raw, issues_truncated = _fetch_list(
        gh_bin, "issue", repo, "number,title,body,labels,state,stateReason,url"
    )
    prs, prs_raw, prs_truncated = _fetch_list(
        gh_bin, "pr", repo, "number,title,body,labels,state,url,files,closingIssuesReferences"
    )

    if issues_truncated or prs_truncated:
        raise ScopeRollupPreflightError(
            "inventory_truncated",
            f"issues_truncated={issues_truncated} prs_truncated={prs_truncated}",
        )

    issues_path = private_dir.write_exclusive("issues.json", issues_raw.encode("utf-8"))
    prs_path = private_dir.write_exclusive("prs.json", prs_raw.encode("utf-8"))

    invocation_id = str(uuid.uuid4())
    result_path = private_dir.path / "plan_result.json"

    _run_planner(project_root, issues_path, prs_path, issue_number, repo, invocation_id, result_path)
    _run_verifier(project_root, result_path)

    plan_data = json.loads(result_path.read_text(encoding="utf-8"))
    planner_script_sha256 = hashlib.sha256(PLAN_SCRIPT.read_bytes()).hexdigest()

    candidates = plan_data.get("candidates", [])
    if not isinstance(candidates, list):
        candidates = []
    high_confidence_count = sum(
        1 for c in candidates if isinstance(c, dict) and c.get("confidence") == "high"
    )

    issues_all_sha256 = hashlib.sha256(issues_raw.encode("utf-8")).hexdigest()
    prs_all_sha256 = hashlib.sha256(prs_raw.encode("utf-8")).hexdigest()

    manifest = {
        "host": TRUSTED_HOST,
        "repo": repo,
        "issue_number": issue_number,
        "invocation_id": invocation_id,
        "gh_realpath": gh_bin,
        "gh_version": gh_version,
        "query_schema_version": QUERY_SCHEMA_VERSION,
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "body_sha256": body_sha256,
        "planner_script_sha256": planner_script_sha256,
        "issues": {
            "page_count": 1,
            "item_count": len(issues),
            "truncated": issues_truncated,
            "max_items_cap": MAX_ITEMS_PER_KIND,
            "sha256": issues_all_sha256,
        },
        "pull_requests": {
            "page_count": 1,
            "item_count": len(prs),
            "truncated": prs_truncated,
            "max_items_cap": MAX_ITEMS_PER_KIND,
            "sha256": prs_all_sha256,
        },
        "truncated": False,
    }

    self_validation = plan_data.get("self_validation", {})
    if not isinstance(self_validation, dict):
        self_validation = {}
    input_block = plan_data.get("input", {})
    if not isinstance(input_block, dict):
        input_block = {}

    return {
        "status": "ok",
        "reason_code": None,
        "manifest": manifest,
        "current_issue": {
            "number": current_issue.get("number"),
            "title": current_issue.get("title"),
            "state": current_issue.get("state"),
            "url": current_issue.get("url"),
        },
        "plan": {
            "plan_schema_name": self_validation.get("schema_name"),
            "plan_schema_version": self_validation.get("schema_version"),
            "payload_sha256": self_validation.get("payload_sha256"),
            "verify_status": "verified",
            "candidate_count": len(candidates),
            "high_confidence_count": high_confidence_count,
            "completeness": input_block.get("completeness"),
        },
        "errors": [],
    }


def _error_result(reason_code: str, message: str) -> dict[str, Any]:
    return {
        "status": "error",
        "reason_code": reason_code,
        "manifest": None,
        "current_issue": None,
        "plan": None,
        "errors": [message] if message else [reason_code],
    }


def _resolve_project_root() -> str:
    env_root = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if env_root:
        return os.path.realpath(env_root)
    return os.path.realpath(str(_ROOT))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exact single-transaction executor for scope_rollup.run (Issue #1547)",
        allow_abbrev=False,
    )
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--repo", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    project_root = _resolve_project_root()

    private_dir: _PrivateInvocationDir | None = None
    try:
        _validate_runtime_context(project_root, args.repo)
        private_dir = _PrivateInvocationDir()
        result = _run_transaction(project_root, args.issue_number, args.repo, private_dir)
        exit_code = 0
    except ScopeRollupPreflightError as exc:
        result = _error_result(exc.reason_code, exc.message)
        exit_code = 1
    except Exception as exc:  # pragma: no cover - defensive fail-closed
        result = _error_result("unexpected_error", str(exc))
        exit_code = 1
    finally:
        if private_dir is not None:
            private_dir.cleanup()

    sys.stdout.write(json.dumps({SCHEMA: result}, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
