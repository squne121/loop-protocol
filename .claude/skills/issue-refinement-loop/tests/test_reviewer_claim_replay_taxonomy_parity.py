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

Backward compatibility (PR #1304 iteration-2 fix_delta): the top-level
`verdict` field ALWAYS returns one of the pre-#1286 verdict set documented
in `issue-refinement-loop/SKILL.md` Step 2a (a routing table that has not
been updated for the new Issue #1286 values still receives a value it
recognizes, with matching routing semantics). The precise Issue #1286
classification (including the new-only `taxonomy_gap` / `checker_gap` /
`checker_gap_repeated` values) is carried in the secondary
`verdict_detail_v1` field instead. `SKILL.md` update itself is Out of Scope
for Issue #1286 (not in Allowed Paths);
`test_legacy_verdict_mapping_full_parity` below pins the full mapping.

PR #1304 iteration-4 fix_delta additions:
- every inline `review` fixture below now carries a `body_sha256` /
  `producer_body_sha256` matching its paired readiness fixture, because
  `analyze()` now fails closed on a missing/mismatched body hash pair
  (see `test_reviewer_claim_replay.py::test_body_hash_*` for the
  fail-closed behavior itself).
- `TestTaxonomyDumpInvariants` pins the Medium-severity taxonomy dump /
  schema invariants (duplicate detection across entries, JSON Schema
  validation, unknown-property rejection, stable `sort_keys=True` output).
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "reviewer_claim_replay.py"
sys.path.insert(0, str(SCRIPTS_DIR))

from reviewer_claim_replay import (  # noqa: E402
    REVIEWER_CHECKER_TAXONOMY_V1,
    analyze,
    dump_taxonomy,
    normalize_taxonomy_key,
    taxonomy_invariant_violations,
    validate_taxonomy_dump,
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
            "body_sha256": "sha256:body-a",
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
            "body_sha256": "sha256:body-a",
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
    # Precise Issue #1286 classification is carried in verdict_detail_v1.
    assert result["verdict_detail_v1"] == "taxonomy_gap"
    assert result["should_consume_iteration"] is False
    # Must not be routed through either reviewer-rerun lane.
    assert result["routing"] not in ("downgrade_to_non_blocking", "human_escalation")
    assert result["blockers"][0]["taxonomy_gap"] is True
    assert result["blockers"][0]["normalized_kind"] == "ac_vc_number_mismatch"
    # A taxonomy_gap verdict must not advance the reviewer-rerun counter --
    # a subsequent identical replay would otherwise be misclassified as a
    # repeated reviewer claim.
    assert next_state["consecutive_unbacked_count"] == 0
    # Backward-compat: the top-level `verdict` field is ALWAYS a value a
    # consumer that only understands the pre-#1286 SKILL.md Step 2a verdict
    # set recognizes, with matching routing semantics (fix_checker_artifact
    # family) -- SKILL.md's routing table itself is not updated for
    # taxonomy_gap (Issue #1286 Allowed Paths do not include SKILL.md).
    assert result["verdict"] == "checker_artifact_inconsistency"
    # This blocker must also appear in the grouped taxonomy_gap_blockers
    # list, and nowhere else (PR #1304 iteration-4 fix_delta, High item).
    assert len(result["taxonomy_gap_blockers"]) == 1
    assert result["rewrite_ready_blockers"] == []
    assert result["checker_gap_blockers"] == []


# --------------------------------------------------------------------------
# AC4: an unregistered reviewer blocker code is classified as checker_gap,
# and a second occurrence in the same lane (same code / body hash) escalates
# instead of allowing a further rerun (max one rerun).
# --------------------------------------------------------------------------


def test_ac4_unknown_blocker_checker_gap_single_rerun():
    review = {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1286",
        "body_sha256": "sha256:body-a",
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
    # Precise Issue #1286 classification lives in verdict_detail_v1.
    assert first["verdict_detail_v1"] == "checker_gap"
    assert first["routing"] == "downgrade_to_non_blocking"
    assert first["should_consume_iteration"] is False
    assert next_state["consecutive_unbacked_count"] == 1
    # Backward-compat: the top-level `verdict` field downgrades checker_gap
    # to the legacy "unbacked" verdict (same downgrade_to_non_blocking
    # routing semantics) -- SKILL.md Step 2a's routing table is not
    # updated for checker_gap (Allowed Paths do not include SKILL.md).
    assert first["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"

    # Second occurrence, same lane (same code + same body hash) -> escalate,
    # do not grant a second rerun.
    second, next_state_2 = analyze(
        review_result=review,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state=next_state,
    )
    assert second["verdict_detail_v1"] == "checker_gap_repeated"
    assert second["routing"] == "human_escalation"
    assert second["should_consume_iteration"] is False
    assert next_state_2["consecutive_unbacked_count"] == 2
    # Backward-compat: the top-level `verdict` field downgrades
    # checker_gap_repeated to the legacy false-positive-suspected verdict
    # (same human_escalation routing semantics).
    assert second["verdict"] == "reviewer_false_positive_suspected"


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


# --------------------------------------------------------------------------
# PR #1304 iteration-4 fix_delta (Medium item): taxonomy dump / schema
# invariants. `dump_taxonomy()` is the single producer of the
# `--dump-taxonomy` payload; `TAXONOMY_DUMP_SCHEMA_V1` pins its structure
# (required keys, `additionalProperties: false`); `taxonomy_invariant_
# violations()` detects duplicate reviewer codes / checker names / domain
# keys / readiness rule ids / (source_check, category) pairs across
# entries.
# --------------------------------------------------------------------------


def test_dump_taxonomy_matches_module_constant():
    payload = dump_taxonomy()
    assert payload["schema"] == "REVIEWER_CHECKER_TAXONOMY_V1"
    assert payload["entries"] == REVIEWER_CHECKER_TAXONOMY_V1


def test_dump_taxonomy_json_stdout_is_sorted_keys():
    # sort_keys=True (PR #1304 iteration-4 fix_delta): the CLI's JSON
    # output must be byte-for-byte reproducible across runs, and the raw
    # stdout bytes must already be in sorted-key form (re-dumping the
    # parsed payload with sort_keys=True must reproduce the exact same
    # bytes).
    first = _run_dump_taxonomy()
    second = _run_dump_taxonomy()
    assert first == second
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--dump-taxonomy"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    raw = proc.stdout.strip()
    reserialized = json.dumps(json.loads(raw), separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    assert raw == reserialized


def test_real_taxonomy_has_no_invariant_violations():
    assert taxonomy_invariant_violations() == []


def test_real_taxonomy_dump_matches_schema():
    # Must not raise.
    validate_taxonomy_dump(dump_taxonomy())


def test_taxonomy_dump_rejects_unknown_property():
    payload = dump_taxonomy()
    corrupted = {**payload, "entries": [dict(payload["entries"][0], unexpected_field="x")] + payload["entries"][1:]}
    with pytest.raises(jsonschema.ValidationError):
        validate_taxonomy_dump(corrupted)


def test_taxonomy_dump_rejects_unknown_top_level_property():
    payload = dump_taxonomy()
    corrupted = {**payload, "unexpected_top_level": True}
    with pytest.raises(jsonschema.ValidationError):
        validate_taxonomy_dump(corrupted)


def test_taxonomy_dump_requires_declared_keys():
    payload = dump_taxonomy()
    entry_missing_key = dict(payload["entries"][0])
    del entry_missing_key["domain_keys"]
    corrupted = {**payload, "entries": [entry_missing_key] + payload["entries"][1:]}
    with pytest.raises(jsonschema.ValidationError):
        validate_taxonomy_dump(corrupted)


def test_duplicate_entry_id_detected():
    broken = copy.deepcopy(REVIEWER_CHECKER_TAXONOMY_V1)
    broken.append(copy.deepcopy(broken[0]))
    violations = taxonomy_invariant_violations(broken)
    assert any("duplicate entry_id" in v for v in violations)


def test_duplicate_reviewer_code_across_entries_detected():
    broken = copy.deepcopy(REVIEWER_CHECKER_TAXONOMY_V1)
    broken[1]["entry_id"] = "duplicate_reviewer_code_entry"
    broken[1]["reviewer_codes"] = list(broken[0]["reviewer_codes"])
    broken[1]["deterministic_checks"] = ["some_other_check"]
    broken[1]["domain_keys"] = ["some_other_domain"]
    violations = taxonomy_invariant_violations(broken)
    assert any("duplicate reviewer_code" in v for v in violations)


def test_duplicate_deterministic_check_across_entries_detected():
    broken = copy.deepcopy(REVIEWER_CHECKER_TAXONOMY_V1)
    broken[1]["entry_id"] = "duplicate_check_entry"
    broken[1]["reviewer_codes"] = ["some_other_reviewer_code"]
    broken[1]["deterministic_checks"] = list(broken[0]["deterministic_checks"])
    broken[1]["domain_keys"] = ["some_other_domain"]
    violations = taxonomy_invariant_violations(broken)
    assert any("duplicate deterministic_check" in v for v in violations)


def test_duplicate_domain_key_across_entries_detected():
    broken = copy.deepcopy(REVIEWER_CHECKER_TAXONOMY_V1)
    broken[1]["entry_id"] = "duplicate_domain_key_entry"
    broken[1]["reviewer_codes"] = ["some_other_reviewer_code_2"]
    broken[1]["deterministic_checks"] = ["some_other_check_2"]
    broken[1]["domain_keys"] = list(broken[0]["domain_keys"])
    violations = taxonomy_invariant_violations(broken)
    assert any("duplicate domain_key" in v for v in violations)


def test_duplicate_readiness_rule_id_across_entries_detected_no_allowlist():
    broken = copy.deepcopy(REVIEWER_CHECKER_TAXONOMY_V1)
    # vc_command_format already declares readiness_rule_ids; reuse one of
    # them on a different entry to force a cross-entry duplicate. No
    # allowlist/exception exists for this invariant.
    broken[1]["readiness_rule_ids"] = list(broken[0]["readiness_rule_ids"][:1])
    violations = taxonomy_invariant_violations(broken)
    assert any("duplicate readiness_rule_id" in v for v in violations)


def test_duplicate_source_check_category_pair_across_entries_detected():
    broken = copy.deepcopy(REVIEWER_CHECKER_TAXONOMY_V1)
    # vc_command_format entry (index 0) already declares
    # ("contract_readiness_check", "non_dollar_command"); reuse the same
    # pair on rva_immediate_field_missing (index 3), which already shares
    # the same readiness_category_source_check.
    broken[3]["readiness_categories"] = list(broken[0]["readiness_categories"][:1])
    violations = taxonomy_invariant_violations(broken)
    assert any("duplicate (source_check, category)" in v for v in violations)


def test_taxonomy_invariant_violations_empty_for_disjoint_minimal_table():
    minimal = [
        {
            "entry_id": "a",
            "reviewer_codes": ["code_a"],
            "deterministic_checks": ["check_a"],
            "readiness_rule_ids": ["RULE_A"],
            "readiness_rule_id_source_check": None,
            "readiness_categories": ["cat_a"],
            "readiness_category_source_check": "source_a",
            "domain_keys": ["domain_a"],
        },
        {
            "entry_id": "b",
            "reviewer_codes": ["code_b"],
            "deterministic_checks": ["check_b"],
            "readiness_rule_ids": ["RULE_B"],
            "readiness_rule_id_source_check": None,
            "readiness_categories": ["cat_b"],
            "readiness_category_source_check": "source_b",
            "domain_keys": ["domain_b"],
        },
    ]
    assert taxonomy_invariant_violations(minimal) == []
    validate_taxonomy_dump({"schema": "REVIEWER_CHECKER_TAXONOMY_V1", "entries": minimal})
