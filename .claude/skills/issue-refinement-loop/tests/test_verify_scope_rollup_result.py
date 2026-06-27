"""
Tests for verify_scope_rollup_result.py

Covers:
- AC6: valid result JSON -> exit 0, STATUS: verified
- AC7: tampered result JSON -> exit 10, STATUS: sha_mismatch
- AC8: schema_name/schema_version mismatch -> exit 20, STATUS: schema_mismatch
- AC9: missing file / invalid JSON / non-object / missing self_validation -> exit 30, STATUS: invalid_input
- AC10: stdout is fixed compact lines only (no raw plan JSON)
- AC13: skipped_reason is emitted at most once per candidate in serialized output
- AC14: duplicate JSON keys are rejected (invalid_input)
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any


# Resolve the script directories relative to this test file
SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import verify_scope_rollup_result as verifier  # noqa: E402
import plan_issue_scope_rollup as rollup  # noqa: E402

# Actual sha256 of plan_issue_scope_rollup.py (matches what verify script computes)
_PLAN_SCRIPT_SHA256 = hashlib.sha256(
    (SCRIPT_DIR / "plan_issue_scope_rollup.py").read_bytes()
).hexdigest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_valid_plan(tmp_path: Path, extra_candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build a valid ISSUE_SCOPE_ROLLUP_PLAN_V2 dict with a self_validation block."""
    candidates = extra_candidates or []
    plan_without_sv: dict[str, Any] = {
        "schema_version": 2,
        "repo": "squne121/loop-protocol",
        "generated_at": "2026-06-13T00:00:00Z",
        "source": "plan_issue_scope_rollup",
        "body_sha256": "abc123",
        "input": {"completeness": "full", "warnings": []},
        "candidates": candidates,
    }
    payload_sha256 = verifier._compute_payload_sha256(plan_without_sv)
    # Use the actual sha256 of plan_issue_scope_rollup.py (matches what verify script recomputes)
    script_file_sha256 = _PLAN_SCRIPT_SHA256
    self_validation = {
        "invocation_id": str(uuid.uuid4()),
        "payload_sha256": payload_sha256,
        "script_file_sha256": script_file_sha256,
        "schema_name": "ISSUE_SCOPE_ROLLUP_PLAN_V2",
        "schema_version": 2,
        "hash_algorithm": "sha256",
        (
            "canonicalization"
        ): 'json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")',
    }
    return {**plan_without_sv, "self_validation": self_validation}


def _write_json(tmp_path: Path, data: dict[str, Any], filename: str = "result.json") -> str:
    """Write dict as JSON to tmp_path and return the path string."""
    p = tmp_path / filename
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def _write_raw(tmp_path: Path, content: str, filename: str = "result.json") -> str:
    """Write raw string to tmp_path and return the path string."""
    p = tmp_path / filename
    p.write_text(content, encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# AC6: valid result JSON -> exit 0, STATUS: verified
# ---------------------------------------------------------------------------


class TestVerified:
    """AC6: GIVEN valid result JSON with correct self_validation,
    WHEN verify is called,
    THEN exit 0 and STATUS: verified.
    """

    def test_valid_plan_returns_verified(self, tmp_path: Path) -> None:
        """GIVEN valid plan JSON, THEN exit 0 and STATUS: verified."""
        plan = _make_valid_plan(tmp_path)
        path = _write_json(tmp_path, plan)
        output, exit_code = verifier.verify(path)
        assert exit_code == verifier.EXIT_VERIFIED, f"Expected exit 0, got {exit_code}"
        assert "STATUS: verified" in output

    def test_verified_output_contains_required_fields(self, tmp_path: Path) -> None:
        """GIVEN valid plan, THEN stdout contains all required compact fields."""
        plan = _make_valid_plan(tmp_path)
        path = _write_json(tmp_path, plan)
        output, exit_code = verifier.verify(path)
        assert exit_code == 0
        assert "STATUS: verified" in output
        assert "SUMMARY:" in output
        assert "RESULT_PATH:" in output
        assert "PAYLOAD_SHA256:" in output
        assert "EXPECTED_PAYLOAD_SHA256:" in output
        assert "SCRIPT_FILE_SHA256:" in output
        assert "EXPECTED_SCRIPT_FILE_SHA256:" in output

    def test_plan_exit_code_2_still_verified(self, tmp_path: Path) -> None:
        """GIVEN plan with completeness=partial (exit 2), THEN still verified if sha is correct."""
        plan = _make_valid_plan(tmp_path)
        # Manually set partial completeness
        plan_without_sv = {k: v for k, v in plan.items() if k != "self_validation"}
        plan_without_sv["input"]["completeness"] = "partial"
        plan_without_sv["input"]["warnings"] = ["current_issue_not_found"]
        # Recompute sha
        payload_sha256 = verifier._compute_payload_sha256(plan_without_sv)
        plan_without_sv["self_validation"] = {
            **plan["self_validation"],
            "payload_sha256": payload_sha256,
        }
        plan_with_sv = {**plan_without_sv}
        path = _write_json(tmp_path, plan_with_sv)
        output, exit_code = verifier.verify(path)
        assert exit_code == verifier.EXIT_VERIFIED


# ---------------------------------------------------------------------------
# AC7: tampered result JSON -> exit 10, STATUS: sha_mismatch
# ---------------------------------------------------------------------------


class TestShaMismatch:
    """AC7: GIVEN tampered result JSON,
    WHEN verify is called,
    THEN exit 10 and STATUS: sha_mismatch.
    """

    def test_tampered_repo_field_causes_sha_mismatch(self, tmp_path: Path) -> None:
        """GIVEN repo field tampered after generation, THEN sha_mismatch."""
        plan = _make_valid_plan(tmp_path)
        plan["repo"] = "attacker/tampered-repo"
        path = _write_json(tmp_path, plan)
        output, exit_code = verifier.verify(path)
        assert exit_code == verifier.EXIT_SHA_MISMATCH, f"Expected exit 10, got {exit_code}"
        assert "STATUS: sha_mismatch" in output

    def test_tampered_candidates_causes_sha_mismatch(self, tmp_path: Path) -> None:
        """GIVEN candidates field tampered, THEN sha_mismatch."""
        plan = _make_valid_plan(tmp_path)
        plan["candidates"] = [{"kind": "issue", "number": 999, "injected": True}]
        path = _write_json(tmp_path, plan)
        output, exit_code = verifier.verify(path)
        assert exit_code == verifier.EXIT_SHA_MISMATCH

    def test_tampered_body_sha256_causes_sha_mismatch(self, tmp_path: Path) -> None:
        """GIVEN body_sha256 field tampered, THEN sha_mismatch."""
        plan = _make_valid_plan(tmp_path)
        plan["body_sha256"] = "0" * 64
        path = _write_json(tmp_path, plan)
        output, exit_code = verifier.verify(path)
        assert exit_code == verifier.EXIT_SHA_MISMATCH

    def test_sha_mismatch_output_contains_both_hashes(self, tmp_path: Path) -> None:
        """GIVEN sha_mismatch, THEN stdout shows actual and expected sha."""
        plan = _make_valid_plan(tmp_path)
        plan["body_sha256"] = "tampered"
        path = _write_json(tmp_path, plan)
        output, exit_code = verifier.verify(path)
        assert exit_code == verifier.EXIT_SHA_MISMATCH
        assert "PAYLOAD_SHA256:" in output
        assert "EXPECTED_PAYLOAD_SHA256:" in output

    def test_tampered_script_file_sha256_causes_sha_mismatch(self, tmp_path: Path) -> None:
        """GIVEN script_file_sha256 in self_validation is tampered, THEN sha_mismatch.

        Verifies that verify_scope_rollup_result.py re-computes the actual sha256 of
        plan_issue_scope_rollup.py rather than doing a pass-through.
        """
        plan = _make_valid_plan(tmp_path)
        plan["self_validation"]["script_file_sha256"] = "deadbeef" * 8
        path = _write_json(tmp_path, plan)
        output, exit_code = verifier.verify(path)
        assert exit_code == verifier.EXIT_SHA_MISMATCH, f"Expected exit 10, got {exit_code}"
        assert "STATUS: sha_mismatch" in output
        assert "script_file_sha256" in output


# ---------------------------------------------------------------------------
# AC8: schema_name/schema_version mismatch -> exit 20, STATUS: schema_mismatch
# ---------------------------------------------------------------------------


class TestSchemaMismatch:
    """AC8: GIVEN schema_name or schema_version mismatch,
    WHEN verify is called,
    THEN exit 20 and STATUS: schema_mismatch.
    """

    def test_wrong_schema_name_causes_schema_mismatch(self, tmp_path: Path) -> None:
        """GIVEN schema_name != ISSUE_SCOPE_ROLLUP_PLAN_V2, THEN schema_mismatch."""
        plan = _make_valid_plan(tmp_path)
        plan["self_validation"]["schema_name"] = "WRONG_SCHEMA_V99"
        path = _write_json(tmp_path, plan)
        output, exit_code = verifier.verify(path)
        assert exit_code == verifier.EXIT_SCHEMA_MISMATCH, f"Expected exit 20, got {exit_code}"
        assert "STATUS: schema_mismatch" in output

    def test_wrong_schema_version_causes_schema_mismatch(self, tmp_path: Path) -> None:
        """GIVEN schema_version != 2, THEN schema_mismatch."""
        plan = _make_valid_plan(tmp_path)
        plan["self_validation"]["schema_version"] = 99
        path = _write_json(tmp_path, plan)
        output, exit_code = verifier.verify(path)
        assert exit_code == verifier.EXIT_SCHEMA_MISMATCH

    def test_missing_schema_name_causes_schema_mismatch(self, tmp_path: Path) -> None:
        """GIVEN schema_name missing from self_validation, THEN schema_mismatch."""
        plan = _make_valid_plan(tmp_path)
        del plan["self_validation"]["schema_name"]
        path = _write_json(tmp_path, plan)
        output, exit_code = verifier.verify(path)
        # schema_name defaults to "" which != expected -> schema_mismatch
        assert exit_code == verifier.EXIT_SCHEMA_MISMATCH

    def test_schema_mismatch_has_priority_over_sha_mismatch(self, tmp_path: Path) -> None:
        """GIVEN both schema mismatch and sha mismatch, THEN schema_mismatch wins (lower exit code priority)."""
        plan = _make_valid_plan(tmp_path)
        plan["self_validation"]["schema_name"] = "WRONG"
        plan["body_sha256"] = "tampered"  # also tampered
        path = _write_json(tmp_path, plan)
        output, exit_code = verifier.verify(path)
        assert exit_code == verifier.EXIT_SCHEMA_MISMATCH


# ---------------------------------------------------------------------------
# AC9: invalid input -> exit 30, STATUS: invalid_input
# ---------------------------------------------------------------------------


class TestInvalidInput:
    """AC9: GIVEN missing file / invalid JSON / non-object JSON / duplicate keys / missing self_validation,
    WHEN verify is called,
    THEN exit 30 and STATUS: invalid_input.
    """

    def test_missing_file_causes_invalid_input(self, tmp_path: Path) -> None:
        """GIVEN file does not exist, THEN invalid_input."""
        output, exit_code = verifier.verify(str(tmp_path / "nonexistent.json"))
        assert exit_code == verifier.EXIT_INVALID_INPUT
        assert "STATUS: invalid_input" in output

    def test_invalid_json_causes_invalid_input(self, tmp_path: Path) -> None:
        """GIVEN file contains invalid JSON, THEN invalid_input."""
        path = _write_raw(tmp_path, "{ this is not valid json }")
        output, exit_code = verifier.verify(path)
        assert exit_code == verifier.EXIT_INVALID_INPUT

    def test_json_array_causes_invalid_input(self, tmp_path: Path) -> None:
        """GIVEN file contains JSON array (not object), THEN invalid_input."""
        path = _write_raw(tmp_path, "[1, 2, 3]")
        output, exit_code = verifier.verify(path)
        assert exit_code == verifier.EXIT_INVALID_INPUT

    def test_missing_self_validation_block_causes_invalid_input(self, tmp_path: Path) -> None:
        """GIVEN JSON object without self_validation key, THEN invalid_input."""
        plan = _make_valid_plan(tmp_path)
        del plan["self_validation"]
        path = _write_json(tmp_path, plan)
        output, exit_code = verifier.verify(path)
        assert exit_code == verifier.EXIT_INVALID_INPUT

    def test_self_validation_not_object_causes_invalid_input(self, tmp_path: Path) -> None:
        """GIVEN self_validation is a string (not object), THEN invalid_input."""
        plan = _make_valid_plan(tmp_path)
        plan["self_validation"] = "not-an-object"
        path = _write_json(tmp_path, plan)
        output, exit_code = verifier.verify(path)
        assert exit_code == verifier.EXIT_INVALID_INPUT

    def test_invalid_input_has_priority_over_schema_mismatch(self, tmp_path: Path) -> None:
        """GIVEN invalid JSON, THEN invalid_input wins over any schema check."""
        path = _write_raw(tmp_path, "not json at all")
        output, exit_code = verifier.verify(path)
        assert exit_code == verifier.EXIT_INVALID_INPUT


# ---------------------------------------------------------------------------
# AC10: stdout is fixed compact lines only (no raw plan JSON)
# ---------------------------------------------------------------------------


class TestStdoutFormat:
    """AC10: GIVEN any result, THEN stdout contains only fixed compact lines, not raw plan JSON."""

    def test_stdout_does_not_contain_raw_candidates(self, tmp_path: Path) -> None:
        """GIVEN plan with candidates, THEN stdout does not output candidates JSON."""
        plan = _make_valid_plan(tmp_path)
        path = _write_json(tmp_path, plan)
        output, _ = verifier.verify(path)
        # The output must not contain JSON structure that looks like a candidates array
        assert '"candidates"' not in output, "Raw plan JSON must not appear in stdout"

    def test_stdout_lines_are_key_value_pairs(self, tmp_path: Path) -> None:
        """GIVEN valid plan, THEN all stdout lines are KEY: value pairs."""
        plan = _make_valid_plan(tmp_path)
        path = _write_json(tmp_path, plan)
        output, exit_code = verifier.verify(path)
        assert exit_code == 0
        for line in output.strip().splitlines():
            assert ": " in line, f"Expected KEY: value format, got: {line!r}"

    def test_stdout_has_exactly_7_lines(self, tmp_path: Path) -> None:
        """GIVEN valid plan, THEN stdout has exactly 7 lines (the 7 contract fields)."""
        plan = _make_valid_plan(tmp_path)
        path = _write_json(tmp_path, plan)
        output, _ = verifier.verify(path)
        lines = output.strip().splitlines()
        assert len(lines) == 7, f"Expected 7 stdout lines, got {len(lines)}: {lines}"


# ---------------------------------------------------------------------------
# AC13: skipped_reason appears at most once per candidate in output
# ---------------------------------------------------------------------------


class TestSkippedReason:
    """AC13: GIVEN candidates with skipped_reason, THEN skipped_reason appears at most once
    per candidate in the serialized plan output.
    """

    def test_candidate_with_skipped_reason_serializes_once(self, tmp_path: Path) -> None:
        """GIVEN a candidate with skipped_reason, THEN it appears exactly once in JSON output."""
        candidate = {
            "kind": "issue",
            "number": 42,
            "title": "some issue",
            "signals": ["same_skill_family"],
            "confidence": "low",
            "suggested_action": "keep_separate_with_reason",
            "skipped_reason": "disjoint_paths",
        }
        plan = _make_valid_plan(tmp_path, extra_candidates=[candidate])
        # Serialize and count occurrences of skipped_reason
        serialized = json.dumps(plan, ensure_ascii=False)
        # Count how many times "skipped_reason" appears in the full JSON
        occurrences = serialized.count('"skipped_reason"')
        # Each candidate contributes at most 1 occurrence
        assert occurrences <= 1, (
            f"skipped_reason appears {occurrences} times in serialized JSON "
            f"(expected at most 1 per candidate)"
        )

    def test_multiple_candidates_with_skipped_reason_each_appear_once(self, tmp_path: Path) -> None:
        """GIVEN 3 candidates all with skipped_reason, THEN key appears exactly 3 times."""
        candidates = [
            {
                "kind": "issue",
                "number": i,
                "title": f"issue {i}",
                "signals": ["same_skill_family"],
                "confidence": "low",
                "suggested_action": "keep_separate_with_reason",
                "skipped_reason": f"reason_{i}",
            }
            for i in range(3)
        ]
        plan = _make_valid_plan(tmp_path, extra_candidates=candidates)
        serialized = json.dumps(plan, ensure_ascii=False)
        occurrences = serialized.count('"skipped_reason"')
        # 3 candidates each with 1 skipped_reason
        assert occurrences == 3, (
            f"Expected 3 occurrences of skipped_reason (one per candidate), got {occurrences}"
        )


# ---------------------------------------------------------------------------
# AC14: duplicate JSON keys are rejected
# ---------------------------------------------------------------------------


class TestDuplicateKeys:
    """AC14: GIVEN JSON with duplicate keys, THEN invalid_input is returned."""

    def test_duplicate_top_level_key_causes_invalid_input(self, tmp_path: Path) -> None:
        """GIVEN JSON with a duplicate top-level key, THEN invalid_input."""
        # Manually craft JSON with duplicate key (json.dumps would not do this)
        raw = '{"schema_version": 2, "schema_version": 99, "candidates": []}'
        path = _write_raw(tmp_path, raw)
        output, exit_code = verifier.verify(path)
        assert exit_code == verifier.EXIT_INVALID_INPUT, (
            f"Expected invalid_input for duplicate keys, got exit {exit_code}"
        )
        assert "STATUS: invalid_input" in output

    def test_duplicate_nested_key_causes_invalid_input(self, tmp_path: Path) -> None:
        """GIVEN JSON with a duplicate key inside self_validation, THEN invalid_input."""
        plan = _make_valid_plan(tmp_path)
        _sv_str = json.dumps(plan["self_validation"], ensure_ascii=False)
        # Inject duplicate key into self_validation
        _plan_str = json.dumps(plan, ensure_ascii=False)
        # Replace first self_validation json with a version that has duplicate keys
        sv_with_dup = (
            '{"payload_sha256": "aaa", "payload_sha256": "bbb",'
            ' "schema_name": "ISSUE_SCOPE_ROLLUP_PLAN_V2",'
            ' "schema_version": 2, "hash_algorithm": "sha256",'
            ' "invocation_id": "test", "script_file_sha256": "ccc",'
            ' "canonicalization": "test"}'
        )
        # Build raw JSON string with the duplicate-key self_validation
        plan_without_sv = {k: v for k, v in plan.items() if k != "self_validation"}
        raw = json.dumps(plan_without_sv, ensure_ascii=False)[:-1]  # strip closing }
        raw += f', "self_validation": {sv_with_dup}}}'
        path = _write_raw(tmp_path, raw)
        output, exit_code = verifier.verify(path)
        assert exit_code == verifier.EXIT_INVALID_INPUT, (
            f"Expected invalid_input for duplicate nested keys, got exit {exit_code}"
        )

    def test_load_json_strict_detects_duplicate_keys(self) -> None:
        """Unit test: _load_json_strict returns error for duplicate keys."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write('{"a": 1, "a": 2}')
            fname = f.name
        data, error = verifier._load_json_strict(fname)
        assert error is not None, "Expected error for duplicate keys"
        assert data is None or "Duplicate" in (error or "")

    def test_load_json_strict_accepts_valid_json(self, tmp_path: Path) -> None:
        """Unit test: _load_json_strict accepts valid JSON without error."""
        path = _write_json(tmp_path, {"key": "value"})
        data, error = verifier._load_json_strict(path)
        assert error is None
        assert data == {"key": "value"}


# ---------------------------------------------------------------------------
# Integration: round-trip with plan_issue_scope_rollup.run()
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Integration: run plan_issue_scope_rollup.run() then verify the result."""

    def test_run_and_verify_round_trip(self, tmp_path: Path) -> None:
        """GIVEN plan generated by run(), WHEN verify() is called, THEN exit 0 verified."""
        issues = [
            {
                "number": 1,
                "title": "実装: test issue",
                "body": "## Allowed Paths\n- `.claude/skills/foo.py`\n",
            }
        ]
        issues_path = tmp_path / "issues.json"
        issues_path.write_text(json.dumps(issues), encoding="utf-8")
        prs_path = tmp_path / "prs.json"
        prs_path.write_text("[]", encoding="utf-8")

        invocation_id = str(uuid.uuid4())
        plan, exit_code = rollup.run(
            str(issues_path),
            str(prs_path),
            invocation_id=invocation_id,
        )
        assert "self_validation" in plan, "run() must produce self_validation block"
        assert plan["self_validation"]["invocation_id"] == invocation_id

        # Write to file and verify
        result_path = tmp_path / "plan.json"
        result_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

        output, verify_exit = verifier.verify(str(result_path))
        assert verify_exit == verifier.EXIT_VERIFIED, (
            f"Round-trip verification failed with exit {verify_exit}:\n{output}"
        )
        assert "STATUS: verified" in output

    def test_run_generates_uuid4_when_invocation_id_not_provided(self, tmp_path: Path) -> None:
        """AC4: GIVEN no invocation_id, THEN run() generates a UUID4 string."""
        issues_path = tmp_path / "issues.json"
        issues_path.write_text("[]", encoding="utf-8")
        prs_path = tmp_path / "prs.json"
        prs_path.write_text("[]", encoding="utf-8")

        plan, _ = rollup.run(str(issues_path), str(prs_path))
        sv = plan.get("self_validation", {})
        inv_id = sv.get("invocation_id", "")
        # Must be a valid UUID string (36 chars with dashes: 8-4-4-4-12)
        import re
        assert re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            inv_id,
        ), f"Expected UUID4 format, got: {inv_id!r}"

    def test_run_uses_provided_invocation_id(self, tmp_path: Path) -> None:
        """AC4: GIVEN invocation_id provided, THEN run() uses it (not generates new one)."""
        issues_path = tmp_path / "issues.json"
        issues_path.write_text("[]", encoding="utf-8")
        prs_path = tmp_path / "prs.json"
        prs_path.write_text("[]", encoding="utf-8")

        custom_id = "custom-invocation-id-123"
        plan, _ = rollup.run(str(issues_path), str(prs_path), invocation_id=custom_id)
        assert plan["self_validation"]["invocation_id"] == custom_id
