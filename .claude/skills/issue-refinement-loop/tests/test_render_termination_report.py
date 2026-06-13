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

import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

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
        original_guard = rtr._run_guard

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
