"""
test_reviewer_claim_replay_taxonomy_parity.py

Issue #1286 AC1-AC5: reviewer_claim_replay.py normalizes reviewer codes /
deterministic check names / readiness rule ids / readiness categories /
domain keys through a single table-driven taxonomy
(REVIEWER_CHECKER_TAXONOMY_V1), and:

- AC1: "c5" / "vc_number_alignment" / "C5_ac_vc_number_alignment" normalize
  to the same taxonomy entry.
- AC2: "c9" / "rva_immediate_field_missing" / "C9_runtime_applicability_present"
  normalize to the same taxonomy entry.
- AC3: a blocker backed by a failing deterministic check for the same body
  hash is never routed back through the reviewer-rerun lanes.
- AC4: a reviewer blocker code unregistered in the taxonomy is classified as
  checker_gap, and reviewer rerun is capped at exactly one attempt before
  escalation.
- AC5: `--dump-taxonomy` prints the taxonomy as JSON, and this test detects
  drift in the checker / reviewer code / readiness category sets.

Backward compatibility: `analyze()` also emits a `verdict_legacy_v1` field
that downgrades the new Issue #1286 verdicts (`taxonomy_gap`, `checker_gap`,
`checker_gap_repeated`) to the pre-#1286 verdict set documented in
`issue-refinement-loop/SKILL.md` Step 2a, so a routing table that has not
been updated for the new values still receives a value it recognizes with
matching routing semantics. `SKILL.md` update itself is Out of Scope for
Issue #1286 (not in Allowed Paths); `test_legacy_verdict_mapping_full_parity`
below pins the full mapping.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "reviewer_claim_replay.py"
sys.path.insert(0, str(SCRIPTS_DIR))

from reviewer_claim_replay import (  # noqa: E402
    REVIEWER_CHECKER_TAXONOMY_V1,
    analyze,
    normalize_taxonomy_key,
)

READINESS_CLEAN = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": "sha256:body-a",
    "errors": [],
}


# --------------------------------------------------------------------------
# AC1: c5 / vc_number_alignment / C5_ac_vc_number_alignment -> single entry
# --------------------------------------------------------------------------


def test_ac1_c5_aliases_normalize_to_single_entry():
    assert normalize_taxonomy_key("c5") == "ac_vc_number_mismatch"
    assert normalize_taxonomy_key("vc_number_alignment") == "ac_vc_number_mismatch"
    assert normalize_taxonomy_key("C5_ac_vc_number_alignment") == "ac_vc_number_mismatch"

    # Behavioral parity: analyze() classifies all three aliases identically.
    for alias in ("c5", "vc_number_alignment", "C5_ac_vc_number_alignment"):
        review = {
            "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
            "issue_url": "https://github.com/squne121/loop-protocol/issues/1286",
            "blocking_issues": [{"code": alias, "message": "ac/vc mismatch"}],
            "structured_blockers": [],
        }
        readiness = {
            "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
            "body_sha256": "sha256:body-a",
            "errors": [
                {
                    "rule_id": "LP010",
                    "source_check": "validate_issue_body",
                    "category": "body_lint",
                    "line_start": 5,
                    "line_end": 5,
                }
            ],
        }
        result, _ = analyze(
            review_result=review,
            readiness_result=readiness,
            vc_syntax_result=None,
            vc_preflight_result=None,
            previous_state={},
        )
        assert result["blockers"][0]["normalized_kind"] == "ac_vc_number_mismatch", alias


# --------------------------------------------------------------------------
# AC2: c9 / rva_immediate_field_missing / C9_runtime_applicability_present -> single entry
# --------------------------------------------------------------------------


def test_ac2_c9_aliases_normalize_to_single_entry():
    assert normalize_taxonomy_key("c9") == "rva_immediate_field_missing"
    assert normalize_taxonomy_key("rva_immediate_field_missing") == "rva_immediate_field_missing"
    assert normalize_taxonomy_key("C9_runtime_applicability_present") == "rva_immediate_field_missing"

    for alias in ("c9", "rva_immediate_field_missing", "C9_runtime_applicability_present"):
        review = {
            "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
            "issue_url": "https://github.com/squne121/loop-protocol/issues/1286",
            "blocking_issues": [{"code": alias, "message": "missing runtime applicability"}],
            "structured_blockers": [],
        }
        readiness = {
            "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
            "body_sha256": "sha256:body-a",
            "errors": [
                {
                    "rule_id": "C9_runtime_applicability_present",
                    "source_check": "contract_readiness_check",
                    "category": "rva_immediate_field_missing",
                    "line_start": 8,
                    "line_end": 8,
                }
            ],
        }
        result, _ = analyze(
            review_result=review,
            readiness_result=readiness,
            vc_syntax_result=None,
            vc_preflight_result=None,
            previous_state={},
        )
        assert result["blockers"][0]["normalized_kind"] == "rva_immediate_field_missing", alias


# --------------------------------------------------------------------------
# AC3: deterministic-check-fail blocker (same body hash) never returns to a
# reviewer-rerun lane, even when structured findings / fallback evidence are
# both absent (the checker/taxonomy-artifact gap scenario from Issue #1277).
# --------------------------------------------------------------------------


def test_ac3_checker_backed_blocker_no_reviewer_rerun():
    review = {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1286",
        "body_sha256": "sha256:body-a",
        "blocking_issues": [{"code": "C5", "message": "ac/vc mismatch"}],
        "structured_blockers": [],
        # No `findings` key at all -- simulates a reviewer artifact that
        # omitted structured findings for this domain.
        "deterministic_checks": {"C5_ac_vc_number_alignment": "fail"},
    }
    # readiness has no LP010 entry -- simulates the category/evidence
    # mapping gap described in Issue #1286 background (#1277).
    result, next_state = analyze(
        review_result=review,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "taxonomy_gap"
    assert result["should_consume_iteration"] is False
    # Must not be routed through either reviewer-rerun lane.
    assert result["routing"] not in ("downgrade_to_non_blocking", "human_escalation")
    assert result["blockers"][0]["taxonomy_gap"] is True
    assert result["blockers"][0]["normalized_kind"] == "ac_vc_number_mismatch"
    # A taxonomy_gap verdict must not advance the reviewer-rerun counter --
    # a subsequent identical replay would otherwise be misclassified as a
    # repeated reviewer claim.
    assert next_state["consecutive_unbacked_count"] == 0
    # Backward-compat: a consumer that only understands the pre-#1286
    # SKILL.md Step 2a verdict set must still receive a recognized value
    # with matching routing semantics (fix_checker_artifact family).
    assert result["verdict_legacy_v1"] == "checker_artifact_inconsistency"


# --------------------------------------------------------------------------
# AC4: an unregistered reviewer blocker code is classified as checker_gap,
# and a second occurrence in the same lane (same code / body hash) escalates
# instead of allowing a further rerun (max one rerun).
# --------------------------------------------------------------------------


def test_ac4_unknown_blocker_checker_gap_single_rerun():
    review = {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1286",
        "blocking_issues": [{"code": "z9_totally_unregistered_blocker", "message": "??"}],
        "structured_blockers": [],
    }

    first, next_state = analyze(
        review_result=review,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert first["blockers"][0]["normalized_kind"] == "unknown_blocker_type"
    assert first["blockers"][0]["checker_gap"] is True
    assert first["verdict"] == "checker_gap"
    assert first["routing"] == "downgrade_to_non_blocking"
    assert first["should_consume_iteration"] is False
    assert next_state["consecutive_unbacked_count"] == 1
    # Backward-compat: checker_gap downgrades to the legacy "unbacked"
    # verdict (same downgrade_to_non_blocking routing semantics).
    assert first["verdict_legacy_v1"] == "reviewer_claim_unbacked_by_deterministic_checker"

    # Second occurrence, same lane (same code + same body hash) -> escalate,
    # do not grant a second rerun.
    second, next_state_2 = analyze(
        review_result=review,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state=next_state,
    )
    assert second["verdict"] == "checker_gap_repeated"
    assert second["routing"] == "human_escalation"
    assert second["should_consume_iteration"] is False
    assert next_state_2["consecutive_unbacked_count"] == 2
    # Backward-compat: checker_gap_repeated downgrades to the legacy
    # false-positive-suspected verdict (same human_escalation routing).
    assert second["verdict_legacy_v1"] == "reviewer_false_positive_suspected"


# --------------------------------------------------------------------------
# AC5: --dump-taxonomy prints JSON, and this test pins the checker /
# reviewer-code / readiness-category sets so any drift (rename, removal,
# silent addition without test coverage) fails closed.
# --------------------------------------------------------------------------


def _run_dump_taxonomy() -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--dump-taxonomy"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_ac5_dump_taxonomy_json_drift_detection():
    dumped = _run_dump_taxonomy()
    assert dumped["schema"] == "REVIEWER_CHECKER_TAXONOMY_V1"
    entries = dumped["entries"]
    assert isinstance(entries, list) and entries

    entries_by_id = {entry["entry_id"]: entry for entry in entries}

    # Pin the known entry_id set. Any addition/removal must update this test
    # (that is the intended drift-detection property of AC5).
    assert set(entries_by_id) == {
        "vc_command_format",
        "ac_vc_number_mismatch",
        "missing_section",
        "rva_immediate_field_missing",
    }

    # Pin deterministic_checks (checker name) parity per entry.
    expected_checks = {
        "vc_command_format": ["C4_vc_commands_present"],
        "ac_vc_number_mismatch": ["C5_ac_vc_number_alignment"],
        "missing_section": ["C1_required_sections"],
        "rva_immediate_field_missing": ["C9_runtime_applicability_present"],
    }
    for entry_id, expected in expected_checks.items():
        assert entries_by_id[entry_id]["deterministic_checks"] == expected, entry_id

    # Pin reviewer_codes parity for the two AC1/AC2 entries specifically.
    assert set(entries_by_id["ac_vc_number_mismatch"]["reviewer_codes"]) >= {"c5", "lp010"}
    assert set(entries_by_id["rva_immediate_field_missing"]["reviewer_codes"]) >= {
        "c9",
        "rva_immediate_field_missing",
    }

    # Pin readiness_categories parity (drift here silently breaks fallback
    # evidence matching for vc_command_format / rva_immediate_field_missing).
    assert set(entries_by_id["vc_command_format"]["readiness_categories"]) == {
        "non_dollar_command",
        "compound_shell",
        "compound_command_disallowed",
        "no_commands_extracted",
    }
    assert set(entries_by_id["rva_immediate_field_missing"]["readiness_categories"]) == {
        "rva_immediate_field_missing"
    }

    # The in-process constant and the CLI-dumped JSON must be identical --
    # this is the actual "drift" the CLI flag is meant to catch (the dump
    # must not silently diverge from the live REVIEWER_CHECKER_TAXONOMY_V1
    # used by analyze()).
    assert entries == REVIEWER_CHECKER_TAXONOMY_V1


# --------------------------------------------------------------------------
# Backward compatibility (PR #1304 review fix_delta): verdict_legacy_v1 must
# downgrade every new Issue #1286 verdict to a value already documented in
# SKILL.md Step 2a, with routing semantics preserved (no unbacked "unknown"
# fallback for a value this module itself produces).
# --------------------------------------------------------------------------


def test_legacy_verdict_mapping_full_parity():
    from reviewer_claim_replay import _legacy_verdict  # noqa: WPS433

    expected = {
        "deterministic_fail_confirmed": "deterministic_fail_confirmed",
        "checker_artifact_inconsistency": "checker_artifact_inconsistency",
        "reviewer_claim_unbacked_by_deterministic_checker": (
            "reviewer_claim_unbacked_by_deterministic_checker"
        ),
        "reviewer_false_positive_suspected": "reviewer_false_positive_suspected",
        "input_or_runtime_error": "input_or_runtime_error",
        "taxonomy_gap": "checker_artifact_inconsistency",
        "checker_gap": "reviewer_claim_unbacked_by_deterministic_checker",
        "checker_gap_repeated": "reviewer_false_positive_suspected",
    }
    for verdict, legacy in expected.items():
        assert _legacy_verdict(verdict) == legacy, verdict

    # The legacy mapping's codomain must be exactly the pre-#1286 SKILL.md
    # Step 2a documented verdict set (plus the pre-existing
    # checker_artifact_inconsistency) -- never one of the new-only values.
    assert set(expected.values()) == {
        "deterministic_fail_confirmed",
        "checker_artifact_inconsistency",
        "reviewer_claim_unbacked_by_deterministic_checker",
        "reviewer_false_positive_suspected",
        "input_or_runtime_error",
    }


def test_ac5_dump_taxonomy_does_not_require_other_args():
    # --dump-taxonomy must work standalone (no --review-result-file /
    # --readiness-result-file required).
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--dump-taxonomy"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert "\n" not in proc.stdout.strip()
