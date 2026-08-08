#!/usr/bin/env python3
"""run_graphify_cli_advisory.py — pinned Graphify CLI advisory prefilter wrapper (Issue #2009).

Wraps the exact-pinned Graphify CLI (``graphifyy==0.9.34``, invoked via
``uvx --from graphifyy==0.9.34 graphify <subcommand>``) as an **optional, read-only,
advisory candidate-prefilter** in front of the existing
``codebase-investigator`` ``local_asset_research`` route (Gemini +
Serena MCP).

Hard boundaries enforced by this module (see SKILL.md for the full contract):

- Exact pinned package spec only (``graphifyy==0.9.34``); no floating version.
- Only ``extract --code-only`` / ``query`` / ``path`` / ``explain`` / ``--version``
  may ever be launched. Every other subcommand (``install`` / ``hook`` /
  ``watch`` / ``prs`` / ``uninstall`` / any MCP server startup) is rejected
  before a subprocess is ever spawned.
- ``subprocess`` is always invoked with a list argv (no ``shell=True``),
  pinned to ``cwd=repo_root``.
- ``repo_root`` must resolve to the actual git top-level and ``head_sha`` must
  match the repository's actual current ``HEAD`` (both independently
  verified via ``git``, not merely sanitized). ``target_path`` must resolve
  under ``repo_root``. Output path components are rejected if any is a
  symlink. The caller can never override the output/graph path — it is
  always derived from ``repo_root``/``head_sha``.
- Output is confined to ``tmp/graphify/<head-sha>/`` under the repository
  root. No other location is ever written to. A local, untracked provenance
  marker (``tmp/graphify/<head-sha>/provenance.json``) records the repo
  root/HEAD/target/version used to build a graph; stale/mismatched markers
  are treated as an unavailable graph, never as stale-but-usable.
- A dirty worktree short-circuits to an ``unavailable`` status *before* any
  Graphify graph is created or read (no graph creation/use on dirty trees).
- Before any action is dispatched, a ``graphify --version`` preflight is run
  and must exactly match the pinned version string; the observed version is
  always included in the result (not only for ``action == "version"``).
- ``graphify query`` always passes an explicit, range-clamped ``--budget``
  (never unbounded output).
- Any failure mode (launch failure, non-zero exit, timeout, missing graph,
  dirty worktree, disallowed subcommand, invalid repo/HEAD/target/output
  path, or any other unexpected error) degrades to ``status: unavailable``
  and never raises — callers must keep using the existing Gemini/Serena
  investigation route regardless of this wrapper's outcome. The entire
  pipeline is wrapped by a single fallback boundary; the CLI entrypoint adds
  a further top-level guard so no exception ever escapes to the caller.
- This module never emits a new repo-wide result schema. It returns a plain
  advisory dict; the existing ``CODEBASE_INVESTIGATION_RESULT_V1`` /
  ``REPO_EVIDENCE_REF_V1`` contracts remain the caller's SSOT for findings.
  This wrapper's stdout/node/community output never generates a blocker,
  Allowed Paths, or a review verdict on its own.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Exact pinned package spec (AC4). Do not float this version.
PACKAGE_SPEC = "graphifyy==0.9.34"

# Expected ``graphify --version`` stdout for the pinned package spec above.
# Every action runs a preflight check against this string before launching;
# a mismatch degrades to ``status: unavailable`` / ``reason: version_mismatch``.
_EXPECTED_VERSION_STRING = "graphify 0.9.34"

# Allowlisted advisory actions (AC8). Nothing outside this set may ever be
# launched by this wrapper. Values are informational only — the actual argv
# shape for each action is constructed explicitly in ``build_argv`` (the
# Graphify 0.9.34 parser requires the positional path/args in a specific
# position relative to flags; see ``build_argv`` for details).
_ALLOWED_SUBCOMMANDS = {
    "extract": ["extract"],
    "query": ["query"],
    "path": ["path"],
    "explain": ["explain"],
    "version": ["--version"],
}

# Explicitly rejected subcommands/tokens, kept only for defensive validation
# and for the deterministic tests to assert against (informational; the
# allowlist above is the actual enforcement mechanism).
_FORBIDDEN_TOKENS = {
    "install",
    "uninstall",
    "hook",
    "watch",
    "prs",
    "clone",
    "merge-driver",
    "merge-graphs",
    "add",
    "update",
    "cluster-only",
    "label",
    "god-nodes",
    "save-result",
    "reflect",
    "check-update",
    "tree",
    "global",
    "benchmark",
    "export",
    "gemini",
    "cursor",
    "claude",
    "codebuddy",
    "codex",
    "opencode",
    "kilo",
    "aider",
    "copilot",
    "vscode",
    "claw",
    "droid",
    "trae",
    "trae-cn",
    "antigravity",
    "hermes",
    "kiro",
    "pi",
    "devin",
    "mcp",
    "serve",
}

_DEFAULT_QUERY_BUDGET = 500
_MIN_QUERY_BUDGET = 1
_MAX_QUERY_BUDGET = 5000
_DEFAULT_TIMEOUT_SEC = 120

_HEAD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_QUERY_LOG_ENV_KEYS_TO_STRIP = (
    "GRAPHIFY_QUERY_LOG",
    "GRAPHIFY_QUERY_LOG_ENABLE",
    "GRAPHIFY_QUERY_LOG_RESPONSES",
)

RunnerFn = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass
class GraphifyAdvisoryRequest:
    """Advisory request. ``action`` must be a key of ``_ALLOWED_SUBCOMMANDS``.

    There is intentionally no caller-supplied output/graph path field — the
    output/graph location is always derived from ``repo_root``/``head_sha``
    so a request can never escape the ``tmp/graphify/<head-sha>/`` confinement.
    """

    action: str
    repo_root: Path
    head_sha: str
    target_path: str | None = None  # for action == "extract"
    question: str | None = None  # for action == "query"
    budget: int = _DEFAULT_QUERY_BUDGET  # for action == "query"
    node_a: str | None = None  # for action == "path"
    node_b: str | None = None  # for action == "path"
    node: str | None = None  # for action == "explain"
    timeout_sec: float = _DEFAULT_TIMEOUT_SEC
    env_overrides: dict = field(default_factory=dict)


@dataclass
class GraphifyAdvisoryResult:
    """Advisory-only result. Never treated as a finding on its own (AC12)."""

    status: str  # "ok" | "unavailable"
    action: str
    reason: str | None = None
    argv: list[str] | None = None
    exit_code: int | None = None
    version: str | None = None
    stdout_excerpt: str | None = None
    stderr_excerpt: str | None = None
    output_dir: str | None = None
    fallback_to_existing_route: bool = True

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "action": self.action,
            "reason": self.reason,
            "argv": self.argv,
            "exit_code": self.exit_code,
            "version": self.version,
            "stdout_excerpt": self.stdout_excerpt,
            "stderr_excerpt": self.stderr_excerpt,
            "output_dir": self.output_dir,
            "fallback_to_existing_route": self.fallback_to_existing_route,
        }


def _unavailable(
    action: str,
    reason: str,
    argv: list[str] | None = None,
    version: str | None = None,
    exit_code: int | None = None,
    stderr_excerpt: str | None = None,
) -> GraphifyAdvisoryResult:
    return GraphifyAdvisoryResult(
        status="unavailable",
        action=action,
        reason=reason,
        argv=argv,
        version=version,
        exit_code=exit_code,
        stderr_excerpt=stderr_excerpt,
    )


def _output_dir(repo_root: Path, head_sha: str) -> Path:
    """Compute the sole allowed output location: tmp/graphify/<head-sha>/ (AC5).

    Never accepts a caller-supplied override — this is intentional so an
    Allowed Paths violation / output-path escape cannot happen via request
    input. By the time this is called, ``head_sha`` has already been
    verified to match ``^[0-9a-f]{40}$`` and the repository's actual HEAD,
    so no sanitization is needed here.
    """
    return repo_root / "tmp" / "graphify" / head_sha


def _graph_path(output_dir: Path) -> Path:
    return output_dir / "graphify-out" / "graph.json"


def _provenance_marker_path(output_dir: Path) -> Path:
    return output_dir / "provenance.json"


def is_worktree_dirty(repo_root: Path, timeout_sec: float = 10.0) -> bool:
    """Return True if the worktree is dirty or its clean status cannot be
    established (fail-closed — AC6). Uses ``git status --porcelain``.
    """
    git = shutil.which("git") or "git"
    try:
        result = subprocess.run(
            [git, "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True  # fail-closed: unknown status treated as dirty
    if result.returncode != 0:
        return True  # fail-closed
    return bool(result.stdout.strip())


def _resolve_repo_root(repo_root: Path, timeout_sec: float = 10.0) -> Path | None:
    """Resolve ``repo_root`` and require it to be the actual git top-level.

    Returns ``None`` (never raises) if ``repo_root`` does not exist, is not
    inside a git repository, or is not itself the repository top-level
    (rejects both non-toplevel ``repo_root`` and non-repo paths).
    """
    try:
        resolved = repo_root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None

    git = shutil.which("git") or "git"
    try:
        toplevel = subprocess.run(
            [git, "-C", str(resolved), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if toplevel.returncode != 0:
        return None

    try:
        toplevel_path = Path(toplevel.stdout.strip()).resolve(strict=True)
    except (OSError, RuntimeError):
        return None

    if toplevel_path != resolved:
        return None
    return resolved


def _verify_head_sha(repo_root: Path, head_sha: str, timeout_sec: float = 10.0) -> bool:
    """Require ``head_sha`` to be exactly 40 lowercase hex chars AND to match
    the repository's actual current ``HEAD`` (never just sanitized).
    """
    if not _HEAD_SHA_RE.match(head_sha):
        return False
    git = shutil.which("git") or "git"
    try:
        result = subprocess.run(
            [git, "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return result.stdout.strip() == head_sha


def _resolve_target_path(repo_root: Path, target_path: str) -> Path | None:
    """Resolve ``target_path`` and require it to be contained under
    ``repo_root``. Returns ``None`` (never raises) on any failure.
    """
    candidate = Path(target_path)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    try:
        resolved.relative_to(repo_root)
    except ValueError:
        return None
    return resolved


def _output_path_has_symlink_component(output_dir: Path, repo_root: Path) -> bool:
    """Reject (fail-closed) if any existing path component between
    ``repo_root`` and ``output_dir`` is a symlink, so ``mkdir(parents=True)``
    can never be tricked into following a symlink out of the repository.
    """
    try:
        rel_parts = output_dir.relative_to(repo_root).parts
    except ValueError:
        return True
    current = repo_root
    for part in rel_parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _validate_budget(value) -> int | None:
    """Clamp/reject ``--budget``. Rejects non-numeric or non-positive values;
    clamps values above the sane upper bound rather than passing them
    through raw.
    """
    try:
        budget = int(value)
    except (TypeError, ValueError):
        return None
    if budget < _MIN_QUERY_BUDGET:
        return None
    if budget > _MAX_QUERY_BUDGET:
        budget = _MAX_QUERY_BUDGET
    return budget


def build_argv(
    request: GraphifyAdvisoryRequest,
    output_dir: Path,
    resolved_target_path: Path | None = None,
    budget: int | None = None,
) -> list[str] | None:
    """Build the list-argv command for ``request.action``.

    Returns ``None`` if ``request.action`` is not in the allowlist, or if
    required action-specific inputs are missing/invalid — callers must
    treat that as an immediate rejection with no subprocess launch (AC8).

    Graphify 0.9.34's own argument parser treats the token immediately
    following the subcommand name as the positional argument; if that token
    starts with ``-`` (e.g. a flag), the positional is treated as
    unspecified and is NOT recovered from later args. For ``extract`` this
    means the positional target path MUST come immediately after
    ``extract`` and before ``--code-only`` (flags-before-positional silently
    drops the graph — see Issue #2009 PR #2010 review).
    """
    if request.action not in _ALLOWED_SUBCOMMANDS:
        return None

    uvx = shutil.which("uvx") or "uvx"
    argv = [uvx, "--from", PACKAGE_SPEC, "graphify"]
    action = request.action

    if action == "extract":
        if resolved_target_path is None:
            return None
        argv += ["extract", str(resolved_target_path), "--code-only", "--out", str(output_dir)]
    elif action == "query":
        if not request.question or budget is None:
            return None
        argv += ["query", request.question, "--budget", str(budget), "--graph", str(_graph_path(output_dir))]
    elif action == "path":
        if not request.node_a or not request.node_b:
            return None
        argv += ["path", request.node_a, request.node_b, "--graph", str(_graph_path(output_dir))]
    elif action == "explain":
        if not request.node:
            return None
        argv += ["explain", request.node, "--graph", str(_graph_path(output_dir))]
    elif action == "version":
        argv += ["--version"]

    return argv


def _default_runner(
    argv,
    env: dict,
    timeout_sec: float,
    cwd: Path | None = None,
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        shell=False,
    )


def _env_with_query_log_disabled(overrides: dict | None) -> dict:
    """Environment with query logging force-disabled.

    Overrides are applied FIRST, then the query-log enable/log-path
    variables are stripped, then ``GRAPHIFY_QUERY_LOG_DISABLE=1`` is set
    LAST and unconditionally — so no caller-supplied ``env_overrides`` can
    ever re-enable query logging (this ordering is load-bearing; see Issue
    #2009 PR #2010 review P1-5).
    """
    env = dict(os.environ)
    env.update(overrides or {})
    for key in _QUERY_LOG_ENV_KEYS_TO_STRIP:
        env.pop(key, None)
    env["GRAPHIFY_QUERY_LOG_DISABLE"] = "1"
    return env


def _write_provenance_marker(
    output_dir: Path,
    repo_root: Path,
    head_sha: str,
    target_path: Path,
    version: str,
) -> None:
    marker = {
        "repo_root": str(repo_root),
        "head_sha": head_sha,
        "target_path": str(target_path),
        "graphify_version": version,
    }
    _provenance_marker_path(output_dir).write_text(
        json.dumps(marker, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )


def _verify_provenance_marker(output_dir: Path, repo_root: Path, head_sha: str) -> bool:
    """Verify a previously written provenance marker still matches the
    current repo state (repo root, HEAD). A missing or mismatched marker is
    treated as an unusable graph — never as stale-but-usable.
    """
    marker_path = _provenance_marker_path(output_dir)
    if not marker_path.exists():
        return False
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(marker, dict):
        return False
    return marker.get("repo_root") == str(repo_root) and marker.get("head_sha") == head_sha


def _run_graphify_advisory_inner(
    request: GraphifyAdvisoryRequest,
    runner: RunnerFn,
) -> GraphifyAdvisoryResult:
    action = request.action

    if action not in _ALLOWED_SUBCOMMANDS:
        return _unavailable(action, "disallowed_subcommand")

    resolved_repo_root = _resolve_repo_root(request.repo_root)
    if resolved_repo_root is None:
        return _unavailable(action, "repo_root_invalid")

    if not _verify_head_sha(resolved_repo_root, request.head_sha):
        return _unavailable(action, "head_sha_mismatch")

    # AC6: dirty worktree short-circuits before any graph creation/use.
    if is_worktree_dirty(resolved_repo_root):
        return _unavailable(action, "dirty_worktree")

    output_dir = _output_dir(resolved_repo_root, request.head_sha)

    resolved_target_path: Path | None = None
    if action == "extract":
        if not request.target_path:
            return _unavailable(action, "missing_target_path")
        resolved_target_path = _resolve_target_path(resolved_repo_root, request.target_path)
        if resolved_target_path is None:
            return _unavailable(action, "target_path_outside_repo_root")

    # AC3: missing/stale graph is unavailable, checked before subprocess
    # launch for query/path/explain (they all require an existing,
    # provenance-verified graph.json).
    if action in {"query", "path", "explain"}:
        if not _graph_path(output_dir).exists():
            return _unavailable(action, "missing_graph")
        if not _verify_provenance_marker(output_dir, resolved_repo_root, request.head_sha):
            return _unavailable(action, "stale_provenance")

    budget: int | None = None
    if action == "query":
        budget = _validate_budget(request.budget)
        if budget is None:
            return _unavailable(action, "invalid_budget")

    if action == "extract":
        if _output_path_has_symlink_component(output_dir, resolved_repo_root):
            return _unavailable(action, "output_path_symlink")
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return _unavailable(action, "output_dir_unavailable")

    if shutil.which("uvx") is None:
        return _unavailable(action, "uvx_not_found")

    env = _env_with_query_log_disabled(request.env_overrides)
    uvx = shutil.which("uvx") or "uvx"

    # AC4: version preflight runs before ANY action is dispatched; the
    # observed version is always populated on the result, not only for
    # action == "version".
    preflight_argv = [uvx, "--from", PACKAGE_SPEC, "graphify", "--version"]
    try:
        preflight = runner(preflight_argv, env, request.timeout_sec, cwd=resolved_repo_root)
    except subprocess.TimeoutExpired:
        return _unavailable(action, "timeout", argv=preflight_argv)
    except OSError:
        return _unavailable(action, "launch_failed", argv=preflight_argv)

    observed_version = (preflight.stdout or "").strip()
    if preflight.returncode != 0 or observed_version != _EXPECTED_VERSION_STRING:
        return _unavailable(
            action,
            "version_mismatch",
            argv=preflight_argv,
            version=observed_version or None,
            exit_code=preflight.returncode,
        )

    if action == "version":
        return GraphifyAdvisoryResult(
            status="ok",
            action=action,
            argv=preflight_argv,
            exit_code=preflight.returncode,
            version=observed_version,
            stdout_excerpt=(preflight.stdout or "")[:2000],
        )

    argv = build_argv(request, output_dir, resolved_target_path, budget)
    if argv is None:
        return _unavailable(action, "disallowed_subcommand", version=observed_version)

    try:
        completed = runner(argv, env, request.timeout_sec, cwd=resolved_repo_root)
    except subprocess.TimeoutExpired:
        return _unavailable(action, "timeout", argv=argv, version=observed_version)
    except OSError:
        return _unavailable(action, "launch_failed", argv=argv, version=observed_version)

    if completed.returncode != 0:
        return _unavailable(
            action,
            "non_zero_exit",
            argv=argv,
            version=observed_version,
            exit_code=completed.returncode,
            stderr_excerpt=(completed.stderr or "")[:2000] or None,
        )

    stdout = completed.stdout or ""
    result = GraphifyAdvisoryResult(
        status="ok",
        action=action,
        argv=argv,
        exit_code=completed.returncode,
        version=observed_version,
        stdout_excerpt=stdout[:2000],
        output_dir=str(output_dir) if action == "extract" else None,
        fallback_to_existing_route=True,
    )

    if action == "extract" and resolved_target_path is not None:
        try:
            _write_provenance_marker(
                output_dir, resolved_repo_root, request.head_sha, resolved_target_path, observed_version
            )
        except OSError:
            pass  # advisory marker write failure does not invalidate the extract result itself

    return result


def run_graphify_advisory(
    request: GraphifyAdvisoryRequest,
    runner: RunnerFn = _default_runner,
) -> GraphifyAdvisoryResult:
    """Run a single allowlisted Graphify advisory action.

    Never raises: every failure mode maps to
    ``GraphifyAdvisoryResult(status="unavailable", ...)`` so a caller can
    always fall back to the existing Gemini/Serena investigation route
    (AC3). The entire pipeline (request validation, directory prep, argv
    construction, env construction, runner invocation, result
    serialization) is wrapped by this single fallback boundary; any
    exception not explicitly classified below is still converted to
    ``status: unavailable`` / ``reason: internal_error`` rather than
    propagating (P1-3 hardening).
    """
    action = request.action
    try:
        return _run_graphify_advisory_inner(request, runner)
    except subprocess.SubprocessError as exc:
        return _unavailable(action, "subprocess_error", stderr_excerpt=str(exc)[:2000])
    except (OSError, ValueError, TypeError) as exc:
        return _unavailable(action, "internal_error", stderr_excerpt=str(exc)[:2000])
    except Exception as exc:  # noqa: BLE001 - deliberate top-level fallback boundary (P1-3)
        return _unavailable(action, "internal_error", stderr_excerpt=str(exc)[:2000])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pinned Graphify CLI advisory prefilter wrapper (optional, read-only)."
    )
    parser.add_argument("--action", required=True, choices=sorted(_ALLOWED_SUBCOMMANDS))
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--target-path")
    parser.add_argument("--question")
    parser.add_argument("--budget", type=int, default=_DEFAULT_QUERY_BUDGET)
    parser.add_argument("--node-a")
    parser.add_argument("--node-b")
    parser.add_argument("--node")
    parser.add_argument("--timeout-sec", type=float, default=_DEFAULT_TIMEOUT_SEC)
    args = parser.parse_args(argv)

    # Top-level CLI fallback boundary: even request construction / dispatch
    # failures never raise a bare traceback out to the caller (P1-3).
    try:
        request = GraphifyAdvisoryRequest(
            action=args.action,
            repo_root=Path(args.repo_root),
            head_sha=args.head_sha,
            target_path=args.target_path,
            question=args.question,
            budget=args.budget,
            node_a=args.node_a,
            node_b=args.node_b,
            node=args.node,
            timeout_sec=args.timeout_sec,
        )
        result = run_graphify_advisory(request)
    except Exception as exc:  # noqa: BLE001 - deliberate top-level fallback boundary (P1-3)
        result = GraphifyAdvisoryResult(
            status="unavailable",
            action=getattr(args, "action", "unknown"),
            reason="internal_error",
            stderr_excerpt=str(exc)[:2000],
        )

    payload = json.dumps(result.to_dict(), ensure_ascii=True, indent=2)
    sys.stdout.write(payload)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
