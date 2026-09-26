"""GIVEN/WHEN/THEN integration tests for the #2699 pre-Step-1 choke point."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
ROUTE = ROOT / ".claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py"
RUNTIME = ROOT / ".claude/skills/impl-review-loop/scripts/verify_implementation_landed_intake_runtime.py"

# research Issue #2761: mirrors `implementation_landed_evidence.py`'s
# `_LIVE_CANDIDATE_REFRESH_FIELDS` -- the decision-time per-candidate
# `gh pr view` field list used by `_live_freshness_reference()`, now
# including `body,closingIssuesReferences` so qualification can be freshly
# re-derived at decision time (AC1).
_LIVE_CANDIDATE_REFRESH_FIELDS = "headRefOid,mergedAt,mergeCommit,body,closingIssuesReferences"


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
    """GIVEN landed or unsafe evidence WHEN pre-Step-1 routes THEN data-plane start is forbidden.

    #2713 AC1/AC5: `resolve_pre_step1_data_plane_action()` now takes an
    already-resolved disposition mapping directly (no raw-evidence
    recompute inside it) -- the caller derives the disposition via
    `resolve_pre_step1_landing_disposition()` first, exactly once."""
    route = _load(ROUTE, "route_loop_verdict_v2_landing")
    landed = route.resolve_pre_step1_landing_disposition(_evidence(), repo="squne121/loop-protocol", issue_number=2119)
    unsafe = route.resolve_pre_step1_landing_disposition(None, repo="squne121/loop-protocol", issue_number=2119)
    landed_gate = route.resolve_pre_step1_data_plane_action(landed)
    unsafe_gate = route.resolve_pre_step1_data_plane_action(unsafe)
    assert landed["disposition"] == "implementation_already_landed"
    assert unsafe["disposition"] == "reconciliation_required"
    assert landed_gate == {
        "disposition": landed,
        "start_data_plane": False,
        "action": "suppress_worker_worktree_new_pr",
    }
    assert unsafe_gate["start_data_plane"] is False
    preparation = (ROOT / ".claude/skills/impl-review-loop/steps/preparation.md").read_text(encoding="utf-8")
    assert preparation.index("Evidence-Based Landing Disposition") < preparation.index("Already-Satisfied Early-Exit")
    assert "worker / worktree / new PR を開始せず" in preparation


BUILD_CAPSULE = ROOT / ".claude/skills/impl-review-loop/scripts/build_intake_capsule.py"
LANDED_EVIDENCE = ROOT / ".claude/skills/impl-review-loop/scripts/implementation_landed_evidence.py"


def _candidate(*, lifecycle: str, number: int = 2137) -> dict:
    candidate: dict = {
        "target": {"repo": "squne121/loop-protocol", "issue_number": 2119},
        "pr": {"number": number, "url": f"https://github.com/squne121/loop-protocol/pull/{number}"},
        "provenance": {"kind": "closing_relation", "verified": True},
        "lifecycle": lifecycle,
        "head_fresh": True,
        "current_scope_ownership": False,
    }
    if lifecycle == "merged":
        candidate.update({"merge_oid": "a" * 40, "main_ancestry": {"verified": True, "reachable": True}})
    return candidate


def test_pr_exists_is_generated_from_candidate_lifecycle_pr_fixtures_not_injected_booleans():
    """#2713 AC3: `pr_exists` is generated from PR fixture lifecycle shapes
    (open / draft / closed-unmerged / merged / no-qualified-candidate) via
    the production `derive_pr_exists_from_landing_candidate()` function --
    tests never inject the `pr_exists` boolean into `_collect_implementation
    _landed_evidence()` directly."""
    landed_evidence = _load(LANDED_EVIDENCE, "implementation_landed_evidence_for_pr_fixture_test")
    assert landed_evidence.derive_pr_exists_from_landing_candidate(_candidate(lifecycle="open")) is True
    assert landed_evidence.derive_pr_exists_from_landing_candidate(_candidate(lifecycle="draft")) is True
    assert landed_evidence.derive_pr_exists_from_landing_candidate(_candidate(lifecycle="merged")) is True
    assert landed_evidence.derive_pr_exists_from_landing_candidate(_candidate(lifecycle="closed_unmerged")) is False
    assert landed_evidence.derive_pr_exists_from_landing_candidate(None) is False


def test_collect_implementation_landed_evidence_applies_precedence_through_default_loader(monkeypatch):
    """#2713 AC3/AC4/AC5: GIVEN a no-landing-authority evidence result
    (closed-unmerged candidate) and a fresh independent base_ac_verification
    _result WHEN `build_intake_capsule.py::_collect_implementation_landed_
    evidence()` (the production entry point) runs WITHOUT any
    `route_loop_verdict_v2_module` injection (default loader only) THEN
    `apply_already_satisfied_precedence()` actually fires (proving the
    #2713 AC4 `sys.modules` registration fix works end-to-end through the
    production entry point, not merely a dependency-injected unit test) and
    the pre_step1_data_plane key set (`start_data_plane` / `action`) stays
    unchanged (AC6)."""
    build_capsule = _load(BUILD_CAPSULE, "build_intake_capsule_for_landed_intake_test")

    main_sha = "9" * 40
    pr_view_full = json.dumps(
        {
            "number": 2137,
            "url": "https://github.com/squne121/loop-protocol/pull/2137",
            "state": "CLOSED",
            "isDraft": False,
            "mergedAt": None,
            "mergeCommit": None,
            "headRefOid": "c" * 40,
            "closingIssuesReferences": [{"number": 2119}],
            "body": "",
            "files": [],
        }
    )
    pr_list = json.dumps([{"number": 2137, "closingIssuesReferences": [{"number": 2119}]}])

    def run(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, pr_list, ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "timeline" in argv[-1]:
            return 0, "[]", ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "commits/main" in argv[2]:
            return 0, main_sha + "\n", ""
        if argv[:3] == ["gh", "pr", "view"]:
            if argv[-1] == _LIVE_CANDIDATE_REFRESH_FIELDS:
                return (
                    0,
                    json.dumps(
                        {
                            "headRefOid": "c" * 40,
                            "mergedAt": None,
                            "mergeCommit": None,
                            "body": "",
                            "closingIssuesReferences": [{"number": 2119}],
                        }
                    ),
                    "",
                )
            return 0, pr_view_full, ""
        if argv[:3] == ["gh", "issue", "view"]:
            return 0, json.dumps({"body": "live body"}), ""
        return 1, "", "unexpected argv: " + " ".join(argv)

    monkeypatch.setattr(build_capsule, "_run_command", run)

    evidence = build_capsule._collect_implementation_landed_evidence(
        issue_number=2119,
        repo="squne121/loop-protocol",
        issue_body="live body",
        command_log=[],
        next_action_route="proceed_to_step_1",
        # #2713 AC9 (PR #2741 review, P1-2): a fully-shaped
        # TEST_VERDICT_MACHINE/v2 payload -- `derive_base_ac_satisfied_from_
        # verification_result()` now reuses `adjudicate_vc_result.py::
        # adapt_test_verdict_to_current_vc_result()`'s validation, which
        # requires `schema`, `contract_body_sha256`, and per-entry
        # `command_hash` -- a bare `{"head_sha", "runtime_ac_results": [{"ac",
        # "status"}]}` (the previous, weaker fixture shape this test used) is
        # exactly the kind of incomplete payload AC9 requires NOT to be
        # trusted as True.
        base_ac_verification_result={
            "schema": "TEST_VERDICT_MACHINE/v2",
            "generated_at": "2026-09-24T00:00:00Z",
            "head_sha": main_sha,
            "contract_body_sha256": "sha256:" + "d" * 64,
            "result": "PASS",
            "runtime_ac_results": [
                {
                    "ac": "AC1",
                    "command_hash": "sha256:" + "1" * 64,
                    "status": "pass",
                    "exit_code": 0,
                    "fallback_detected": False,
                    "human_review_required": False,
                    "stop_condition_triggered": False,
                }
            ],
        },
    )

    assert evidence["landing_disposition"]["disposition"] == "already_satisfied"
    assert set(evidence["pre_step1_data_plane"]) == {"start_data_plane", "action"}
    assert evidence["pre_step1_data_plane"] == {
        "start_data_plane": False,
        "action": "suppress_worker_worktree_new_pr",
    }


def test_collect_implementation_landed_evidence_skips_precedence_when_base_ac_undeterminable(monkeypatch):
    """#2713 In Scope: GIVEN no `base_ac_verification_result` (today's
    `build_intake_capsule.py` CLI has no production source for it) WHEN
    `_collect_implementation_landed_evidence()` runs THEN
    `apply_already_satisfied_precedence()` is skipped (never a fabricated
    True/False fallback) and the freshness-rebound landing disposition
    passes through unchanged."""
    build_capsule = _load(BUILD_CAPSULE, "build_intake_capsule_for_landed_intake_skip_test")

    def run(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, "[]", ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "timeline" in argv[-1]:
            return 0, "[]", ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "commits/main" in argv[2]:
            return 0, "9" * 40 + "\n", ""
        if argv[:3] == ["gh", "issue", "view"]:
            return 0, json.dumps({"body": "live body"}), ""
        return 1, "", "unexpected argv: " + " ".join(argv)

    monkeypatch.setattr(build_capsule, "_run_command", run)

    evidence = build_capsule._collect_implementation_landed_evidence(
        issue_number=2119,
        repo="squne121/loop-protocol",
        issue_body="live body",
        command_log=[],
        next_action_route="proceed_to_step_1",
    )

    assert evidence["landing_disposition"]["disposition"] == "ordinary_dispatch_or_explicit_recovery"
    assert evidence["landing_disposition"]["reason_codes"] == ["no_qualified_candidate"]
    assert evidence["pre_step1_data_plane"] == {"start_data_plane": True, "action": "dispatch_step1"}


_NO_CANDIDATE_ISSUE_BODY = "## Machine-Readable Contract\n\nstatus: full-body\n"


def _no_candidate_already_satisfied_run(issue_body: str, main_sha: str):
    """#2713 AC8 (PR #2741 review, P1-1): a full `_run_command` fixture
    covering `build_intake_capsule()`'s ENTIRE production call graph
    (issue metadata, repo state, comments, and the
    `_collect_implementation_landed_evidence()` candidate discovery +
    freshness rebind) for a no-qualified-PR-candidate scenario, driven by
    argv shape (not call-order position) so it is robust across the
    multiple entry points (`build_intake_capsule()`, `main()`) these tests
    exercise."""

    def run(argv):
        if argv[:2] == ["git", "rev-parse"]:
            return 0, "f" * 40 + "\n", ""
        if argv[:2] == ["git", "branch"]:
            return 0, "main\n", ""
        if argv[:2] == ["git", "status"]:
            return 0, "", ""
        if argv[:3] == ["gh", "issue", "view"]:
            json_flag_index = argv.index("--json")
            if argv[json_flag_index + 1] == "body":
                return 0, json.dumps({"body": issue_body}), ""
            return (
                0,
                json.dumps(
                    {
                        "title": "実装: production reachability テスト",
                        "state": "open",
                        "labels": [{"name": "phase/implementation"}],
                        "body": issue_body,
                        "updatedAt": "2026-09-24T00:00:00Z",
                    }
                ),
                "",
            )
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, "[]", ""
        if argv[:2] == ["gh", "api"] and len(argv) > 3 and "comments?per_page" in argv[3]:
            return 0, "", ""
        if argv[:2] == ["gh", "api"] and "timeline" in argv[-1]:
            return 0, "[]", ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "commits/main" in argv[2]:
            return 0, main_sha + "\n", ""
        return 1, "", "unexpected argv: " + " ".join(argv)

    return run


def _fresh_test_verdict(main_sha: str) -> dict:
    return {
        "schema": "TEST_VERDICT_MACHINE/v2",
        "generated_at": "2026-09-24T00:00:00Z",
        "head_sha": main_sha,
        "contract_body_sha256": "sha256:" + "e" * 64,
        "result": "PASS",
        "runtime_ac_results": [
            {
                "ac": "AC1",
                "command_hash": "sha256:" + "1" * 64,
                "status": "pass",
                "exit_code": 0,
                "fallback_detected": False,
                "human_review_required": False,
                "stop_condition_triggered": False,
            }
        ],
    }


def test_public_build_intake_capsule_reaches_already_satisfied_via_base_ac_verification_result(monkeypatch):
    """#2713 AC8 (PR #2741 review, P1-1): GIVEN a `base_ac_verification_result`
    supplied through the PUBLIC `build_intake_capsule()` function (the
    canonical production entry point -- not `_collect_implementation_landed_
    evidence()` called directly) WHEN no qualified PR candidate exists and
    the supplied verification result is fresh and fully clean THEN the
    capsule's `implementation_landed_evidence.landing_disposition.
    disposition` reaches `already_satisfied` and
    `pre_step1_data_plane.start_data_plane` reaches `False` -- proving
    production reachability through the public API boundary, not merely a
    private-helper direct-argument-injection unit test."""
    build_capsule = _load(BUILD_CAPSULE, "build_intake_capsule_for_public_reachability_test")
    main_sha = "9" * 40

    monkeypatch.setattr(
        build_capsule,
        "_run_command",
        _no_candidate_already_satisfied_run(_NO_CANDIDATE_ISSUE_BODY, main_sha),
    )

    capsule, _artifact, exit_code = build_capsule.build_intake_capsule(
        2119,
        "squne121/loop-protocol",
        None,
        include_implementation_landed_evidence=True,
        base_ac_verification_result=_fresh_test_verdict(main_sha),
    )

    assert exit_code == 0
    landed = capsule["implementation_landed_evidence"]
    assert landed["landing_disposition"]["disposition"] == "already_satisfied"
    assert landed["pre_step1_data_plane"] == {
        "start_data_plane": False,
        "action": "suppress_worker_worktree_new_pr",
    }


def test_cli_main_reaches_already_satisfied_via_base_ac_verification_result_file(tmp_path, monkeypatch):
    """#2713 AC8 (PR #2741 review, P1-1): the `main()` canonical CLI
    entrypoint (argparse -> build_intake_capsule -> artifact write), driven
    by `--base-ac-verification-result-file`, reaches the SAME
    already_satisfied / start_data_plane:false outcome as the public-function
    test above -- proving the flag is wired end-to-end from the actual CLI
    boundary (not merely the Python function signature)."""
    build_capsule = _load(BUILD_CAPSULE, "build_intake_capsule_for_cli_reachability_test")
    main_sha = "9" * 40

    monkeypatch.setattr(
        build_capsule,
        "_run_command",
        _no_candidate_already_satisfied_run(_NO_CANDIDATE_ISSUE_BODY, main_sha),
    )

    verification_result_file = tmp_path / "base-ac-verification-result.json"
    verification_result_file.write_text(json.dumps(_fresh_test_verdict(main_sha)), encoding="utf-8")
    artifact_dir = tmp_path / "artifacts"

    argv = [
        "build_intake_capsule.py",
        "--issue-number",
        "2119",
        "--repo",
        "squne121/loop-protocol",
        "--max-stdout-bytes",
        "65536",
        "--include-implementation-landed-evidence",
        "--base-ac-verification-result-file",
        str(verification_result_file),
        "--artifact-dir",
        str(artifact_dir),
    ]

    with patch.object(sys, "argv", argv):
        exit_code = build_capsule.main()

    assert exit_code == 0
    artifact_payload = json.loads((artifact_dir / "intake-capsule-2119.json").read_text(encoding="utf-8"))
    landed = artifact_payload["implementation_landed_evidence"]
    assert landed["landing_disposition"]["disposition"] == "already_satisfied"
    assert landed["pre_step1_data_plane"] == {
        "start_data_plane": False,
        "action": "suppress_worker_worktree_new_pr",
    }


def test_cli_main_base_ac_verification_result_file_missing_is_fail_safe_not_fatal(tmp_path, monkeypatch):
    """#2713 In Scope: a missing/unreadable
    `--base-ac-verification-result-file` must degrade to `None`
    (undeterminable) rather than blocking intake (non-zero exit) or being
    fabricated into a True/False `base_ac_satisfied` -- the existing
    freshness-rebound landing disposition passes through unchanged."""
    build_capsule = _load(BUILD_CAPSULE, "build_intake_capsule_for_cli_missing_file_test")
    main_sha = "9" * 40

    monkeypatch.setattr(
        build_capsule,
        "_run_command",
        _no_candidate_already_satisfied_run(_NO_CANDIDATE_ISSUE_BODY, main_sha),
    )

    artifact_dir = tmp_path / "artifacts"
    argv = [
        "build_intake_capsule.py",
        "--issue-number",
        "2119",
        "--repo",
        "squne121/loop-protocol",
        "--max-stdout-bytes",
        "65536",
        "--include-implementation-landed-evidence",
        "--base-ac-verification-result-file",
        str(tmp_path / "does-not-exist.json"),
        "--artifact-dir",
        str(artifact_dir),
    ]

    with patch.object(sys, "argv", argv):
        exit_code = build_capsule.main()

    assert exit_code == 0
    artifact_payload = json.loads((artifact_dir / "intake-capsule-2119.json").read_text(encoding="utf-8"))
    landed = artifact_payload["implementation_landed_evidence"]
    assert landed["landing_disposition"]["disposition"] == "ordinary_dispatch_or_explicit_recovery"
    assert landed["pre_step1_data_plane"] == {"start_data_plane": True, "action": "dispatch_step1"}


def test_live_runtime_verifier_records_2119_2137_legacy_compatibility_without_fixture_fallback(tmp_path):
    """AC12: GIVEN live-shaped #2119/#2137 read responses (a legacy merged
    candidate with no durable IMPLEMENTATION_SCOPE_COVERAGE_V1 marker) WHEN
    the runtime verifier runs THEN it records non-fallback evidence and the
    legacy-compatibility disposition itself is derived through the same
    production `derive_landing_disposition()` function `build_intake_capsule
    .py` uses -- not a verifier-local inline ternary -- and correctly stays
    ordinary_dispatch_or_explicit_recovery (never implementation_already_landed)."""
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

    artifact = tmp_path / "artifacts" / "runtime-verification-AC12-test.log"
    payload, code = runtime.verify(artifact_path=artifact, run_command=run)
    assert code == 0
    assert payload["status"] == "PASS"
    assert payload["fallback_used"] is False
    assert payload["legacy_scope_coverage_marker_missing"] is True
    assert payload["legacy_compatibility_disposition"] == "ordinary_dispatch_or_explicit_recovery"
    assert artifact.exists()


def test_collect_candidate_inputs_production_shaped_2727_sibling_cross_references_excluded(monkeypatch):
    """AC9: production-shaped regression through `collect_candidate_inputs()`
    (timeline candidate collection -> real `gh pr view` PR body fetch -> a
    genuine `IMPLEMENTATION_SCOPE_COVERAGE_V1` marker text produced by
    `build_scope_coverage_marker()`/`render_scope_coverage_marker()` and
    parsed by `coverage_from_pr_body()` -> candidate qualification via
    `derive_landing_disposition()`), reusing this module's existing mock
    `gh` pattern -- not a hand-built candidate dict. Reproduces the exact
    #2727 incident shape: PR #2735 and PR #2746 analogues are discovered as
    timeline cross-references for the current target Issue #2727, but each
    carries a well-formed durable marker naming a *different* sibling Issue
    (#2725 / #2726). Neither is a qualified landing candidate for the
    current target, and `qualified_candidate_conflict` never fires (AC1/AC2).

    #2750 PR #2758 review (P1 blocker, comment
    https://github.com/squne121/loop-protocol/pull/2758#issuecomment-5831103538):
    this test used to stop at `derive_landing_disposition()`. It now
    continues the SAME #2727 incident-shaped fixture through the canonical
    production composition `build_intake_capsule.py::
    _collect_implementation_landed_evidence()` actually runs
    (`resolve_landing_disposition_with_freshness_rebind()` ->
    `derive_landing_disposition()` -> `apply_already_satisfied_precedence()`
    -> `resolve_pre_step1_data_plane_action()`), reusing the existing mock
    `gh` / `base_ac_verification_result` fixture patterns from
    `test_collect_implementation_landed_evidence_applies_precedence_through_
    default_loader()` above -- never a bespoke test-only re-implementation
    of that composition logic."""
    landed_evidence = _load(LANDED_EVIDENCE, "implementation_landed_evidence_for_2727_integration_test")

    target_issue = 2727
    repo = "squne121/loop-protocol"
    target_issue_body = "## Allowed Paths\n- `.claude/a.py`\n"
    sibling_body_2725 = "## Allowed Paths\n- `.claude/x.py`\n"
    sibling_body_2726 = "## Allowed Paths\n- `.claude/y.py`\n"
    main_sha = "9" * 40

    marker_2735 = landed_evidence.render_scope_coverage_marker(
        landed_evidence.build_scope_coverage_marker(
            issue_number=2725, issue_body=sibling_body_2725, pr_head_sha="a" * 40
        )
    )
    marker_2746 = landed_evidence.render_scope_coverage_marker(
        landed_evidence.build_scope_coverage_marker(
            issue_number=2726, issue_body=sibling_body_2726, pr_head_sha="b" * 40
        )
    )

    timeline_events = [
        {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": 2735,
                    "pull_request": {"url": f"https://api.github.com/repos/{repo}/pulls/2735"},
                    "repository_url": f"https://api.github.com/repos/{repo}",
                }
            },
        },
        {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": 2746,
                    "pull_request": {"url": f"https://api.github.com/repos/{repo}/pulls/2746"},
                    "repository_url": f"https://api.github.com/repos/{repo}",
                }
            },
        },
    ]

    # #2750 PR #2758 review / research Issue #2761: `_LIGHT_PR_FIELDS`
    # distinguishes the freshness-rebind `gh pr view --json
    # headRefOid,mergedAt,mergeCommit,body,closingIssuesReferences` shape
    # (`_live_freshness_reference()`) from the discovery-time
    # `gh pr view --json number,url,state,...` shape (`collect_candidate_
    # inputs()`) -- both real production call shapes the same fixture must
    # answer once this test continues through
    # `resolve_landing_disposition_with_freshness_rebind()`. #2761: the
    # light-field response now also carries `body`/`closingIssuesReferences`
    # so decision-time re-qualification independently reconfirms each
    # sibling is still irrelevant from FRESH live data (never merely trusted
    # from collection time).
    _LIGHT_PR_FIELDS = _LIVE_CANDIDATE_REFRESH_FIELDS

    def run(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            # Neither PR's closingIssuesReferences names the current target
            # Issue #2727 -- both close a different sibling Issue instead.
            return (
                0,
                json.dumps(
                    [
                        {"number": 2735, "closingIssuesReferences": [{"number": 2725}]},
                        {"number": 2746, "closingIssuesReferences": [{"number": 2726}]},
                    ]
                ),
                "",
            )
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "timeline" in argv[-1]:
            return 0, json.dumps(timeline_events), ""
        if argv[:3] == ["gh", "pr", "view"] and argv[3] == "2735" and argv[-1] == _LIGHT_PR_FIELDS:
            return (
                0,
                json.dumps(
                    {
                        "headRefOid": "a" * 40,
                        "mergedAt": "2026-09-01T00:00:00Z",
                        "mergeCommit": {"oid": "a" * 40},
                        "body": marker_2735,
                        "closingIssuesReferences": [{"number": 2725}],
                    }
                ),
                "",
            )
        if argv[:3] == ["gh", "pr", "view"] and argv[3] == "2746" and argv[-1] == _LIGHT_PR_FIELDS:
            return (
                0,
                json.dumps(
                    {
                        "headRefOid": "b" * 40,
                        "mergedAt": "2026-09-02T00:00:00Z",
                        "mergeCommit": {"oid": "b" * 40},
                        "body": marker_2746,
                        "closingIssuesReferences": [{"number": 2726}],
                    }
                ),
                "",
            )
        if argv[:3] == ["gh", "pr", "view"] and argv[3] == "2735":
            return (
                0,
                json.dumps(
                    {
                        "number": 2735,
                        "url": f"https://github.com/{repo}/pull/2735",
                        "state": "MERGED",
                        "isDraft": False,
                        "mergedAt": "2026-09-01T00:00:00Z",
                        "mergeCommit": {"oid": "a" * 40},
                        "headRefOid": "a" * 40,
                        "closingIssuesReferences": [{"number": 2725}],
                        "body": marker_2735,
                        "files": [],
                    }
                ),
                "",
            )
        if argv[:3] == ["gh", "pr", "view"] and argv[3] == "2746":
            return (
                0,
                json.dumps(
                    {
                        "number": 2746,
                        "url": f"https://github.com/{repo}/pull/2746",
                        "state": "MERGED",
                        "isDraft": False,
                        "mergedAt": "2026-09-02T00:00:00Z",
                        "mergeCommit": {"oid": "b" * 40},
                        "headRefOid": "b" * 40,
                        "closingIssuesReferences": [{"number": 2726}],
                        "body": marker_2746,
                        "files": [],
                    }
                ),
                "",
            )
        if argv[:3] == ["gh", "issue", "view"] and argv[3] == str(target_issue):
            return 0, json.dumps({"body": target_issue_body}), ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "compare" in argv[2]:
            # research Issue #2761: neither sibling's own main-ancestry
            # compare may be invoked at all once qualified irrelevant
            # (skip-by-construction) -- this branch existing/succeeding is
            # not itself proof of correctness; `ancestry_compare_calls`
            # below asserts it is never actually reached.
            ancestry_compare_calls.append(argv)
            return 0, "ahead\n", ""
        if argv[:2] == ["gh", "api"] and len(argv) > 2 and "commits/main" in argv[2]:
            return 0, main_sha + "\n", ""
        return 1, "", "unexpected argv: " + " ".join(argv)

    ancestry_compare_calls: list[list[str]] = []
    evidence = landed_evidence.collect_candidate_inputs(
        repo=repo, issue_number=target_issue, current_scope=target_issue_body, run_command=run
    )
    assert ancestry_compare_calls == []
    assert len(evidence["candidates"]) == 2
    for candidate in evidence["candidates"]:
        assert candidate["provenance"]["kind"] == "verified_cross_reference"
        assert candidate["scope_coverage"]["status"] == "invalid"
        assert candidate["scope_coverage"]["errors"] == ["scope_coverage_issue_identity_mismatch"]
        assert candidate["qualified_irrelevant_sibling"] is True
        assert candidate["main_ancestry"] == {"verified": False, "reachable": False}

    result = landed_evidence.derive_landing_disposition(evidence, repo=repo, issue_number=target_issue)
    assert result["disposition"] == "ordinary_dispatch_or_explicit_recovery"
    assert result["reason_codes"] == ["no_qualified_candidate"]

    # #2750 PR #2758 review (P1 blocker): continue the SAME #2727
    # incident-shaped fixture through the canonical production entry point
    # `build_intake_capsule.py::_collect_implementation_landed_evidence()`
    # -- `resolve_landing_disposition_with_freshness_rebind()` ->
    # `derive_landing_disposition()` -> `apply_already_satisfied_precedence()`
    # -> `resolve_pre_step1_data_plane_action()` -- with a fresh, clean
    # base AC PASS `base_ac_verification_result` (reusing `_fresh_test_
    # verdict()` from above) so the no-qualified-candidate disposition
    # composes into `already_satisfied` / `suppress_worker_worktree_new_pr`,
    # never re-deriving that composition locally in the test.
    build_capsule = _load(BUILD_CAPSULE, "build_intake_capsule_for_2727_integration_test")
    monkeypatch.setattr(build_capsule, "_run_command", run)

    production_evidence = build_capsule._collect_implementation_landed_evidence(
        issue_number=target_issue,
        repo=repo,
        issue_body=target_issue_body,
        command_log=[],
        next_action_route="proceed_to_step_1",
        base_ac_verification_result=_fresh_test_verdict(main_sha),
    )

    assert production_evidence["landing_disposition"]["reason_codes"] != ["qualified_candidate_conflict"]
    assert production_evidence["landing_disposition"]["disposition"] == "already_satisfied"
    assert production_evidence["pre_step1_data_plane"] == {
        "start_data_plane": False,
        "action": "suppress_worker_worktree_new_pr",
    }
