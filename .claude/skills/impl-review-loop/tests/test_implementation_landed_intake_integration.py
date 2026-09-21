"""GIVEN/WHEN/THEN integration tests for the #2699 pre-Step-1 choke point."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
ROUTE = ROOT / ".claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py"
RUNTIME = ROOT / ".claude/skills/impl-review-loop/scripts/verify_implementation_landed_intake_runtime.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _evidence():
    return {
        "schema": "IMPLEMENTATION_LANDED_EVIDENCE_V1",
        "schema_version": 1,
        "target": {
            "repo": "squne121/loop-protocol",
            "issue_number": 2119,
            "body_sha256": "sha256:" + "b" * 64,
        },
        "freshness": {"status": "fresh", "observed_at": "2026-09-21T00:00:00Z", "max_age_seconds": 300},
        "contradictory": False,
        "candidates": [
            {
                "target": {"repo": "squne121/loop-protocol", "issue_number": 2119},
                "pr": {"number": 2137},
                "provenance": {"kind": "closing_relation", "verified": True},
                "lifecycle": "merged",
                "merge_oid": "a" * 40,
                "main_ancestry": {"verified": True, "reachable": True},
                "head_fresh": False,
                "current_scope_ownership": False,
            }
        ],
        "scope_coverage": {
            "schema": "IMPLEMENTATION_SCOPE_COVERAGE_V1",
            "immutable_merged_snapshot": {"content_sha256": "sha256:" + "c" * 64},
            "live_current_scope": {"content_sha256": "sha256:" + "c" * 64},
            "exact_coverage": True,
            "later_scope_expansion": False,
        },
    }


def test_disposition_precedes_worker_worktree_and_new_pr():
    """GIVEN landed or unsafe evidence WHEN pre-Step-1 routes THEN data-plane start is forbidden."""
    route = _load(ROUTE, "route_loop_verdict_v2_landing")
    landed = route.resolve_pre_step1_landing_disposition(_evidence(), repo="squne121/loop-protocol", issue_number=2119)
    unsafe = route.resolve_pre_step1_landing_disposition(None, repo="squne121/loop-protocol", issue_number=2119)
    landed_gate = route.resolve_pre_step1_data_plane_action(_evidence(), repo="squne121/loop-protocol", issue_number=2119)
    unsafe_gate = route.resolve_pre_step1_data_plane_action(None, repo="squne121/loop-protocol", issue_number=2119)
    assert landed["disposition"] == "implementation_already_landed"
    assert unsafe["disposition"] == "reconciliation_required"
    assert landed_gate == {"disposition": landed, "start_data_plane": False, "action": "suppress_worker_worktree_new_pr"}
    assert unsafe_gate["start_data_plane"] is False
    preparation = (ROOT / ".claude/skills/impl-review-loop/steps/preparation.md").read_text(encoding="utf-8")
    assert preparation.index("Evidence-Based Landing Disposition") < preparation.index("Already-Satisfied Early-Exit")
    assert "worker / worktree / new PR を開始せず" in preparation


def test_live_runtime_verifier_records_2119_2137_without_fixture_fallback(tmp_path):
    """GIVEN live-shaped read responses WHEN AC9 verifier runs THEN it records non-fallback evidence."""
    runtime = _load(RUNTIME, "implementation_landed_runtime")
    issue = {"number": 2119, "url": "https://github.com/squne121/loop-protocol/issues/2119", "body": "live body"}
    pr = {
        "number": 2137,
        "url": "https://github.com/squne121/loop-protocol/pull/2137",
        "state": "MERGED",
        "mergeCommit": {"oid": "a" * 40},
        "closingIssuesReferences": [],
    }
    responses = [(0, json.dumps(issue), ""), (0, json.dumps(pr), ""), (0, "ahead\n", "")]

    def run(_argv):
        return responses.pop(0)

    artifact = tmp_path / "artifacts" / "runtime-verification-AC9-test.log"
    payload, code = runtime.verify(artifact_path=artifact, run_command=run)
    assert code == 0
    assert payload["status"] == "PASS"
    assert payload["fallback_used"] is False
    assert artifact.exists()
