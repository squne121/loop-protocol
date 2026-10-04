"""Tests for the assertion-level applicability `disposition` of
`runtime_assertion_bindings` in the shared evaluator
(`extension_surface_policy_matcher.evaluate_runtime_assertion_binding_coverage`,
Issue #2852 AC1-AC8, plus the carrier formatter used by AC9/AC10).

Fixtures use the real production policy
(`docs/dev/extension-surface-runtime-policy.yaml`) via real Allowed Path
matches:

- `.claude/hooks/foo.py` -> hook-chain-runtime-smoke (hard, 2 assertions;
  the #2827-shaped fixture)
- `.claude/skills/foo/SKILL.md` -> skill-invocation-runtime-smoke (hard, 2
  assertions) AND subagent-lifecycle-causal-evidence-smoke (hard, 1
  assertion) -- the #2860-shaped fixture

These tests assert a CLASSIFICATION difference (disposition / source /
missing / unknown / duplicate), never merely that a test file exists.
Structural completeness is not runtime verification PASS: no result field
may be readable as runtime evidence (AC1).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import extension_surface_policy_matcher as matcher  # noqa: E402

HOOK_PATHS = [".claude/hooks/foo.py"]
SKILL_PATHS = [".claude/skills/foo/SKILL.md"]

HOOK = "hook-chain-runtime-smoke"
HOOK_A1 = "all_matching_hooks_observed"
HOOK_A2 = "sibling_side_effect_inventory_complete"
SKILL = "skill-invocation-runtime-smoke"
SKILL_A1 = "procedure_steps_executed_in_declared_order"
SKILL_A2 = "output_contract_schema_fields_present"
SUBAGENT = "subagent-lifecycle-causal-evidence-smoke"
SUBAGENT_A1 = "subagent_start_stop_causal_evidence_correlated"

# Field names that would read as "runtime verification passed / observed".
# `demonstrated_by` is a declared REFERENCE (an AC number), not evidence, and
# is intentionally allowed.
_FORBIDDEN_EVIDENCE_KEYS = {
    "runtime_pass",
    "runtime_passed",
    "runtime_verified",
    "verified",
    "passed",
    "pass",
    "proven",
    "executed",
    "observed",
    "evidence",
    "runtime_evidence",
    "demonstrated",
    "demonstrated_runtime",
    "proof",
}


def _entry(profile: str, assertion: str, **fields: str) -> str:
    lines = [f"  - profile: {profile}", f"    assertion: {assertion}"]
    lines.extend(f"    {key}: {value}" for key, value in fields.items())
    return "\n".join(lines) + "\n"


def _rva(bindings: str, *, decision: str = "immediate", applicable: tuple[int, ...] = (1,)) -> str:
    applicable_yaml = "\n".join(f"  - AC{n}" for n in applicable)
    return (
        "```yaml\n"
        f"decision: {decision}\n"
        f"applicable_acs:\n{applicable_yaml}\n"
        "execution_environment:\n  cli_tools:\n    - python3\n"
        'skip_conditions:\n  - "none"\n'
        "fallback_policy:\n  fallback_success_is_pass: false\n"
        'artifact_requirements:\n  - "artifacts/out.json"\n'
        f"runtime_assertion_bindings:\n{bindings}"
        "```\n"
    )


def _ac_section(numbers: tuple[int, ...], tagged: tuple[int, ...]) -> str:
    return "\n".join(
        f"- [ ] AC{n}: concrete AC {n}" + (" <!-- runtime-verification: true -->" if n in tagged else "")
        for n in numbers
    )


def _coverage(
    allowed: list[str],
    bindings: str,
    *,
    ac_numbers: tuple[int, ...] = (1, 2),
    tagged: tuple[int, ...] = (1,),
    applicable: tuple[int, ...] = (1,),
    vc_refs: Optional[set[str]] = None,
    decision: str = "immediate",
) -> dict:
    return matcher.evaluate_runtime_assertion_binding_coverage(
        allowed_path_entries=allowed,
        rva_section_text=_rva(bindings, decision=decision, applicable=applicable),
        ac_section_text=_ac_section(ac_numbers, tagged),
        ac_vc_refs=vc_refs if vc_refs is not None else {str(n) for n in ac_numbers},
    )


def _hook_valid_mixed() -> str:
    """One dispositive (AC1) + one not_applicable for the 2-assertion hook profile."""
    return _entry(HOOK, HOOK_A1, ac="AC1") + _entry(
        HOOK, HOOK_A2, disposition="not_applicable", reason="対象の振る舞いが今回の変更面に存在しない"
    )


def _assert_no_runtime_evidence_field(result: dict) -> None:
    assert not (set(result) & _FORBIDDEN_EVIDENCE_KEYS), sorted(set(result) & _FORBIDDEN_EVIDENCE_KEYS)
    for view in result["classified_bindings"]:
        assert not (set(view) & _FORBIDDEN_EVIDENCE_KEYS), view
    for key in ("declared_bindings",):
        for view in result[key]:
            assert not (set(view) & _FORBIDDEN_EVIDENCE_KEYS), view
    assert result["structural_completeness_only"] is True


# --- AC1: legacy vs explicit dispositive ---------------------------------


def test_legacy_three_field_binding_is_dispositive_with_legacy_default_source():
    """GIVEN legacy 3-field bindings (disposition omitted) for a 2-assertion profile
    WHEN coverage is evaluated
    THEN it approves and both are dispositive with disposition_source=legacy_default."""
    bindings = _entry(HOOK, HOOK_A1, ac="AC1") + _entry(HOOK, HOOK_A2, ac="AC2")
    result = _coverage(HOOK_PATHS, bindings, tagged=(1, 2), applicable=(1, 2))
    assert result["verdict"] == "approve", result["reasons"]
    assert [(b["disposition"], b["disposition_source"]) for b in result["classified_bindings"]] == [
        ("dispositive", "legacy_default"),
        ("dispositive", "legacy_default"),
    ]
    _assert_no_runtime_evidence_field(result)


def test_explicit_dispositive_is_distinguished_from_legacy_default():
    """GIVEN one legacy binding and one `disposition: dispositive` binding
    WHEN coverage is evaluated
    THEN both approve; the explicit one is disposition_source=explicit."""
    bindings = _entry(HOOK, HOOK_A1, ac="AC1") + _entry(
        HOOK, HOOK_A2, ac="AC2", disposition="dispositive"
    )
    result = _coverage(HOOK_PATHS, bindings, tagged=(1, 2), applicable=(1, 2))
    assert result["verdict"] == "approve", result["reasons"]
    sources = {b["assertion"]: b["disposition_source"] for b in result["classified_bindings"]}
    assert sources == {HOOK_A1: "legacy_default", HOOK_A2: "explicit"}
    assert {b["disposition"] for b in result["classified_bindings"]} == {"dispositive"}
    assert result["dispositive_assertions"] == [f"{HOOK}/{HOOK_A1}", f"{HOOK}/{HOOK_A2}"]
    _assert_no_runtime_evidence_field(result)


def test_legacy_valid_fixture_parse_shape_is_unchanged():
    """GIVEN a legacy 3-field entry
    WHEN parsed
    THEN the parsed dict keeps exactly profile/assertion/ac (the #2771 shape)."""
    bindings, malformed = matcher.parse_runtime_assertion_bindings(_rva(_entry(HOOK, HOOK_A1, ac="AC1")))
    assert malformed == []
    assert bindings == [{"profile": HOOK, "assertion": HOOK_A1, "ac": "1"}]


# --- AC2: not_applicable ---------------------------------------------------


def test_not_applicable_declares_required_key_without_fictional_references():
    """GIVEN a not_applicable binding with a non-empty reason, no ac, no demonstrated_by
    WHEN coverage is evaluated
    THEN the required key is declared (approve) and nothing fictional is required."""
    result = _coverage(HOOK_PATHS, _hook_valid_mixed())
    assert result["verdict"] == "approve", result["reasons"]
    assert result["not_applicable_assertions"] == [f"{HOOK}/{HOOK_A2}"]
    na_view = next(b for b in result["classified_bindings"] if b["assertion"] == HOOK_A2)
    assert na_view["disposition"] == "not_applicable"
    assert na_view["disposition_source"] == "explicit"
    assert "ac" not in na_view and "demonstrated_by" not in na_view
    assert na_view["reason"]
    assert result["missing"] == [] and result["invalid_ac_bindings"] == []
    _assert_no_runtime_evidence_field(result)


@pytest.mark.parametrize(
    "extra, expected_fragment",
    [
        ("", "missing a non-empty 'reason'"),
        ("    reason: ''\n", "empty 'reason'"),
        ("    reason: '   '\n", "empty 'reason'"),
        ("    reason: 12\n", "non-string 'reason'"),
        ("    reason: [a, b]\n", "non-string 'reason'"),
        ("    reason: r\n    ac: AC1\n", "unexpected key(s) ['ac']"),
        ("    reason: r\n    demonstrated_by: AC2\n", "unexpected key(s) ['demonstrated_by']"),
    ],
)
def test_not_applicable_malformed_forms_need_fix_with_distinct_reason(extra, expected_fragment):
    """GIVEN a not_applicable binding with a missing / empty / non-string reason, or an ac /
    demonstrated_by attached
    WHEN coverage is evaluated
    THEN it needs_fix and the reason distinguishes the cause."""
    bindings = _entry(HOOK, HOOK_A1, ac="AC1") + (
        f"  - profile: {HOOK}\n    assertion: {HOOK_A2}\n    disposition: not_applicable\n{extra}"
    )
    result = _coverage(HOOK_PATHS, bindings)
    assert result["verdict"] == "needs_fix"
    joined = " | ".join(result["reasons"])
    assert expected_fragment in joined, joined
    # The malformed not_applicable entry never counts as declared.
    assert result["not_applicable_assertions"] == []
    assert f"{HOOK}/{HOOK_A2}" in result["missing"]


# --- AC3: non_dispositive_readiness_compat ---------------------------------


def _compat_bindings(**overrides: str) -> str:
    fields = {
        "disposition": "non_dispositive_readiness_compat",
        "demonstrated_by": "AC2",
        "reason": "fault-injection AC2 が実証を所有する",
    }
    fields.update(overrides)
    fields = {k: v for k, v in fields.items() if v is not None}
    return _entry(HOOK, HOOK_A1, ac="AC1") + _entry(HOOK, HOOK_A2, **fields)


def test_compat_references_existing_ac_without_requiring_runtime_tag_or_applicable_acs():
    """GIVEN a compat binding whose demonstrated_by AC2 exists and has a canonical VC ref,
    but AC2 is neither in applicable_acs nor runtime-tagged
    WHEN coverage is evaluated
    THEN it approves and is classified separately (not dispositive)."""
    result = _coverage(HOOK_PATHS, _compat_bindings(), tagged=(1,), applicable=(1,))
    assert result["verdict"] == "approve", result["reasons"]
    view = next(b for b in result["classified_bindings"] if b["assertion"] == HOOK_A2)
    assert view["disposition"] == "non_dispositive_readiness_compat"
    assert view["demonstrated_by"] == "AC2"
    assert view["reason"]
    assert "ac" not in view
    assert result["non_dispositive_readiness_compat_assertions"] == [f"{HOOK}/{HOOK_A2}"]
    assert result["dispositive_assertions"] == [f"{HOOK}/{HOOK_A1}"]
    _assert_no_runtime_evidence_field(result)


@pytest.mark.parametrize(
    "overrides, expected_fragment",
    [
        ({"demonstrated_by": None}, "missing 'demonstrated_by'"),
        ({"demonstrated_by": "''"}, "empty or non-string 'demonstrated_by'"),
        ({"demonstrated_by": "tests/test_x.py::test_y"}, "test path / node-id references are not accepted"),
        ({"demonstrated_by": "test_x.py"}, "expected 'AC<N>' form"),
        ({"demonstrated_by": "AC99"}, "demonstrated_by_ac_not_found"),
        ({"reason": None}, "missing a non-empty 'reason'"),
        ({"reason": "''"}, "empty 'reason'"),
        ({"ac": "AC1"}, "unexpected key(s) ['ac']"),
    ],
)
def test_compat_malformed_or_invalid_forms_need_fix_with_distinct_reason(overrides, expected_fragment):
    """GIVEN a compat binding that violates one of the contract rules
    WHEN coverage is evaluated
    THEN it needs_fix and the cause is reported distinctly."""
    result = _coverage(HOOK_PATHS, _compat_bindings(**overrides))
    assert result["verdict"] == "needs_fix"
    joined = " | ".join(result["reasons"])
    assert expected_fragment in joined, joined


def test_compat_demonstrated_by_without_canonical_vc_reference_is_invalid():
    """GIVEN demonstrated_by AC2 exists in Acceptance Criteria but has no `# AC2` VC reference
    WHEN coverage is evaluated
    THEN it needs_fix with demonstrated_by_ac_missing_vc_reference (and not _not_found)."""
    result = _coverage(HOOK_PATHS, _compat_bindings(), vc_refs={"1"})
    assert result["verdict"] == "needs_fix"
    assert result["invalid_demonstrated_by_bindings"] == [
        {
            "profile": HOOK,
            "assertion": HOOK_A2,
            "demonstrated_by": "AC2",
            "reasons": ["demonstrated_by_ac_missing_vc_reference"],
        }
    ]


# --- AC4: closed enum / closed key sets / duplicate / unknown -------------


@pytest.mark.parametrize("value", ["bogus", "Dispositive", "NOT_APPLICABLE", "''", "null", "12"])
def test_disposition_outside_enum_is_malformed(value):
    """GIVEN a disposition outside the closed enum
    WHEN coverage is evaluated
    THEN it needs_fix (malformed) and the entry never counts as declared."""
    bindings = _entry(HOOK, HOOK_A1, ac="AC1") + _entry(HOOK, HOOK_A2, ac="AC2", disposition=value)
    result = _coverage(HOOK_PATHS, bindings, tagged=(1, 2), applicable=(1, 2))
    assert result["verdict"] == "needs_fix"
    assert any("expected one of" in r for r in result["reasons"]), result["reasons"]
    assert f"{HOOK}/{HOOK_A2}" in result["missing"]


@pytest.mark.parametrize(
    "fields",
    [
        {"disposition": "dispositive", "ac": "AC2", "reason": "x"},
        {"disposition": "dispositive", "ac": "AC2", "demonstrated_by": "AC1"},
        {"ac": "AC2", "reason": "legacy form must stay 3-field"},
        {"disposition": "non_dispositive_readiness_compat", "demonstrated_by": "AC2", "reason": "x", "extra": "1"},
        {"disposition": "not_applicable", "reason": "x", "extra": "1"},
    ],
)
def test_keys_outside_the_disposition_closed_set_are_malformed(fields):
    """GIVEN an entry carrying a key outside its disposition's closed key set
    WHEN coverage is evaluated
    THEN it needs_fix with an unexpected-key reason."""
    bindings = _entry(HOOK, HOOK_A1, ac="AC1") + _entry(HOOK, HOOK_A2, **fields)
    result = _coverage(HOOK_PATHS, bindings, tagged=(1, 2), applicable=(1, 2))
    assert result["verdict"] == "needs_fix"
    assert any("unexpected key(s)" in r for r in result["reasons"]), result["reasons"]


@pytest.mark.parametrize(
    "first, second",
    [
        ({"ac": "AC1"}, {"disposition": "not_applicable", "reason": "r"}),
        ({"disposition": "not_applicable", "reason": "r"}, {"disposition": "not_applicable", "reason": "r2"}),
        (
            {"disposition": "non_dispositive_readiness_compat", "demonstrated_by": "AC2", "reason": "r"},
            {"ac": "AC1", "disposition": "dispositive"},
        ),
    ],
)
def test_duplicate_key_is_reported_regardless_of_disposition_combination(first, second):
    """GIVEN the same (profile, assertion) declared twice with any disposition mix
    WHEN coverage is evaluated
    THEN it needs_fix and the key is reported as duplicate."""
    bindings = (
        _entry(HOOK, HOOK_A1, **first)
        + _entry(HOOK, HOOK_A1, **second)
        + _entry(HOOK, HOOK_A2, ac="AC1")
    )
    result = _coverage(HOOK_PATHS, bindings)
    assert result["verdict"] == "needs_fix"
    assert result["duplicate"] == [f"{HOOK}/{HOOK_A1}"]


def test_not_applicable_for_a_key_outside_the_required_set_is_unknown():
    """GIVEN a not_applicable binding for a (profile, assertion) the policy does not require
    WHEN coverage is evaluated
    THEN it is reported unknown (it cannot invent applicability for a non-candidate)."""
    bindings = _hook_valid_mixed() + _entry(
        "claude-gpt-process-io-smoke",
        "external_process_exit_code_and_stdio_observed",
        disposition="not_applicable",
        reason="not a candidate here",
    )
    result = _coverage(HOOK_PATHS, bindings)
    assert result["verdict"] == "needs_fix"
    assert result["unknown"] == ["claude-gpt-process-io-smoke/external_process_exit_code_and_stdio_observed"]


# --- AC5: #2860-shaped fixture ---------------------------------------------


def _scratch_bindings(*, drop_skill_a1: bool = False) -> str:
    text = ""
    if not drop_skill_a1:
        text += _entry(SKILL, SKILL_A1, ac="AC1", disposition="dispositive")
    text += _entry(SKILL, SKILL_A2, ac="AC2", disposition="dispositive")
    text += _entry(
        SUBAGENT,
        SUBAGENT_A1,
        disposition="not_applicable",
        reason="scratch から SubAgent への handoff が production path に存在しない",
    )
    return text


def test_scratch_change_fixture_approves_without_dummy_ac_or_artificial_evidence():
    """GIVEN a skill change whose SubAgent causal-evidence profile has no behaviour to verify
    (not_applicable + reason) while the skill-invocation profile stays dispositive
    WHEN coverage is evaluated
    THEN it approves with no dummy AC, SubAgent, runtime log or marker demanded."""
    result = _coverage(SKILL_PATHS, _scratch_bindings(), tagged=(1, 2), applicable=(1, 2))
    assert result["verdict"] == "approve", result["reasons"]
    assert result["required_assertions"] == sorted(
        [f"{SKILL}/{SKILL_A1}", f"{SKILL}/{SKILL_A2}", f"{SUBAGENT}/{SUBAGENT_A1}"]
    )
    assert result["not_applicable_assertions"] == [f"{SUBAGENT}/{SUBAGENT_A1}"]
    assert result["dispositive_assertions"] == [f"{SKILL}/{SKILL_A1}", f"{SKILL}/{SKILL_A2}"]
    assert result["missing"] == [] and result["unknown"] == [] and result["duplicate"] == []
    assert result["invalid_ac_bindings"] == [] and result["invalid_demonstrated_by_bindings"] == []


def test_scratch_change_fixture_still_reports_a_removed_applicable_assertion_as_missing():
    """GIVEN the same fixture with one applicable (dispositive) binding removed
    WHEN coverage is evaluated
    THEN that assertion is missing -- the applicable verification does not disappear."""
    result = _coverage(SKILL_PATHS, _scratch_bindings(drop_skill_a1=True), tagged=(1, 2), applicable=(1, 2))
    assert result["verdict"] == "needs_fix"
    assert result["missing"] == [f"{SKILL}/{SKILL_A1}"]
    assert result["not_applicable_assertions"] == [f"{SUBAGENT}/{SUBAGENT_A1}"]


# --- AC6: #2827-shaped fixture ---------------------------------------------


def test_fault_injection_ac_owning_evidence_is_compat_and_never_counted_as_runtime_pass():
    """GIVEN the runtime AC (AC1) verifies assertion 1 while a separate fault-injection AC (AC2)
    owns assertion 2's substantive evidence
    WHEN coverage is evaluated
    THEN assertion 2 is classified compat with demonstrated_by kept, apart from dispositive."""
    result = _coverage(HOOK_PATHS, _compat_bindings())
    assert result["verdict"] == "approve", result["reasons"]
    by_assertion = {b["assertion"]: b for b in result["classified_bindings"]}
    assert by_assertion[HOOK_A1]["disposition"] == "dispositive"
    assert by_assertion[HOOK_A1]["disposition_source"] == "legacy_default"
    assert by_assertion[HOOK_A2]["disposition"] == "non_dispositive_readiness_compat"
    assert by_assertion[HOOK_A2]["demonstrated_by"] == "AC2"
    assert HOOK_A2 not in " ".join(result["dispositive_assertions"])
    _assert_no_runtime_evidence_field(result)


def test_legacy_ac_bearing_data_gets_no_demonstrated_meaning():
    """GIVEN legacy `ac`-bearing data
    WHEN coverage is evaluated
    THEN it is only dispositive/legacy_default -- no demonstrated_by / reason / evidence is invented."""
    bindings = _entry(HOOK, HOOK_A1, ac="AC1") + _entry(HOOK, HOOK_A2, ac="AC2")
    result = _coverage(HOOK_PATHS, bindings, tagged=(1, 2), applicable=(1, 2))
    for view in result["classified_bindings"]:
        assert view["disposition_source"] == "legacy_default"
        assert "demonstrated_by" not in view and "reason" not in view
    assert result["non_dispositive_readiness_compat_assertions"] == []
    assert result["not_applicable_assertions"] == []


# --- AC7: mixed / unverified -----------------------------------------------


def test_not_applicable_does_not_hide_a_missing_required_assertion():
    """GIVEN a not_applicable for one assertion and NO binding for another required assertion
    WHEN coverage is evaluated
    THEN the unbound one is missing -- never not_applicable / PASS."""
    bindings = _entry(
        SUBAGENT, SUBAGENT_A1, disposition="not_applicable", reason="handoff が存在しない"
    ) + _entry(SKILL, SKILL_A1, ac="AC1")
    result = _coverage(SKILL_PATHS, bindings)
    assert result["verdict"] == "needs_fix"
    assert result["missing"] == [f"{SKILL}/{SKILL_A2}"]
    declared = {c["profile"] + "/" + c["assertion"] for c in result["classified_bindings"]}
    assert f"{SKILL}/{SKILL_A2}" not in declared
    assert f"{SKILL}/{SKILL_A2}" not in result["not_applicable_assertions"]
    assert f"{SKILL}/{SKILL_A2}" not in result["dispositive_assertions"]


def test_not_applicable_does_not_hide_invalid_ac_or_duplicate():
    """GIVEN a mixed input with not_applicable plus a dispositive binding to a nonexistent AC
    and a duplicated key
    WHEN coverage is evaluated
    THEN invalid_ac and duplicate are both still reported."""
    bindings = (
        _entry(SUBAGENT, SUBAGENT_A1, disposition="not_applicable", reason="handoff が存在しない")
        + _entry(SKILL, SKILL_A1, ac="AC99")
        + _entry(SKILL, SKILL_A2, ac="AC1")
        + _entry(SKILL, SKILL_A2, ac="AC1")
    )
    result = _coverage(SKILL_PATHS, bindings)
    assert result["verdict"] == "needs_fix"
    assert result["duplicate"] == [f"{SKILL}/{SKILL_A2}"]
    assert [e["assertion"] for e in result["invalid_ac_bindings"]] == [SKILL_A1]
    assert "ac_not_found" in result["invalid_ac_bindings"][0]["reasons"]
    assert result["missing"] == []


# --- AC8: evaluate_issue_risk_trigger is unchanged -------------------------


def _risk(allowed: list[str], bindings: str, decision: str, applicable=(1,)) -> dict:
    return matcher.evaluate_issue_risk_trigger(
        allowed_path_entries=allowed,
        declared_decision=decision,
        rva_section_text=_rva(bindings, decision=decision, applicable=applicable),
    )


def test_risk_trigger_verdict_is_independent_of_not_applicable_declarations():
    """GIVEN identical Allowed Paths / decision with and without a not_applicable binding
    WHEN the Issue-level risk trigger is evaluated
    THEN the whole result is identical (assertion-level declarations never reach it)."""
    without = _risk(SKILL_PATHS, _entry(SKILL, SKILL_A1, ac="AC1"), "immediate")
    with_na = _risk(SKILL_PATHS, _scratch_bindings(), "immediate")
    assert without == with_na
    assert with_na["verdict"] == "approve"


def test_scratch_fixture_passes_both_risk_trigger_and_binding_coverage():
    """GIVEN the #2860-shaped fixture with decision: immediate
    WHEN both the risk trigger and the binding coverage evaluate
    THEN both approve."""
    risk = _risk(SKILL_PATHS, _scratch_bindings(), "immediate", applicable=(1, 2))
    coverage = _coverage(SKILL_PATHS, _scratch_bindings(), tagged=(1, 2), applicable=(1, 2))
    assert risk["verdict"] == "approve"
    assert coverage["verdict"] == "approve"


def test_decision_downgrade_with_applicable_assertions_remaining_still_needs_fix():
    """GIVEN an applicable assertion remains and the decision is lowered to not_applicable
    WHEN the risk trigger evaluates
    THEN it still needs_fix, as before this Issue (a not_applicable binding grants no relief)."""
    risk = _risk(SKILL_PATHS, _scratch_bindings(), "not_applicable", applicable=(1, 2))
    assert risk["verdict"] == "needs_fix"
    assert any("most-restrictive default_decision" in r for r in risk["reasons"])


def test_boundary_all_hard_required_assertions_not_applicable_keeps_policy_decision_requirement():
    """GIVEN every hard-required assertion is not_applicable or compat and decision: not_applicable
    WHEN risk trigger and binding coverage both evaluate
    THEN the risk trigger keeps its policy-derived requirement (needs_fix, unchanged) while the
    binding coverage approves. Conservative, intended limitation (policy rule design is #2775)."""
    bindings = _entry(
        HOOK, HOOK_A1, disposition="not_applicable", reason="対象の振る舞いが存在しない"
    ) + _entry(
        HOOK,
        HOOK_A2,
        disposition="non_dispositive_readiness_compat",
        demonstrated_by="AC2",
        reason="AC2 が実証を所有する",
    )
    risk = _risk(HOOK_PATHS, bindings, "not_applicable")
    assert risk["verdict"] == "needs_fix"
    assert risk == _risk(HOOK_PATHS, "", "not_applicable")  # same as with no bindings at all
    coverage = _coverage(HOOK_PATHS, bindings, tagged=(), applicable=(), decision="not_applicable")
    assert coverage["verdict"] == "approve", coverage["reasons"]
    assert coverage["dispositive_assertions"] == []


# --- carrier formatter (AC9/AC10 support) ----------------------------------


def _parse_carrier_line(line: str) -> dict:
    """Test-local inverse of the carrier line (fixed-order tokens, reason last)."""
    import re

    match = re.match(
        r"^(?P<profile>[^/]+)/(?P<assertion>.+?): disposition=(?P<disposition>\w+); "
        r"source=(?P<source>\w+); demonstrated_by=(?P<demonstrated_by>AC\d+|-); "
        r"reason=(?P<reason>.*)$",
        line,
    )
    assert match is not None, line
    return match.groupdict()


def test_carrier_lines_one_per_binding_with_exact_shape():
    """GIVEN a mixed approve result
    WHEN the carrier is formatted
    THEN there is exactly one single-line string per binding in the fixed grammar."""
    result = _coverage(HOOK_PATHS, _compat_bindings())
    lines = matcher.format_runtime_assertion_disposition_carrier_lines(result)
    assert lines == [
        f"{HOOK}/{HOOK_A1}: disposition=dispositive; source=legacy_default; demonstrated_by=-; reason=-",
        f"{HOOK}/{HOOK_A2}: disposition=non_dispositive_readiness_compat; source=explicit; "
        "demonstrated_by=AC2; reason=fault-injection AC2 が実証を所有する",
    ]


def test_carrier_is_empty_for_pure_legacy_and_for_needs_fix():
    """GIVEN a pure legacy approve result, and a needs_fix result with not_applicable
    WHEN the carrier is formatted
    THEN both yield no carrier (existing approve output unchanged; carrier never decorates a failure)."""
    legacy = _coverage(
        HOOK_PATHS,
        _entry(HOOK, HOOK_A1, ac="AC1") + _entry(HOOK, HOOK_A2, ac="AC2"),
        tagged=(1, 2),
        applicable=(1, 2),
    )
    assert legacy["verdict"] == "approve"
    assert matcher.format_runtime_assertion_disposition_carrier_lines(legacy) == []

    failing = _coverage(HOOK_PATHS, _entry(HOOK, HOOK_A2, disposition="not_applicable", reason="r"))
    assert failing["verdict"] == "needs_fix"
    assert matcher.format_runtime_assertion_disposition_carrier_lines(failing) == []


def test_carrier_is_emitted_for_a_single_explicit_dispositive_binding():
    """GIVEN legacy bindings plus one explicit `disposition: dispositive`
    WHEN the carrier is formatted
    THEN it is emitted (explicit disposition counts) for ALL bindings."""
    bindings = _entry(HOOK, HOOK_A1, ac="AC1") + _entry(HOOK, HOOK_A2, ac="AC2", disposition="dispositive")
    result = _coverage(HOOK_PATHS, bindings, tagged=(1, 2), applicable=(1, 2))
    lines = matcher.format_runtime_assertion_disposition_carrier_lines(result)
    assert len(lines) == 2
    assert [_parse_carrier_line(line)["source"] for line in lines] == ["legacy_default", "explicit"]


@pytest.mark.parametrize(
    "reason, expected_reason",
    [
        ("単純な理由", "単純な理由"),
        ("a; b; c", "a; b; c"),
        ("first; reason=second; demonstrated_by=AC9", "first; reason=second; demonstrated_by=AC9"),
        ('"line1\\nline2\\n\\n  line3"', "line1 line2 line3"),
    ],
)
def test_carrier_reason_round_trips_through_normalization(reason, expected_reason):
    """GIVEN reasons containing `;`, look-alike field tokens, or newlines
    WHEN formatted and parsed back with the fixed-order grammar
    THEN every binding stays exactly one physical line and the other fields are unambiguous."""
    bindings = _entry(HOOK, HOOK_A1, ac="AC1") + (
        f"  - profile: {HOOK}\n    assertion: {HOOK_A2}\n    disposition: not_applicable\n"
        f"    reason: {reason if reason.startswith(chr(34)) else chr(34) + reason + chr(34)}\n"
    )
    result = _coverage(HOOK_PATHS, bindings)
    assert result["verdict"] == "approve", result["reasons"]
    lines = matcher.format_runtime_assertion_disposition_carrier_lines(result)
    assert len(lines) == 2 and all("\n" not in line for line in lines)
    parsed = _parse_carrier_line(lines[1])
    assert parsed["disposition"] == "not_applicable"
    assert parsed["source"] == "explicit"
    assert parsed["demonstrated_by"] == "-"
    assert parsed["assertion"] == HOOK_A2
    assert parsed["reason"] == expected_reason
