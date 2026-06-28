#!/usr/bin/env python3
"""git_worktree_probe.py — read-only Git worktree catalog probe (Issue #1197).

Reuses ``worktree_catalog.parse_worktree_porcelain_z`` and ``list_worktrees``
so there is a single parser implementation. Outputs
``GIT_WORKTREE_PROBE_RESULT_V1`` JSON to stdout (one object only).
stderr is capped at 5 lines. No raw commands, env secrets, or absolute
paths are emitted to stderr.

Usage:
    uv run python3 scripts/agent-ops/git_worktree_probe.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_AGENT_OPS_DIR = Path(__file__).resolve().parent
if str(_AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_OPS_DIR))

from worktree_catalog import Deadline, list_worktrees  # noqa: E402

SCHEMA = "GIT_WORKTREE_PROBE_RESULT_V1"

# Maximum number of stderr lines emitted (AC4)
_MAX_STDERR_LINES = 5

_SECRET_LIKE_RE = re.compile(
    r"(/[^\s]+)|"               # absolute path
    r"([A-Za-z0-9+/]{40,}=*)",  # base64-like long token
)


def _redact(text: str) -> str:
    return _SECRET_LIKE_RE.sub("<redacted>", text)


def _bounded_stderr(lines: list[str]) -> None:
    for line in lines[:_MAX_STDERR_LINES]:
        print(_redact(line.rstrip()), file=sys.stderr)


def _enrich_entry(entry: dict) -> dict:
    """Add ``exists_on_disk`` and ``head`` (if missing) to a catalog entry."""
    enriched = dict(entry)
    wt_path = entry.get("worktree_realpath")
    enriched["exists_on_disk"] = bool(wt_path and os.path.isdir(wt_path))
    # ``head`` is populated by parse_worktree_porcelain_z when present.
    if "head" not in enriched:
        enriched["head"] = None
    return enriched


def probe(project_root: str | None = None) -> dict:
    """Run the worktree catalog probe and return GIT_WORKTREE_PROBE_RESULT_V1 dict."""
    if project_root is None:
        project_root = os.getcwd()
    project_root = os.path.realpath(project_root)

    deadline = Deadline(30.0)
    errors: list[str] = []

    catalog = list_worktrees(project_root, deadline=deadline)
    if catalog is None:
        errors.append("git worktree list failed or git not available")
        catalog = []

    entries = [_enrich_entry(e) for e in catalog]

    return {
        "schema": SCHEMA,
        "project_root": project_root,
        "entries": entries,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Git worktree catalog read-only probe")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output JSON (always true, kept for explicit invocation)")
    _args = parser.parse_args()

    result = probe()
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
