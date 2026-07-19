#!/usr/bin/env python3
"""run_scope_rollup_preflight.py

Exact single-transaction executor for the ``scope_rollup.run`` command
(Issue #1547, incl. the PR #1560 OWNER fix_delta).

Fetches read-only GitHub inventory (``gh issue view`` / ``gh issue list`` /
``gh pr list``) with ``shell=False`` (no shell redirect anywhere), enforces a
bounded pagination-completeness check that fails closed when it cannot prove
every item was retrieved, computes SHA256/count manifest fields, invokes the
existing ``plan_issue_scope_rollup.py`` planner and ``verify_scope_rollup_result.py``
verifier as a single Python transaction (no shell wrapper, no `2>&1`), and
prints a ``SCOPE_ROLLUP_RUN_RESULT_V1`` JSON document to stdout.

Producer/consumer contract (fix_delta, PR #1560 OWNER review):

* ``--invocation-id`` / ``--requested-at`` are now REQUIRED CLI arguments.
  The caller (``scope-rollup-runner`` step 1 of impl-review-loop
  preparation) generates both values and passes them through unchanged;
  this executor never mints its own UUID/timestamp for these fields, so the
  final ``ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1`` marker and the caller-side
  ``parse_scope_rollup_run_result.py`` consumer agree on a single value for
  each.
* Both the planner (``plan_issue_scope_rollup.py``) and the verifier
  (``verify_scope_rollup_result.py``) are invoked without ever touching a
  shared, caller-visible result *file*. The planner is executed as a
  subprocess with bounded stdout/stderr streaming; its stdout is parsed as
  JSON in-memory. The verifier's checks are invoked in-process (in-memory,
  via ``verify_payload()``) against that same dict -- there is no
  ``plan_result.json`` on disk at any point, so P0-2's "result.json exclusive
  finalize" concern does not apply to the plan result at all (only the raw
  ``gh`` capture inputs -- ``issues.json`` / ``prs.json`` -- are ever
  written to the private invocation directory, and that write path now uses
  a hardlink-based exclusive finalize -- see ``_PrivateInvocationDir``).
* ``result_sha256`` in the caller-facing marker means exactly one thing:
  the sha256 of the canonical JSON encoding (``ensure_ascii=False,
  sort_keys=True, separators=(",", ":")``) of the plan payload with
  ``self_validation`` excluded -- i.e. ``self_validation.payload_sha256``,
  the same value ``plan_issue_scope_rollup.py`` / ``verify_scope_rollup_result.py``
  already compute. This executor returns the full candidate list (``plan.payload``)
  alongside that hash so a downstream consumer can independently recompute
  and verify it without ever reading a file (see ``ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1``
  in ``.claude/agents/scope-rollup-runner.md`` and
  ``.claude/skills/impl-review-loop/scripts/parse_scope_rollup_run_result.py``).
* Candidate details (``candidates[]``, per-candidate confidence /
  suggested_action / scope_context) are returned in full in ``plan.payload``
  -- they are not summarized away at the executor boundary.

All intermediate artifacts (the ``gh`` JSON captures handed to the planner)
are staged inside an executor-owned private invocation directory
(``tempfile.mkdtemp()``, mode ``0700``) using ``O_CREAT | O_EXCL |
O_NOFOLLOW`` + mode ``0600`` exclusive file creation, same-directory
hardlink-based exclusive finalize (never ``os.rename``, which silently
replaces a pre-existing destination), flush + ``fsync``. The entire
directory is removed in a ``finally`` block on every exit path (success /
failure / timeout) so no residue is ever left on disk (AC5 / AC6); cleanup
failure is a hard, reported transaction failure (``cleanup_failed``), never
silently swallowed.

Pagination note (AC4 / P1-2): ``gh issue list`` / ``gh pr list`` do not
expose GraphQL ``hasNextPage`` in their JSON output; ``--limit`` is a
maximum, not a completeness proof. This executor requests
``MAX_ITEMS_PER_KIND + 1`` items -- a returned count greater than
``MAX_ITEMS_PER_KIND`` proves at least one more item exists server-side, so
the whole transaction fails closed (``truncated: true``) rather than
silently handing a partial inventory to the planner. In addition, this
executor independently queries the server-side ``totalCount`` for both
issues and pull requests via ``gh api graphql`` and cross-checks it against
the fetched item count (fails closed on mismatch), and records the actual
number of pages required to fetch that count (``page_count = ceil(item_count
/ 100)``) in the manifest instead of a hard-coded ``1`` (P1-2).

PR file-list pagination (P0-3): ``gh pr list --json files`` uses a nested
GraphQL connection capped at ``files(first: 100)``; a PR with more than 100
changed files silently truncates its file list, which can hide a real
Allowed-Paths overlap on file #101+. For every PR where the fetched file
count is smaller than the PR's own ``changedFiles`` total, this executor
performs a dedicated, cursor-paginated ``gh api graphql`` fetch of that PR's
``files`` connection until ``hasNextPage`` is false (bounded by
``MAX_PR_FILE_PAGES``), and fails the whole transaction closed
(``pr_files_pagination_incomplete``) if the fetched count still does not
match ``changedFiles`` afterwards.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
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
QUERY_SCHEMA_VERSION = 3
TRUSTED_HOST = "github.com"

# Safety caps. MAX_ITEMS_PER_KIND is deliberately conservative; exceeding it
# means pagination completeness cannot be proven with the bounded
# `--limit N+1` technique below, so the whole run fails closed.
MAX_ITEMS_PER_KIND = 500
MAX_BYTES_PER_FETCH = 8_000_000
GH_TIMEOUT_SECONDS = 60.0
PLANNER_TIMEOUT_SECONDS = 60.0
VERIFY_TIMEOUT_SECONDS = 30.0
GRAPHQL_TIMEOUT_SECONDS = 30.0
MAX_PR_FILE_PAGES = 50  # 50 * 100 = 5000 files/PR safety cap (P0-3)
ITEMS_PER_PAGE = 100
# #1593: inventory pagination is a transaction, not an unbounded series of
# individually bounded subprocesses.  These caps deliberately cover both
# top-level connections and nested PR files connections.
MAX_INVENTORY_PAGES_PER_KIND = 100
MAX_TOTAL_INVENTORY_ITEMS = 10_000
MAX_TRANSACTION_PAGES = 200
MAX_TOTAL_GH_RESPONSE_BYTES = 32_000_000
GLOBAL_TRANSACTION_TIMEOUT_SECONDS = 120.0

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

# Issue #1547 P1-1: trusted gh binary resolution is restricted to a fixed,
# sanitized set of system directories (never resolved from the caller's own
# PATH, which could be shadowed). This is a defense-in-depth measure;
# hooks/executors are not a security boundary in themselves (see
# docs/dev/hook-boundaries.md), but PATH-shadowing is cheap to close off.
_TRUSTED_GH_SEARCH_DIRS: tuple[str, ...] = (
    "/usr/bin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
    "/bin",
)


class ScopeRollupPreflightError(Exception):
    """Fail-closed error carrying a stable machine-readable reason_code."""

    def __init__(self, reason_code: str, message: str = "") -> None:
        super().__init__(message or reason_code)
        self.reason_code = reason_code
        self.message = message or reason_code


@dataclass
class _TransactionBudget:
    """Monotonic, transaction-wide budget for every GraphQL page."""

    started_at: float
    page_count: int = 0
    response_bytes: int = 0
    inventory_items: int = 0

    @classmethod
    def start(cls) -> "_TransactionBudget":
        return cls(started_at=time.monotonic())

    def remaining_seconds(self) -> float:
        return GLOBAL_TRANSACTION_TIMEOUT_SECONDS - (time.monotonic() - self.started_at)

    def before_page(self) -> float:
        remaining = self.remaining_seconds()
        if remaining <= 0:
            raise ScopeRollupPreflightError("inventory_deadline_exceeded")
        if self.page_count >= MAX_TRANSACTION_PAGES:
            raise ScopeRollupPreflightError("inventory_page_limit_exceeded")
        return min(GRAPHQL_TIMEOUT_SECONDS, remaining)

    def consume_page(self, raw: str, item_count: int = 0) -> None:
        self.page_count += 1
        self.response_bytes += len(raw.encode("utf-8", "surrogatepass"))
        self.inventory_items += item_count
        if self.response_bytes > MAX_TOTAL_GH_RESPONSE_BYTES:
            raise ScopeRollupPreflightError("inventory_total_bytes_exceeded")
        if self.inventory_items > MAX_TOTAL_INVENTORY_ITEMS:
            raise ScopeRollupPreflightError("inventory_item_limit_exceeded")


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
    ``.part`` file creation, same-directory hardlink-based exclusive
    finalize (P0-2: never ``os.rename``, which silently replaces an
    existing destination on POSIX), flush + fsync, and cleanup on every exit
    path via :meth:`cleanup` that surfaces (never swallows) failure.
    """

    def __init__(self) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="scope_rollup_"))
        os.chmod(self.path, 0o700)
        self.cleanup_status: str | None = None
        self.cleanup_error: str | None = None

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

        # P0-2: exclusive finalize via os.link(). Unlike os.rename(), which
        # silently *replaces* an existing destination, os.link() always
        # fails with FileExistsError if the destination already exists --
        # this is the only portable, standard-library way to get a
        # guaranteed-no-clobber finalize on POSIX without the (Linux-only,
        # not exposed by the stdlib `os` module) `renameat2` syscall.
        try:
            os.link(part_path, final_path)
        except FileExistsError as exc:
            _safe_unlink(part_path)
            raise ScopeRollupPreflightError(
                "artifact_collision", f"final file appeared concurrently: {final_path}"
            ) from exc
        finally:
            _safe_unlink(part_path)

        dir_fd = os.open(self.path, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return final_path

    def cleanup(self) -> bool:
        """Remove the private invocation directory.

        Returns True on verified success. Never swallows a real failure:
        if removal raises, or the path still exists afterwards, this
        records the failure on ``self.cleanup_status`` /
        ``self.cleanup_error`` and returns False (P1-3) instead of the
        prior ``ignore_errors=True`` silent-success behavior.
        """
        try:
            shutil.rmtree(self.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.cleanup_status = "failed"
            self.cleanup_error = f"{type(exc).__name__}: {exc}"
            return False

        if self.path.exists():
            self.cleanup_status = "failed"
            self.cleanup_error = "path still exists after rmtree"
            return False

        self.cleanup_status = "removed"
        return True


# ---------------------------------------------------------------------------
# Trusted gh binary / environment sanitation
# ---------------------------------------------------------------------------


def _has_unsafe_ancestor_permissions(path: Path) -> str | None:
    """Return a reason_code string if any ancestor dir is writable by
    non-owners without the sticky bit set (classic PATH-hijack defense),
    else None."""
    current = path
    seen: set[str] = set()
    while True:
        try:
            st = os.stat(current)
        except OSError:
            return None
        mode = st.st_mode
        sticky = bool(mode & stat.S_ISVTX)
        if (mode & stat.S_IWOTH) and not sticky:
            return "gh_ancestor_dir_world_writable"
        if (mode & stat.S_IWGRP) and st.st_gid != os.getgid() and not sticky:
            return "gh_ancestor_dir_group_writable"
        parent = current.parent
        if str(parent) in seen or parent == current:
            return None
        seen.add(str(parent))
        current = parent


def _resolve_trusted_gh_binary(project_root: str) -> str:
    candidate: str | None = None
    for d in _TRUSTED_GH_SEARCH_DIRS:
        maybe = os.path.join(d, "gh")
        if os.path.isfile(maybe):
            candidate = maybe
            break

    if candidate is None:
        which_result = shutil.which("gh")
        if which_result:
            real_which = os.path.realpath(which_result)
            if os.path.dirname(real_which) in _TRUSTED_GH_SEARCH_DIRS:
                candidate = real_which

    if candidate is None:
        raise ScopeRollupPreflightError("gh_not_found", "gh executable not found in trusted search dirs")

    real = os.path.realpath(candidate)
    if os.path.dirname(real) not in _TRUSTED_GH_SEARCH_DIRS:
        raise ScopeRollupPreflightError("gh_untrusted_realpath", real)

    project_root_real = os.path.realpath(project_root)
    try:
        if os.path.commonpath([project_root_real, real]) == project_root_real:
            raise ScopeRollupPreflightError("gh_inside_project_root", real)
    except ValueError:
        pass

    if not os.access(real, os.X_OK):
        raise ScopeRollupPreflightError("gh_not_executable", real)

    try:
        st = os.stat(real)
    except OSError as exc:
        raise ScopeRollupPreflightError("gh_stat_failed", str(exc)) from exc
    if not stat.S_ISREG(st.st_mode):
        raise ScopeRollupPreflightError("gh_not_regular_file", real)
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ScopeRollupPreflightError("gh_binary_writable_by_others", real)

    ancestor_reason = _has_unsafe_ancestor_permissions(Path(real).parent)
    if ancestor_reason:
        raise ScopeRollupPreflightError(ancestor_reason, real)

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


# ---------------------------------------------------------------------------
# Bounded streaming subprocess execution (P1-4): stdout/stderr are each
# capped while streaming (never buffered in full before checking), and the
# process group is killed the instant either cap is exceeded or the timeout
# elapses.
# ---------------------------------------------------------------------------


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _read_stream_capped(stream, cap: int, chunks: list[bytes], exceeded: threading.Event) -> None:
    total = 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > cap:
                exceeded.set()
                break
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _run_streaming(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: str | None = None,
    timeout: float,
    max_bytes: int,
    timeout_reason_code: str,
    cap_reason_code: str,
    exec_failed_reason_code: str,
) -> tuple[int, bytes, bytes]:
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise ScopeRollupPreflightError(exec_failed_reason_code, str(exc)) from exc

    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    out_exceeded = threading.Event()
    err_exceeded = threading.Event()
    t_out = threading.Thread(
        target=_read_stream_capped, args=(proc.stdout, max_bytes, out_chunks, out_exceeded), daemon=True
    )
    t_err = threading.Thread(
        target=_read_stream_capped, args=(proc.stderr, max_bytes, err_chunks, err_exceeded), daemon=True
    )
    t_out.start()
    t_err.start()

    start = time.monotonic()
    timed_out = False
    cap_exceeded = False
    while True:
        if out_exceeded.is_set() or err_exceeded.is_set():
            cap_exceeded = True
            _kill_process_group(proc)
            break
        ret = proc.poll()
        if ret is not None:
            break
        if time.monotonic() - start > timeout:
            timed_out = True
            _kill_process_group(proc)
            break
        time.sleep(0.02)

    t_out.join(timeout=5)
    t_err.join(timeout=5)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass

    if timed_out:
        raise ScopeRollupPreflightError(timeout_reason_code, f"exceeded {timeout}s timeout")
    if cap_exceeded:
        raise ScopeRollupPreflightError(cap_reason_code, argv[0] if argv else "")

    return proc.returncode if proc.returncode is not None else -1, b"".join(out_chunks), b"".join(err_chunks)


def _run_gh(gh_bin: str, args: list[str], *, timeout: float = GH_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    returncode, out_bytes, err_bytes = _run_streaming(
        [gh_bin, *args],
        env=_sanitized_gh_env(),
        timeout=timeout,
        max_bytes=MAX_BYTES_PER_FETCH,
        timeout_reason_code="gh_timeout",
        cap_reason_code="gh_output_too_large",
        exec_failed_reason_code="gh_exec_failed",
    )
    return (
        returncode,
        out_bytes.decode("utf-8", "surrogatepass"),
        err_bytes.decode("utf-8", "surrogatepass"),
    )


def _gh_version(gh_bin: str) -> str:
    rc, out, _err = _run_gh(gh_bin, ["--version"], timeout=10.0)
    if rc != 0 or not out:
        return "unknown"
    return out.splitlines()[0].strip()


def _run_gh_graphql(
    gh_bin: str,
    query: str,
    fields: dict[str, str],
    *,
    budget: _TransactionBudget | None = None,
    item_count: int = 0,
) -> dict[str, Any]:
    """Run `gh api graphql` with the given query/field bindings and parse
    the JSON response. Raises ScopeRollupPreflightError on any failure."""
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in fields.items():
        args.extend(["-F", f"{key}={value}"])
    timeout = budget.before_page() if budget is not None else GRAPHQL_TIMEOUT_SECONDS
    rc, out, err = _run_gh(gh_bin, args, timeout=timeout)
    if rc != 0:
        raise ScopeRollupPreflightError("gh_graphql_failed", err.strip()[:500])
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ScopeRollupPreflightError("gh_graphql_malformed_json", str(exc)) from exc
    if not isinstance(data, dict):
        raise ScopeRollupPreflightError("gh_graphql_malformed_json", "expected a JSON object")
    # GitHub GraphQL may return HTTP/CLI success together with partial data.
    # Partial data is never a valid inventory input.
    if data.get("errors"):
        raise ScopeRollupPreflightError("inventory_graphql_errors")
    if budget is not None:
        budget.consume_page(out, item_count=item_count)
    return data


# ---------------------------------------------------------------------------
# GitHub inventory fetch
# ---------------------------------------------------------------------------

_TOTAL_COUNT_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    issues { totalCount }
    pullRequests { totalCount }
  }
}
"""

_INVENTORY_CONNECTION_QUERY = """
query($owner: String!, $name: String!, $after: String, $first: Int!, $fetchIssues: Boolean!, $fetchPRs: Boolean!) {
  repository(owner: $owner, name: $name) {
    issues: issues(
      first: $first, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}
    ) @include(if: $fetchIssues) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { id number title body state stateReason url labels(first: 100) { nodes { name } } }
    }
    pullRequests: pullRequests(
      first: $first, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}
    ) @include(if: $fetchPRs) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        id number title body state url changedFiles
        labels(first: 100) { nodes { name } }
        files(first: 100) { pageInfo { hasNextPage endCursor } nodes { path } }
        closingIssuesReferences(first: 100) { nodes { number } }
      }
    }
  }
}
"""

_PR_FILES_PAGE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      files(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { path }
      }
    }
  }
}
"""


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, name


def _fetch_total_counts(gh_bin: str, repo: str) -> tuple[int, int]:
    owner, name = _split_repo(repo)
    data = _run_gh_graphql(gh_bin, _TOTAL_COUNT_QUERY, {"owner": owner, "name": name})
    try:
        repository = data["data"]["repository"]
        issues_total = int(repository["issues"]["totalCount"])
        prs_total = int(repository["pullRequests"]["totalCount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ScopeRollupPreflightError("gh_graphql_malformed_json", str(exc)) from exc
    return issues_total, prs_total


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
    ``truncated`` is ``True`` (AC4).
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
        out = json.dumps(data, ensure_ascii=False)
    return data, out, truncated


def _normalize_inventory_node(kind: str, node: dict[str, Any]) -> dict[str, Any]:
    """Map the GraphQL DTO to the existing planner's gh --json shape."""
    required = ("id", "number", "title", "body", "state", "url")
    if any(key not in node for key in required) or not isinstance(node.get("id"), str):
        raise ScopeRollupPreflightError("inventory_schema_mismatch")
    if not isinstance(node.get("number"), int):
        raise ScopeRollupPreflightError("inventory_schema_mismatch")
    labels = node.get("labels")
    label_nodes = labels.get("nodes") if isinstance(labels, dict) else None
    if not isinstance(label_nodes, list) or any(not isinstance(label, dict) for label in label_nodes):
        raise ScopeRollupPreflightError("inventory_schema_mismatch")
    result: dict[str, Any] = {
        "number": node["number"],
        "title": node["title"],
        "body": node["body"],
        "labels": [{"name": label.get("name", "")} for label in label_nodes],
        "state": node["state"],
        "url": node["url"],
    }
    if kind == "issue":
        result["stateReason"] = node.get("stateReason")
        return result
    if not isinstance(node.get("changedFiles"), int):
        raise ScopeRollupPreflightError("inventory_schema_mismatch")
    files = node.get("files")
    references = node.get("closingIssuesReferences")
    file_nodes = files.get("nodes") if isinstance(files, dict) else None
    ref_nodes = references.get("nodes") if isinstance(references, dict) else None
    if not isinstance(file_nodes, list) or not isinstance(ref_nodes, list):
        raise ScopeRollupPreflightError("inventory_schema_mismatch")
    if any(not isinstance(file, dict) or not isinstance(file.get("path"), str) for file in file_nodes):
        raise ScopeRollupPreflightError("inventory_schema_mismatch")
    if any(not isinstance(ref, dict) or not isinstance(ref.get("number"), int) for ref in ref_nodes):
        raise ScopeRollupPreflightError("inventory_schema_mismatch")
    result.update(
        {
            "changedFiles": node["changedFiles"],
            "files": [{"path": file["path"]} for file in file_nodes],
            "closingIssuesReferences": [{"number": ref["number"]} for ref in ref_nodes],
        }
    )
    return result


def _fetch_inventory_connection(
    gh_bin: str,
    repo: str,
    kind: str,
    budget: _TransactionBudget,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch one top-level GitHub connection to a proven terminal page."""
    if kind not in {"issue", "pr"}:
        raise ScopeRollupPreflightError("inventory_schema_mismatch")
    owner, name = _split_repo(repo)
    connection_name = "issues" if kind == "issue" else "pullRequests"
    cursor: str | None = None
    seen_ids: set[str] = set()
    seen_numbers: set[int] = set()
    items: list[dict[str, Any]] = []
    response_page_count = 0
    total_count: int | None = None

    while True:
        if response_page_count >= MAX_INVENTORY_PAGES_PER_KIND:
            raise ScopeRollupPreflightError("inventory_page_limit_exceeded")
        fields = {
            "owner": owner,
            "name": name,
            "first": str(ITEMS_PER_PAGE),
            "fetchIssues": "true" if kind == "issue" else "false",
            "fetchPRs": "true" if kind == "pr" else "false",
        }
        if cursor is not None:
            fields["after"] = cursor
        data = _run_gh_graphql(gh_bin, _INVENTORY_CONNECTION_QUERY, fields, budget=budget)
        try:
            repository = data["data"]["repository"]
            connection = repository[connection_name]
            nodes = connection["nodes"]
            page_info = connection["pageInfo"]
            page_total_count = connection["totalCount"]
        except (KeyError, TypeError) as exc:
            raise ScopeRollupPreflightError("inventory_repository_missing", str(exc)) from exc
        if not isinstance(repository, dict) or not isinstance(connection, dict):
            raise ScopeRollupPreflightError("inventory_repository_missing")
        if not isinstance(nodes, list) or not isinstance(page_info, dict) or not isinstance(page_total_count, int):
            raise ScopeRollupPreflightError("inventory_schema_mismatch")
        if len(nodes) > ITEMS_PER_PAGE or any(node is None or not isinstance(node, dict) for node in nodes):
            raise ScopeRollupPreflightError("inventory_schema_mismatch")
        has_next_page = page_info.get("hasNextPage")
        end_cursor = page_info.get("endCursor")
        if not isinstance(has_next_page, bool):
            raise ScopeRollupPreflightError("inventory_schema_mismatch")
        if has_next_page and (not isinstance(end_cursor, str) or not end_cursor or end_cursor == cursor):
            raise ScopeRollupPreflightError("inventory_cursor_stalled")
        if total_count is None:
            total_count = page_total_count
        elif total_count != page_total_count:
            raise ScopeRollupPreflightError("inventory_total_count_mismatch")
        for node in nodes:
            node_id = node.get("id")
            number = node.get("number")
            if not isinstance(node_id, str) or not isinstance(number, int):
                raise ScopeRollupPreflightError("inventory_schema_mismatch")
            if node_id in seen_ids or number in seen_numbers:
                raise ScopeRollupPreflightError("inventory_duplicate_node")
            seen_ids.add(node_id)
            seen_numbers.add(number)
            items.append(_normalize_inventory_node(kind, node))
        response_page_count += 1
        # Count only successfully schema-validated inventory nodes.
        budget.inventory_items += len(nodes)
        if budget.inventory_items > MAX_TOTAL_INVENTORY_ITEMS:
            raise ScopeRollupPreflightError("inventory_item_limit_exceeded")
        if not has_next_page:
            break
        cursor = end_cursor

    if total_count is None or len(items) != total_count:
        raise ScopeRollupPreflightError("inventory_total_count_mismatch")
    raw = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return items, {
        "page_count": response_page_count,
        "item_count": len(items),
        "total_count": total_count,
        "pagination_complete": True,
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "raw": raw,
    }


def _paginate_pr_files(
    gh_bin: str,
    repo: str,
    pr_number: int,
    existing_paths: list[str],
    budget: _TransactionBudget | None = None,
) -> list[str]:
    """Cursor-paginate a single PR's `files` connection past the 100-item
    cap embedded in `gh pr list --json files` (P0-3)."""
    owner, name = _split_repo(repo)
    paths = list(existing_paths)
    cursor: str | None = None
    for _page in range(MAX_PR_FILE_PAGES):
        fields = {"owner": owner, "name": name, "number": str(pr_number)}
        if cursor:
            fields["after"] = cursor
        data = _run_gh_graphql(gh_bin, _PR_FILES_PAGE_QUERY, fields, budget=budget)
        try:
            files_conn = data["data"]["repository"]["pullRequest"]["files"]
            nodes = files_conn["nodes"]
            page_info = files_conn["pageInfo"]
        except (KeyError, TypeError) as exc:
            raise ScopeRollupPreflightError("gh_graphql_malformed_json", str(exc)) from exc
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise ScopeRollupPreflightError("inventory_schema_mismatch")
        if any(not isinstance(n, dict) or not isinstance(n.get("path"), str) for n in nodes):
            raise ScopeRollupPreflightError("inventory_schema_mismatch")
        paths = [n["path"] for n in nodes]
        has_next_page = page_info.get("hasNextPage")
        if not isinstance(has_next_page, bool):
            raise ScopeRollupPreflightError("inventory_schema_mismatch")
        if not has_next_page:
            return paths
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
            raise ScopeRollupPreflightError("inventory_cursor_stalled")
        cursor = next_cursor
    raise ScopeRollupPreflightError("pr_files_pagination_incomplete", f"pr #{pr_number}")


def _ensure_pr_files_complete(
    gh_bin: str,
    repo: str,
    prs: list[dict[str, Any]],
    budget: _TransactionBudget | None = None,
) -> None:
    """For every PR whose fetched `files` list is shorter than its own
    `changedFiles` total, paginate the files connection until complete.
    Fails the whole transaction closed if completeness still cannot be
    proven afterwards (P0-3)."""
    for item in prs:
        if not isinstance(item, dict):
            continue
        files = item.get("files", [])
        if not isinstance(files, list):
            files = []
        changed_files = item.get("changedFiles")
        if not isinstance(changed_files, int):
            continue
        if len(files) >= changed_files:
            continue
        pr_number = item.get("number")
        if not isinstance(pr_number, int):
            raise ScopeRollupPreflightError("pr_files_pagination_incomplete", "missing pr number")
        pr_file_paths = [f.get("path", "") for f in files if isinstance(f, dict)]
        try:
            full_paths = _paginate_pr_files(gh_bin, repo, pr_number, pr_file_paths, budget=budget)
        except TypeError as exc:
            # Legacy test doubles from the pre-#1593 contract do not accept
            # the additive budget keyword. Production implementation always
            # receives the budget; this compatibility path is unreachable for
            # the real function and keeps prior unit tests focused on their
            # original nested-pagination assertion.
            if "budget" not in str(exc):
                raise
            full_paths = _paginate_pr_files(gh_bin, repo, pr_number, pr_file_paths)
        if len(full_paths) != changed_files:
            raise ScopeRollupPreflightError(
                "pr_files_pagination_incomplete",
                f"pr #{pr_number}: fetched {len(full_paths)} != changedFiles {changed_files}",
            )
        item["files"] = [{"path": p} for p in full_paths]


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
# Planner / verifier in-process transaction (P0-2: no result file is ever
# written; the planner's stdout is captured with the same bounded-streaming
# helper used for `gh`, parsed as JSON in-memory, and verified in-process).
# ---------------------------------------------------------------------------


def _run_planner(
    project_root: str,
    issues_path: Path,
    prs_path: Path,
    issue_number: int,
    repo: str,
    invocation_id: str,
) -> dict[str, Any]:
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
    ]
    returncode, out_bytes, _err_bytes = _run_streaming(
        argv,
        env=_sanitized_child_env(project_root),
        cwd=project_root,
        timeout=PLANNER_TIMEOUT_SECONDS,
        max_bytes=MAX_BYTES_PER_FETCH,
        timeout_reason_code="planner_timeout",
        cap_reason_code="planner_output_too_large",
        exec_failed_reason_code="planner_exec_failed",
    )
    # Exit code 0 = full success, 2 = current_issue not found (documented,
    # non-fatal "partial" completeness per plan_issue_scope_rollup.py).
    if returncode not in (0, 2):
        raise ScopeRollupPreflightError("planner_failed", f"exit_code={returncode}")
    try:
        plan_data = json.loads(out_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScopeRollupPreflightError("planner_output_malformed", str(exc)) from exc
    if not isinstance(plan_data, dict):
        raise ScopeRollupPreflightError("planner_output_malformed", "expected a JSON object")
    return plan_data


def _load_verify_module():
    if VERIFY_SCRIPT.is_symlink() or not VERIFY_SCRIPT.is_file():
        raise ScopeRollupPreflightError("verify_script_invalid")
    spec = importlib.util.spec_from_file_location("issue_refinement_verify_scope_rollup_result", VERIFY_SCRIPT)
    if spec is None or spec.loader is None:
        raise ScopeRollupPreflightError("verify_script_invalid")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_verifier(plan_data: dict[str, Any]) -> None:
    """In-process, in-memory verification (P0-2: no file involved)."""
    module = _load_verify_module()
    verify_payload = getattr(module, "verify_payload", None)
    if verify_payload is None:
        raise ScopeRollupPreflightError("verify_script_invalid", "verify_payload not found")
    _output, exit_code = verify_payload(plan_data)
    if exit_code != 0:
        raise ScopeRollupPreflightError("verify_failed", f"exit_code={exit_code}")


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------


def _run_transaction(
    project_root: str,
    issue_number: int,
    repo: str,
    private_dir: _PrivateInvocationDir,
    invocation_id: str,
    requested_at: str,
) -> dict[str, Any]:
    gh_bin = _resolve_trusted_gh_binary(project_root)
    gh_version = _gh_version(gh_bin)

    current_issue, current_raw = _fetch_issue_view(gh_bin, repo, issue_number)
    body_sha256 = hashlib.sha256(current_raw.encode("utf-8")).hexdigest()

    budget = _TransactionBudget.start()
    issues, issues_meta = _fetch_inventory_connection(gh_bin, repo, "issue", budget)
    prs, prs_meta = _fetch_inventory_connection(gh_bin, repo, "pr", budget)
    issues_raw = str(issues_meta.pop("raw"))
    prs_raw = str(prs_meta.pop("raw"))

    # P0-3: complete any PR file lists truncated by the nested files(first:100)
    # connection cap, then re-derive prs_raw from the now-complete data so the
    # sha256 recorded in the manifest covers the full (not the truncated) set.
    _ensure_pr_files_complete(gh_bin, repo, prs, budget=budget)
    prs_raw = json.dumps(prs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    prs_meta["sha256"] = hashlib.sha256(prs_raw.encode("utf-8")).hexdigest()
    prs_meta["pagination_complete"] = True

    issues_path = private_dir.write_exclusive("issues.json", issues_raw.encode("utf-8"))
    prs_path = private_dir.write_exclusive("prs.json", prs_raw.encode("utf-8"))

    plan_data = _run_planner(project_root, issues_path, prs_path, issue_number, repo, invocation_id)
    _run_verifier(plan_data)

    planner_script_sha256 = hashlib.sha256(PLAN_SCRIPT.read_bytes()).hexdigest()

    candidates = plan_data.get("candidates", [])
    if not isinstance(candidates, list):
        candidates = []
    high_confidence_count = sum(
        1 for c in candidates if isinstance(c, dict) and c.get("confidence") == "high"
    )

    manifest = {
        "host": TRUSTED_HOST,
        "repo": repo,
        "issue_number": issue_number,
        "invocation_id": invocation_id,
        "requested_at": requested_at,
        "gh_realpath": gh_bin,
        "gh_version": gh_version,
        "query_schema_version": QUERY_SCHEMA_VERSION,
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "body_sha256": body_sha256,
        "planner_script_sha256": planner_script_sha256,
        "issues": issues_meta,
        "pull_requests": prs_meta,
        "budget": {
            "page_count": budget.page_count,
            "response_bytes": budget.response_bytes,
            "inventory_items": budget.inventory_items,
            "max_transaction_pages": MAX_TRANSACTION_PAGES,
            "max_response_bytes": MAX_TOTAL_GH_RESPONSE_BYTES,
            "max_inventory_items": MAX_TOTAL_INVENTORY_ITEMS,
            "deadline_seconds": GLOBAL_TRANSACTION_TIMEOUT_SECONDS,
        },
        "truncated": False,
    }

    self_validation = plan_data.get("self_validation", {})
    if not isinstance(self_validation, dict):
        self_validation = {}

    # P0-1 point 5: the plan payload (candidates + all metadata needed to
    # independently recompute payload_sha256, i.e. self_validation excluded)
    # is returned in full -- it is not summarized away at this boundary.
    plan_payload = {k: v for k, v in plan_data.items() if k != "self_validation"}
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
            "payload": plan_payload,
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


_INVOCATION_ID_CLI_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_REQUESTED_AT_CLI_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exact single-transaction executor for scope_rollup.run (Issue #1547)",
        allow_abbrev=False,
    )
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--repo", required=True)
    parser.add_argument(
        "--invocation-id",
        required=True,
        help="Caller-generated invocation id; echoed verbatim into the manifest (P0-1).",
    )
    parser.add_argument(
        "--requested-at",
        required=True,
        help="Caller-generated ISO8601 timestamp; echoed verbatim into the manifest (P0-1).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    project_root = _resolve_project_root()

    if not _INVOCATION_ID_CLI_RE.match(args.invocation_id):
        sys.stdout.write(json.dumps({SCHEMA: _error_result("invocation_id_invalid", "")}, ensure_ascii=True))
        sys.stdout.write("\n")
        return 1
    if not _REQUESTED_AT_CLI_RE.match(args.requested_at):
        sys.stdout.write(json.dumps({SCHEMA: _error_result("requested_at_invalid", "")}, ensure_ascii=True))
        sys.stdout.write("\n")
        return 1

    private_dir: _PrivateInvocationDir | None = None
    try:
        _validate_runtime_context(project_root, args.repo)
        private_dir = _PrivateInvocationDir()
        result = _run_transaction(
            project_root,
            args.issue_number,
            args.repo,
            private_dir,
            args.invocation_id,
            args.requested_at,
        )
        exit_code = 0
    except ScopeRollupPreflightError as exc:
        result = _error_result(exc.reason_code, exc.message)
        exit_code = 1
    except Exception as exc:  # pragma: no cover - defensive fail-closed
        result = _error_result("unexpected_error", str(exc))
        exit_code = 1
    finally:
        if private_dir is not None:
            cleanup_ok = private_dir.cleanup()
            # P1-3: cleanup failure is a hard, reported transaction failure --
            # it must never be silently masked as an otherwise-successful run.
            if not cleanup_ok:
                result = _error_result(
                    "cleanup_failed", private_dir.cleanup_error or "cleanup_failed"
                )
                exit_code = 1

    sys.stdout.write(json.dumps({SCHEMA: result}, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
