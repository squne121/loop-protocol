#!/usr/bin/env python3
"""apply_triage_label_transition.py — best-effort presentation-only label sync.

#2084: GitHub Issue labels are presentation-only / non-authoritative metadata
(SSOT: docs/dev/workflow.md, docs/dev/github-ops.md). This script performs
`triage-required` removal and `phase/implementation` / `agent/implementer`
addition strictly as a **best-effort presentation sync** executed *after* a
readiness decision has already been made by non-label evidence (native issue
state, dependency close state, CONTRACT_REVIEW_RESULT_V1 status, explicit
OWNER/operator directive).

This script MUST NOT be treated as a readiness gate. Its result
(`applied | noop | failed`) is telemetry only — it never changes
`status` / `routing_action` / `implementation_allowed` for any caller.

The existing `create_issue_txn.py reconcile --label` helper is add-only and
does not support label removal, hence this dedicated script.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any, Callable

_SCHEMA_NAME = "APPLY_TRIAGE_LABEL_TRANSITION_RESULT_V1"
_SCHEMA_VERSION = 1

DEFAULT_REMOVE_LABELS = ["triage-required"]
DEFAULT_ADD_LABELS = ["phase/implementation", "agent/implementer"]

RunFn = Callable[[list[str]], tuple[int, str, str]]


def _default_run(cmd: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def _fetch_current_labels(
    repo: str,
    issue_number: int,
    gh_bin: str,
    run_fn: RunFn,
) -> tuple[list[str] | None, str | None]:
    """Return (label_names, error_reason). error_reason is None on success."""
    argv = [gh_bin, "issue", "view", str(issue_number), "--repo", repo, "--json", "labels"]
    rc, stdout, stderr = run_fn(argv)
    if rc != 0:
        return None, f"gh_issue_view_failed: {stderr.strip()}"
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None, "gh_issue_view_invalid_json"
    labels = payload.get("labels") or []
    if not isinstance(labels, list):
        return None, "gh_issue_view_invalid_json"
    names = [str(item.get("name") or "") for item in labels if isinstance(item, dict)]
    return names, None


def apply_triage_label_transition(
    *,
    repo: str,
    issue_number: int,
    remove_labels: list[str] | None = None,
    add_labels: list[str] | None = None,
    gh_bin: str = "gh",
    run_fn: RunFn = _default_run,
) -> dict[str, Any]:
    """Perform a best-effort, idempotent presentation-only label sync.

    Returns an ``APPLY_TRIAGE_LABEL_TRANSITION_RESULT_V1`` dict. Never
    raises for expected gh CLI / network / permission failures — those are
    surfaced as ``result: failed`` with ``warnings`` populated. This
    function's return value MUST NOT be used to gate readiness,
    ``status``, ``routing_action``, or ``implementation_allowed`` by any
    caller (#2084).
    """
    remove_labels = list(remove_labels) if remove_labels is not None else list(DEFAULT_REMOVE_LABELS)
    add_labels = list(add_labels) if add_labels is not None else list(DEFAULT_ADD_LABELS)

    warnings: list[str] = []
    errors: list[str] = []

    current_labels, fetch_error = _fetch_current_labels(repo, issue_number, gh_bin, run_fn)
    if fetch_error is not None:
        warnings.append(fetch_error)
        return {
            "schema": _SCHEMA_NAME,
            "schema_version": _SCHEMA_VERSION,
            "repo": repo,
            "issue_number": issue_number,
            "result": "failed",
            "removed": [],
            "added": [],
            "unrelated_labels_preserved": [],
            "warnings": warnings,
            "errors": errors,
        }

    assert current_labels is not None
    current_set = set(current_labels)
    unrelated_labels_preserved = sorted(
        name for name in current_set if name not in set(remove_labels) and name not in set(add_labels)
    )

    to_remove = [name for name in remove_labels if name in current_set]
    to_add = [name for name in add_labels if name not in current_set]

    if not to_remove and not to_add:
        return {
            "schema": _SCHEMA_NAME,
            "schema_version": _SCHEMA_VERSION,
            "repo": repo,
            "issue_number": issue_number,
            "result": "noop",
            "removed": [],
            "added": [],
            "unrelated_labels_preserved": unrelated_labels_preserved,
            "warnings": warnings,
            "errors": errors,
        }

    applied_removed: list[str] = []
    applied_added: list[str] = []

    if to_remove:
        argv = [
            gh_bin,
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            repo,
            "--remove-label",
            ",".join(to_remove),
        ]
        rc, _stdout, stderr = run_fn(argv)
        if rc != 0:
            warnings.append(f"remove_label_failed: {stderr.strip()}")
        else:
            applied_removed = to_remove

    if to_add:
        argv = [
            gh_bin,
            "issue",
            "edit",
            str(issue_number),
            "--repo",
            repo,
            "--add-label",
            ",".join(to_add),
        ]
        rc, _stdout, stderr = run_fn(argv)
        if rc != 0:
            warnings.append(f"add_label_failed: {stderr.strip()}")
        else:
            applied_added = to_add

    expected_mutations = len(to_remove) + len(to_add)
    applied_mutations = len(applied_removed) + len(applied_added)
    if applied_mutations == 0:
        result = "failed"
    elif applied_mutations < expected_mutations:
        # Partial success is still surfaced as failed (fail-closed for the
        # telemetry payload) — callers must not infer completion from a
        # partial set of applied/added labels.
        result = "failed"
    else:
        result = "applied"

    return {
        "schema": _SCHEMA_NAME,
        "schema_version": _SCHEMA_VERSION,
        "repo": repo,
        "issue_number": issue_number,
        "result": result,
        "removed": applied_removed,
        "added": applied_added,
        "unrelated_labels_preserved": unrelated_labels_preserved,
        "warnings": warnings,
        "errors": errors,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--gh-bin", default="gh")
    parser.add_argument(
        "--remove-label",
        action="append",
        default=None,
        dest="remove_labels",
        help="Label to remove (repeatable). Defaults to triage-required.",
    )
    parser.add_argument(
        "--add-label",
        action="append",
        default=None,
        dest="add_labels",
        help="Label to add (repeatable). Defaults to phase/implementation, agent/implementer.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    result = apply_triage_label_transition(
        repo=args.repo,
        issue_number=args.issue_number,
        remove_labels=args.remove_labels,
        add_labels=args.add_labels,
        gh_bin=args.gh_bin,
    )
    print(json.dumps(result, ensure_ascii=False))
    # Exit 0 regardless of result value — this is best-effort telemetry, not
    # a gate. Callers that need to react to `failed` inspect the JSON.
    return 0


if __name__ == "__main__":
    sys.exit(main())
