#!/usr/bin/env python3
"""Apply post-merge cleanup's trusted Task Context commit points (Issue #2565).

The caller supplies a fresh, already-fetched GraphQL merged-PR snapshot.  This
adapter performs no GitHub I/O, so snapshot acquisition remains outside the
Task Context SQLite transaction.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]
_CTL = _ROOT / "scripts" / "task-context" / "task_contextctl.py"


def _merged_evidence(snapshot: object, issue_number: int, pr_number: int) -> tuple[dict | None, str]:
    try:
        pr = snapshot["data"]["repository"]["pullRequest"]
        repo = snapshot["data"]["repository"]["nameWithOwner"]
        nodes = pr["closingIssuesReferences"]["nodes"]
        oid = pr["mergeCommit"]["oid"]
    except (KeyError, TypeError):
        return None, "RELATION_UNAVAILABLE"
    if pr.get("merged") is not True or pr.get("number") != pr_number or not isinstance(repo, str):
        return None, "MERGED_SNAPSHOT_INVALID"
    if not isinstance(nodes, list) or len(nodes) != 1 or not isinstance(nodes[0], dict) or nodes[0].get("number") != issue_number:
        return None, "RELATION_ISSUE_MISMATCH"
    if not isinstance(oid, str):
        return None, "MERGE_OID_INVALID"
    return {"repo": repo.lower(), "issue_number": issue_number, "pr_number": pr_number, "merge_commit_oid": oid}, "OK"


def _run(argv: list[str], body: dict) -> dict:
    proc = subprocess.run([sys.executable, str(_CTL), *argv], input=json.dumps(body), text=True, capture_output=True, timeout=15)
    if not proc.stdout.splitlines():
        return {"disposition": "deferred", "reason_code": "ADAPTER_UNAVAILABLE"}
    try:
        envelope = json.loads(proc.stdout.splitlines()[-1])
        return envelope.get("data", {}) if isinstance(envelope, dict) else {}
    except json.JSONDecodeError:
        return {"disposition": "deferred", "reason_code": "ADAPTER_UNAVAILABLE"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-file", type=Path, required=True)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--phase", choices=("merged", "completed"), required=True)
    args = parser.parse_args(argv)
    try:
        snapshot = json.loads(args.snapshot_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(json.dumps({"disposition": "deferred", "reason_code": "RELATION_UNAVAILABLE"}))
        return 0
    evidence, reason = _merged_evidence(snapshot, args.issue_number, args.pr_number)
    if evidence is None:
        print(json.dumps({"disposition": "deferred", "reason_code": reason}))
        return 0
    if args.phase == "merged":
        signal = {"signal_kind": "pr_merged_observed", "source": "post-merge-cleanup", "source_schema_version": "v1", "evidence": evidence}
        result = _run(["signal", "apply"], signal)
        if result.get("disposition") not in {"applied", "duplicate_noop"}:
            print(json.dumps(result))
            return 0
        begin = {"repo": evidence["repo"], "issue_number": evidence["issue_number"], "pr_number": evidence["pr_number"], "merge_identity": evidence["merge_commit_oid"]}
        result = _run(["cleanup", "begin"], {"schema_version": "task-context-request/v1", "operation": "cleanup_begin", "request_id": "post-merge-cleanup", "payload": begin})
        print(json.dumps(result))
        return 0
    completed_evidence = {
        "repo": evidence["repo"],
        "issue_number": evidence["issue_number"],
        "pr_number": evidence["pr_number"],
        "merge_identity": evidence["merge_commit_oid"],
    }
    completed = {"signal_kind": "cleanup_completed", "source": "post-merge-cleanup", "source_schema_version": "v1", "evidence": completed_evidence}
    result = _run(["signal", "apply"], completed)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
