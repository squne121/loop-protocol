"""GIVEN/WHEN/THEN regression coverage for #2699 landing evidence."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / ".claude/skills/impl-review-loop/scripts/implementation_landed_evidence.py"
spec = importlib.util.spec_from_file_location("implementation_landed_evidence", SCRIPT)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ROUTE_SCRIPT = ROOT / ".claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py"
route_spec = importlib.util.spec_from_file_location("route_loop_verdict_v2_for_landed_evidence_test", ROUTE_SCRIPT)
assert route_spec and route_spec.loader
route_mod = importlib.util.module_from_spec(route_spec)
sys.modules[route_spec.name] = route_mod
route_spec.loader.exec_module(route_mod)

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


def _scope_manifest_issue_body(
    *, reversed_lists: bool = False, operational: str = "progress one", ac_checked: bool = False
) -> str:
    """An Issue body containing both semantic fields (In Scope / Acceptance
    Criteria / Allowed Paths) and the operational-prose / progress-state /
    runtime-evidence sections `build_scope_manifest()` must exclude."""
    in_scope = "- build intake\n- publish marker" if not reversed_lists else "- publish marker\n- build intake"
    ac1_box = "[x]" if ac_checked else "[ ]"
    ac2_box = "[ ]" if ac_checked else "[x]"
    acs = (
        f"- {ac2_box} AC2: marker persists\n- {ac1_box} AC1: scope normalizes"
        if not reversed_lists
        else f"- {ac1_box} AC1: scope normalizes\n- {ac2_box} AC2: marker persists"
    )
    paths = "- `.claude/a.py`\n- `.claude/b.py`" if not reversed_lists else "- `.claude/b.py`\n- `.claude/a.py`"
    return f"""## Machine-Readable Contract
```yaml
goal_ref: marker goal
change_kind: workflow
```
## In Scope
{in_scope}
## Acceptance Criteria
{acs}
## Allowed Paths
{paths}
## Remaining Parent Gaps
- {operational}
## Runtime Evidence
- changing this does not alter semantic scope
"""


def test_scope_normalizer_excludes_operational_prose_and_progress_state():
    """AC2: the canonical normalizer includes only the "Canonical
    Implementation Scope Normalizer" semantic fields (goal_ref / change_kind
    / In Scope / Acceptance Criteria text / Allowed Paths), excludes
    operational prose / checkbox state / runtime evidence / progress text,
    and is order-independent (sorted) per list -- none of these alone may
    change the digest, while a genuine semantic change must."""
    baseline = mod.canonicalize_scope_manifest(
        mod.build_scope_manifest(_scope_manifest_issue_body(operational="first update"))
    )

    # Checkbox state ([ ] vs [x], per item) alone must not change the digest.
    checkbox_flip = mod.canonicalize_scope_manifest(
        mod.build_scope_manifest(_scope_manifest_issue_body(operational="first update", ac_checked=True))
    )
    assert checkbox_flip["content_sha256"] == baseline["content_sha256"]

    # "## Remaining Parent Gaps" operational/progress prose alone must not
    # change the digest.
    operational_only = mod.canonicalize_scope_manifest(
        mod.build_scope_manifest(_scope_manifest_issue_body(operational="a completely different later update"))
    )
    assert operational_only["content_sha256"] == baseline["content_sha256"]

    # Recording order of the In Scope / Acceptance Criteria / Allowed Paths
    # bullets alone must not change the digest (order-independent sort
    # normalization).
    reordered = mod.canonicalize_scope_manifest(
        mod.build_scope_manifest(_scope_manifest_issue_body(reversed_lists=True, operational="first update"))
    )
    assert reordered["content_sha256"] == baseline["content_sha256"]

    # "## Runtime Evidence" content is excluded entirely.
    body_with_extra_runtime_evidence = (
        _scope_manifest_issue_body(operational="first update") + "- another runtime detail\n"
    )
    runtime_extra = mod.canonicalize_scope_manifest(mod.build_scope_manifest(body_with_extra_runtime_evidence))
    assert runtime_extra["content_sha256"] == baseline["content_sha256"]

    # Sanity: a genuine semantic change (a new Allowed Paths entry) DOES
    # change the digest -- the exclusions above are not vacuously trivial.
    manifest = mod.build_scope_manifest(_scope_manifest_issue_body(operational="first update"))
    manifest["allowed_paths"] = manifest["allowed_paths"] + [".claude/c.py"]
    changed = mod.canonicalize_scope_manifest(manifest)
    assert changed["content_sha256"] != baseline["content_sha256"]

    # Only the semantic fields the normalizer section lists are present.
    assert set(mod.build_scope_manifest(_scope_manifest_issue_body()).keys()) == {
        "schema_version",
        "goal_ref",
        "change_kind",
        "in_scope",
        "acceptance_criteria",
        "allowed_paths",
    }


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
    result = mod.derive_landing_disposition(_evidence(candidates=[one, two]), repo=REPO, issue_number=ISSUE)
    assert result["disposition"] == "reconciliation_required"
    assert result["reason_codes"] == ["qualified_candidate_conflict"]

    # Closing relation wins over an independently verified non-closing source;
    # only same-authority multiplicity is contradictory.
    closing = _candidate(lifecycle="open", provenance="closing_relation")
    cross_ref = _candidate(lifecycle="draft", provenance="verified_cross_reference")
    result = mod.derive_landing_disposition(_evidence(candidates=[closing, cross_ref]), repo=REPO, issue_number=ISSUE)
    assert result["disposition"] == "existing_pr_resume"


def test_markerless_open_draft_candidate_allowed_paths_coverage_determines_resume_or_reconciliation():
    """AC6: legacy resumable branches need explicit current Allowed Paths coverage."""
    resumable = _candidate(lifecycle="open", fresh=True, ownership=True)
    blocked = _candidate(lifecycle="draft", fresh=True, ownership=False)
    assert (
        mod.derive_landing_disposition(_evidence(candidates=[resumable]), repo=REPO, issue_number=ISSUE)["disposition"]
        == "existing_pr_resume"
    )
    assert (
        mod.derive_landing_disposition(_evidence(candidates=[blocked]), repo=REPO, issue_number=ISSUE)["disposition"]
        == "reconciliation_required"
    )


def test_candidate_discovery_is_not_landing_authority_or_body_heuristic():
    """AC8: Refs/title text cannot masquerade as timeline PR provenance."""
    rows = [{"number": 1, "body": "Refs #2119", "state": "MERGED", "mergedAt": "x", "mergeCommit": {"oid": SHA}}]

    def run(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, __import__("json").dumps(rows), ""
        return 0, "[]", ""

    evidence = mod.collect_candidate_inputs(repo=REPO, issue_number=ISSUE, current_scope={}, run_command=run)
    assert evidence["candidates"] == []


def test_disposition_precedence_reuses_already_satisfied_before_landed_evidence():
    """AC4: a no-landing-authority result (no qualified candidate, or only
    closed-unmerged candidates survive qualification) falls back to #2607's
    existing already_satisfied early-exit instead of a new enum. Every other
    disposition (reconciliation_required / implementation_already_landed /
    existing_pr_resume / any other ordinary_dispatch reason) is untouched --
    Disposition Precedence step 1 always wins over step 2."""
    no_candidate = mod.derive_landing_disposition(_evidence(candidates=[]), repo=REPO, issue_number=ISSUE)
    assert no_candidate["reason_codes"] == ["no_qualified_candidate"]

    closed_unmerged = mod.derive_landing_disposition(
        _evidence(candidates=[_candidate(lifecycle="closed_unmerged")]), repo=REPO, issue_number=ISSUE
    )
    assert closed_unmerged["reason_codes"] == ["closed_unmerged_candidate"]

    for landing_result in (no_candidate, closed_unmerged):
        composed = mod.apply_already_satisfied_precedence(
            landing_result,
            next_action_route="proceed_to_step_1",
            product_spec_routing_action="continue",
            pr_exists=False,
            base_ac_satisfied=True,
            route_loop_verdict_v2_module=route_mod,
        )
        assert composed["disposition"] == "already_satisfied"
        # Reuses route_loop_verdict_v2's existing production decision
        # function rather than re-implementing the early-exit判定 (#2607).
        assert composed["already_satisfied_decision"] == route_mod.resolve_already_satisfied_early_exit_decision(
            next_action_route="proceed_to_step_1",
            product_spec_routing_action="continue",
            pr_exists=False,
            base_ac_satisfied=True,
        )

    # base_ac_satisfied False must not fire the fallback.
    untouched = mod.apply_already_satisfied_precedence(
        no_candidate,
        next_action_route="proceed_to_step_1",
        product_spec_routing_action="continue",
        pr_exists=False,
        base_ac_satisfied=False,
        route_loop_verdict_v2_module=route_mod,
    )
    assert untouched == no_candidate

    # Step 1 always wins: reconciliation_required is never overridden, even
    # when base_ac_satisfied is independently true.
    reconciliation = mod.derive_landing_disposition(None, repo=REPO, issue_number=ISSUE)
    passthrough = mod.apply_already_satisfied_precedence(
        reconciliation,
        next_action_route="proceed_to_step_1",
        product_spec_routing_action="continue",
        pr_exists=False,
        base_ac_satisfied=True,
        route_loop_verdict_v2_module=route_mod,
    )
    assert passthrough == reconciliation

    # implementation_already_landed / existing_pr_resume are also untouched.
    landed = mod.derive_landing_disposition(
        _evidence(candidates=[_candidate(lifecycle="merged")], coverage=_exact_coverage()),
        repo=REPO,
        issue_number=ISSUE,
    )
    resume = mod.derive_landing_disposition(
        _evidence(candidates=[_candidate(lifecycle="open")]), repo=REPO, issue_number=ISSUE
    )
    for result in (landed, resume):
        assert (
            mod.apply_already_satisfied_precedence(
                result,
                next_action_route="proceed_to_step_1",
                product_spec_routing_action="continue",
                pr_exists=False,
                base_ac_satisfied=True,
                route_loop_verdict_v2_module=route_mod,
            )
            == result
        )


def test_legacy_merged_candidate_without_durable_marker_never_yields_implementation_already_landed():
    """AC5: a merged candidate with a missing durable marker (legacy PR,
    e.g. #2137) must never yield implementation_already_landed, for either
    provenance kind, and must not silently reconstruct merge-time scope from
    the current body."""
    missing_marker_coverage = mod.coverage_from_pr_body(
        pr_body="no marker here", issue_number=ISSUE, live_issue_body="## Allowed Paths\n- `.claude/a.py`\n"
    )
    assert missing_marker_coverage["status"] == "missing_marker"

    for provenance in ("closing_relation", "verified_cross_reference"):
        candidate = _candidate(lifecycle="merged", provenance=provenance)
        evidence = _evidence(candidates=[candidate], coverage=missing_marker_coverage)
        result = mod.derive_landing_disposition(evidence, repo=REPO, issue_number=ISSUE)
        assert result["disposition"] != "implementation_already_landed"
        assert result["disposition"] == "ordinary_dispatch_or_explicit_recovery"
        assert result["reason_codes"] == ["legacy_or_later_scope_expansion"]


def test_2119_2137_legacy_and_durable_marker_fixture_regressions():
    """AC10: #2119/PR#2137-shaped fixtures pin every required lifecycle
    disposition, including the legacy (marker-missing) vs durable-marker
    merged-closing distinction, scope expansion, open/draft resume (marker
    and markerless), closed-unmerged, no-candidate, contradictory,
    insufficient evidence, and a malformed merged marker."""
    legacy_missing_marker = mod.coverage_from_pr_body(pr_body="", issue_number=ISSUE, live_issue_body="body")
    legacy = _evidence(
        candidates=[_candidate(lifecycle="merged", provenance="closing_relation")], coverage=legacy_missing_marker
    )
    assert (
        mod.derive_landing_disposition(legacy, repo=REPO, issue_number=ISSUE)["disposition"]
        == "ordinary_dispatch_or_explicit_recovery"
    )

    durable = _evidence(
        candidates=[_candidate(lifecycle="merged", provenance="closing_relation")], coverage=_exact_coverage()
    )
    assert (
        mod.derive_landing_disposition(durable, repo=REPO, issue_number=ISSUE)["disposition"]
        == "implementation_already_landed"
    )

    expansion = mod.normalize_scope_coverage({"ac": ["AC1"]}, {"ac": ["AC1", "AC9"]})
    expanded = _evidence(candidates=[_candidate(lifecycle="merged")], coverage=expansion)
    assert (
        mod.derive_landing_disposition(expanded, repo=REPO, issue_number=ISSUE)["disposition"]
        == "ordinary_dispatch_or_explicit_recovery"
    )

    open_marker = _evidence(candidates=[_candidate(lifecycle="open")], coverage=_exact_coverage())
    assert (
        mod.derive_landing_disposition(open_marker, repo=REPO, issue_number=ISSUE)["disposition"]
        == "existing_pr_resume"
    )

    open_markerless = _evidence(candidates=[_candidate(lifecycle="open", ownership=True, fresh=True)])
    assert (
        mod.derive_landing_disposition(open_markerless, repo=REPO, issue_number=ISSUE)["disposition"]
        == "existing_pr_resume"
    )

    draft_markerless_blocked = _evidence(candidates=[_candidate(lifecycle="draft", ownership=False, fresh=True)])
    assert (
        mod.derive_landing_disposition(draft_markerless_blocked, repo=REPO, issue_number=ISSUE)["disposition"]
        == "reconciliation_required"
    )

    closed_unmerged = _evidence(candidates=[_candidate(lifecycle="closed_unmerged")])
    assert (
        mod.derive_landing_disposition(closed_unmerged, repo=REPO, issue_number=ISSUE)["disposition"]
        == "ordinary_dispatch_or_explicit_recovery"
    )

    no_candidate = _evidence(candidates=[])
    assert (
        mod.derive_landing_disposition(no_candidate, repo=REPO, issue_number=ISSUE)["disposition"]
        == "ordinary_dispatch_or_explicit_recovery"
    )

    contradictory = _evidence(
        candidates=[_candidate(lifecycle="merged")], coverage=_exact_coverage(), contradictory=True
    )
    assert (
        mod.derive_landing_disposition(contradictory, repo=REPO, issue_number=ISSUE)["disposition"]
        == "reconciliation_required"
    )

    assert (
        mod.derive_landing_disposition(None, repo=REPO, issue_number=ISSUE)["disposition"]
        == "reconciliation_required"
    )

    # #2699 P1-1: a malformed merged-candidate marker is reconciliation_required
    # regardless of lifecycle, ahead of the ancestry/exact-coverage checks.
    malformed_merged_marker = {"status": "invalid", "errors": ["scope_coverage_manifest_digest_mismatch"]}
    malformed = _evidence(candidates=[_candidate(lifecycle="merged")], coverage=malformed_merged_marker)
    malformed_result = mod.derive_landing_disposition(malformed, repo=REPO, issue_number=ISSUE)
    assert malformed_result["disposition"] == "reconciliation_required"
    assert malformed_result["reason_codes"] == ["scope_coverage_manifest_digest_mismatch"]


def test_decision_time_freshness_rebind_and_bounded_retry():
    """AC9: disposition finalization re-verifies issue body sha256 /
    candidate head-or-merge-oid / current main sha live, immediately before
    finalizing, bounded-retries collection exactly once on drift, and
    returns reconciliation_required(freshness_rebind_failed) if the retry
    still disagrees."""
    issue_body = "## Allowed Paths\n- `.claude/a.py`\n"
    allowed_files = [{"path": ".claude/a.py"}]

    def _pr_list_response():
        return 0, json.dumps([{"number": 2137, "closingIssuesReferences": [{"number": ISSUE}]}]), ""

    def _pr_view_full_response():
        return (
            0,
            json.dumps(
                {
                    "number": 2137,
                    "url": f"https://github.com/{REPO}/pull/2137",
                    "state": "OPEN",
                    "isDraft": False,
                    "mergedAt": None,
                    "mergeCommit": None,
                    "headRefOid": SHA,
                    "closingIssuesReferences": [{"number": ISSUE}],
                    "body": "",
                    "files": allowed_files,
                }
            ),
            "",
        )

    def _pr_view_identity_response():
        return 0, json.dumps({"headRefOid": SHA, "mergedAt": None, "mergeCommit": None}), ""

    def make_run(main_sha_sequence):
        state = {"main_sha_calls": 0, "pr_list_calls": 0}

        def run(argv):
            if argv[:3] == ["gh", "pr", "list"]:
                state["pr_list_calls"] += 1
                return _pr_list_response()
            if argv[:2] == ["gh", "api"] and len(argv) > 2 and "timeline" in argv[-1]:
                return 0, "[]", ""
            if argv[:2] == ["gh", "api"] and len(argv) > 2 and "commits/main" in argv[2]:
                sha = main_sha_sequence[state["main_sha_calls"]]
                state["main_sha_calls"] += 1
                return 0, sha, ""
            if argv[:3] == ["gh", "pr", "view"]:
                if argv[-1] == "headRefOid,mergedAt,mergeCommit":
                    return _pr_view_identity_response()
                return _pr_view_full_response()
            if argv[:3] == ["gh", "issue", "view"]:
                return 0, json.dumps({"body": issue_body}), ""
            return 1, "", "unexpected argv: " + " ".join(argv)

        return run, state

    # Drift on the first rebind check, then a matching retry: succeeds using
    # the retried (second) collection's disposition.
    main_shas_recover = ["1" * 40, "2" * 40, "3" * 40, "3" * 40]
    run_recover, state_recover = make_run(main_shas_recover)
    result_recover = mod.resolve_landing_disposition_with_freshness_rebind(
        repo=REPO, issue_number=ISSUE, current_scope=issue_body, run_command=run_recover
    )
    assert result_recover["decision_time_rebind"]["status"] == "fresh"
    assert result_recover["landing_disposition"]["disposition"] == "existing_pr_resume"
    # Bounded: exactly one initial collection + one retry collection.
    assert state_recover["pr_list_calls"] == 2

    # Drift on both the first and the (bounded, single) retry: gives up.
    main_shas_fail = ["1" * 40, "2" * 40, "3" * 40, "4" * 40]
    run_fail, state_fail = make_run(main_shas_fail)
    result_fail = mod.resolve_landing_disposition_with_freshness_rebind(
        repo=REPO, issue_number=ISSUE, current_scope=issue_body, run_command=run_fail
    )
    assert result_fail["decision_time_rebind"]["status"] == "stale"
    assert result_fail["landing_disposition"] == {
        "disposition": "reconciliation_required",
        "reason_codes": ["freshness_rebind_failed"],
        "candidate": None,
    }
    assert state_fail["pr_list_calls"] == 2


def test_default_route_loop_verdict_v2_loader_succeeds_without_module_injection():
    """AC4: GIVEN no `route_loop_verdict_v2_module` injection WHEN the
    default (dependency-injection-free) `_load_route_loop_verdict_v2_module()`
    loader runs THEN it actually loads `route_loop_verdict_v2.py` (returns a
    non-None module exposing the production decision function) instead of
    silently swallowing the `sys.modules`-registration-order exec failure
    and returning None."""
    loaded = mod._load_route_loop_verdict_v2_module()
    assert loaded is not None
    assert hasattr(loaded, "resolve_already_satisfied_early_exit_decision")
    assert hasattr(loaded, "resolve_pre_step1_data_plane_action")


def test_apply_already_satisfied_precedence_fires_through_default_loader():
    """AC3/AC4: GIVEN a no-landing-authority result WHEN
    `apply_already_satisfied_precedence()` is called WITHOUT injecting
    `route_loop_verdict_v2_module` (default loader only) THEN it still
    successfully loads `route_loop_verdict_v2.py` and fires the
    `already_satisfied` composition -- proving the default production load
    path (not just the dependency-injected test path) actually applies the
    precedence."""
    no_candidate = mod.derive_landing_disposition(_evidence(candidates=[]), repo=REPO, issue_number=ISSUE)
    composed = mod.apply_already_satisfied_precedence(
        no_candidate,
        next_action_route="proceed_to_step_1",
        product_spec_routing_action="not_yet_evaluated_at_pre_step1_landing_disposition",
        pr_exists=False,
        base_ac_satisfied=True,
    )
    assert composed["disposition"] == "already_satisfied"
    assert composed["reason_codes"] == ["already_satisfied_no_pr_created"]


def test_derive_pr_exists_from_landing_candidate_across_pr_fixture_shapes():
    """#2713 AC3: `pr_exists` production source. open/draft/merged candidates
    are resume/conflict-relevant targets (True); a closed-unmerged candidate
    was abandoned without landing and is not (False); no candidate at all is
    also not (False)."""
    assert mod.derive_pr_exists_from_landing_candidate(_candidate(lifecycle="open")) is True
    assert mod.derive_pr_exists_from_landing_candidate(_candidate(lifecycle="draft")) is True
    assert mod.derive_pr_exists_from_landing_candidate(_candidate(lifecycle="merged")) is True
    assert mod.derive_pr_exists_from_landing_candidate(_candidate(lifecycle="closed_unmerged")) is False
    assert mod.derive_pr_exists_from_landing_candidate(None) is False


def test_derive_base_ac_satisfied_from_verification_result_never_fabricates_a_fallback():
    """#2713 In Scope: `base_ac_satisfied` production source. A fresh
    (head_sha-matching) all-pass TEST_VERDICT_MACHINE/v2 yields True; a
    fresh result with any non-pass entry yields False; anything
    undeterminable (missing result, missing live_main_sha, stale/mismatched
    head_sha, or malformed runtime_ac_results) yields None -- never a
    fabricated True/False fallback."""
    fresh_pass = {
        "head_sha": SHA,
        "runtime_ac_results": [{"ac": "AC1", "status": "pass"}, {"ac": "AC2", "status": "pass"}],
    }
    fresh_fail = {
        "head_sha": SHA,
        "runtime_ac_results": [{"ac": "AC1", "status": "pass"}, {"ac": "AC2", "status": "fail"}],
    }
    assert mod.derive_base_ac_satisfied_from_verification_result(fresh_pass, live_main_sha=SHA) is True
    assert mod.derive_base_ac_satisfied_from_verification_result(fresh_fail, live_main_sha=SHA) is False

    # Undeterminable cases -- never fabricated.
    assert mod.derive_base_ac_satisfied_from_verification_result(None, live_main_sha=SHA) is None
    assert mod.derive_base_ac_satisfied_from_verification_result(fresh_pass, live_main_sha=None) is None
    assert mod.derive_base_ac_satisfied_from_verification_result(fresh_pass, live_main_sha="b" * 40) is None
    assert (
        mod.derive_base_ac_satisfied_from_verification_result({"head_sha": SHA}, live_main_sha=SHA) is None
    )
    assert (
        mod.derive_base_ac_satisfied_from_verification_result(
            {"head_sha": SHA, "runtime_ac_results": []}, live_main_sha=SHA
        )
        is None
    )
