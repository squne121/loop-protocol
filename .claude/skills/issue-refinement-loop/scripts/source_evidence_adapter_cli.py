#!/usr/bin/env python3
"""
Thin CLI adapter connecting the SOURCE_EVIDENCE_ACQUISITION_RESULT_V1
producer (`source_evidence_acquisition.run_acquisition()`) to real
collector executors and to the issue-refinement-loop consumer
(`route_source_evidence_result.validate_envelope()` /
`decide_routing_action()`), so that the #2195 machinery is actually
reachable from a single subprocess invocation instead of only being
exercised in-process by unit tests (#2195 PR #2315 review fix, mandate 4:
"実ワークフローへ接続されていない").

This intentionally stays a *thin* adapter: it does not implement route
selection, retry, or semantic evaluation itself -- it only wires the
already-existing producer/consumer functions together and persists the
run-scoped `cross_lane_recovery_budget` / no-redispatch ledger across
per-claim invocations via a caller-supplied `--state-file`.

Usage:
    uv run --locked python3 source_evidence_adapter_cli.py \\
        --request-file <path to REQUEST JSON> \\
        --state-file <path to run-scoped state JSON, created if absent> \\
        --output-file <path to write the RESULT JSON to>

Request JSON shape:
    {
      "run_id": "<string>",
      "claim": {
        "claim_id": "<string>",
        "claim_kind": "dispositive" | "supporting",
        "evidence_kind": "repo_blob_at_commit",
        "dependency_group": <string|null>,
        "baseline": {...opaque...},
        "commit_sha": "<40 or 64 char hex>",
        "path": "<repo-relative path>",
        "start_line": <int>,
        "end_line": <int>,
        "object_format": "sha1" | "sha256"  # optional, defaults to sha1
      },
      "owner": "<github owner>",             # optional, defaults to squne121
      "repo": "<github repo>",               # optional, defaults to loop-protocol
      "repo_root": "<local git worktree root>",  # optional, defaults to cwd
      "capability_snapshot": {"local_git": true, "github_blob": true},  # optional
      "budget": {"max_total": 1, "per_claim_max": 1}  # optional, only used to
                                                        # initialize a new state file
    }

Result JSON shape (written to --output-file and echoed to stdout):
    {
      "envelope": <SOURCE_EVIDENCE_ACQUISITION_RESULT_V1>,
      "validation": {"ok": bool, "errors": [str]},
      "routing_action": <decide_routing_action() output>
    }

Exit codes:
    0  envelope produced and schema-valid (regardless of disposition)
    1  request/state file error (usage error, fail-closed)
    2  envelope failed schema/binding validation (fail-closed; caller
       must not act on `envelope`/`routing_action` in this case)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_GEMINI_SCRIPTS_DIR = _SCRIPTS_DIR.parent.parent / "gemini-cli-headless-delegation" / "scripts"
for _p in (_SCRIPTS_DIR, _GEMINI_SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from route_source_evidence_result import (  # noqa: E402
    RecoveryBudget,
    decide_routing_action,
    reconcile_budget_consumption,
    validate_envelope,
)
from source_evidence_acquisition import (  # noqa: E402
    collect_github_blob_evidence,
    collect_local_git_evidence,
    run_acquisition,
)

DEFAULT_OWNER = "squne121"
DEFAULT_REPO = "loop-protocol"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_state(state_path: Path, *, default_budget: dict) -> dict:
    if state_path.exists():
        return _load_json(state_path)
    return {
        "dispatched_routes": [],
        "budget": {
            "max_total": default_budget.get("max_total", 1),
            "per_claim_max": default_budget.get("per_claim_max", 1),
            "_consumed_total": 0,
            "_consumed_by_claim": {},
        },
    }


def _budget_from_state(state: dict) -> RecoveryBudget:
    budget_state = state["budget"]
    budget = RecoveryBudget(
        max_total=budget_state["max_total"],
        per_claim_max=budget_state["per_claim_max"],
    )
    budget._consumed_total = budget_state.get("_consumed_total", 0)  # noqa: SLF001
    budget._consumed_by_claim = dict(budget_state.get("_consumed_by_claim", {}))  # noqa: SLF001
    return budget


def _state_from_budget(budget: RecoveryBudget, dispatched_routes: set) -> dict:
    return {
        "dispatched_routes": [list(key) for key in sorted(dispatched_routes)],
        "budget": {
            "max_total": budget.max_total,
            "per_claim_max": budget.per_claim_max,
            "_consumed_total": budget._consumed_total,  # noqa: SLF001
            "_consumed_by_claim": dict(budget._consumed_by_claim),  # noqa: SLF001
        },
    }


def build_executors(*, claim: dict, owner: str, repo: str, repo_root: Path) -> dict:
    """Build the real local_git / github_blob executors for `claim`. This
    is the piece that was previously only exercised via lambda stubs in
    unit tests -- it invokes the actual collectors, which in turn invoke
    real `git show` / `gh api --method GET` subprocess calls."""
    object_format = claim.get("object_format", "sha1")

    def _local_git():
        return collect_local_git_evidence(
            commit_sha=claim["commit_sha"],
            path=claim["path"],
            start_line=claim["start_line"],
            end_line=claim["end_line"],
            repo_root=repo_root,
            object_format=object_format,
            owner=owner,
            repo=repo,
        )

    def _github_blob():
        return collect_github_blob_evidence(
            commit_sha=claim["commit_sha"],
            path=claim["path"],
            start_line=claim["start_line"],
            end_line=claim["end_line"],
            object_format=object_format,
            owner=owner,
            repo=repo,
        )

    return {"local_git": _local_git, "github_blob": _github_blob}


def run_adapter(request: dict, state: dict) -> tuple[dict, dict]:
    """Pure(ish) core: given a parsed request and a parsed state dict,
    returns (result, new_state). Split out from `main()` for the
    subprocess smoke test to exercise without shelling out twice."""
    claim = request["claim"]
    run_id = request["run_id"]
    owner = request.get("owner", DEFAULT_OWNER)
    repo = request.get("repo", DEFAULT_REPO)
    repo_root = Path(request.get("repo_root", "."))

    executors = build_executors(claim=claim, owner=owner, repo=repo, repo_root=repo_root)

    budget = _budget_from_state(state)
    dispatched_routes = {tuple(entry) for entry in state.get("dispatched_routes", [])}

    envelope = run_acquisition(
        claim=claim,
        run_id=run_id,
        executors=executors,
        budget=budget,
        dispatched_routes=dispatched_routes,
        capability_snapshot=request.get("capability_snapshot"),
    )

    reconcile_budget_consumption(envelope, budget)

    validation = validate_envelope(
        envelope,
        expected_claim_id=claim.get("claim_id"),
        expected_evidence_kind=claim.get("evidence_kind"),
    )
    routing_action = decide_routing_action(envelope) if validation["ok"] else None

    result = {
        "envelope": envelope,
        "validation": validation,
        "routing_action": routing_action,
    }
    new_state = _state_from_budget(budget, dispatched_routes)
    return result, new_state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-file", required=True, type=Path)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--output-file", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        request = _load_json(args.request_file)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"failed to read request-file: {exc}"}), file=sys.stderr)
        return 1

    default_budget = request.get("budget", {"max_total": 1, "per_claim_max": 1})
    try:
        state = _load_state(args.state_file, default_budget=default_budget)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"failed to read state-file: {exc}"}), file=sys.stderr)
        return 1

    result, new_state = run_adapter(request, state)

    args.state_file.write_text(json.dumps(new_state, indent=2, sort_keys=True), encoding="utf-8")

    output = json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False)
    if args.output_file is not None:
        args.output_file.write_text(output, encoding="utf-8")
    print(output)

    return 0 if result["validation"]["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
