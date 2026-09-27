"""scripts/claude-gpt/tests/test_runtime_smoke_auto_classifier_scenario.py

Issue #2772 AC10 sub-scenario 3: static/structural + argv-parsing regression
tests for `scripts/claude-gpt/runtime_smoke_test.sh --scenario
auto_classifier`.

This scenario proves that an auto-mode classifier request (Claude Code's own
built-in Bash-command-risk classifier, routed via `CLAUDE_CODE_AUTO_MODE_SERVER=0`
+ `CCP_AUTO_REVIEW_MODEL=gpt-6-luna`, both already wired unconditionally by
`launch.sh`/`lib.sh`) actually reaches `gpt-6-luna` as an upstream request that
is independent of the session model (`gpt-6-sol`). It deliberately reuses the
SAME default smoke launch/convo-step machinery as the two other `--scenario`
modes (`issue_create` / `issue_to_impl`) instead of a separate fixture-backed
harness -- so, unlike those two, it has no bespoke fixture/fake-provider setup
of its own to unit-test; the only genuinely new production logic is:

  1. the `--scenario auto_classifier` argv dispatch itself (deterministic,
     bounded, exercised here via real subprocess invocations that are
     rejected/accepted purely by the argv pre-scan -- never reaching a live
     `claude`/proxy launch, so no live ChatGPT auth is required);
  2. the extra `classifier_probe` convo step and the post-run
     `AUTO_CLASSIFIER_LUNA_OBSERVED` / `AUTO_CLASSIFIER_SESSION_MODEL_OK`
     assertions, which can only be driven end-to-end with a live authenticated
     `claude` + `claude-code-proxy` run (out of scope for this module -- a
     separate live test-runner pass covers that, per this Issue's own
     Runtime Verification Applicability).

The genuinely unit-testable piece of AC10 sub-scenario 3 -- the new `model`
field `transport_log.py` now carries per-request, which this scenario's
post-run assertion depends on -- is covered separately in
`scripts/claude-gpt/test_transport_log.py` (not duplicated here).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

RUNTIME_SMOKE_SH = Path(__file__).resolve().parents[1] / "runtime_smoke_test.sh"


def _source() -> str:
    return RUNTIME_SMOKE_SH.read_text(encoding="utf-8")


# --- AC11-style argv dispatch: unknown/known --scenario value handling ---


def test_unknown_scenario_error_message_lists_auto_classifier():
    """The (pre-existing) unknown-`--scenario`-value rejection message must
    be updated to also advertise `auto_classifier` as a known value, so a
    caller mistyping it gets an accurate error instead of a stale list."""
    proc = subprocess.run(
        ["sh", str(RUNTIME_SMOKE_SH), "--scenario", "does_not_exist"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2
    assert "auto_classifier" in proc.stderr, proc.stderr
    assert "issue_create" in proc.stderr, proc.stderr
    assert "issue_to_impl" in proc.stderr, proc.stderr


def test_unknown_scenario_still_does_not_fall_back_to_default_smoke():
    """Regression guard: adding a third known scenario value must not loosen
    the AC11 fall-back-refusal contract for genuinely unknown values."""
    proc = subprocess.run(
        ["sh", str(RUNTIME_SMOKE_SH), "--scenario", "does_not_exist"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2
    assert "CLAUDE_GPT_SMOKE_RESULT_V1" not in proc.stdout
    assert "ISSUE_TO_IMPL_E2E_RESULT_V1" not in proc.stdout


def test_auto_classifier_scenario_value_is_accepted_by_the_case_statement():
    """Static source check (no subprocess -- an accepted value proceeds past
    argv parsing into the live default-smoke flow, which requires a real
    `claude`/proxy environment and is out of this module's scope). The
    `--scenario` value case/esac must map `auto_classifier` to
    `AUTO_CLASSIFIER_SCENARIO=true`, exactly like `issue_to_impl` maps to its
    own flag on the same line shape."""
    source = _source()
    assert re.search(r"issue_create\)\s*:\s*;;", source)
    assert re.search(r"issue_to_impl\)\s*ISSUE_TO_IMPL_SCENARIO=true\s*;;", source)
    assert re.search(r"auto_classifier\)\s*AUTO_CLASSIFIER_SCENARIO=true\s*;;", source)


def test_auto_classifier_scenario_flag_declared_false_by_default():
    """`AUTO_CLASSIFIER_SCENARIO` must have an explicit default (`false`)
    declared before the argv-parsing loop runs, matching the existing
    `ISSUE_TO_IMPL_SCENARIO`/`ISSUE_CREATE_SCENARIO` convention -- an
    undeclared/unset flag would make every later `[ "$AUTO_CLASSIFIER_SCENARIO"
    = "true" ]` guard silently always-false under `set -u`-style strict shells
    or simply fragile."""
    source = _source()
    declaration_index = source.index("AUTO_CLASSIFIER_SCENARIO=false")
    case_index = source.index("auto_classifier) AUTO_CLASSIFIER_SCENARIO=true")
    assert declaration_index < case_index


# --- default-flow wiring: the extra classifier-forcing convo step ---


def test_classifier_probe_convo_step_is_gated_behind_the_scenario_flag():
    """The extra `classifier_probe` convo step must only run when
    `--scenario auto_classifier` was selected -- the two pre-existing
    scenarios and plain default-smoke invocations must never pay for (or be
    affected by) this extra step."""
    source = _source()
    step_index = source.index('run_convo_step "classifier_probe"')
    preceding = source[:step_index]
    guard_index = preceding.rfind('if [ "$AUTO_CLASSIFIER_SCENARIO" = "true" ]; then')
    assert guard_index != -1, "classifier_probe step is not gated behind AUTO_CLASSIFIER_SCENARIO"
    # No unrelated `fi`/`if` between the guard and the step call that would
    # place it outside the gated block.
    between = preceding[guard_index:]
    assert between.count("if ") <= 1


def test_classifier_probe_step_does_not_pre_grant_allowed_tools():
    """Unlike the "bash" canary step (which pre-approves `Bash(echo *)` via
    `--allowedTools`, deliberately bypassing Claude Code's own auto-mode risk
    classifier), the `classifier_probe` step must NOT pass a pre-approved
    tool allowlist -- otherwise the command would be permitted without ever
    reaching the classifier, defeating the scenario's own purpose."""
    source = _source()
    step_index = source.index('run_convo_step "classifier_probe"')
    # find the end of the run_convo_step invocation (its four double-quoted
    # arguments end at the closing quote of the 4th argument on the same
    # logical shell statement)
    call_end = source.index('\n', source.index('" "" ""', step_index))
    call_text = source[step_index:call_end]
    assert '" "" ""' in call_text, (
        "classifier_probe step must pass empty step_allowed_tools/"
        "step_use_canary_env arguments (no pre-approved tool grant): "
        + call_text
    )


def test_classifier_probe_rc_only_gates_convo_rc_when_scenario_selected():
    """`CLASSIFIER_PROBE_RC` must only affect `CONVO_RC` (and therefore
    overall pass/fail) when the scenario is actually selected -- its default
    value (0) must never spuriously fail an unrelated scenario/default run."""
    source = _source()
    assert 'CLASSIFIER_PROBE_RC=0' in source
    assert re.search(
        r'if \[ "\$AUTO_CLASSIFIER_SCENARIO" = "true" \] && \[ "\$CLASSIFIER_PROBE_RC" -ne 0 \]',
        source,
    )


# --- post-run independence assertion + evidence shape ---


def test_luna_observed_and_session_model_ok_are_vacuously_true_when_not_applicable():
    """When `--scenario auto_classifier` was not selected, both new
    pass/fail inputs must default to `true` (n/a / vacuously satisfied) so
    this addition can never regress the two pre-existing scenarios or plain
    default-smoke runs."""
    source = _source()
    else_block_index = source.index("AUTO_CLASSIFIER_LUNA_OBSERVED=true")
    preceding = source[:else_block_index]
    assert preceding.rstrip().endswith("else")
    assert "AUTO_CLASSIFIER_SESSION_MODEL_OK=true" in source[else_block_index:else_block_index + 200]


def test_runtime_conversation_ok_requires_both_auto_classifier_checks():
    """The overall `RUNTIME_CONVERSATION_OK` gate (which feeds the script's
    exit code) must AND in both new checks, matching the existing
    all-conditions-required convention used for every other check in this
    gate (marker presence, transport, cleanup, git-dirty)."""
    source = _source()
    gate_start = source.index('RUNTIME_CONVERSATION_OK=false\nif [ "$CONVO_RC" -eq 0 ]')
    gate_end = source.index("fi", gate_start)
    gate_block = source[gate_start:gate_end]
    assert '[ "$AUTO_CLASSIFIER_LUNA_OBSERVED" = "true" ]' in gate_block
    assert '[ "$AUTO_CLASSIFIER_SESSION_MODEL_OK" = "true" ]' in gate_block


def test_evidence_json_carries_an_auto_classifier_object_without_bumping_schema_version():
    """The evidence shape addition is purely additive (Issue #2772 background:
    schema_version was previously bumped 1->2 only for a genuinely breaking
    restructure -- PR #2205/#2204 -- not for an additive field). This new
    field must not trigger another bump."""
    source = _source()
    assert '"schema_version": 2,' in source
    assert '"auto_classifier": {' in source
    assert '"applicable": ${AUTO_CLASSIFIER_SCENARIO}' in source
    assert '"luna_classifier_request_observed": ${AUTO_CLASSIFIER_LUNA_OBSERVED}' in source
    assert '"session_model_is_sol": ${AUTO_CLASSIFIER_SESSION_MODEL_OK}' in source


def test_shell_syntax_is_valid():
    """Cheap regression guard: `sh -n` must accept the file (catches
    unbalanced quotes/heredocs/`if`/`fi` introduced by this scenario without
    needing any live environment)."""
    proc = subprocess.run(
        ["sh", "-n", str(RUNTIME_SMOKE_SH)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
