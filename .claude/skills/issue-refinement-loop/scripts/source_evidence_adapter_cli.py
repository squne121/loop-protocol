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
per-claim invocations via a caller-supplied `--state-file`. The optional
#2889 C1 Step 1 handoff is a separate post-routing, read-only second mode;
its root-selected context and operator readback are not generated from the
candidate request, result, state or GitHub author metadata.

Usage:
    uv run --locked python3 source_evidence_adapter_cli.py \\
        --request-file <path to REQUEST JSON> \\
        --state-file <path to run-scoped state JSON, created if absent> \\
        --output-file <path to write the RESULT JSON to>

    # C1 initial (same command, with independent root-pinned inputs):
    ... --step1-context-file <pinned claim/main/target JSON> \
        --issue-body-file <fresh #2889 body text> --output-file <initial JSON>
    # C1 post-acquisition (same CLI, no collector/no state writes):
    ... --resolution-only --prior-result-file <initial JSON> \
        --expected-result-sha256 <SHA pinned by Step 1 BEFORE this invocation> \
        --step1-context-file <same pinned context> --issue-body-file <same body> \
        --operator-snapshot-file <root-selected with_human_context snapshot> \
        --operator-readback-file <root-fetched current comment readback>

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
      "routing_action": <decide_routing_action() output>,
      "state_sha256": <full persisted state byte SHA>,
      "initial_binding": <C1 run/claim/envelope byte SHA, initial and resolution>,
      "effective_step1_action": <C1 resolution-only action; base router unchanged>
    }

Exit codes:
    0  envelope produced and schema-valid (regardless of disposition)
    1  request/state file error (usage error, fail-closed)
    2  envelope failed schema/binding validation (fail-closed; caller
       must not act on `envelope`/`routing_action` in this case)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_GEMINI_SCRIPTS_DIR = _SCRIPTS_DIR.parent.parent / "gemini-cli-headless-delegation" / "scripts"
for _p in (_SCRIPTS_DIR, _GEMINI_SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from route_source_evidence_result import (  # noqa: E402
    C1_CLAIM,
    C1_PATH,
    C1_REPO,
    RecoveryBudget,
    c1_baseline,
    decide_effective_step1_action,
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


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encoded(value: dict) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")


def _utc(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("operator timestamp must have a timezone")
    return stamp.astimezone(timezone.utc)


def _trusted_c1_context(context_path: Path, issue_body_path: Path, repo_root: Path) -> tuple[dict, dict]:
    """Root-pinned Step 1 inputs: not request, receipt, envelope or worktree HEAD."""
    context = _load_json(context_path)
    issue_body = issue_body_path.read_text(encoding="utf-8")
    if context.get("repo") != C1_REPO or context.get("claim_id") != "C1" or context.get("claim_text") != C1_CLAIM:
        raise ValueError("independently pinned C1 claim/repository mismatch")
    if context.get("issue_number") != 2889 or context.get("issue_body_sha256") != _sha256(issue_body.encode("utf-8")):
        raise ValueError("fresh pinned Issue body snapshot mismatch")
    main = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/heads/main^{commit}"],
        cwd=repo_root,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    if context.get("canonical_main_sha") != main:
        raise ValueError("canonical main ref drift or missing independent main pin")
    target = context.get("c1_target")
    if not isinstance(target, dict) or target.get("repo") != C1_REPO or target.get("path") != C1_PATH:
        raise ValueError("independent C1 expected target mismatch")
    start, end = target.get("start_line"), target.get("end_line")
    if type(start) is not int or type(end) is not int or not (1 <= start <= end) or end - start > 30:
        raise ValueError("independent C1 expected line range invalid")
    source = subprocess.run(
        ["git", "show", f"{main}:{C1_PATH}"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    if end > len(source) or not all(
        term in b"\n".join(source[start - 1 : end])
        for term in (b"latest_main_net_diff", b"allowed_paths_conflict", b"allowed_paths")
    ):
        raise ValueError("C1 target is not the canonical production condition on main")
    return c1_baseline(issue_body=issue_body, main_sha=main), target


def _operator_decision(snapshot_path: Path, readback_path: Path, *, persisted_at: str) -> tuple[dict, dict]:
    """Consume only root-selected with_human_context snapshot + drift readback.

    The root operator, not this CLI, chooses the lane and acquires both
    snapshots. Metadata (including author association) alone grants nothing.
    """
    snapshot = _load_json(snapshot_path)
    readback = _load_json(readback_path)
    fields = ("issue_number", "comment_id", "user_id", "body", "updated_at")
    if snapshot.get("lane") != "with_human_context" or snapshot.get("issue_number") != 2889:
        raise ValueError("operator-selected with_human_context Issue snapshot required")
    if (
        not isinstance(snapshot.get("comment_id"), int)
        or type(snapshot.get("user_id")) is not int
        or snapshot["user_id"] <= 0
    ):
        raise ValueError("operator snapshot stable identity missing")
    if any(snapshot.get(key) != readback.get(key) for key in fields):
        raise ValueError("operator snapshot drift or Issue/identity mismatch")
    body = snapshot.get("body")
    if (
        not isinstance(body, str)
        or snapshot.get("body_sha256") != _sha256(body.encode("utf-8"))
        or readback.get("body_sha256") != snapshot["body_sha256"]
    ):
        raise ValueError("operator body hash mismatch")
    if _utc(snapshot["updated_at"]) <= _utc(persisted_at):
        raise ValueError("operator confirmation predates initial acquisition")
    decision = json.loads(body)
    if (
        not isinstance(decision, dict)
        or _utc(decision["recorded_at"]) <= _utc(persisted_at)
        or _utc(decision["recorded_at"]) > _utc(snapshot["updated_at"])
    ):
        raise ValueError("operator decision not explicitly recorded after initial acquisition")
    return decision, {
        "issue_number": snapshot["issue_number"],
        "comment_id": snapshot["comment_id"],
        "user_id": snapshot["user_id"],
        "body_sha256": snapshot["body_sha256"],
        "updated_at": snapshot["updated_at"],
    }


def _resolve_only(args: argparse.Namespace, request: dict, repo_root: Path) -> dict:
    """Read-only second invocation: no collector, acquisition, state writes or dispatch."""
    if any(
        getattr(args, name) is None
        for name in (
            "prior_result_file",
            "expected_result_sha256",
            "step1_context_file",
            "issue_body_file",
            "operator_snapshot_file",
            "operator_readback_file",
        )
    ):
        raise ValueError(
            "resolution requires independent result pin, Step 1 context, Issue snapshot and operator readback"
        )
    if args.output_file and args.output_file.resolve() in (args.state_file.resolve(), args.prior_result_file.resolve()):
        raise ValueError("resolution output must not replace pinned result or state")
    baseline, target = _trusted_c1_context(args.step1_context_file, args.issue_body_file, repo_root)
    result_bytes = args.prior_result_file.read_bytes()
    if _sha256(result_bytes) != args.expected_result_sha256:
        raise ValueError("independent Step 1 expected result byte SHA mismatch")
    prior = json.loads(result_bytes)
    state_bytes = args.state_file.read_bytes()  # never synthesize state on resolution
    if not isinstance(prior, dict) or prior.get("state_sha256") != _sha256(state_bytes):
        raise ValueError("initial result state full byte SHA mismatch")
    state = json.loads(state_bytes)
    envelope = prior["envelope"]
    envelope_digest = _sha256(_encoded(envelope))
    run_id, claim_id = request["run_id"], request["claim"]["claim_id"]
    binding = {"run_id": run_id, "claim_id": claim_id, "envelope_sha256": envelope_digest}
    if (
        claim_id != "C1"
        or prior.get("initial_binding") != binding
        or binding not in state.get("resolution_bindings", [])
        or prior.get("validation", {}).get("ok") is not True
        or prior.get("routing_action") != decide_routing_action(envelope)
    ):
        raise ValueError("initial result/state/run/claim/envelope binding mismatch")
    if request["claim"].get("baseline") != baseline:
        raise ValueError("request baseline does not match independently pinned Issue/claim/main")
    if (
        request["claim"].get("commit_sha") != baseline["current_main_sha"]
        or request["claim"].get("path") != C1_PATH
        or request["claim"].get("start_line") != target["start_line"]
        or request["claim"].get("end_line") != target["end_line"]
    ):
        raise ValueError("request target does not match independently pinned C1 target")
    decision, receipt = _operator_decision(
        args.operator_snapshot_file,
        args.operator_readback_file,
        persisted_at=prior["persisted_at"],
    )
    overlay = decide_effective_step1_action(
        envelope,
        expected_baseline=baseline,
        target=target,
        repo_root=repo_root,
        operator_decision=decision,
        run_id=run_id,
        envelope_sha256=envelope_digest,
    )
    validation = validate_envelope(
        envelope,
        expected_claim_id="C1",
        expected_evidence_kind="repo_blob_at_commit",
        expected_baseline=baseline,
    )
    validation["errors"].extend(overlay["errors"])
    validation["ok"] = not validation["errors"]
    return {
        "envelope": envelope,
        "validation": validation,
        "routing_action": decide_routing_action(envelope),
        "effective_step1_action": overlay["effective_step1_action"],
        "claim_resolution": overlay["claim_resolution"],
        "initial_binding": binding,
        "operator_receipt": receipt,
        "state_sha256": _sha256(state_bytes),
        "initial_result_sha256": _sha256(result_bytes),
        "runtime_counts": {"run_acquisition": 0, "collector_dispatch": 0},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-file", required=True, type=Path)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--output-file", type=Path, default=None)
    parser.add_argument("--step1-context-file", type=Path, help="independent root-pinned C1 context")
    parser.add_argument("--issue-body-file", type=Path, help="fresh pinned #2889 Issue body")
    parser.add_argument("--resolution-only", action="store_true")
    parser.add_argument("--prior-result-file", type=Path)
    parser.add_argument("--expected-result-sha256")
    parser.add_argument("--operator-snapshot-file", type=Path)
    parser.add_argument("--operator-readback-file", type=Path)
    args = parser.parse_args(argv)

    try:
        request = _load_json(args.request_file)
        repo_root = Path(request.get("repo_root", "."))
        if args.resolution_only:
            result = _resolve_only(args, request, repo_root)
        else:
            if args.operator_snapshot_file or args.operator_readback_file or args.prior_result_file:
                raise ValueError("operator judgment is not accepted on initial acquisition")
            c1 = args.step1_context_file is not None or args.issue_body_file is not None
            if c1:
                if not args.step1_context_file or not args.issue_body_file or args.output_file is None:
                    raise ValueError("C1 initial acquisition requires independent context, Issue snapshot, result file")
                if args.output_file.resolve() == args.state_file.resolve():
                    raise ValueError("initial result and state must be distinct files")
                baseline, target = _trusted_c1_context(args.step1_context_file, args.issue_body_file, repo_root)
                claim = request["claim"]
                if (
                    request.get("owner", DEFAULT_OWNER) + "/" + request.get("repo", DEFAULT_REPO) != C1_REPO
                    or claim.get("claim_id") != "C1"
                    or claim.get("baseline") != baseline
                    or claim.get("commit_sha") != baseline["current_main_sha"]
                    or claim.get("path") != C1_PATH
                    or claim.get("start_line") != target["start_line"]
                    or claim.get("end_line") != target["end_line"]
                ):
                    raise ValueError("initial request differs from independently pinned C1 context")
            state = _load_state(
                args.state_file,
                default_budget=request.get("budget", {"max_total": 1, "per_claim_max": 1}),
            )
            result, new_state = run_adapter(request, state)
            result["runtime_counts"] = {
                "run_acquisition": 1,
                "collector_dispatch": len(result["envelope"].get("attempts", [])),
            }
            if c1:
                envelope = result["envelope"]
                validation = validate_envelope(
                    envelope,
                    expected_claim_id="C1",
                    expected_evidence_kind="repo_blob_at_commit",
                    expected_baseline=baseline,
                )
                result["validation"] = validation
                if (
                    not validation["ok"]
                    or envelope.get("semantic_verdict") != "not_evaluated"
                    or envelope.get("disposition") != "human_review"
                    or result["routing_action"]["action"] != "human_review"
                    or len(envelope.get("evidence_refs", [])) != 1
                    or envelope["evidence_refs"][0].get("verification_status") != "verified"
                ):
                    raise ValueError("initial C1 acquisition did not yield verified human_review envelope")
                binding = {
                    "run_id": request["run_id"],
                    "claim_id": "C1",
                    "envelope_sha256": _sha256(_encoded(envelope)),
                }
                new_state["resolution_bindings"] = [*state.get("resolution_bindings", []), binding]
                result["initial_binding"] = binding
                result["persisted_at"] = datetime.now(timezone.utc).isoformat()
            state_bytes = _encoded(new_state)
            result["state_sha256"] = _sha256(state_bytes)
            args.state_file.write_bytes(state_bytes)
        output = _encoded(result)
        if args.output_file is not None:
            args.output_file.write_bytes(output)
        print(output.decode("utf-8"))
        return 0 if result["validation"]["ok"] else 2
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
