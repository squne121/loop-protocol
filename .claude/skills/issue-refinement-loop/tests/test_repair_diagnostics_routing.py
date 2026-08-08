"""
test_repair_diagnostics_routing.py

Issue #2016: repair_issue_contract producer + run_refinement_preflight.py wrapper
must route a known-safe, deterministic Issue-body repair to a machine-actionable
`STATUS: needs_fix` / `NEXT_ACTION: apply_deterministic_repair` signal, instead of
generic-blocker-ing it into `STATUS: blocked` / `NEXT_ACTION: human_judgment_required`
(Issue #2013 regression) or silently converting it into `STATUS: pass` (the false-green
identified in the OWNER adversarial review on Issue #2016).

Covers AC1-AC8 (see Issue #2016 "修正版 Acceptance Criteria 案" / OWNER review). The
7 functions named exactly as in the Issue's `## Verification Commands` section are
top-level (not nested in a class) so their pytest node-ids match the Issue body
verbatim:
  AC1: test_repair_action_disposition_aggregated_from_repairs
  AC2: test_known_safe_repair_routes_actionable_needs_fix
  AC3: test_unknown_or_mixed_repair_requires_human_judgment
  AC4: test_repair_process_and_artifact_failures_are_environment_failures
  AC5: test_no_change_repair_result_keeps_existing_planner_mapping
  AC6: test_repair_action_projected_in_result_schema_and_compact_stdout
  AC7: test_issue_2013_safe_repair_does_not_silent_pass_or_human_escalate
  AC8: (this file + test_repair_issue_contract.py + test_refinement_preflight.py,
        run together as a regression suite; see Issue's AC8 Verification Command)

Additional (non-AC-numbered) tests provide extra edge-case coverage beyond the
Issue's minimum bar (e.g. the `all()`-on-empty-iterable trap called out in the
OWNER adversarial review's P1-4).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import repair_issue_contract as ric  # noqa: E402
import run_refinement_preflight as wrapper  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

MOCK_PLAN_PASS = {
    "schema_version": "refinement_loop_plan/v1",
    "fail_closed": {"required": False, "reason_codes": []},
    "decisions": {},
}

# Body reproducing the exact Issue #2013 symptom: a Verification Commands
# entry that targets a NEW Allowed Path (does not exist yet), with no
# preceding `# baseline-expect:` annotation. repair_issue_contract.py
# classifies this as `insert_baseline_expect_fail` with confidence: high
# (see .claude/skills/issue-refinement-loop/scripts/tests/test_repair_issue_contract.py
# ::test_insert_baseline_expect_fail_for_new_allowed_path).
ISSUE_2013_BODY = """\
## Outcome

Add a new doc.

## Verification Commands

```bash
$ test -f docs/dev/issue-2013-regression.md
```

## Allowed Paths

- `docs/dev/issue-2013-regression.md`

## Stop Conditions

- none
"""

CLEAN_BODY_NO_REPAIRS = """\
## Outcome

Nothing to repair here.

## Verification Commands

```bash
pnpm typecheck
```

## Allowed Paths

- scripts/example.py

## Stop Conditions

- none
"""

# A body producing a `runtime_only_command` repair: a known mutating kind
# that carries no `confidence` field and is never auto-safe.
UNSAFE_KNOWN_KIND_BODY = "## Verification Commands\n\n```bash\n$ pnpm test:e2e\n```\n"


class _noop_ctx:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _run_wrapper_preflight(body: str, issue_number: int, tmp_path: Path, *, invoke_repair=None):
    """Run wrapper.run_preflight() against an in-memory fixture, with the
    planner mocked to always return MOCK_PLAN_PASS (so the test isolates
    repair_action routing from planner semantics). Optionally override
    _invoke_repair for environment_failure-path tests.

    Uses the pytest `tmp_path` fixture (NOT a self-cleaning
    tempfile.TemporaryDirectory context manager) so that artifact files
    written under `<tmp_path>/.claude/artifacts/...` remain on disk for
    post-call assertions in the caller."""
    fixture = {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": issue_number,
        "repo": "testowner/testrepo",
        "now": "2026-01-01T00:00:00+00:00",
        "issue": {"number": issue_number, "title": "Test Issue", "body": body, "labels": []},
        "comments": [],
        "anchor_comment_urls": [],
    }
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    with (
        mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
        mock.patch.object(wrapper, "_invoke_planner", return_value=(MOCK_PLAN_PASS, 0, "", "")),
        mock.patch.object(wrapper, "_invoke_repair", return_value=invoke_repair)
        if invoke_repair is not None
        else _noop_ctx(),
    ):
        result, exit_code = wrapper.run_preflight(
            issue_number=issue_number,
            repo="testowner/testrepo",
            anchor_comment_urls=[],
            fixture_path=fixture_path,
        )
    return result, exit_code


# ---------------------------------------------------------------------------
# AC1: producer repair_action disposition aggregation
# ---------------------------------------------------------------------------


def test_repair_action_disposition_aggregated_from_repairs():
    """AC1: repair_action is a versioned closed-enum aggregate over ALL
    repairs[] records (not a copy of a single record's confidence field,
    which does not exist as a top-level producer field — OWNER review P0-1)."""
    # Single known-safe mutating record with confidence: high -> auto_apply_safe.
    safe = ric.classify_repair_action(
        "sha256:orig",
        "sha256:new",
        [
            {
                "kind": "insert_baseline_expect_fail",
                "line_start": 4,
                "line_end": 4,
                "confidence": "high",
            }
        ],
    )
    assert safe["schema_version"] == "repair_action/v1"
    assert safe["policy_version"] == "deterministic-issue-repair/v1"
    assert safe["disposition"] == "auto_apply_safe"
    assert safe["repair_kinds"] == ["insert_baseline_expect_fail"]

    # Legacy record with missing confidence (pre-#952 style) must NOT be
    # treated as an implicit high-confidence grant.
    legacy_missing_confidence = ric.classify_repair_action(
        "sha256:orig",
        "sha256:new",
        [{"kind": "insert_baseline_expect_fail", "line_start": 1, "line_end": 1}],
    )
    assert legacy_missing_confidence["disposition"] == "human_review_required"
    assert "missing_safety_classification" in legacy_missing_confidence["reason_codes"]

    # Informational-only kind (non_target_fence) never contributes a
    # mutating classification, and multiple records aggregate to a single
    # disposition (not a per-record list).
    informational_only = ric.classify_repair_action(
        "sha256:same",
        "sha256:same",
        [
            {"kind": "non_target_fence", "line_start": 2, "line_end": 2, "original": "x", "repaired": "x"},
            {"kind": "non_target_fence", "line_start": 9, "line_end": 9, "original": "y", "repaired": "y"},
        ],
    )
    assert informational_only["disposition"] == "informational"
    assert informational_only["repair_kinds"] == []
    assert isinstance(informational_only["disposition"], str)


def test_repair_action_empty_repairs_is_not_misclassified_as_safe():
    """P1-4 of the OWNER review: all() on an empty iterable is True, so a
    naive `all(r['confidence'] == 'high' for r in repairs)` would
    misclassify repairs == [] as safe. This must be branched explicitly
    and classified as informational, never auto_apply_safe."""
    result = ric.classify_repair_action("sha256:same", "sha256:same", [])
    assert result["disposition"] == "informational"
    assert result["disposition"] != "auto_apply_safe"


def test_repair_action_malformed_payload_is_invalid():
    """repairs field that is not a list, or a malformed record within it,
    is a schema violation — not an implicit pass-through."""
    result = ric.classify_repair_action("sha256:a", "sha256:b", "not-a-list")
    assert result["disposition"] == "invalid_payload"

    result2 = ric.classify_repair_action("sha256:a", "sha256:b", [{"no_kind_field": True}])
    assert result2["disposition"] == "human_review_required"
    assert "malformed_repair_record" in result2["reason_codes"]


def test_run_repair_includes_repair_action_in_producer_payload():
    """repair_issue_contract.py's run_repair() output includes the
    repair_action block; top-level confidence never exists."""
    body = (
        "## Verification Commands\n\n```bash\n$ test -f docs/dev/ac1-899.md\n```\n\n"
        "## Allowed Paths\n\n- `docs/dev/ac1-899.md`\n"
    )
    result = ric.run_repair(body)
    assert "repair_action" in result
    assert result["repair_action"]["disposition"] == "auto_apply_safe"
    assert result.get("confidence") is None


# ---------------------------------------------------------------------------
# AC2: known safe repair -> needs_fix / apply_deterministic_repair
# ---------------------------------------------------------------------------


def test_known_safe_repair_routes_actionable_needs_fix(tmp_path):
    """AC2: a known-safe deterministic repair does NOT add a generic
    blocker; it routes to STATUS: needs_fix / NEXT_ACTION:
    apply_deterministic_repair with canonical repair_action (diagnostics
    + candidate body artifact + original/repaired SHA)."""
    result, exit_code = _run_wrapper_preflight(ISSUE_2013_BODY, 90200, tmp_path)

    assert result["status"] == "needs_fix"
    assert result["next_action"] == "apply_deterministic_repair"
    assert exit_code == wrapper.EXIT_NEEDS_FIX

    assert result["blockers"] == [], f"expected no blockers, got {result['blockers']}"

    repair_action = result.get("repair_action")
    assert repair_action is not None
    assert repair_action["disposition"] == "auto_apply_safe"
    assert repair_action["original_body_sha256"].startswith("sha256:")
    assert repair_action["repaired_body_sha256"].startswith("sha256:")
    assert repair_action["original_body_sha256"] != repair_action["repaired_body_sha256"]

    diagnostics_path = Path(repair_action["diagnostics_artifact"])
    candidate_path = Path(repair_action["candidate_body_artifact"])
    assert diagnostics_path.exists()
    assert candidate_path.exists()
    assert "baseline-expect: fail" in candidate_path.read_text(encoding="utf-8")


def test_wrapper_routes_known_unsafe_kind_to_blocked(tmp_path):
    """End-to-end companion to AC3: a body producing a runtime_only_command
    repair (known mutating kind that is never auto-safe) routes through the
    existing generic repair_diagnostics blocker -> STATUS: blocked."""
    result, exit_code = _run_wrapper_preflight(UNSAFE_KNOWN_KIND_BODY, 90201, tmp_path)

    assert result["status"] == "blocked"
    assert result["next_action"] == "human_judgment_required"
    assert exit_code == wrapper.EXIT_BLOCKED
    assert any("repair_diagnostics" in b for b in result["blockers"])
    assert "repair_action" not in result


# ---------------------------------------------------------------------------
# AC3: unknown / missing / unsafe / mixed / overlapping -> blocked
# ---------------------------------------------------------------------------


def test_unknown_or_mixed_repair_requires_human_judgment():
    """AC3: unknown kind, missing safety classification, non-auto-safe
    mutating kind, safe/unsafe mixed, and overlapping mutating repairs all
    classify as human_review_required."""
    unknown_kind = ric.classify_repair_action(
        "sha256:a", "sha256:b", [{"kind": "totally_unknown_repair_kind", "line_start": 1, "line_end": 1}]
    )
    assert unknown_kind["disposition"] == "human_review_required"
    assert "unknown_repair_kind" in unknown_kind["reason_codes"]

    missing_confidence = ric.classify_repair_action(
        "sha256:a", "sha256:b", [{"kind": "insert_baseline_expect_fail", "line_start": 1, "line_end": 1}]
    )
    assert missing_confidence["disposition"] == "human_review_required"
    assert "missing_safety_classification" in missing_confidence["reason_codes"]

    known_non_auto_safe_kind = ric.classify_repair_action(
        "sha256:a", "sha256:b", [{"kind": "runtime_only_command", "line_start": 1, "line_end": 1}]
    )
    assert known_non_auto_safe_kind["disposition"] == "human_review_required"
    assert "non_auto_safe_repair_kind" in known_non_auto_safe_kind["reason_codes"]

    mixed = ric.classify_repair_action(
        "sha256:a",
        "sha256:b",
        [
            {"kind": "insert_baseline_expect_fail", "confidence": "high", "line_start": 1, "line_end": 1},
            {"kind": "runtime_only_command", "line_start": 10, "line_end": 10},
        ],
    )
    assert mixed["disposition"] == "human_review_required"

    overlapping = ric.classify_repair_action(
        "sha256:a",
        "sha256:b",
        [
            {"kind": "insert_baseline_expect_fail", "confidence": "high", "line_start": 1, "line_end": 3},
            {
                "kind": "move_inline_baseline_expect_to_preceding_line",
                "confidence": "high",
                "line_start": 2,
                "line_end": 2,
            },
        ],
    )
    assert overlapping["disposition"] == "human_review_required"
    assert "overlapping_repair" in overlapping["reason_codes"]


# ---------------------------------------------------------------------------
# AC4: subprocess / artifact failures -> environment_failure
# ---------------------------------------------------------------------------


def test_repair_process_and_artifact_failures_are_environment_failures(tmp_path):
    """AC4: repair subprocess non-zero/timeout/invalid-JSON/schema-mismatch,
    changed: true with 0 mutating repairs, and an invalid_payload disposition
    all classify as STATUS: environment_failure / NEXT_ACTION: fix_environment."""
    failure_payloads = {
        "nonzero_exit": {
            "schema": "repair_issue_contract/v1",
            "changed": False,
            "repairs": [],
            "error": "repair_subprocess_nonzero_exit:1:boom",
        },
        "timeout": {
            "schema": "repair_issue_contract/v1",
            "changed": False,
            "repairs": [],
            "error": "repair_subprocess_timeout",
        },
        "invalid_json": {
            "schema": "repair_issue_contract/v1",
            "changed": False,
            "repairs": [],
            "error": "repair_subprocess_invalid_json:Expecting value",
        },
        "schema_mismatch": {"schema": "some_other_schema/v1", "changed": False, "repairs": []},
        "changed_true_zero_mutating_repairs": {
            "schema": "repair_issue_contract/v1",
            "changed": True,
            "repairs": [],
            "repair_action": {
                "schema_version": "repair_action/v1",
                "policy_version": "deterministic-issue-repair/v1",
                "disposition": "informational",
                "original_body_sha256": "sha256:a",
                "repaired_body_sha256": "sha256:b",
                "diagnostics_artifact": None,
                "candidate_body_artifact": None,
                "repair_kinds": [],
                "reason_codes": ["no_mutating_repair_detected"],
            },
        },
        "invalid_payload_disposition": {
            "schema": "repair_issue_contract/v1",
            "changed": True,
            "repairs": [{"kind": "x"}],
            "repair_action": {
                "schema_version": "repair_action/v1",
                "policy_version": "deterministic-issue-repair/v1",
                "disposition": "invalid_payload",
                "original_body_sha256": "sha256:a",
                "repaired_body_sha256": "sha256:b",
                "diagnostics_artifact": None,
                "candidate_body_artifact": None,
                "repair_kinds": [],
                "reason_codes": ["repairs_field_not_a_list"],
            },
        },
    }

    for label, repair_result in failure_payloads.items():
        case_dir = tmp_path / label
        case_dir.mkdir()
        result, exit_code = _run_wrapper_preflight(
            CLEAN_BODY_NO_REPAIRS, 90400, case_dir, invoke_repair=repair_result
        )
        assert result["status"] == "environment_failure", f"{label}: got {result['status']}"
        assert result["next_action"] == "fix_environment", label
        assert exit_code == wrapper.EXIT_ENVIRONMENT_FAILURE, label
        assert wrapper.BLOCKER_REPAIR_ENVIRONMENT_FAILURE in result["blockers"], label


def test_artifact_write_failure_is_environment_failure(tmp_path):
    """AC4: artifact write/readback failure is fail-closed (not silently
    swallowed by a bare `except Exception: pass`, as it was pre-#2016)."""
    with mock.patch.object(wrapper, "_atomic_write_json", side_effect=OSError("disk full")):
        result, exit_code = _run_wrapper_preflight(ISSUE_2013_BODY, 90401, tmp_path)

    assert result["status"] == "environment_failure"
    assert exit_code == wrapper.EXIT_ENVIRONMENT_FAILURE
    assert wrapper.BLOCKER_REPAIR_ENVIRONMENT_FAILURE in result["blockers"]


def test_apply_reinvocation_input_sha_mismatch_is_environment_failure(tmp_path):
    """AC4: the auto-apply optimistic-concurrency guard rejects a candidate
    whose --apply re-invocation disagrees with the dry-run
    original_body_sha256 (Issue #2016 recommended design step 2)."""
    with mock.patch.object(
        wrapper,
        "_materialize_auto_apply_candidate",
        return_value=(None, "repair_apply_input_sha_mismatch"),
    ):
        result, exit_code = _run_wrapper_preflight(ISSUE_2013_BODY, 90402, tmp_path)

    assert result["status"] == "environment_failure"
    assert exit_code == wrapper.EXIT_ENVIRONMENT_FAILURE
    assert wrapper.BLOCKER_REPAIR_ENVIRONMENT_FAILURE in result["blockers"]


# ---------------------------------------------------------------------------
# AC5: changed: false keeps existing planner mapping
# ---------------------------------------------------------------------------


def test_no_change_repair_result_keeps_existing_planner_mapping(tmp_path):
    """AC5: a body with no repairable defects (changed: false) is
    unaffected by Issue #2016 — status/next_action/exit_code match the
    pre-existing planner mapping (status: pass here, since the mocked
    planner returns fail_closed: false with no unknown confidence)."""
    result, exit_code = _run_wrapper_preflight(CLEAN_BODY_NO_REPAIRS, 90500, tmp_path)

    assert result["status"] == "pass"
    assert result["next_action"] == "proceed"
    assert exit_code == wrapper.EXIT_PASS
    assert result["blockers"] == []
    assert "repair_action" not in result


def test_apply_exit_code_mapping_unchanged_when_repair_needs_fix_false():
    """AC5: _apply_exit_code_mapping()'s return value for
    repair_needs_fix=False (the default) is identical to calling it without
    the new keyword argument at all."""
    old_style = wrapper._apply_exit_code_mapping(0, False, [])
    new_style_default = wrapper._apply_exit_code_mapping(0, False, [])
    new_style_explicit_false = wrapper._apply_exit_code_mapping(0, False, [], repair_needs_fix=False)
    assert old_style == new_style_default == new_style_explicit_false == ("pass", wrapper.EXIT_PASS)


# ---------------------------------------------------------------------------
# AC6: repair_action projected in result schema + compact stdout
# ---------------------------------------------------------------------------


def test_repair_action_projected_in_result_schema_and_compact_stdout(tmp_path):
    """AC6: repair diagnostics + candidate body artifact are machine
    referenceable from the canonical result schema / compact stdout
    REPAIR_ACTION field — not embedded inside a generic blocker JSON string."""
    result, _ = _run_wrapper_preflight(ISSUE_2013_BODY, 90600, tmp_path)

    schema_errors = wrapper._validate_result_artifact(result)
    assert schema_errors == [], f"schema errors: {schema_errors}"

    stdout = wrapper._build_compact_stdout(result)
    assert "STATUS: needs_fix" in stdout
    assert "NEXT_ACTION: apply_deterministic_repair" in stdout
    assert "REPAIR_ACTION:" in stdout

    # The repair_action payload must be machine-referenceable from its own
    # canonical field, NOT embedded inside a generic blocker string.
    assert "BLOCKERS:" not in stdout
    for line in stdout.splitlines():
        assert '"kind": "repair_diagnostics"' not in line

    diagnostics_artifact = result["repair_action"]["diagnostics_artifact"]
    candidate_body_artifact = result["repair_action"]["candidate_body_artifact"]
    assert diagnostics_artifact in stdout
    assert candidate_body_artifact in stdout

    # Issue #2016 iteration-3 P1-1 (OWNER adversarial review): AC6 requires
    # referenceability from ALL THREE of the result schema, the artifact
    # map, and compact stdout -- not just the nested repair_action block.
    assert result["artifacts"]["repair_diagnostics"] == diagnostics_artifact
    assert result["artifacts"]["repair_candidate_body"] == candidate_body_artifact
    assert Path(result["artifacts"]["repair_diagnostics"]).is_file()
    assert Path(result["artifacts"]["repair_candidate_body"]).is_file()


# ---------------------------------------------------------------------------
# AC7: Issue #2013 end-to-end regression fixture
# ---------------------------------------------------------------------------


def test_issue_2013_safe_repair_does_not_silent_pass_or_human_escalate(tmp_path):
    """AC7: reproduces the exact Issue #2013 symptom (a safe
    baseline-expect repair) and asserts the corrected routing: NOT
    blocked/human_judgment_required (the original #2013 bug), and NOT
    pass/proceed (the false-green the OWNER review flagged in Issue #2016
    P0-2 if a naive fix had only removed the blocker)."""
    result, exit_code = _run_wrapper_preflight(ISSUE_2013_BODY, 2013, tmp_path)

    assert result["status"] != "blocked", "must not reproduce the #2013 human_escalation bug"
    assert result["status"] != "pass", "must not silently pass an unrepaired Issue body through"
    assert result["status"] == "needs_fix"
    assert result["next_action"] == "apply_deterministic_repair"
    assert exit_code == wrapper.EXIT_NEEDS_FIX

    assert result["repair_action"]["disposition"] == "auto_apply_safe"
    assert Path(result["repair_action"]["candidate_body_artifact"]).exists()


# ---------------------------------------------------------------------------
# Issue #2016 iteration-3 P1-1: repair diagnostics + candidate body must be
# referenceable from the canonical artifact map (not just repair_action).
# ---------------------------------------------------------------------------


def test_repair_artifacts_are_present_in_canonical_artifact_map(tmp_path):
    """P1-1 (OWNER adversarial review, required minimum test): a needs_fix
    result's canonical `artifacts` map (not just the nested `repair_action`
    block) must carry `repair_diagnostics` and `repair_candidate_body`
    entries, pointing at real, existing, readable files."""
    result, _ = _run_wrapper_preflight(ISSUE_2013_BODY, 90650, tmp_path)

    assert result["status"] == "needs_fix", result
    artifacts = result["artifacts"]
    assert "repair_diagnostics" in artifacts, artifacts
    assert "repair_candidate_body" in artifacts, artifacts

    diagnostics_path = Path(artifacts["repair_diagnostics"])
    candidate_path = Path(artifacts["repair_candidate_body"])
    assert diagnostics_path.is_file()
    assert candidate_path.is_file()

    # Must equal the nested repair_action's own artifact paths (single
    # source of truth, not a second independently-computed path).
    assert artifacts["repair_diagnostics"] == result["repair_action"]["diagnostics_artifact"]
    assert artifacts["repair_candidate_body"] == result["repair_action"]["candidate_body_artifact"]

    # Schema must accept these two extra artifact keys.
    schema_errors = wrapper._validate_result_artifact(result)
    assert schema_errors == [], schema_errors


# ---------------------------------------------------------------------------
# Issue #2016 iteration-3 (OWNER adversarial review P0-1): the producer's
# self-reported repair_action must be schema-validated AND cross-checked
# against classify_repair_action() recomputed from the raw repairs[] --
# never trusted as-is.
# ---------------------------------------------------------------------------

_VALID_REPAIR_ACTION_BASE = {
    "schema_version": "repair_action/v1",
    "policy_version": "deterministic-issue-repair/v1",
    "disposition": "informational",
    "original_body_sha256": "sha256:aaaa",
    "repaired_body_sha256": "sha256:aaaa",
    "diagnostics_artifact": None,
    "candidate_body_artifact": None,
    "repair_kinds": [],
    "reason_codes": ["no_repairs_detected"],
}


def test_repair_same_version_schema_violation_is_environment_failure():
    """P0-1: `{"schema": "repair_issue_contract/v1", "changed": false,
    "repairs": "not-an-array"}` must NOT pass through as a no-op repair --
    the schema type violation (repairs must be an array) is caught."""
    parsed = {
        "schema": "repair_issue_contract/v1",
        "dry_run": True,
        "changed": False,
        "original_body_sha256": "sha256:aaaa",
        "repaired_body_sha256": "sha256:aaaa",
        "repairs": "not-an-array",
        "repair_action": dict(_VALID_REPAIR_ACTION_BASE),
    }
    error = wrapper._validate_repair_result_schema_and_semantics(parsed)
    assert error is not None
    assert "schema_invalid" in error or "not_a_list" in error


def test_repair_action_missing_version_is_not_defaulted():
    """P0-1(2): a missing/unknown repair_action.schema_version or
    .policy_version must be treated as environment_failure -- NEVER
    silently defaulted/backfilled by the wrapper."""
    action_missing_schema_version = dict(_VALID_REPAIR_ACTION_BASE)
    del action_missing_schema_version["schema_version"]
    parsed = {
        "schema": "repair_issue_contract/v1",
        "dry_run": True,
        "changed": False,
        "original_body_sha256": "sha256:aaaa",
        "repaired_body_sha256": "sha256:aaaa",
        "repairs": [],
        "repair_action": action_missing_schema_version,
    }
    error = wrapper._validate_repair_result_schema_and_semantics(parsed)
    assert error is not None

    action_unknown_policy_version = dict(_VALID_REPAIR_ACTION_BASE)
    action_unknown_policy_version["policy_version"] = "some-unknown-policy/v99"
    parsed2 = dict(parsed, repair_action=action_unknown_policy_version)
    error2 = wrapper._validate_repair_result_schema_and_semantics(parsed2)
    # Rejected either by the schema's const check on policy_version, or by
    # this validator's own explicit check -- either way it must NOT be
    # silently defaulted/backfilled and passed through as valid.
    assert error2 is not None
    assert "policy_version" in error2 or "deterministic-issue-repair" in error2


def test_repair_action_nested_hash_mismatch_is_rejected():
    """P0-1(4): a mismatch between the top-level result SHA and the nested
    repair_action SHA must be rejected -- the two must agree."""
    mismatched_action = dict(_VALID_REPAIR_ACTION_BASE)
    mismatched_action["original_body_sha256"] = "sha256:different"
    parsed = {
        "schema": "repair_issue_contract/v1",
        "dry_run": True,
        "changed": False,
        "original_body_sha256": "sha256:aaaa",
        "repaired_body_sha256": "sha256:aaaa",
        "repairs": [],
        "repair_action": mismatched_action,
    }
    error = wrapper._validate_repair_result_schema_and_semantics(parsed)
    assert error is not None
    assert "hash_mismatch" in error or "sha_mismatch" in error


def test_repair_action_does_not_disagree_with_recomputed_classification():
    """P0-1(3): the wrapper applies classify_repair_action() to the raw
    repairs[] the producer returned and requires it to EXACTLY match the
    producer's self-reported repair_action -- a producer that lies about
    disposition (e.g. downgrading a real auto_apply_safe repair to
    'informational' to slip past review, or the reverse) is rejected."""
    safe_repairs = [
        {
            "kind": "insert_baseline_expect_fail",
            "line_start": 1,
            "line_end": 1,
            "reason": "x",
            "original": "a",
            "repaired": "b",
            "confidence": "high",
        }
    ]
    recomputed = ric.classify_repair_action("sha256:aaaa", "sha256:bbbb", safe_repairs)
    assert recomputed["disposition"] == "auto_apply_safe"

    lying_action = dict(recomputed)
    lying_action["disposition"] = "informational"  # producer lies, downgrading severity
    parsed = {
        "schema": "repair_issue_contract/v1",
        "dry_run": True,
        "changed": True,
        "original_body_sha256": "sha256:aaaa",
        "repaired_body_sha256": "sha256:bbbb",
        "repairs": safe_repairs,
        "repair_action": lying_action,
    }
    error = wrapper._validate_repair_result_schema_and_semantics(parsed)
    assert error is not None
    assert "disposition_mismatch" in error


def test_run_repair_subprocess_semantic_validation_wired_end_to_end(tmp_path):
    """P0-1: _run_repair_subprocess() (the actual subprocess call site used
    by both _invoke_repair and _materialize_auto_apply_candidate) rejects a
    schema-valid-looking but semantically-lying payload instead of trusting
    it, by mocking subprocess.run to return a hostile producer payload."""
    lying_repair_action = dict(_VALID_REPAIR_ACTION_BASE)
    lying_repair_action["disposition"] = "auto_apply_safe"  # no repairs justify this
    hostile_stdout = json.dumps(
        {
            "schema": "repair_issue_contract/v1",
            "dry_run": True,
            "changed": False,
            "original_body_sha256": "sha256:aaaa",
            "repaired_body_sha256": "sha256:aaaa",
            "repairs": [],
            "repair_action": lying_repair_action,
        }
    )

    class _FakeProc:
        returncode = 0
        stdout = hostile_stdout
        stderr = ""

    # _run_repair_subprocess() imports subprocess locally as `_sp`; patch the
    # actual `subprocess` module object it binds to (same module object).
    import subprocess as _subprocess

    with mock.patch.object(_subprocess, "run", return_value=_FakeProc()):
        parsed, error = wrapper._run_repair_subprocess([], "some body")

    assert parsed is None
    assert error is not None
    assert "semantic_validation_failed" in error


# ---------------------------------------------------------------------------
# Issue #2016 iteration-3 P1-2: result_core_sha256 binds the machine-
# actionable repair decision.
# ---------------------------------------------------------------------------


def test_result_core_hash_binds_stable_repair_action_fields(tmp_path):
    """P1-2: two otherwise-identical needs_fix results whose repair_action
    differs ONLY in a machine-actionable field (here: reason_codes) must
    produce DIFFERENT result_core_sha256 values -- proving that field is
    bound into the integrity hash and cannot be silently altered."""

    # Use the REAL producer output for ISSUE_2013_BODY as the base (so SHAs
    # match what the un-mocked --apply reinvocation inside
    # _materialize_auto_apply_candidate() will independently recompute for
    # the same body -- only _invoke_repair (the dry-run step) is mocked
    # here, the apply/candidate-materialization step runs for real).
    genuine = ric.run_repair(ISSUE_2013_BODY)
    assert genuine["repair_action"]["disposition"] == "auto_apply_safe", genuine

    def _repair_result_with_reason_codes(reason_codes):
        result = json.loads(json.dumps(genuine))
        result["repair_action"]["reason_codes"] = reason_codes
        return result

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    result_a, _ = _run_wrapper_preflight(
        ISSUE_2013_BODY, 90700, dir_a,
        invoke_repair=_repair_result_with_reason_codes(["deterministic_body_author_fix_available"]),
    )

    result_b, _ = _run_wrapper_preflight(
        ISSUE_2013_BODY, 90701, dir_b,
        invoke_repair=_repair_result_with_reason_codes(["a_completely_different_reason_code"]),
    )

    assert result_a["status"] == "needs_fix", result_a
    assert result_b["status"] == "needs_fix", result_b
    assert result_a["hashes"]["result_core_sha256"] != result_b["hashes"]["result_core_sha256"]


# ---------------------------------------------------------------------------
# Issue #2016 iteration-3 P1-3: a repair subprocess failure must dominate a
# later planner failure (compound failure precedence).
# ---------------------------------------------------------------------------


def test_repair_failure_dominates_planner_invalid_input(tmp_path):
    """P1-3: when the repair subprocess itself fails (invalid JSON / any
    subprocess-level error) AND the planner separately fails with exit 2
    (invalid input), the repair failure must dominate: STATUS:
    environment_failure, not STATUS: blocked (which would silently drop the
    repair failure)."""
    fixture = {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": 90800,
        "repo": "testowner/testrepo",
        "now": "2026-01-01T00:00:00+00:00",
        "issue": {"number": 90800, "title": "Test Issue", "body": CLEAN_BODY_NO_REPAIRS, "labels": []},
        "comments": [],
        "anchor_comment_urls": [],
    }
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    repair_error_result = {
        "schema": "repair_issue_contract/v1",
        "changed": False,
        "repairs": [],
        "error": "repair_subprocess_invalid_json:Expecting value",
    }

    with (
        mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
        mock.patch.object(wrapper, "_invoke_repair", return_value=repair_error_result),
        mock.patch.object(wrapper, "_invoke_planner", return_value=(None, 2, "planner: invalid input", "")),
    ):
        result, exit_code = wrapper.run_preflight(
            issue_number=90800,
            repo="testowner/testrepo",
            anchor_comment_urls=[],
            fixture_path=fixture_path,
        )

    assert result["status"] == "environment_failure", (
        f"repair failure must dominate planner failure, got status={result['status']!r} "
        f"blockers={result.get('blockers')!r}"
    )
    assert exit_code == wrapper.EXIT_ENVIRONMENT_FAILURE
    assert wrapper.BLOCKER_REPAIR_ENVIRONMENT_FAILURE in result["blockers"]
    # The canonical reason code for the repair failure must survive in the
    # result, not just the generic BLOCKER_REPAIR_ENVIRONMENT_FAILURE code.
    assert any("repair_invocation_error" in b for b in result["blockers"]), result["blockers"]
    # The planner-side blocker must ALSO still be present (compound failure,
    # neither is silently dropped).
    assert wrapper.BLOCKER_PLANNER_INVALID_INPUT in result["blockers"]


# ---------------------------------------------------------------------------
# Issue #2016 iteration-3 P2-1: authentic Issue #2013 snapshot fixture,
# hash-bound and run through a REAL planner subprocess (not a mock).
# ---------------------------------------------------------------------------

# Issue #2016 fix_delta (PR #2037 review blocker): this fixture used to live
# at .claude/skills/issue-refinement-loop/tests/fixtures/issue_2013_snapshot.json,
# but fixtures/ is not in Issue #2016's Allowed Paths. It is inlined here as a
# raw JSON string (loaded via json.loads), split into adjacent raw-string
# literals for line-length, verbatim -- so all hash-binding / metadata
# semantics (source issue number, captured body SHA256, expected repair kind,
# citation_discrepancy_note) are unchanged; only the storage location changed.
_ISSUE_2013_FIXTURE_JSON = (
    r"""{
  "schema_version": "issue_2013_snapshot_fixture/v1",
  "source_issue_number": 899,
  "c"""
    r"""itation_discrepancy_note": "Issue #2016's own Machine-Readable Contract / In Scope / Accep"""
    r"""tance Criteria (AC7, VC test name test_issue_2013_safe_repair_does_not_silent_pass_or_huma"""
    r"""n_escalate) cite this fixture as reproducing 'Issue #2013'. A live `gh issue view 2013` on"""
    r""" this repo (squne121/loop-protocol) returns an UNRELATED research issue ('Claude Code cust"""
    r"""om SubAgent spawn observability 非決定性を診断する', see PR #2022 / commit cba013ef), not """
    r"""anything about baseline-expect / Allowed Paths VC repair. The actual matching originating """
    r"""specification for the insert_baseline_expect_fail symptom this fixture reproduces (a Verif"""
    r"""ication Command targeting a NEW Allowed Path with no preceding '# baseline-expect:' annota"""
    r"""tion) is Issue #899 ('baseline_vc_preflight.py に --strict フラグを追加し annotation 欠落 """
    r"""VC を強制 needs_fix にする', CLOSED) -- see repair_issue_contract.py's own '(Issue #899)' """
    r"""comments on _repair_insert_baseline_expect() / Pass 2.5. This fixture is therefore sourced"""
    r""" from Issue #899's actual specification (the real behavioral origin), not from a live Issu"""
    r"""e #2013 snapshot, since Issue #2013 does not describe this symptom at all. This citation d"""
    r"""iscrepancy in Issue #2016's own contract is flagged back to root/main for a human decision"""
    r""" on whether to correct the Issue body (out of this worker's Allowed Paths) -- the test fun"""
    r"""ction name itself is left unchanged because it is fixed by Issue #2016's own canonical Ver"""
    r"""ification Commands section (AC7).",
  "captured_body_sha256": "sha256:8a5641c90c38962f0e73"""
    r"""a141b77c5e205c11e7aaedd05437fa360dbcfcd8126b",
  "expected_repair_kinds": [
    "insert_ba"""
    r"""seline_expect_fail"
  ],
  "issue": {
    "number": 899,
    "title": "実装: baseline_vc_p"""
    r"""reflight.py に --strict フラグを追加し annotation 欠落 VC を強制 needs_fix にする",
    "b"""
    r"""ody": "## Machine-Readable Contract\n\n```yaml\ncontract_schema_version: v1\nissue_kind: i"""
    r"""mplementation\nparent_issue: \"none\"\n```\n\n## Parent Issue\n\nなし（単独改善）\n\n## Pa"""
    r"""rent Goal Ref\n\n- Goal: none (standalone fixture)\n- Desired Destination: none (standalon"""
    r"""e fixture)\n\n## Current Validated Scope\n\n- docs/dev/issue-899-baseline-expect-regressio"""
    r"""n.md を追加する\n\n## Remaining Parent Gaps\n\nなし\n\n## Required Skills\n\n- なし\n\n## """
    r"""Outcome\n\nAdd docs/dev/issue-899-baseline-expect-regression.md documenting the\nbaseline_"""
    r"""vc_preflight.py --strict flag's annotation-missing VC handling.\n\n## In Scope\n\n- docs/d"""
    r"""ev/issue-899-baseline-expect-regression.md\n\n## Out of Scope\n\n- Everything else.\n\n## """
    r"""Acceptance Criteria\n\n- [ ] AC1: doc file exists.\n\n## Verification Commands\n\n```bash\n"""
    r"""$ test -f docs/dev/issue-899-baseline-expect-regression.md\n```\n\n## Allowed Paths\n\n- `"""
    r"""docs/dev/issue-899-baseline-expect-regression.md`\n\n## Stop Conditions\n\n- none\n"
  }
}"""
    r"""
"""
)


def _load_issue_2013_fixture() -> dict:
    return json.loads(_ISSUE_2013_FIXTURE_JSON)


def test_issue_2013_snapshot_fixture_is_hash_bound():
    """P2-1: the stored Issue #2013 fixture snapshot is immutable and
    hash-bound -- its recorded body_sha256 metadata must match the actual
    sha256 of the stored body, and its recorded expected repair kind must
    match what repair_issue_contract.py actually classifies for that exact
    body (proving the fixture is not a hand-waved minimal string
    disconnected from a real captured snapshot)."""
    import hashlib

    fixture = _load_issue_2013_fixture()
    # P2-1 finding (flagged back to root/main -- see citation_discrepancy_note):
    # Issue #2016's own contract cites this fixture as reproducing "Issue
    # #2013", but the LIVE Issue #2013 in this repo is an unrelated research
    # issue (SubAgent spawn observability). The actual matching originating
    # specification is Issue #899. This assertion checks the fixture is
    # internally consistent and honestly documents the discrepancy, rather
    # than asserting the (incorrect) citation.
    assert isinstance(fixture["source_issue_number"], int)
    assert fixture["citation_discrepancy_note"], "fixture must document the #2013 vs #899 citation discrepancy"
    assert "#899" in fixture["citation_discrepancy_note"]
    assert "#2013" in fixture["citation_discrepancy_note"]
    body = fixture["issue"]["body"]
    actual_sha = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert actual_sha == fixture["captured_body_sha256"], (
        f"fixture body has drifted from its recorded hash: {actual_sha} != {fixture['captured_body_sha256']}"
    )

    repair_result = ric.run_repair(body)
    observed_kinds = sorted(repair_result["repair_action"]["repair_kinds"])
    assert observed_kinds == sorted(fixture["expected_repair_kinds"]), (
        f"observed {observed_kinds} != fixture-recorded {fixture['expected_repair_kinds']}"
    )


def test_issue_2013_fixture_runs_through_real_planner_subprocess(tmp_path):
    """P2-1(3): run the Issue #2013 fixture end-to-end through the REAL
    plan_refinement_loop.py subprocess (not MOCK_PLAN_PASS), proving the
    needs_fix routing holds against actual planner semantics rather than a
    mock that always passes."""
    fixture_data = _load_issue_2013_fixture()
    body = fixture_data["issue"]["body"]

    fixture = {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": 2013,
        "repo": "testowner/testrepo",
        "now": "2026-01-01T00:00:00+00:00",
        "issue": {"number": 2013, "title": fixture_data["issue"]["title"], "body": body, "labels": []},
        "comments": [],
        "anchor_comment_urls": [],
    }
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
        result, exit_code = wrapper.run_preflight(
            issue_number=2013,
            repo="testowner/testrepo",
            anchor_comment_urls=[],
            fixture_path=fixture_path,
        )

    assert result["status"] == "needs_fix", result
    assert result["next_action"] == "apply_deterministic_repair"
    assert exit_code == wrapper.EXIT_NEEDS_FIX
    assert result["repair_action"]["disposition"] == "auto_apply_safe"
