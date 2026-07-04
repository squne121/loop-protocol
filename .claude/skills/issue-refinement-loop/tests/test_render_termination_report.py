"""
test_render_termination_report.py

Tests for render_termination_report.py (Issue #656).

AC coverage:
  AC1: TERMINATION_REPORT_RENDER_RESULT_V1 schema present
  AC2: publishable=true -> body != null; publishable=false -> body == null
  AC3: only prose_boundary_policy public API imported (no re-implementation)
  AC4: max 2 attempts; no LLM/ask/network/gh
  AC5: guard fail x2 -> publishable=false, reason_code fixed
  AC6: stderr/artifacts have no publishable markdown (verified by stdout-only check)
  AC7: termination_reason / termination_cause separation
  AC8: GFM fence / HTML marker / adversarial input robustness
  AC9: simulated replay evidence (schema verification in test)
  AC10: existing tests still pass (run separately)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest
import re as _re
import yaml


# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import render_termination_report as rtr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_input(
    termination_reason: str = "approved",
    termination_cause: str | None = None,
    issue_number: int | None = 42,
    iteration: int | None = 1,
    blockers_summary: list[str] | None = None,
) -> dict:
    data: dict = {"termination_reason": termination_reason}
    if termination_cause is not None:
        data["termination_cause"] = termination_cause
    if issue_number is not None:
        data["issue_number"] = issue_number
    if iteration is not None:
        data["iteration"] = iteration
    if blockers_summary is not None:
        data["blockers_summary"] = blockers_summary
    return data


# ---------------------------------------------------------------------------
# AC1: schema present in result
# ---------------------------------------------------------------------------

class TestSchemaPresent:
    def test_schema_field_is_TERMINATION_REPORT_RENDER_RESULT_V1(self):
        result = rtr.render(_make_input())
        assert result["schema"] == "TERMINATION_REPORT_RENDER_RESULT_V1"

    def test_schema_version_is_1(self):
        result = rtr.render(_make_input())
        assert result["schema_version"] == 1

    def test_result_has_publishable_field(self):
        result = rtr.render(_make_input())
        assert "publishable" in result

    def test_result_has_body_field(self):
        result = rtr.render(_make_input())
        assert "body" in result

    def test_result_has_reason_code_field(self):
        result = rtr.render(_make_input())
        assert "reason_code" in result

    def test_result_has_termination_reason(self):
        result = rtr.render(_make_input())
        assert "termination_reason" in result

    def test_result_has_termination_cause(self):
        result = rtr.render(_make_input())
        assert "termination_cause" in result

    def test_result_has_attempts(self):
        result = rtr.render(_make_input())
        assert "attempts" in result

    def test_result_has_generated_at(self):
        result = rtr.render(_make_input())
        assert "generated_at" in result


# ---------------------------------------------------------------------------
# AC2: publishable=true -> body != null; publishable=false -> body == null
# ---------------------------------------------------------------------------

class TestPublishableBodyInvariant:
    def test_approved_publishable_true_body_not_null(self):
        result = rtr.render(_make_input("approved"))
        assert result["publishable"] is True
        assert result["body"] is not None

    def test_human_escalation_publishable_true_body_not_null(self):
        result = rtr.render(_make_input(
            "human_escalation",
            termination_cause="needs_fix_at_iteration_limit",
        ))
        assert result["publishable"] is True
        assert result["body"] is not None

    def test_superseded_publishable_true_body_not_null(self):
        result = rtr.render(_make_input("superseded_by_decision"))
        assert result["publishable"] is True
        assert result["body"] is not None

    def test_publishable_false_body_is_null(self):
        # Force guard to always fail by patching _run_guard
        with patch.object(rtr, "_run_guard", return_value=(False, ["simulated fail"])):
            result = rtr.render(_make_input("approved"))
        assert result["publishable"] is False
        assert result["body"] is None

    def test_body_is_string_when_publishable(self):
        result = rtr.render(_make_input())
        if result["publishable"]:
            assert isinstance(result["body"], str)


# ---------------------------------------------------------------------------
# AC3: only prose_boundary_policy public API imported
# ---------------------------------------------------------------------------

class TestPublicAPIImportOnly:
    def test_classify_block_imported(self):
        from prose_boundary_policy import classify_block
        assert callable(rtr.classify_block)
        assert rtr.classify_block is classify_block

    def test_iter_markdown_blocks_imported(self):
        from prose_boundary_policy import iter_markdown_blocks
        assert callable(rtr.iter_markdown_blocks)
        assert rtr.iter_markdown_blocks is iter_markdown_blocks

    def test_no_regex_reimplementation_for_fence(self):
        # Guard: classify_block and iter_markdown_blocks must be called via
        # the prose_boundary_policy module, not re-implemented inline.
        # Verify by calling _run_guard and checking it uses the imported functions.
        called = []
        original_classify = rtr.classify_block
        original_iter = rtr.iter_markdown_blocks

        def mock_classify(block):
            called.append("classify_block")
            return original_classify(block)

        def mock_iter(text):
            called.append("iter_markdown_blocks")
            return original_iter(text)

        with (
            patch.object(rtr, "classify_block", side_effect=mock_classify),
            patch.object(rtr, "iter_markdown_blocks", side_effect=mock_iter),
        ):
            rtr._run_guard("## Hello\n\nsome prose\n")

        assert "iter_markdown_blocks" in called or "classify_block" in called, (
            "_run_guard must use prose_boundary_policy public API"
        )


# ---------------------------------------------------------------------------
# AC4: max 2 attempts; no LLM/ask/network/gh
# ---------------------------------------------------------------------------

class TestNoLLMNoAskNoNetworkNoGH:
    def test_no_llm(self):
        # Ensure no openai, anthropic, gemini, or similar LLM calls
        banned_modules = ["openai", "anthropic", "google.generativeai", "langchain"]
        for mod in banned_modules:
            assert mod not in sys.modules, f"LLM module '{mod}' must not be imported"

    def test_no_subprocess_gh(self):
        # Verify gh is not called via subprocess
        import subprocess
        with patch.object(subprocess, "run") as mock_run, \
             patch.object(subprocess, "check_output") as mock_co, \
             patch.object(subprocess, "Popen") as mock_popen:
            rtr.render(_make_input())
            mock_run.assert_not_called()
            mock_co.assert_not_called()
            mock_popen.assert_not_called()

    def test_no_urllib_network_call(self):
        import urllib.request
        with patch.object(urllib.request, "urlopen") as mock_open:
            rtr.render(_make_input())
            mock_open.assert_not_called()

    def test_attempts_normal_path_is_1(self):
        result = rtr.render(_make_input("approved"))
        assert result["attempts"] == 1

    def test_attempts_fallback_path_is_2(self):
        # Force guard to fail once then pass
        call_count = [0]
        _original_guard = rtr._run_guard

        def guard_once_fail(body):
            call_count[0] += 1
            if call_count[0] == 1:
                return False, ["simulated fail attempt 1"]
            return True, []

        with patch.object(rtr, "_run_guard", side_effect=guard_once_fail):
            result = rtr.render(_make_input("approved"))

        assert result["attempts"] == 2

    def test_max_attempts_is_2_never_3(self):
        # Guard always fails -> should stop at 2 attempts
        with patch.object(rtr, "_run_guard", return_value=(False, ["fail"])):
            result = rtr.render(_make_input("approved"))
        assert result["attempts"] == 2
        assert len(result["attempts_log"]) == 2


# ---------------------------------------------------------------------------
# AC5: guard fail x2 -> publishable=false, reason_code fixed
# ---------------------------------------------------------------------------

class TestGuardFailPublishableFalse:
    def test_guard_fail_limit_exceeded_publishable_false(self):
        with patch.object(rtr, "_run_guard", return_value=(False, ["fail"])):
            result = rtr.render(_make_input())
        assert result["publishable"] is False

    def test_guard_fail_limit_exceeded_body_null(self):
        with patch.object(rtr, "_run_guard", return_value=(False, ["fail"])):
            result = rtr.render(_make_input())
        assert result["body"] is None

    def test_guard_fail_limit_exceeded_reason_code_fixed(self):
        with patch.object(rtr, "_run_guard", return_value=(False, ["fail"])):
            result = rtr.render(_make_input())
        assert result["reason_code"] == "guard_fail_limit_exceeded"

    def test_guard_fail_attempts_log_has_two_entries(self):
        with patch.object(rtr, "_run_guard", return_value=(False, ["fail"])):
            result = rtr.render(_make_input())
        assert len(result["attempts_log"]) == 2

    def test_guard_fail_attempt1_is_normal_template(self):
        with patch.object(rtr, "_run_guard", return_value=(False, ["fail"])):
            result = rtr.render(_make_input())
        assert result["attempts_log"][0]["template"] == "normal"

    def test_guard_fail_attempt2_is_fallback_minimal(self):
        with patch.object(rtr, "_run_guard", return_value=(False, ["fail"])):
            result = rtr.render(_make_input())
        assert result["attempts_log"][1]["template"] == "fallback_minimal"


# ---------------------------------------------------------------------------
# AC7: termination_reason / termination_cause separation
# ---------------------------------------------------------------------------

class TestTerminationReasonCauseSeparation:
    def test_approved_termination_reason(self):
        result = rtr.render(_make_input("approved"))
        assert result["termination_reason"] == "approved"
        assert result["termination_cause"] is None

    def test_human_escalation_with_cause_needs_fix(self):
        result = rtr.render(_make_input(
            "human_escalation",
            termination_cause="needs_fix_at_iteration_limit",
        ))
        assert result["termination_reason"] == "human_escalation"
        assert result["termination_cause"] == "needs_fix_at_iteration_limit"

    def test_human_escalation_with_cause_max_iterations(self):
        result = rtr.render(_make_input(
            "human_escalation",
            termination_cause="max_iterations_exceeded",
        ))
        assert result["termination_reason"] == "human_escalation"
        assert result["termination_cause"] == "max_iterations_exceeded"

    def test_superseded_by_decision(self):
        result = rtr.render(_make_input("superseded_by_decision"))
        assert result["termination_reason"] == "superseded_by_decision"

    def test_needs_fix_is_not_termination_reason(self):
        # needs_fix is a cause, not a reason
        data, err = rtr._validate_input({
            "termination_reason": "needs_fix_at_iteration_limit"
        })
        assert err != "", "needs_fix_at_iteration_limit must not be valid as termination_reason"

    def test_max_iterations_exceeded_is_not_termination_reason(self):
        data, err = rtr._validate_input({
            "termination_reason": "max_iterations_exceeded"
        })
        assert err != "", "max_iterations_exceeded must not be valid as termination_reason"

    def test_invalid_termination_reason_rejected(self):
        data, err = rtr._validate_input({
            "termination_reason": "invalid_value"
        })
        assert err != ""
        assert data is None

    def test_invalid_termination_cause_rejected(self):
        data, err = rtr._validate_input({
            "termination_reason": "approved",
            "termination_cause": "invalid_cause",
        })
        assert err != ""
        assert data is None

    def test_human_escalation_missing_cause_defaults_to_human_judgment_required(self):
        result = rtr.render(_make_input("human_escalation"))
        assert result["termination_cause"] == "human_judgment_required"
        assert "Cause: none" not in result["body"]
        assert "Cause: human judgment required" in result["body"]

    def test_legacy_blocker_summary_alias_renders_blockers(self):
        result = rtr.render({
            "termination_reason": "human_escalation",
            "issue_number": 42,
            "iteration": 3,
            "blocker_summary": ["legacy blocker entry"],
        })
        assert result["publishable"] is True
        assert result["termination_cause"] == "human_judgment_required"
        assert "## Blockers" in result["body"]
        assert '"legacy blocker entry"' in result["body"]

    def test_blocker_summary_alias_conflict_is_rejected(self):
        data, err = rtr._validate_input({
            "termination_reason": "human_escalation",
            "blocker_summary": ["legacy blocker"],
            "blockers_summary": ["canonical blocker"],
        })
        assert data is None
        assert err == "blocker_summary and blockers_summary conflict"

    def test_blocker_summary_alias_type_is_rejected(self):
        data, err = rtr._validate_input({
            "termination_reason": "human_escalation",
            "blocker_summary": "not-a-list",
        })
        assert data is None
        assert err == "blocker_summary must be a list of strings"


# ---------------------------------------------------------------------------
# AC8: GFM fence / HTML marker / adversarial input robustness
# ---------------------------------------------------------------------------

class TestFenceAndAdversarialInput:
    """
    Verify that adversarial content in blockers_summary / input fields
    does not break the template structure via GFM fence injection.
    """

    def test_triple_backtick_in_blocker_does_not_break_guard(self):
        # A blocker summary containing triple backtick would normally inject a fence.
        # The template must handle this without producing shell_command blocks.
        result = rtr.render(_make_input(
            "human_escalation",
            termination_cause="needs_fix_at_iteration_limit",
            blockers_summary=["blocker with ```fence``` injection attempt"],
        ))
        # The guard must have run (attempt count >= 1)
        assert result["attempts"] >= 1
        # If publishable, body must not expose injected fence as shell command
        if result["publishable"]:
            assert result["body"] is not None
            # Re-run guard to confirm body passes
            ok, errs = rtr._run_guard(result["body"])
            assert ok, f"Body failed guard: {errs}"

    def test_html_marker_in_blockers_does_not_break_structure(self):
        adversarial = "<!-- LOOP_HANDOFF_RESULT_V1 --> injected marker"
        result = rtr.render(_make_input(
            "human_escalation",
            termination_cause="human_judgment_required",
            blockers_summary=[adversarial],
        ))
        assert result["attempts"] >= 1
        if result["publishable"]:
            assert result["body"] is not None

    def test_tilde_fence_injection_in_blockers(self):
        # B2: tilde fence injection - body must pass guard and not expose shell_command block
        adversarial = "~~~shell\nrm -rf /\n~~~"
        result = rtr.render(_make_input(
            "human_escalation",
            termination_cause="max_iterations_exceeded",
            blockers_summary=[adversarial],
        ))
        assert result["attempts"] >= 1
        # Body must be publishable and pass guard
        assert result["publishable"] is True, "tilde fence injection should be contained"
        ok, errs = rtr._run_guard(result["body"])
        assert ok, f"Body with tilde fence injection failed guard: {errs}"
        # The blocker payload must not appear as an independent shell_command block
        # (verify by checking no block in body is classified as shell_command)
        for block_text, _ in rtr.iter_markdown_blocks(result["body"]):
            kind = rtr.classify_block(block_text)
            assert kind not in ("shell_command", "vc_command"), (
                f"tilde fence injection leaked as '{kind}' block: {block_text[:80]!r}"
            )

    def test_english_stack_trace_in_blockers(self):
        # B2: multiline stack trace - body must pass guard
        adversarial = (
            "Traceback (most recent call last):\n"
            '  File "foo.py", line 1, in <module>\n'
            "ValueError: something went wrong"
        )
        result = rtr.render(_make_input(
            "human_escalation",
            termination_cause="human_judgment_required",
            blockers_summary=[adversarial],
        ))
        assert result["attempts"] >= 1
        assert result["publishable"] is True, "stack trace should be safely encoded"
        ok, errs = rtr._run_guard(result["body"])
        assert ok, f"Body with stack trace failed guard: {errs}"

    def test_triple_backtick_shell_injection_guard_pass(self):
        # B2: explicit regression - ```shell block injection in blocker
        adversarial = "```shell\nrm -rf /\n```"
        result = rtr.render(_make_input(
            "human_escalation",
            termination_cause="needs_fix_at_iteration_limit",
            blockers_summary=[adversarial],
        ))
        assert result["publishable"] is True
        ok, errs = rtr._run_guard(result["body"])
        assert ok, f"```shell injection leaked through guard: {errs}"
        for block_text, _ in rtr.iter_markdown_blocks(result["body"]):
            kind = rtr.classify_block(block_text)
            assert kind not in ("shell_command", "vc_command"), (
                f"```shell injection classified as '{kind}': {block_text[:80]!r}"
            )

    def test_loop_handoff_marker_in_blocker_guard_pass(self):
        # B2: LOOP_HANDOFF_RESULT_V1 marker injection in blocker
        adversarial = "<!-- LOOP_HANDOFF_RESULT_V1 --> injected"
        result = rtr.render(_make_input(
            "human_escalation",
            termination_cause="human_judgment_required",
            blockers_summary=[adversarial],
        ))
        assert result["publishable"] is True
        ok, errs = rtr._run_guard(result["body"])
        assert ok, f"HTML marker injection failed guard: {errs}"

    def test_dynamic_fence_used_in_blocker_output(self):
        # B2: verify _make_dynamic_fence is actually used in blocker rendering
        # by checking that a blocker with backticks is wrapped in a fence
        # longer than the backtick sequence
        adversarial = "has `````five backtick````` sequence"
        result = rtr.render(_make_input(
            "human_escalation",
            termination_cause="needs_fix_at_iteration_limit",
            blockers_summary=[adversarial],
        ))
        assert result["publishable"] is True
        body = result["body"]
        # The body must contain a fence of length >= 6 (five backticks + 1)
        import re
        fences = re.findall(r"^(`{6,})", body, re.MULTILINE)
        assert fences, (
            "Expected a dynamic fence of length >= 6 in body when blocker contains "
            "a 5-backtick sequence, but none found. "
            "This suggests _make_dynamic_fence is not being used."
        )

    def test_dynamic_fence_length_increases_with_content(self):
        # _make_dynamic_fence must return >= max backtick run + 1
        fence = rtr._make_dynamic_fence("```some content```")
        # content has ``` (3 backticks) -> fence should be 4+
        assert len(fence) >= 4
        assert all(c == "`" for c in fence)

    def test_dynamic_fence_minimum_3(self):
        fence = rtr._make_dynamic_fence("no backticks here")
        assert len(fence) >= 3

    def test_dynamic_fence_with_4_backtick_sequence(self):
        fence = rtr._make_dynamic_fence("````long fence````")
        assert len(fence) >= 5


# ---------------------------------------------------------------------------
# AC9: simulated replay evidence (schema verification)
# ---------------------------------------------------------------------------

class TestSimulatedReplayEvidence:
    """
    AC9: simulated replay — verify that no GitHub mutation is triggered
    by calling render() directly. The test acts as the evidence.
    """

    def test_render_approved_no_gh_mutation(self):
        """
        GIVEN approved termination input
        WHEN render() is called
        THEN TERMINATION_REPORT_RENDER_RESULT_V1 schema is returned
        AND no gh/network/subprocess call is made
        """
        import subprocess
        with patch.object(subprocess, "run") as mock_run:
            result = rtr.render({
                "termination_reason": "approved",
                "issue_number": 656,
                "iteration": 2,
            })
            mock_run.assert_not_called()

        assert result["schema"] == "TERMINATION_REPORT_RENDER_RESULT_V1"
        assert result["publishable"] is True
        assert result["body"] is not None

    def test_render_human_escalation_no_gh_mutation(self):
        """
        GIVEN human_escalation termination input
        WHEN render() is called
        THEN TERMINATION_REPORT_RENDER_RESULT_V1 schema is returned
        AND no gh/network/subprocess call is made
        """
        import subprocess
        with patch.object(subprocess, "run") as mock_run:
            result = rtr.render({
                "termination_reason": "human_escalation",
                "termination_cause": "needs_fix_at_iteration_limit",
                "issue_number": 656,
                "iteration": 3,
                "blockers_summary": ["AC3 missing", "VC not passing"],
            })
            mock_run.assert_not_called()

        assert result["schema"] == "TERMINATION_REPORT_RENDER_RESULT_V1"
        assert result["publishable"] is True

    def test_render_publishable_false_no_gh_mutation(self):
        """
        GIVEN a render that produces publishable=false (guard forced to fail)
        WHEN render() is called
        THEN publishable=false, body=null, reason_code=guard_fail_limit_exceeded
        AND no gh/network/subprocess call is made
        """
        import subprocess
        with (
            patch.object(subprocess, "run") as mock_run,
            patch.object(rtr, "_run_guard", return_value=(False, ["simulated guard fail"])),
        ):
            result = rtr.render({
                "termination_reason": "human_escalation",
                "termination_cause": "max_iterations_exceeded",
            })
            mock_run.assert_not_called()

        assert result["schema"] == "TERMINATION_REPORT_RENDER_RESULT_V1"
        assert result["publishable"] is False
        assert result["body"] is None
        assert result["reason_code"] == "guard_fail_limit_exceeded"


# ---------------------------------------------------------------------------
# Input validation edge cases
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_null_cause_is_valid(self):
        data = {"termination_reason": "approved", "termination_cause": None}
        validated, err = rtr._validate_input(data)
        assert err == ""

    def test_missing_cause_is_valid(self):
        data = {"termination_reason": "approved"}
        validated, err = rtr._validate_input(data)
        assert err == ""

    def test_invalid_reason_returns_error(self):
        data = {"termination_reason": "invalid"}
        validated, err = rtr._validate_input(data)
        assert err != ""
        assert validated is None

    def test_non_dict_input_returns_error(self):
        validated, err = rtr._validate_input([])
        assert err != ""
        assert validated is None

    def test_issue_number_must_be_int(self):
        data = {"termination_reason": "approved", "issue_number": "not-int"}
        validated, err = rtr._validate_input(data)
        assert err != ""

    def test_all_valid_reasons_pass(self):
        for reason in ["approved", "human_escalation", "superseded_by_decision"]:
            data = {"termination_reason": reason}
            validated, err = rtr._validate_input(data)
            assert err == "", f"Reason '{reason}' should be valid"

    def test_all_valid_causes_pass(self):
        for cause in [
            "needs_fix_at_iteration_limit",
            "max_iterations_exceeded",
            "human_judgment_required",
            None,
        ]:
            data = {"termination_reason": "approved", "termination_cause": cause}
            validated, err = rtr._validate_input(data)
            assert err == "", f"Cause '{cause}' should be valid"

    # B3: blockers_summary element type validation
    def test_blockers_summary_with_none_element_rejected(self):
        data = {
            "termination_reason": "human_escalation",
            "termination_cause": "needs_fix_at_iteration_limit",
            "blockers_summary": [None],
        }
        validated, err = rtr._validate_input(data)
        assert err != "", "None element in blockers_summary must be rejected"
        assert validated is None

    def test_blockers_summary_with_dict_element_rejected(self):
        data = {
            "termination_reason": "human_escalation",
            "termination_cause": "needs_fix_at_iteration_limit",
            "blockers_summary": [{"key": "value"}],
        }
        validated, err = rtr._validate_input(data)
        assert err != "", "dict element in blockers_summary must be rejected"
        assert validated is None

    def test_blockers_summary_with_int_element_rejected(self):
        data = {
            "termination_reason": "human_escalation",
            "termination_cause": "needs_fix_at_iteration_limit",
            "blockers_summary": [42],
        }
        validated, err = rtr._validate_input(data)
        assert err != "", "int element in blockers_summary must be rejected"
        assert validated is None

    def test_blockers_summary_with_string_elements_valid(self):
        data = {
            "termination_reason": "human_escalation",
            "termination_cause": "needs_fix_at_iteration_limit",
            "blockers_summary": ["blocker one", "blocker two"],
        }
        validated, err = rtr._validate_input(data)
        assert err == "", "list of strings should be valid"

    # B3: bool rejection for issue_number and iteration
    def test_issue_number_bool_rejected(self):
        # isinstance(True, int) is True in Python, so we must use type() check
        data = {"termination_reason": "approved", "issue_number": True}
        validated, err = rtr._validate_input(data)
        assert err != "", "bool must not be accepted as issue_number"
        assert validated is None

    def test_iteration_bool_rejected(self):
        data = {"termination_reason": "approved", "iteration": False}
        validated, err = rtr._validate_input(data)
        assert err != "", "bool must not be accepted as iteration"
        assert validated is None



# ---------------------------------------------------------------------------
# #1311: LOOP_HANDOFF_RESULT_V1 marker generation
# ---------------------------------------------------------------------------

def _make_skipped_auto_fix_item(kind: str = "metadata_hygiene") -> dict:
    """A single valid auto_fixes.skipped entry (schema definitions.auto_fix_item)."""
    return {
        "kind": kind,
        "executor": "implementation-worker",
        "result": "skipped",
        "evidence": {
            "before": "before-state",
            "after": "after-state",
            "comment_url": "https://github.com/o/r/issues/1#issuecomment-2",
        },
    }


def _make_loop_handoff(
    *,
    status: str = "impl_ready",
    routing_action: str = "run_impl_review_loop",
    contract_review_status: str = "go",
    gate_result: str = "fresh_go",
    blockers: list[dict] | None = None,
    latest_comment_url: str = "https://github.com/o/r/issues/1#issuecomment-1",
    metadata_ready: bool = True,
    auto_fixes_result: str = "auto_fixed",
    auto_fixes_skipped: list[dict] | None = None,
) -> dict:
    return {
        "status": status,
        "routing_action": routing_action,
        "contract_review": {
            "status": contract_review_status,
            "gate_result": gate_result,
            "latest_comment_url": latest_comment_url,
            "generated_at": "2026-07-04T00:00:00Z",
            "body_sha256": "sha256:" + "a" * 64,
        },
        "metadata": {
            "title_prefix_ready": metadata_ready,
            "phase_label_ready": metadata_ready,
        },
        "auto_fixes": {
            "result": auto_fixes_result,
            "required": [],
            "skipped": auto_fixes_skipped if auto_fixes_skipped is not None else [],
        },
        "blockers": blockers if blockers is not None else [],
        "permissions": {"unavailable": []},
        "generated_at": "2026-07-04T00:00:00Z",
    }


def _load_loop_handoff_schema() -> dict:
    schema_path = (
        Path(__file__).resolve().parent.parent / "schemas" / "loop_handoff_result_v1.json"
    )
    import json as _json

    return _json.loads(schema_path.read_text(encoding="utf-8"))


def _extract_marker_yaml_block(body: str) -> dict:
    """Extract and parse the fenced YAML block following the marker HTML comment."""
    pattern = (
        r"^<!-- LOOP_HANDOFF_RESULT_V1 -->" + "\n"
        r"(`{3,}|~{3,})yaml" + "\n"
        r"(.*?)" + "\n"
        r"\1\s*$"
    )
    m = _re.search(pattern, body, _re.DOTALL | _re.MULTILINE)
    assert m is not None, "LOOP_HANDOFF_RESULT_V1 marker block not found in body:\n" + body
    return yaml.safe_load(m.group(2))


class TestLoopHandoffMarkerApproved:
    """AC1: approved + loop_handoff -> marker + fenced YAML block emitted."""

    def test_render_termination_report_approved_with_loop_handoff_emits_marker(self):
        loop_handoff = _make_loop_handoff()
        result = rtr.render(_make_input("approved", issue_number=1311))
        # baseline: no loop_handoff -> no marker (sanity check before positive case)
        assert rtr.LOOP_HANDOFF_MARKER not in result["body"]

        data = _make_input("approved", issue_number=1311)
        data["loop_handoff"] = loop_handoff
        result = rtr.render(data)

        assert result["publishable"] is True
        assert rtr.LOOP_HANDOFF_MARKER in result["body"]
        parsed = _extract_marker_yaml_block(result["body"])
        assert parsed["LOOP_HANDOFF_RESULT_V1"]["status"] == "impl_ready"
        assert parsed["LOOP_HANDOFF_RESULT_V1"]["routing_action"] == "run_impl_review_loop"

        # Guard must still pass with the marker attached
        ok, errs = rtr._run_guard(result["body"])
        assert ok, f"marker body failed guard: {errs}"


class TestLoopHandoffMarkerOmittedWithoutInput:
    """AC2: approved without loop_handoff -> no marker, back-compat preserved."""

    def test_render_termination_report_approved_without_loop_handoff_omits_marker(self):
        data = _make_input("approved", issue_number=42)
        assert "loop_handoff" not in data
        result = rtr.render(data)

        assert result["publishable"] is True
        assert rtr.LOOP_HANDOFF_MARKER not in result["body"]
        # No exception raised, no extra required field errors
        assert result["reason_code"] is None


class TestLoopHandoffSchemaValid:
    """AC4: marker YAML block validates against schemas/loop_handoff_result_v1.json."""

    def test_render_termination_report_loop_handoff_schema_valid(self):
        loop_handoff = _make_loop_handoff()
        data = _make_input("approved", issue_number=1311)
        data["loop_handoff"] = loop_handoff
        result = rtr.render(data)

        parsed = _extract_marker_yaml_block(result["body"])
        schema = _load_loop_handoff_schema()
        validator = jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker())
        errors = list(validator.iter_errors(parsed))
        assert errors == [], f"schema validation errors: {errors}"


class TestLoopHandoffRoutingRulesMatrix:
    """AC5: status/routing_action pairing follows the Routing Rules table
    (references/termination-policy.md), fixed for at least impl_ready and
    blocked (plus human_judgment_required for full table coverage)."""

    def test_render_termination_report_routing_rules_matrix(self):
        # Each case is built to be consistent with exactly one Routing Rules
        # row (references/termination-policy.md): impl_ready via all
        # invariants satisfied; blocked via a bad gate_result + non-empty
        # blockers; human_judgment_required via non-empty auto_fixes.skipped
        # with an otherwise-clean payload (blockers must stay empty, since a
        # non-empty blockers list forces blocked/blocked regardless of the
        # other fields per _validate_loop_handoff_policy()).
        cases = [
            (
                "impl_ready",
                "run_impl_review_loop",
                dict(
                    contract_review_status="go",
                    gate_result="fresh_go",
                    blockers=[],
                    auto_fixes_result="auto_fixed",
                    auto_fixes_skipped=[],
                ),
            ),
            (
                "blocked",
                "blocked",
                dict(
                    contract_review_status="blocked",
                    gate_result="missing_go",
                    blockers=[{"kind": "x", "description": "y"}],
                    auto_fixes_result="blocked",
                    auto_fixes_skipped=[],
                ),
            ),
            (
                "human_judgment_required",
                "ask_human",
                dict(
                    contract_review_status="go",
                    gate_result="fresh_go",
                    blockers=[],
                    auto_fixes_result="human_judgment_required",
                    auto_fixes_skipped=[_make_skipped_auto_fix_item()],
                ),
            ),
        ]
        for status, routing_action, overrides in cases:
            loop_handoff = _make_loop_handoff(
                status=status,
                routing_action=routing_action,
                **overrides,
            )
            data = _make_input("approved", issue_number=1)
            data["loop_handoff"] = loop_handoff
            result = rtr.render(data)
            assert result["publishable"] is True, (
                f"status={status} routing_action={routing_action} should render: "
                f"{result.get('reason_code')}"
            )
            parsed = _extract_marker_yaml_block(result["body"])
            assert parsed["LOOP_HANDOFF_RESULT_V1"]["status"] == status
            assert parsed["LOOP_HANDOFF_RESULT_V1"]["routing_action"] == routing_action

    def test_render_termination_report_routing_rules_mismatch_rejected(self):
        # status=blocked with routing_action=run_impl_review_loop violates the
        # Routing Rules table and must fail closed (InputValidationError).
        loop_handoff = _make_loop_handoff(
            status="blocked",
            routing_action="run_impl_review_loop",
            contract_review_status="blocked",
            gate_result="missing_go",
            blockers=[{"kind": "x", "description": "y"}],
        )
        data = _make_input("approved", issue_number=1)
        data["loop_handoff"] = loop_handoff
        with pytest.raises(rtr.InputValidationError):
            rtr.render(data)

    def test_render_termination_report_human_judgment_required_with_missing_go_rejected(self):
        """Blocker (reviewer #1317): a payload declaring
        status=human_judgment_required/routing_action=ask_human while
        contract_review.gate_result=missing_go (a gate result the Routing
        Rules table reserves for status=blocked/routing_action=blocked) must
        be rejected, not silently accepted as valid."""
        loop_handoff = _make_loop_handoff(
            status="human_judgment_required",
            routing_action="ask_human",
            contract_review_status="blocked",
            gate_result="missing_go",
            blockers=[],
            auto_fixes_result="human_judgment_required",
            auto_fixes_skipped=[],
        )
        data = _make_input("approved", issue_number=1)
        data["loop_handoff"] = loop_handoff
        with pytest.raises(rtr.InputValidationError):
            rtr.render(data)

    def test_render_termination_report_impl_ready_with_nonempty_skipped_rejected(self):
        """status=impl_ready with a non-empty auto_fixes.skipped violates both
        the schema's allOf/if-then clause and the auto_fixes.skipped ->
        human_judgment_required/ask_human Routing Rules row."""
        loop_handoff = _make_loop_handoff(
            status="impl_ready",
            routing_action="run_impl_review_loop",
            contract_review_status="go",
            gate_result="fresh_go",
            blockers=[],
            auto_fixes_result="human_judgment_required",
            auto_fixes_skipped=[_make_skipped_auto_fix_item()],
        )
        data = _make_input("approved", issue_number=1)
        data["loop_handoff"] = loop_handoff
        with pytest.raises(rtr.InputValidationError):
            rtr.render(data)

    def test_render_termination_report_impl_ready_metadata_false_without_auto_fix_evidence_rejected(self):
        """status=impl_ready with metadata.title_prefix_ready=false and no
        applied metadata_hygiene/template_hygiene auto_fixes.required entry
        must be rejected (references/termination-policy.md impl_ready
        definition items 3-4)."""
        loop_handoff = _make_loop_handoff(
            status="impl_ready",
            routing_action="run_impl_review_loop",
            contract_review_status="go",
            gate_result="fresh_go",
            blockers=[],
            metadata_ready=False,
            auto_fixes_result="auto_fixed",
            auto_fixes_skipped=[],
        )
        data = _make_input("approved", issue_number=1)
        data["loop_handoff"] = loop_handoff
        with pytest.raises(rtr.InputValidationError):
            rtr.render(data)

    def test_render_termination_report_impl_ready_metadata_false_with_applied_auto_fix_accepted(self):
        """status=impl_ready with metadata.title_prefix_ready=false /
        phase_label_ready=false is allowed when auto_fixes.required contains
        an applied metadata_hygiene entry with evidence (title prefix / phase
        label unreadiness alone must not block impl_ready when a worker
        auto-fix was applied)."""
        loop_handoff = _make_loop_handoff(
            status="impl_ready",
            routing_action="run_impl_review_loop",
            contract_review_status="go",
            gate_result="fresh_go",
            blockers=[],
            metadata_ready=False,
            auto_fixes_result="auto_fixed",
            auto_fixes_skipped=[],
        )
        loop_handoff["auto_fixes"]["required"] = [
            {
                "kind": "metadata_hygiene",
                "executor": "implementation-worker",
                "result": "applied",
                "evidence": {
                    "before": "no title prefix",
                    "after": "[Impl] title prefix",
                    "comment_url": "https://github.com/o/r/issues/1#issuecomment-3",
                },
            }
        ]
        data = _make_input("approved", issue_number=1)
        data["loop_handoff"] = loop_handoff
        result = rtr.render(data)
        assert result["publishable"] is True, result.get("reason_code")


class TestLoopHandoffWrapperFormAccepted:
    """High 2 (reviewer #1317): the schema's canonical wrapper form
    ``{"LOOP_HANDOFF_RESULT_V1": {...}}`` must be accepted in addition to the
    bare inner object, without synthesizing/deriving any field from a
    partial payload."""

    def test_render_termination_report_wrapper_form_loop_handoff_accepted(self):
        inner = _make_loop_handoff()
        data = _make_input("approved", issue_number=1)
        data["loop_handoff"] = {"LOOP_HANDOFF_RESULT_V1": inner}
        result = rtr.render(data)
        assert result["publishable"] is True, result.get("reason_code")
        parsed = _extract_marker_yaml_block(result["body"])
        assert parsed["LOOP_HANDOFF_RESULT_V1"]["status"] == "impl_ready"

    def test_render_termination_report_wrapper_form_and_bare_form_equivalent(self):
        inner = _make_loop_handoff()
        data_bare = _make_input("approved", issue_number=1)
        data_bare["loop_handoff"] = inner
        result_bare = rtr.render(data_bare)

        data_wrapped = _make_input("approved", issue_number=1)
        data_wrapped["loop_handoff"] = {"LOOP_HANDOFF_RESULT_V1": inner}
        result_wrapped = rtr.render(data_wrapped)

        assert result_bare["publishable"] is True
        assert result_wrapped["publishable"] is True
        assert _extract_marker_yaml_block(result_bare["body"]) == _extract_marker_yaml_block(
            result_wrapped["body"]
        )

    def test_render_termination_report_wrapper_form_does_not_synthesize_partial_payload(self):
        """A minimal/partial wrapper payload (missing required inner fields)
        must still fail closed -- normalization must not derive/synthesize
        the missing fields."""
        data = _make_input("approved", issue_number=1)
        data["loop_handoff"] = {"LOOP_HANDOFF_RESULT_V1": {"status": "impl_ready"}}
        with pytest.raises(rtr.InputValidationError):
            rtr.render(data)

    def test_render_termination_report_ambiguous_wrapper_shape_rejected(self):
        """A dict with the wrapper key plus an extra sibling key is ambiguous
        and must be rejected rather than silently unwrapped."""
        inner = _make_loop_handoff()
        data = _make_input("approved", issue_number=1)
        data["loop_handoff"] = {
            "LOOP_HANDOFF_RESULT_V1": inner,
            "unexpected_sibling_key": "nope",
        }
        with pytest.raises(rtr.InputValidationError):
            rtr.render(data)


class TestLoopHandoffDateTimeFormatRejected:
    """Medium (reviewer #1317): invalid date-time strings in loop_handoff
    generated_at fields must be rejected even though jsonschema's default
    FormatChecker does not register a "date-time" checker without an
    optional dependency (rfc3339-validator / strict-rfc3339)."""

    def test_render_termination_report_invalid_generated_at_rejected(self):
        loop_handoff = _make_loop_handoff()
        loop_handoff["generated_at"] = "not-a-date-time"
        data = _make_input("approved", issue_number=1)
        data["loop_handoff"] = loop_handoff
        with pytest.raises(rtr.InputValidationError):
            rtr.render(data)

    def test_render_termination_report_invalid_contract_review_generated_at_rejected(self):
        loop_handoff = _make_loop_handoff()
        loop_handoff["contract_review"]["generated_at"] = "not-a-date-time"
        data = _make_input("approved", issue_number=1)
        data["loop_handoff"] = loop_handoff
        with pytest.raises(rtr.InputValidationError):
            rtr.render(data)

    def test_render_termination_report_valid_generated_at_accepted(self):
        loop_handoff = _make_loop_handoff()
        data = _make_input("approved", issue_number=1)
        data["loop_handoff"] = loop_handoff
        result = rtr.render(data)
        assert result["publishable"] is True, result.get("reason_code")


class TestLoopHandoffNonApprovedOmitsMarker:
    """AC6: non-approved termination_reason never emits the marker, even if a
    valid loop_handoff payload is supplied."""

    def test_render_termination_report_non_approved_omits_marker_even_with_loop_handoff(self):
        loop_handoff = _make_loop_handoff()
        for reason, cause in [
            ("human_escalation", "human_judgment_required"),
            ("superseded_by_decision", None),
        ]:
            data = _make_input(reason, termination_cause=cause, issue_number=1)
            data["loop_handoff"] = loop_handoff
            result = rtr.render(data)
            assert result["publishable"] is True
            assert rtr.LOOP_HANDOFF_MARKER not in result["body"], (
                f"marker leaked for termination_reason={reason!r}"
            )


class TestLoopHandoffFailClosed:
    """loop_handoff must fail closed (InputValidationError) on invalid payloads,
    regardless of termination_reason (non-approved included, per reviewer note 6)."""

    def test_invalid_loop_handoff_missing_required_field_approved(self):
        loop_handoff = _make_loop_handoff()
        del loop_handoff["contract_review"]
        data = _make_input("approved", issue_number=1)
        data["loop_handoff"] = loop_handoff
        with pytest.raises(rtr.InputValidationError):
            rtr.render(data)

    def test_invalid_loop_handoff_additional_property_rejected(self):
        loop_handoff = _make_loop_handoff()
        loop_handoff["unexpected_extra_field"] = "nope"
        data = _make_input("approved", issue_number=1)
        data["loop_handoff"] = loop_handoff
        with pytest.raises(rtr.InputValidationError):
            rtr.render(data)

    def test_invalid_loop_handoff_non_dict_rejected(self):
        data = _make_input("approved", issue_number=1)
        data["loop_handoff"] = "not-a-dict"
        with pytest.raises(rtr.InputValidationError):
            rtr.render(data)

    def test_invalid_loop_handoff_rejected_even_when_non_approved(self):
        # Reviewer note 6: silently ignoring an invalid loop_handoff on
        # non-approved termination_reason is not acceptable; must still
        # fail closed.
        data = _make_input(
            "human_escalation",
            termination_cause="human_judgment_required",
            issue_number=1,
        )
        data["loop_handoff"] = {"status": "impl_ready"}  # missing required fields
        with pytest.raises(rtr.InputValidationError):
            rtr.render(data)


class TestLoopHandoffFenceInjectionRegression:
    """
    Reviewer note 5: backtick / tilde / HTML-comment / shell-looking text
    embedded in loop_handoff string fields must not break the marker out of
    its fence, must not duplicate the marker as a standalone line, and the
    fenced YAML must still parse cleanly.
    """

    ADVERSARIAL_PAYLOADS = [
        "```shell\nrm -rf /\n```",
        "~~~shell\nrm -rf /\n~~~",
        "<!-- LOOP_HANDOFF_RESULT_V1 --> injected duplicate marker",
        "$ pnpm test && rg 'secret'",
        "``````six-backtick-run``````",
    ]

    def test_adversarial_string_fields_do_not_break_marker(self):
        for adversarial in self.ADVERSARIAL_PAYLOADS:
            loop_handoff = _make_loop_handoff(
                latest_comment_url="https://example.com/x",
                blockers=[],
            )
            loop_handoff["contract_review"]["body_sha256"] = "sha256:" + "a" * 64
            loop_handoff["metadata"]["title_prefix_ready"] = True
            # Inject adversarial content into a free-form string field.
            loop_handoff["contract_review"]["latest_comment_url"] = adversarial

            data = _make_input("approved", issue_number=1)
            data["loop_handoff"] = loop_handoff
            result = rtr.render(data)

            assert result["publishable"] is True, (
                f"adversarial payload broke render: {adversarial!r} "
                f"reason_code={result.get('reason_code')}"
            )
            body = result["body"]

            # Marker appears exactly once as a standalone line.
            marker_lines = [
                line for line in body.splitlines() if line.strip() == rtr.LOOP_HANDOFF_MARKER
            ]
            assert len(marker_lines) == 1, (
                f"expected exactly 1 standalone marker line for {adversarial!r}, "
                f"got {len(marker_lines)}"
            )

            # YAML block still parses.
            parsed = _extract_marker_yaml_block(body)
            assert parsed["LOOP_HANDOFF_RESULT_V1"]["status"] == "impl_ready"

            # Guard passes (no shell_command / vc_command leak).
            ok, errs = rtr._run_guard(body)
            assert ok, f"guard failed for adversarial payload {adversarial!r}: {errs}"
            for block_text, _ in rtr.iter_markdown_blocks(body):
                kind = rtr.classify_block(block_text)
                assert kind not in ("shell_command", "vc_command"), (
                    f"adversarial payload leaked as {kind!r}: {block_text[:80]!r}"
                )


class TestLoopHandoffFallbackTemplateRetainsMarker:
    """
    Reviewer note 3: the marker must not disappear when the normal template
    fails guard and the fallback template is used; if both templates fail
    guard even with the marker attached, the whole render fails closed
    (publishable=false) rather than dropping the marker to force a success.
    """

    def test_fallback_template_still_carries_marker_when_normal_fails_guard(self):
        loop_handoff = _make_loop_handoff()
        data = _make_input("approved", issue_number=1)
        data["loop_handoff"] = loop_handoff

        call_count = {"n": 0}
        real_guard = rtr._run_guard

        def _guard_fail_first_attempt_only(body: str):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return False, ["forced failure on attempt 1"]
            return real_guard(body)

        with patch.object(rtr, "_run_guard", side_effect=_guard_fail_first_attempt_only):
            result = rtr.render(data)

        assert result["attempts"] == 2
        assert result["publishable"] is True
        assert rtr.LOOP_HANDOFF_MARKER in result["body"]

    def test_both_attempts_guard_fail_drops_marker_via_publishable_false(self):
        loop_handoff = _make_loop_handoff()
        data = _make_input("approved", issue_number=1)
        data["loop_handoff"] = loop_handoff

        with patch.object(rtr, "_run_guard", return_value=(False, ["forced failure"])):
            result = rtr.render(data)

        # Both attempts failed guard -> fail closed, marker not silently
        # "succeeded" without it.
        assert result["publishable"] is False
        assert result["body"] is None
        assert result["reason_code"] == rtr.GUARD_FAIL_REASON_CODE
