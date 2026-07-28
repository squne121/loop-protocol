"""
test_refinement_phase_gate_validation_seam_gates.py

Issue #1755 AC2-AC6: dedicated fail-closed / pass-closed coverage for the
review-phase gate's binding to the child-intermediate validator output
(`REVIEW_COMPACT_INTERMEDIATE_VALIDATION_RESULT_V1`,
`review_compact.validate_intermediate_v1`), independent of
`test_refinement_phase_gate.py` / `test_refinement_phase_gate_validation_seam.py`
(which cover the general phase-gate / AC24 fail-closed seam).

AC2 (`test_legacy_schema_literal_rejected`): a validation result whose
`schema` is the legacy `REVIEW_COMPACT_VALIDATION_RESULT_V1` literal is
fail-closed rejected (non-zero exit, no phase-state file written) even when
every other field looks otherwise valid.

AC3 (`test_gate_rejects_malformed_intermediate_result`): `schema_version !=
"1"` / `validation_status != "valid"` / `envelope_kind` outside
{"approve", "needs_fix_intermediate"} / non-empty `violations` are each
fail-closed rejected (parametrized).

AC4 (`test_gate_rejects_input_sha256_mismatch`): a validation result whose
`input_sha256` does not match the SHA256 recomputed from `--source-path`'s
actual bytes (stale / cross-input receipt) is fail-closed rejected.

AC5 (`test_gate_rejects_issue_number_mismatch`): a validation result whose
`normalized_payload.ARTIFACT` issue-number segment does not match
`--issue-number` (cross-issue receipt) is fail-closed rejected.

AC6 (`test_gate_accepts_real_producer_intermediate_result`): the REAL
producer chain (`compact_review_result.py` -> real needs-fix stdout ->
`emit_parent_review_envelope_v2.py --validate-intermediate` ->
`build_refinement_phase_state.py`) succeeds end-to-end (regression
confirmation that the AC2-AC5 gates do not break the normal-path flow).

Issue #1755 fix_delta (OWNER REQUEST_CHANGES, PR #1826) P0/P2/P3: additional
coverage for the "forged receipt" closing check (the gate now re-runs the
REAL `review_compact.validate_intermediate_v1` validator against
`--source-path`'s actual bytes and requires an exact match against the
caller-supplied validation result, instead of trusting individually-checked
fields alone):

- `test_gate_rejects_forged_receipt_for_grammar_invalid_raw_source`: raw
  source is grammar-invalid, but the paired receipt merely CLAIMS
  `validation_status: valid` -- rejected (P0).
- `test_gate_rejects_forged_receipt_envelope_kind_approve_with_needs_fix_payload`:
  `envelope_kind: approve` claimed, but `normalized_payload` carries the
  needs-fix `REVIEWER_BLOCKER_CLAIM` field -- rejected (P0).
- `test_gate_rejects_forged_receipt_needs_fix_missing_canonical_claim`: a
  real needs-fix receipt with `canonical_reviewer_blocker_claim` forged to
  `None` -- rejected (P0).
- `test_gate_rejects_input_byte_count_mismatch`: `input_byte_count` disjoint
  from `--source-path`'s actual byte count -- rejected (P2-2).
- `test_gate_rejects_duplicate_json_key_top_level` /
  `test_gate_rejects_duplicate_json_key_normalized_payload`: duplicate JSON
  object keys at the top level / inside `normalized_payload` -- rejected
  (P2-1).
- `test_gate_rejects_unknown_top_level_field`: an extra top-level field not
  in the schema -- rejected (P2-5).
- `test_gate_rejects_oversized_source_bounded_read`: `--source-path` larger
  than the bounded-read cap -- rejected via a bounded (never unbounded) read
  (P2-2).
- `test_gate_accepts_real_producer_approve_intermediate_result`: the REAL
  producer chain for the APPROVE path (mirrors AC6's needs-fix coverage) --
  regression confirmation for the approve envelope shape (P2-3).
- `test_build_phase_state_direct_call_rejects_non_positive_issue_number`:
  calling `build_phase_state()` directly (bypassing argparse) with a
  non-positive `issue_number` is rejected by the gate function itself (P3).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
FIXTURES_DIR = SKILL_ROOT / "fixtures"
BUILD_SCRIPT = SCRIPTS_DIR / "build_refinement_phase_state.py"
COMPACT_SCRIPT = SCRIPTS_DIR / "compact_review_result.py"
EMIT_SCRIPT = SCRIPTS_DIR / "emit_parent_review_envelope_v2.py"
NEEDS_FIX_FIXTURE = FIXTURES_DIR / "review_result_needs_fix.json"
APPROVE_FIXTURE = FIXTURES_DIR / "review_result_approve.json"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_refinement_phase_state as _phase_state_builder  # noqa: E402
import emit_parent_review_envelope_v2 as _emit2  # noqa: E402

_INTERMEDIATE_SCHEMA = "REVIEW_COMPACT_INTERMEDIATE_VALIDATION_RESULT_V1"
_LEGACY_SCHEMA = "REVIEW_COMPACT_VALIDATION_RESULT_V1"


def _artifact_value(issue_number: "int | str") -> str:
    return (
        "compact_review_result_v1="
        f".claude/artifacts/issue-refinement-loop/{issue_number}/"
        "compact_review_result_fixture.json"
    )


def _real_approve_child_bytes(issue_number: int) -> bytes:
    """A REAL, grammar-valid 8-line child-intermediate approve envelope
    (Issue #1755 fix_delta P0), used so a genuine receipt can be produced by
    actually RUNNING the real validator against these bytes."""
    artifact_path = (
        f".claude/artifacts/issue-refinement-loop/{issue_number}/"
        "compact_review_result_20260728T000000Z.json"
    )
    lines = [
        "STATUS: ok",
        "VERDICT: approve",
        "SUMMARY: contract ready",
        "BLOCKERS: 0",
        "NEXT_ACTION: proceed",
        "MUST_READ: ",
        f"EVIDENCE: {artifact_path}",
        f"ARTIFACT: compact_review_result_v1={artifact_path}",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _valid_validation_payload(
    source_bytes: bytes, *, issue_number: "int | str" = 1755
) -> dict:
    """A schema-correct REVIEW_COMPACT_INTERMEDIATE_VALIDATION_RESULT_V1
    payload bound to `source_bytes` and `issue_number` (all AC2-AC5 checks
    pass for this baseline; individual tests below mutate exactly one
    field to prove each gate fires)."""
    return {
        "schema": _INTERMEDIATE_SCHEMA,
        "schema_version": "1",
        "validation_status": "valid",
        "envelope_kind": "approve",
        "input_sha256": f"sha256:{hashlib.sha256(source_bytes).hexdigest()}",
        "input_byte_count": len(source_bytes),
        "normalized_payload": {"ARTIFACT": _artifact_value(issue_number)},
        "canonical_reviewer_blocker_claim": None,
        "violations": [],
    }


def _run_build(
    tmp_path: Path,
    validation_payload: dict,
    *,
    source_bytes: bytes = b"{}",
    issue_number: "int | None" = 1755,
) -> subprocess.CompletedProcess:
    source_file = tmp_path / "source.txt"
    source_file.write_bytes(source_bytes)
    validation_file = tmp_path / "validation_result.json"
    validation_file.write_text(json.dumps(validation_payload), encoding="utf-8")
    out_file = tmp_path / "phase_state.json"

    argv = [
        sys.executable,
        str(BUILD_SCRIPT),
        "--phase", "review",
        "--source-kind", "issue_review_result_compact_v1",
        "--source-path", str(source_file),
        "--review-validation-result-path", str(validation_file),
        "--output-path", str(out_file),
    ]
    if issue_number is not None:
        argv += ["--issue-number", str(issue_number)]

    proc = subprocess.run(argv, capture_output=True, text=True)
    proc.out_file = out_file  # type: ignore[attr-defined]
    return proc


# ---------------------------------------------------------------------------
# AC2: legacy schema literal rejected
# ---------------------------------------------------------------------------


def test_legacy_schema_literal_rejected(tmp_path):
    source_bytes = b"{}"
    payload = _valid_validation_payload(source_bytes)
    payload["schema"] = _LEGACY_SCHEMA

    proc = _run_build(tmp_path, payload, source_bytes=source_bytes)

    assert proc.returncode != 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert not proc.out_file.exists(), (  # type: ignore[attr-defined]
        "phase-state file must NOT be written for legacy schema literal (AC2)"
    )


# ---------------------------------------------------------------------------
# AC3: malformed intermediate result rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate,expected_reason_snippet",
    [
        (lambda p: p.__setitem__("schema_version", "2"), "schema_version"),
        (lambda p: p.__setitem__("validation_status", "invalid"), "validation_status"),
        (lambda p: p.__setitem__("envelope_kind", "unknown"), "envelope_kind"),
        (
            lambda p: p.__setitem__(
                "violations", [{"code": "unknown_field", "field": "FOO"}]
            ),
            "violations",
        ),
    ],
    ids=[
        "schema_version_mismatch",
        "validation_status_not_valid",
        "envelope_kind_invalid",
        "violations_non_empty",
    ],
)
def test_gate_rejects_malformed_intermediate_result(
    tmp_path, mutate, expected_reason_snippet
):
    source_bytes = b"{}"
    payload = _valid_validation_payload(source_bytes)
    mutate(payload)

    proc = _run_build(tmp_path, payload, source_bytes=source_bytes)

    assert proc.returncode != 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert not proc.out_file.exists(), (  # type: ignore[attr-defined]
        f"phase-state file must NOT be written for malformed intermediate result "
        f"({expected_reason_snippet}, AC3)"
    )
    assert expected_reason_snippet in proc.stdout, (
        f"Expected error message to mention {expected_reason_snippet!r}:\n{proc.stdout}"
    )


# ---------------------------------------------------------------------------
# AC4: input_sha256 mismatch (stale / cross-input receipt) rejected
# ---------------------------------------------------------------------------


def test_gate_rejects_input_sha256_mismatch(tmp_path):
    source_bytes = b"{}"
    payload = _valid_validation_payload(source_bytes)
    # Tamper input_sha256 so it no longer matches source_bytes' actual digest.
    payload["input_sha256"] = "sha256:" + "0" * 64

    proc = _run_build(tmp_path, payload, source_bytes=source_bytes)

    assert proc.returncode != 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert not proc.out_file.exists(), (  # type: ignore[attr-defined]
        "phase-state file must NOT be written for input_sha256 mismatch (AC4)"
    )
    assert "input_sha256" in proc.stdout, (
        f"Expected error message to mention input_sha256:\n{proc.stdout}"
    )


def test_gate_rejects_input_sha256_mismatch_different_actual_source(tmp_path):
    """AC4 variant: the validation result's input_sha256 IS internally
    consistent with SOME bytes, but --source-path's actual content differs
    (stale / different-iteration receipt combined with the current source
    artifact)."""
    stale_bytes = b"stale child stdout bytes from a previous iteration"
    payload = _valid_validation_payload(stale_bytes)

    different_actual_bytes = b"{}"
    proc = _run_build(tmp_path, payload, source_bytes=different_actual_bytes)

    assert proc.returncode != 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert not proc.out_file.exists()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# AC5: issue-number mismatch (cross-issue receipt) rejected
# ---------------------------------------------------------------------------


def test_gate_rejects_issue_number_mismatch(tmp_path):
    source_bytes = b"{}"
    payload = _valid_validation_payload(source_bytes, issue_number=1)

    proc = _run_build(tmp_path, payload, source_bytes=source_bytes, issue_number=1755)

    assert proc.returncode != 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert not proc.out_file.exists(), (  # type: ignore[attr-defined]
        "phase-state file must NOT be written for issue-number mismatch (AC5)"
    )
    assert "issue" in proc.stdout.lower(), (
        f"Expected error message to mention the issue-number mismatch:\n{proc.stdout}"
    )


def test_gate_requires_issue_number_argument(tmp_path):
    """AC5: --issue-number itself is required for this (phase, source_kind)
    combination -- omitting it fails closed even with an otherwise valid
    validation result."""
    source_bytes = b"{}"
    payload = _valid_validation_payload(source_bytes)

    proc = _run_build(tmp_path, payload, source_bytes=source_bytes, issue_number=None)

    assert proc.returncode != 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert not proc.out_file.exists()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# AC6: real producer chain regression (positive control)
# ---------------------------------------------------------------------------


def test_gate_accepts_real_producer_intermediate_result(tmp_path):
    """AC6: the actual needs-fix stdout produced by `compact_review_result.py`
    from the real `review_result_needs_fix.json` fixture, run through the
    real `emit_parent_review_envelope_v2.py --validate-intermediate`
    producer, is accepted by the AC2-AC5-hardened phase gate and generates
    a phase-state file (regression: the gate must not break the normal
    path)."""
    issue_number = 1755
    assert NEEDS_FIX_FIXTURE.exists(), f"Missing fixture: {NEEDS_FIX_FIXTURE}"

    compact_proc = subprocess.run(
        [
            sys.executable,
            str(COMPACT_SCRIPT),
            "--input-file", str(NEEDS_FIX_FIXTURE.resolve()),
            "--issue-number", str(issue_number),
            "--repo-root", str(tmp_path),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert compact_proc.returncode == 0, (
        f"stdout: {compact_proc.stdout}\nstderr: {compact_proc.stderr}"
    )
    assert "VERDICT: needs-fix" in compact_proc.stdout

    raw_stdout_bytes = compact_proc.stdout.encode("utf-8")
    child_stdout_path = tmp_path / "child_stdout.txt"
    child_stdout_path.write_bytes(raw_stdout_bytes)

    validate_proc = subprocess.run(
        [
            sys.executable,
            str(EMIT_SCRIPT),
            "--validate-intermediate",
            "--issue-number", str(issue_number),
        ],
        input=raw_stdout_bytes,
        capture_output=True,
    )
    assert validate_proc.returncode == 0, (
        f"stdout: {validate_proc.stdout!r}\nstderr: {validate_proc.stderr!r}"
    )
    validation_payload = json.loads(validate_proc.stdout.decode("utf-8"))
    assert validation_payload["schema"] == _INTERMEDIATE_SCHEMA
    assert validation_payload["validation_status"] == "valid"
    assert validation_payload["violations"] == []

    validation_result_path = tmp_path / "validation_result.json"
    validation_result_path.write_bytes(validate_proc.stdout)

    out_path = tmp_path / "phase_state.json"
    build_proc = subprocess.run(
        [
            sys.executable,
            str(BUILD_SCRIPT),
            "--phase", "review",
            "--source-kind", "issue_review_result_compact_v1",
            "--source-path", str(child_stdout_path),
            "--review-validation-result-path", str(validation_result_path),
            "--issue-number", str(issue_number),
            "--output-path", str(out_path),
        ],
        capture_output=True,
        text=True,
    )
    assert build_proc.returncode == 0, (
        f"stdout: {build_proc.stdout}\nstderr: {build_proc.stderr}"
    )
    assert out_path.exists()
    phase_state = json.loads(out_path.read_text(encoding="utf-8"))
    assert phase_state["schema_version"] == "ISSUE_REFINEMENT_PHASE_STATE_V1"
    assert phase_state["phase"] == "review"
    assert phase_state["review_validation_result_path"] == str(validation_result_path)


# ---------------------------------------------------------------------------
# Issue #1755 fix_delta P0: forged-receipt closing check (OWNER
# REQUEST_CHANGES, PR #1826). Each test below builds a receipt whose
# INDIVIDUAL fields all look internally consistent (would pass every AC2-AC5
# check in isolation) but which was never actually produced by running the
# real validator against the paired --source-path bytes.
# ---------------------------------------------------------------------------


def test_gate_rejects_forged_receipt_for_grammar_invalid_raw_source(tmp_path):
    """P0: raw --source-path bytes are grammar-invalid (not a valid 8/9-line
    child-intermediate envelope), but the paired receipt merely CLAIMS
    `validation_status: valid` with internally-consistent input_sha256 /
    input_byte_count / ARTIFACT issue segment. The real validator, re-run
    against these actual bytes, would report `validation_status: invalid` --
    the mismatch is rejected."""
    source_bytes = b"not a valid child-intermediate envelope at all"
    payload = _valid_validation_payload(source_bytes)  # internally consistent, but forged

    proc = _run_build(tmp_path, payload, source_bytes=source_bytes)

    assert proc.returncode != 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert not proc.out_file.exists()  # type: ignore[attr-defined]
    assert "does not match the REAL" in proc.stdout, (
        f"Expected the P0 forged-receipt rejection message:\n{proc.stdout}"
    )


def test_gate_rejects_forged_receipt_envelope_kind_approve_with_needs_fix_payload(
    tmp_path,
):
    """P0: raw --source-path bytes are a REAL needs-fix envelope (9 lines,
    `REVIEWER_BLOCKER_CLAIM` included), but the receipt forges
    `envelope_kind: approve` while still carrying the needs-fix
    `normalized_payload` (with `REVIEWER_BLOCKER_CLAIM`). The real validator
    would classify this source as `needs_fix_intermediate`, not `approve` --
    rejected."""
    assert NEEDS_FIX_FIXTURE.exists(), f"Missing fixture: {NEEDS_FIX_FIXTURE}"
    issue_number = 1755

    compact_proc = subprocess.run(
        [
            sys.executable,
            str(COMPACT_SCRIPT),
            "--input-file", str(NEEDS_FIX_FIXTURE.resolve()),
            "--issue-number", str(issue_number),
            "--repo-root", str(tmp_path),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert compact_proc.returncode == 0, compact_proc.stderr
    raw_bytes = compact_proc.stdout.encode("utf-8")

    real_result = _emit2.build_validate_intermediate_result(
        raw_bytes, issue_number=issue_number
    )
    assert real_result["validation_status"] == "valid"
    assert real_result["envelope_kind"] == "needs_fix_intermediate"

    forged = dict(real_result)
    forged["envelope_kind"] = "approve"  # forged: real source is needs-fix, not approve

    proc = _run_build(tmp_path, forged, source_bytes=raw_bytes, issue_number=issue_number)

    assert proc.returncode != 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert not proc.out_file.exists()  # type: ignore[attr-defined]
    assert "does not match the REAL" in proc.stdout


def test_gate_rejects_forged_receipt_needs_fix_missing_canonical_claim(tmp_path):
    """P0: a REAL needs-fix receipt (produced by the real validator) with
    `canonical_reviewer_blocker_claim` forged to `None`. The real validator
    populates this field for a valid needs-fix envelope; a receipt claiming
    it is absent does not match the real recomputed output -- rejected."""
    assert NEEDS_FIX_FIXTURE.exists(), f"Missing fixture: {NEEDS_FIX_FIXTURE}"
    issue_number = 1755

    compact_proc = subprocess.run(
        [
            sys.executable,
            str(COMPACT_SCRIPT),
            "--input-file", str(NEEDS_FIX_FIXTURE.resolve()),
            "--issue-number", str(issue_number),
            "--repo-root", str(tmp_path),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert compact_proc.returncode == 0, compact_proc.stderr
    raw_bytes = compact_proc.stdout.encode("utf-8")

    real_result = _emit2.build_validate_intermediate_result(
        raw_bytes, issue_number=issue_number
    )
    assert real_result["validation_status"] == "valid"
    assert real_result["canonical_reviewer_blocker_claim"] is not None

    forged = dict(real_result)
    forged["canonical_reviewer_blocker_claim"] = None  # forged: real value is non-null

    proc = _run_build(tmp_path, forged, source_bytes=raw_bytes, issue_number=issue_number)

    assert proc.returncode != 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert not proc.out_file.exists()  # type: ignore[attr-defined]
    assert "does not match the REAL" in proc.stdout


# ---------------------------------------------------------------------------
# Issue #1755 fix_delta P2-2: input_byte_count / bounded-read
# ---------------------------------------------------------------------------


def test_gate_rejects_input_byte_count_mismatch(tmp_path):
    """P2-2: `input_byte_count` disjoint from --source-path's actual byte
    count is rejected, independent of input_sha256 (which is left
    correct)."""
    source_bytes = b"{}"
    payload = _valid_validation_payload(source_bytes)
    payload["input_byte_count"] = len(source_bytes) + 1

    proc = _run_build(tmp_path, payload, source_bytes=source_bytes)

    assert proc.returncode != 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert not proc.out_file.exists()  # type: ignore[attr-defined]
    assert "input_byte_count" in proc.stdout, (
        f"Expected error message to mention input_byte_count:\n{proc.stdout}"
    )


def test_gate_rejects_oversized_source_bounded_read(tmp_path):
    """P2-2: --source-path larger than the bounded-read cap (the same
    MAX_INPUT_BYTES the intermediate validator itself enforces) is rejected
    via a bounded read, not an unbounded read followed by a size check."""
    oversized_bytes = b"x" * (_emit2.MAX_INPUT_BYTES + 1)
    payload = _valid_validation_payload(oversized_bytes)

    proc = _run_build(tmp_path, payload, source_bytes=oversized_bytes)

    assert proc.returncode != 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert not proc.out_file.exists()  # type: ignore[attr-defined]
    assert "bounded-read" in proc.stdout or "max_bytes" in proc.stdout, (
        f"Expected a bounded-read rejection message:\n{proc.stdout}"
    )


# ---------------------------------------------------------------------------
# Issue #1755 fix_delta P2-1: duplicate JSON object key rejected
# ---------------------------------------------------------------------------


def test_gate_rejects_duplicate_json_key_top_level(tmp_path):
    """P2-1: a duplicate top-level JSON object key in the validation result
    is rejected (strict JSON, never silently last-value-wins)."""
    source_bytes = b"{}"
    source_file = tmp_path / "source.txt"
    source_file.write_bytes(source_bytes)
    out_file = tmp_path / "phase_state.json"
    input_sha256 = f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"

    raw_json = (
        '{"schema": "' + _INTERMEDIATE_SCHEMA + '", '
        '"schema_version": "1", '
        '"validation_status": "valid", '
        '"validation_status": "valid", '  # duplicate top-level key
        '"envelope_kind": "approve", '
        f'"input_sha256": "{input_sha256}", '
        f'"input_byte_count": {len(source_bytes)}, '
        '"normalized_payload": {"ARTIFACT": "' + _artifact_value(1755) + '"}, '
        '"canonical_reviewer_blocker_claim": null, '
        '"violations": []}'
    )
    validation_file = tmp_path / "validation_result.json"
    validation_file.write_text(raw_json, encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(BUILD_SCRIPT),
            "--phase", "review",
            "--source-kind", "issue_review_result_compact_v1",
            "--source-path", str(source_file),
            "--review-validation-result-path", str(validation_file),
            "--issue-number", "1755",
            "--output-path", str(out_file),
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode != 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert not out_file.exists()
    assert "duplicate" in proc.stdout.lower(), (
        f"Expected a duplicate-key rejection message:\n{proc.stdout}"
    )


def test_gate_rejects_duplicate_json_key_normalized_payload(tmp_path):
    """P2-1: a duplicate JSON object key INSIDE `normalized_payload` (nested,
    not top-level) is also rejected."""
    source_bytes = b"{}"
    source_file = tmp_path / "source.txt"
    source_file.write_bytes(source_bytes)
    out_file = tmp_path / "phase_state.json"
    input_sha256 = f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"

    raw_json = (
        '{"schema": "' + _INTERMEDIATE_SCHEMA + '", '
        '"schema_version": "1", '
        '"validation_status": "valid", '
        '"envelope_kind": "approve", '
        f'"input_sha256": "{input_sha256}", '
        f'"input_byte_count": {len(source_bytes)}, '
        '"normalized_payload": {"ARTIFACT": "' + _artifact_value(1755) + '", '
        '"ARTIFACT": "' + _artifact_value(1755) + '"}, '  # duplicate nested key
        '"canonical_reviewer_blocker_claim": null, '
        '"violations": []}'
    )
    validation_file = tmp_path / "validation_result.json"
    validation_file.write_text(raw_json, encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(BUILD_SCRIPT),
            "--phase", "review",
            "--source-kind", "issue_review_result_compact_v1",
            "--source-path", str(source_file),
            "--review-validation-result-path", str(validation_file),
            "--issue-number", "1755",
            "--output-path", str(out_file),
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode != 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert not out_file.exists()
    assert "duplicate" in proc.stdout.lower(), (
        f"Expected a duplicate-key rejection message:\n{proc.stdout}"
    )


# ---------------------------------------------------------------------------
# Issue #1755 fix_delta P2-5: unknown top-level field rejected
# ---------------------------------------------------------------------------


def test_gate_rejects_unknown_top_level_field(tmp_path):
    """P2-5: an extra top-level field not in
    REVIEW_COMPACT_INTERMEDIATE_VALIDATION_RESULT_V1's schema is rejected,
    even though every KNOWN field is otherwise internally consistent."""
    source_bytes = b"{}"
    payload = _valid_validation_payload(source_bytes)
    payload["unexpected_extra_field"] = "smuggled"

    proc = _run_build(tmp_path, payload, source_bytes=source_bytes)

    assert proc.returncode != 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert not proc.out_file.exists()  # type: ignore[attr-defined]
    assert "unknown_field" in proc.stdout or "unknown top-level" in proc.stdout, (
        f"Expected an unknown-field rejection message:\n{proc.stdout}"
    )


# ---------------------------------------------------------------------------
# Issue #1755 fix_delta P2-3: real producer chain regression, APPROVE path
# ---------------------------------------------------------------------------


def test_gate_accepts_real_producer_approve_intermediate_result(tmp_path):
    """P2-3: the actual approve stdout (8 lines) produced by
    `compact_review_result.py` from the real `review_result_approve.json`
    fixture, run through the real
    `emit_parent_review_envelope_v2.py --validate-intermediate` producer, is
    accepted by the AC2-AC5/P0-hardened phase gate and generates a
    phase-state file (mirrors AC6's needs-fix coverage for the approve
    envelope shape)."""
    issue_number = 42  # matches review_result_approve.json's issue_url (.../issues/42)
    assert APPROVE_FIXTURE.exists(), f"Missing fixture: {APPROVE_FIXTURE}"

    compact_proc = subprocess.run(
        [
            sys.executable,
            str(COMPACT_SCRIPT),
            "--input-file", str(APPROVE_FIXTURE.resolve()),
            "--issue-number", str(issue_number),
            "--repo-root", str(tmp_path),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert compact_proc.returncode == 0, (
        f"stdout: {compact_proc.stdout}\nstderr: {compact_proc.stderr}"
    )
    assert "VERDICT: approve" in compact_proc.stdout

    raw_stdout_bytes = compact_proc.stdout.encode("utf-8")
    child_stdout_path = tmp_path / "child_stdout.txt"
    child_stdout_path.write_bytes(raw_stdout_bytes)

    validate_proc = subprocess.run(
        [
            sys.executable,
            str(EMIT_SCRIPT),
            "--validate-intermediate",
            "--issue-number", str(issue_number),
        ],
        input=raw_stdout_bytes,
        capture_output=True,
    )
    assert validate_proc.returncode == 0, (
        f"stdout: {validate_proc.stdout!r}\nstderr: {validate_proc.stderr!r}"
    )
    validation_payload = json.loads(validate_proc.stdout.decode("utf-8"))
    assert validation_payload["schema"] == _INTERMEDIATE_SCHEMA
    assert validation_payload["validation_status"] == "valid"
    assert validation_payload["envelope_kind"] == "approve"
    assert validation_payload["violations"] == []

    validation_result_path = tmp_path / "validation_result.json"
    validation_result_path.write_bytes(validate_proc.stdout)

    out_path = tmp_path / "phase_state.json"
    build_proc = subprocess.run(
        [
            sys.executable,
            str(BUILD_SCRIPT),
            "--phase", "review",
            "--source-kind", "issue_review_result_compact_v1",
            "--source-path", str(child_stdout_path),
            "--review-validation-result-path", str(validation_result_path),
            "--issue-number", str(issue_number),
            "--output-path", str(out_path),
        ],
        capture_output=True,
        text=True,
    )
    assert build_proc.returncode == 0, (
        f"stdout: {build_proc.stdout}\nstderr: {build_proc.stderr}"
    )
    assert out_path.exists()
    phase_state = json.loads(out_path.read_text(encoding="utf-8"))
    assert phase_state["schema_version"] == "ISSUE_REFINEMENT_PHASE_STATE_V1"
    assert phase_state["phase"] == "review"
    assert phase_state["issue_number"] == issue_number


# ---------------------------------------------------------------------------
# Issue #1755 fix_delta P3: direct build_phase_state() call bypasses argparse
# ---------------------------------------------------------------------------


def test_build_phase_state_direct_call_rejects_non_positive_issue_number(tmp_path):
    """P3: calling `build_phase_state()` directly (bypassing the argparse
    `--issue-number` positive-int constraint entirely) with a non-positive
    `issue_number` is still rejected -- the gate function itself enforces the
    constraint, closing the direct-call bypass."""
    source_file = tmp_path / "source.txt"
    source_bytes = _real_approve_child_bytes(1755)
    source_file.write_bytes(source_bytes)

    real_result = _emit2.build_validate_intermediate_result(
        source_bytes, issue_number=1755
    )
    validation_file = tmp_path / "validation_result.json"
    validation_file.write_text(json.dumps(real_result), encoding="utf-8")

    for bad_issue_number in (0, -1, -1755):
        with pytest.raises(ValueError, match="positive integer"):
            _phase_state_builder.build_phase_state(
                phase="review",
                source_kind="issue_review_result_compact_v1",
                source_path=str(source_file),
                review_validation_result_path=str(validation_file),
                issue_number=bad_issue_number,
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
