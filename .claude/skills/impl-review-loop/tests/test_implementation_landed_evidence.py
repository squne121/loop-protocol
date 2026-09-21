"""GIVEN/WHEN/THEN regression coverage for #2699 landing evidence."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / ".claude/skills/impl-review-loop/scripts/implementation_landed_evidence.py"
spec = importlib.util.spec_from_file_location("implementation_landed_evidence", SCRIPT)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

REPO = "squne121/loop-protocol"
ISSUE = 2119
SHA = "a" * 40


def _candidate(*, lifecycle="merged", provenance="closing_relation", ancestry=True, fresh=True, ownership=True):
    candidate = {
        "target": {"repo": REPO, "issue_number": ISSUE},
        "pr": {"number": 2137, "url": f"https://github.com/{REPO}/pull/2137", "head_sha": SHA},
        "provenance": {"kind": provenance, "verified": True},
        "lifecycle": lifecycle,
        "head_fresh": fresh,
        "current_scope_ownership": ownership,
    }
    if lifecycle == "merged":
        candidate.update({"merge_oid": SHA, "main_ancestry": {"verified": ancestry, "reachable": ancestry}})
    return candidate


def _evidence(*, candidates, coverage=None, freshness="fresh", contradictory=False, repo=REPO, issue=ISSUE):
    return {
        "schema": mod.EVIDENCE_SCHEMA,
        "schema_version": 1,
        "target": {
            "repo": repo,
            "issue_number": issue,
            "body_sha256": "sha256:" + "b" * 64,
        },
        "freshness": {"status": freshness, "observed_at": "2026-09-21T00:00:00Z", "max_age_seconds": 300},
        "contradictory": contradictory,
        "candidates": candidates,
        "scope_coverage": coverage,
    }


def _exact_coverage():
    return mod.normalize_scope_coverage({"ac": ["AC1", "AC2"]}, {"ac": ["AC2", "AC1"]})


def test_validator_rejects_missing_stale_and_identity_mismatched_evidence():
    """GIVEN malformed or stale evidence WHEN validated THEN it cannot land."""
    for raw in (None, _evidence(candidates=[], freshness="stale"), _evidence(candidates=[], repo="other/repo")):
        result = mod.derive_landing_disposition(raw, repo=REPO, issue_number=ISSUE)
        assert result["disposition"] == "reconciliation_required"


def test_scope_normalizer_distinguishes_exact_coverage_from_later_expansion():
    """GIVEN immutable and live scopes WHEN normalized THEN expansion is explicit."""
    exact = mod.normalize_scope_coverage({"ac": ["AC1"]}, {"ac": ["AC1"]})
    expanded = mod.normalize_scope_coverage({"ac": ["AC1"]}, {"ac": ["AC1", "AC2"]})
    assert exact["exact_coverage"] is True
    assert expanded["later_scope_expansion"] is True
    assert exact["immutable_merged_snapshot"]["content_sha256"] != expanded["live_current_scope"]["content_sha256"]


def test_disposition_precedence_reconciles_contradictory_insufficient_stale_and_identity_mismatch():
    """GIVEN unsafe evidence WHEN routed THEN reconciliation wins all lifecycle paths."""
    cases = [
        _evidence(candidates=[_candidate()], coverage=_exact_coverage(), contradictory=True),
        _evidence(candidates=[_candidate(ancestry=False)], coverage=_exact_coverage()),
        _evidence(candidates=[_candidate()], coverage=_exact_coverage(), freshness="stale"),
        _evidence(candidates=[_candidate()], coverage=_exact_coverage(), issue=9999),
    ]
    assert all(
        mod.derive_landing_disposition(case, repo=REPO, issue_number=ISSUE)["disposition"] == "reconciliation_required"
        for case in cases
    )


def test_open_draft_closed_unmerged_and_no_candidate_dispositions():
    """GIVEN candidate lifecycle states WHEN routed THEN only verified resumable PRs resume."""
    assert (
        mod.derive_landing_disposition(
            _evidence(candidates=[_candidate(lifecycle="open")]), repo=REPO, issue_number=ISSUE
        )["disposition"]
        == "existing_pr_resume"
    )
    assert (
        mod.derive_landing_disposition(
            _evidence(candidates=[_candidate(lifecycle="draft")]), repo=REPO, issue_number=ISSUE
        )["disposition"]
        == "existing_pr_resume"
    )
    assert (
        mod.derive_landing_disposition(
            _evidence(candidates=[_candidate(lifecycle="closed_unmerged")]), repo=REPO, issue_number=ISSUE
        )["disposition"]
        == "ordinary_dispatch_or_explicit_recovery"
    )
    assert (
        mod.derive_landing_disposition(_evidence(candidates=[]), repo=REPO, issue_number=ISSUE)["disposition"]
        == "ordinary_dispatch_or_explicit_recovery"
    )


def test_candidate_discovery_is_not_landing_authority():
    """GIVEN generic refs/title-like rows WHEN discovered THEN they never produce a candidate."""
    rows = [{"number": 1, "body": "Refs #2119", "state": "MERGED", "mergedAt": "x", "mergeCommit": {"oid": SHA}}]

    def run(argv):
        return 0, __import__("json").dumps(rows), ""

    evidence = mod.collect_candidate_inputs(repo=REPO, issue_number=ISSUE, current_scope={}, run_command=run)
    # A generic Refs form is not a verified cross-reference.
    assert evidence["candidates"] == []
    assert (
        mod.derive_landing_disposition(evidence, repo=REPO, issue_number=ISSUE)["disposition"]
        == "ordinary_dispatch_or_explicit_recovery"
    )


def test_2119_2137_non_closing_and_merged_closing_regressions():
    """GIVEN verified ancestry and exact scope WHEN relation differs THEN both qualify only with full evidence."""
    non_closing = _evidence(candidates=[_candidate(provenance="verified_cross_reference")], coverage=_exact_coverage())
    closing = _evidence(candidates=[_candidate(provenance="closing_relation")], coverage=_exact_coverage())
    assert (
        mod.derive_landing_disposition(non_closing, repo=REPO, issue_number=ISSUE)["disposition"]
        == "implementation_already_landed"
    )
    assert (
        mod.derive_landing_disposition(closing, repo=REPO, issue_number=ISSUE)["disposition"]
        == "implementation_already_landed"
    )


def test_lifecycle_and_scope_expansion_regressions():
    """GIVEN historical merged work and expanded scope WHEN routed THEN a new implementation remains allowed."""
    expanded = mod.normalize_scope_coverage({"ac": ["AC1"]}, {"ac": ["AC1", "AC9"]})
    result = mod.derive_landing_disposition(
        _evidence(candidates=[_candidate()], coverage=expanded), repo=REPO, issue_number=ISSUE
    )
    assert result["disposition"] == "ordinary_dispatch_or_explicit_recovery"


def test_candidate_discovery_dedupes_and_reconciles_conflicting_qualified_candidates():
    """AC7: two independently qualified PRs are never selected first-hit."""
    one = _candidate(lifecycle="open")
    two = _candidate(lifecycle="draft")
    result = mod.derive_landing_disposition(
        _evidence(candidates=[one, two]), repo=REPO, issue_number=ISSUE
    )
    assert result["disposition"] == "reconciliation_required"
    assert result["reason_codes"] == ["qualified_candidate_conflict"]

    # Closing relation wins over an independently verified non-closing source;
    # only same-authority multiplicity is contradictory.
    closing = _candidate(lifecycle="open", provenance="closing_relation")
    cross_ref = _candidate(lifecycle="draft", provenance="verified_cross_reference")
    result = mod.derive_landing_disposition(
        _evidence(candidates=[closing, cross_ref]), repo=REPO, issue_number=ISSUE
    )
    assert result["disposition"] == "existing_pr_resume"


def test_markerless_open_draft_candidate_allowed_paths_coverage_determines_resume_or_reconciliation():
    """AC6: legacy resumable branches need explicit current Allowed Paths coverage."""
    resumable = _candidate(lifecycle="open", fresh=True, ownership=True)
    blocked = _candidate(lifecycle="draft", fresh=True, ownership=False)
    assert mod.derive_landing_disposition(_evidence(candidates=[resumable]), repo=REPO, issue_number=ISSUE)["disposition"] == "existing_pr_resume"
    assert mod.derive_landing_disposition(_evidence(candidates=[blocked]), repo=REPO, issue_number=ISSUE)["disposition"] == "reconciliation_required"


def test_candidate_discovery_is_not_landing_authority_or_body_heuristic():
    """AC8: Refs/title text cannot masquerade as timeline PR provenance."""
    rows = [{"number": 1, "body": "Refs #2119", "state": "MERGED", "mergedAt": "x", "mergeCommit": {"oid": SHA}}]
    def run(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, __import__("json").dumps(rows), ""
        return 0, "[]", ""
    evidence = mod.collect_candidate_inputs(repo=REPO, issue_number=ISSUE, current_scope={}, run_command=run)
    assert evidence["candidates"] == []
