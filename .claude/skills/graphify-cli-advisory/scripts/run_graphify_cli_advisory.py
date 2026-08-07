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
- ``subprocess`` is always invoked with a list argv (no ``shell=True``).
- Output is confined to ``tmp/graphify/<head-sha>/`` under the repository
  root. No other location is ever written to.
- A dirty worktree short-circuits to an ``unavailable`` status *before* any
  Graphify graph is created or read (no graph creation/use on dirty trees).
- ``graphify query`` always passes an explicit ``--budget`` (never unbounded
  output).
- Any failure mode (launch failure, non-zero exit, timeout, missing graph,
  dirty worktree, disallowed subcommand) degrades to ``status: unavailable``
  and never raises — callers must keep using the existing Gemini/Serena
  investigation route regardless of this wrapper's outcome.
- This module never emits a new repo-wide result schema. It returns a plain
  advisory dict; the existing ``CODEBASE_INVESTIGATION_RESULT_V1`` /
  ``REPO_EVIDENCE_REF_V1`` contracts remain the caller's SSOT for findings.
  This wrapper's stdout/node/community output never generates a blocker,
  Allowed Paths, or a review verdict on its own.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

# Exact pinned package spec (AC4). Do not float this version.
PACKAGE_SPEC = "graphifyy==0.9.34"

# Allowlisted advisory actions -> underlying Graphify CLI subcommand tokens (AC8).
# Nothing outside this set may ever be launched by this wrapper.
_ALLOWED_SUBCOMMANDS = {
    "extract": ["extract", "--code-only"],
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
_DEFAULT_TIMEOUT_SEC = 120

RunnerFn = Callable[[Sequence[str], dict, float], "subprocess.CompletedProcess[str]"]


@dataclass
class GraphifyAdvisoryRequest:
    """Advisory request. ``action`` must be a key of ``_ALLOWED_SUBCOMMANDS``."""

    action: str
    repo_root: Path
    head_sha: str
    target_path: str | None = None  # for action == "extract"
    question: str | None = None  # for action == "query"
    budget: int = _DEFAULT_QUERY_BUDGET  # for action == "query"
    graph_path: Path | None = None  # for action in {"query", "path", "explain"}
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
            "output_dir": self.output_dir,
            "fallback_to_existing_route": self.fallback_to_existing_route,
        }


def _unavailable(action: str, reason: str, argv: list[str] | None = None) -> GraphifyAdvisoryResult:
    return GraphifyAdvisoryResult(status="unavailable", action=action, reason=reason, argv=argv)


def _output_dir(repo_root: Path, head_sha: str) -> Path:
    """Compute the sole allowed output location: tmp/graphify/<head-sha>/ (AC5).

    Never accepts a caller-supplied override — this is intentional so an
    Allowed Paths violation / output-path escape cannot happen via request
    input.
    """
    safe_sha = "".join(c for c in head_sha if c.isalnum()) or "unknown"
    return repo_root / "tmp" / "graphify" / safe_sha


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


def build_argv(request: GraphifyAdvisoryRequest, output_dir: Path) -> list[str] | None:
    """Build the list-argv command for ``request.action``.

    Returns ``None`` if ``request.action`` is not in the allowlist —
    callers must treat that as an immediate rejection with no subprocess
    launch (AC8).
    """
    if request.action not in _ALLOWED_SUBCOMMANDS:
        return None

    uvx = shutil.which("uvx") or "uvx"
    argv = [uvx, "--from", PACKAGE_SPEC, "graphify", *_ALLOWED_SUBCOMMANDS[request.action]]

    if request.action == "extract":
        if not request.target_path:
            return None
        argv += [request.target_path, "--out", str(output_dir)]
    elif request.action == "query":
        if not request.question:
            return None
        budget = int(request.budget) if request.budget else _DEFAULT_QUERY_BUDGET
        argv += [request.question, "--budget", str(budget)]
        if request.graph_path is not None:
            argv += ["--graph", str(request.graph_path)]
    elif request.action == "path":
        if not request.node_a or not request.node_b:
            return None
        argv += [request.node_a, request.node_b]
        if request.graph_path is not None:
            argv += ["--graph", str(request.graph_path)]
    elif request.action == "explain":
        if not request.node:
            return None
        argv += [request.node]
        if request.graph_path is not None:
            argv += ["--graph", str(request.graph_path)]
    elif request.action == "version":
        pass  # argv already complete: uvx --from <spec> graphify --version

    return argv


def _default_runner(argv: Sequence[str], env: dict, timeout_sec: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        env=env,
        shell=False,
    )


def _query_log_disabled_env() -> dict:
    """Environment with query logging force-disabled.

    ``GRAPHIFY_QUERY_LOG_DISABLE=1`` takes priority over any enable
    variable per upstream Graphify semantics; we also pop the enable
    variables defensively so this wrapper never opts into query logging.
    """
    import os

    env = dict(os.environ)
    env.pop("GRAPHIFY_QUERY_LOG_ENABLE", None)
    env.pop("GRAPHIFY_QUERY_LOG", None)
    env["GRAPHIFY_QUERY_LOG_DISABLE"] = "1"
    return env


def run_graphify_advisory(
    request: GraphifyAdvisoryRequest,
    runner: RunnerFn = _default_runner,
) -> GraphifyAdvisoryResult:
    """Run a single allowlisted Graphify advisory action.

    Never raises: every failure mode maps to
    ``GraphifyAdvisoryResult(status="unavailable", ...)`` so a caller can
    always fall back to the existing Gemini/Serena investigation route
    (AC3).
    """
    action = request.action

    if action not in _ALLOWED_SUBCOMMANDS:
        return _unavailable(action, "disallowed_subcommand")

    # AC6: dirty worktree short-circuits before any graph creation/use.
    if is_worktree_dirty(request.repo_root):
        return _unavailable(action, "dirty_worktree")

    output_dir = _output_dir(request.repo_root, request.head_sha)

    # AC3: missing graph is unavailable, checked before subprocess launch
    # for query/path/explain (they all require an existing graph.json).
    if action in {"query", "path", "explain"}:
        graph_path = request.graph_path or (output_dir / "graphify-out" / "graph.json")
        if not graph_path.exists():
            return _unavailable(action, "missing_graph")
        request = _with_graph_path(request, graph_path)

    if action == "extract":
        output_dir.mkdir(parents=True, exist_ok=True)

    argv = build_argv(request, output_dir)
    if argv is None:
        return _unavailable(action, "disallowed_subcommand")

    if shutil.which("uvx") is None:
        return _unavailable(action, "uvx_not_found", argv=argv)

    env = _query_log_disabled_env()
    env.update(request.env_overrides or {})

    try:
        completed = runner(argv, env, request.timeout_sec)
    except subprocess.TimeoutExpired:
        return _unavailable(action, "timeout", argv=argv)
    except OSError:
        return _unavailable(action, "launch_failed", argv=argv)

    if completed.returncode != 0:
        return _unavailable(action, "non_zero_exit", argv=argv)

    stdout = completed.stdout or ""
    result = GraphifyAdvisoryResult(
        status="ok",
        action=action,
        argv=argv,
        exit_code=completed.returncode,
        stdout_excerpt=stdout[:2000],
        output_dir=str(output_dir) if action == "extract" else None,
        fallback_to_existing_route=True,
    )
    if action == "version":
        result.version = stdout.strip()
    return result


def _with_graph_path(request: GraphifyAdvisoryRequest, graph_path: Path) -> GraphifyAdvisoryRequest:
    """Return a shallow copy of ``request`` with ``graph_path`` resolved."""
    from dataclasses import replace

    return replace(request, graph_path=graph_path)


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
    parser.add_argument("--graph-path")
    parser.add_argument("--node-a")
    parser.add_argument("--node-b")
    parser.add_argument("--node")
    parser.add_argument("--timeout-sec", type=float, default=_DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--output-file")
    args = parser.parse_args(argv)

    request = GraphifyAdvisoryRequest(
        action=args.action,
        repo_root=Path(args.repo_root),
        head_sha=args.head_sha,
        target_path=args.target_path,
        question=args.question,
        budget=args.budget,
        graph_path=Path(args.graph_path) if args.graph_path else None,
        node_a=args.node_a,
        node_b=args.node_b,
        node=args.node,
        timeout_sec=args.timeout_sec,
    )

    result = run_graphify_advisory(request)
    payload = json.dumps(result.to_dict(), ensure_ascii=True, indent=2)
    if args.output_file:
        Path(args.output_file).write_text(payload + "\n", encoding="utf-8")
    else:
        sys.stdout.write(payload)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
