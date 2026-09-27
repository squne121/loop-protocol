#!/usr/bin/env python3
"""Regression tests for Issue #2788: unify `baseline_vc_preflight.py`'s
normal execution, `compute_canonical_vc_plan()`, and
`_distinct_command_texts_from_body()` onto the SAME canonical VC command
grammar authority `--static-only` already used
(`vc_contract_syntax.parse_verification_commands_section()` /
`VcParseResult.commands`), so an explanatory non-`$` line inside a
```bash fence is never promoted to a runnable subprocess candidate in ANY
of the three execution paths.

Covers AC1-AC4, AC6-AC7, AC9-AC13 of Issue #2788. AC5 and AC8 are covered
by the EXISTING `test_baseline_vc_preflight.py` / `.claude/skills/issue-contract-review/tests/`
suites, which this Issue keeps passing (see PR description).
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent
PREFLIGHT_SCRIPT = _SCRIPTS_DIR / "baseline_vc_preflight.py"

sys.path.insert(0, str(_SCRIPTS_DIR))

import baseline_vc_preflight as bvp  # noqa: E402
import vc_contract_syntax as vcs  # noqa: E402


def _write_body(body: str) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body)
        return f.name


def run_normal(body: str, issue_num: int = 999) -> dict:
    """Run `baseline_vc_preflight.py` in normal (execute) mode via CLI."""
    fixture_file = _write_body(body)
    try:
        result = subprocess.run(
            [sys.executable, str(PREFLIGHT_SCRIPT), "--body-file", fixture_file,
             "--issue", str(issue_num), "--strict"],
            capture_output=True, text=True, timeout=60,
        )
        assert result.stdout, f"No stdout: stderr={result.stderr}"
        return json.loads(result.stdout)
    finally:
        Path(fixture_file).unlink(missing_ok=True)


def run_static_only(body: str, issue_num: int = 999) -> dict:
    """Run `baseline_vc_preflight.py --static-only` via CLI."""
    fixture_file = _write_body(body)
    try:
        result = subprocess.run(
            [sys.executable, str(PREFLIGHT_SCRIPT), "--body-file", fixture_file,
             "--issue", str(issue_num), "--static-only"],
            capture_output=True, text=True, timeout=60,
        )
        assert result.stdout, f"No stdout: stderr={result.stderr}"
        return json.loads(result.stdout)
    finally:
        Path(fixture_file).unlink(missing_ok=True)


EXPLANATORY_NON_DOLLAR_BODY = """## Verification Commands

```bash
# AC1
This line explains what the command below does.
$ echo canonical_command
```
"""

CANONICAL_BODY = """## Verification Commands

```bash
# AC1
$ echo canonical_command
```
"""


# ---------------------------------------------------------------------------
# AC1/AC2/AC7: single shared adapter authority, no third parser
# ---------------------------------------------------------------------------


def test_shared_adapter_is_the_single_conversion_point():
    """AC1/AC2/AC7: `_command_entries_from_shared_parser()` is sourced from
    `vc_contract_syntax.parse_verification_commands_section()` -- the SAME
    authority `--static-only` uses -- and is the only place that converts
    `VcCommandEntry` objects into the legacy 9-tuple shape. No independent
    VC grammar parser is introduced (this module's OWN `parse_commands_from_block()`
    remains, but is exercised ONLY by the diagnostic-only
    `compute_duplicate_diagnostic_report()` -- Issue #2788 Out of Scope)."""
    section = "```bash\n# AC1\n$ echo hi\n```\n"
    tuples, parse_result = bvp._command_entries_from_shared_parser(section)
    assert isinstance(parse_result, vcs.VcParseResult)
    assert len(tuples) == 1
    assert tuples[0][1] == "echo hi"


def test_compute_canonical_vc_plan_uses_shared_adapter_command_source():
    """AC2: `compute_canonical_vc_plan()`'s command occurrence count matches
    the shared adapter's own command count for the SAME body (no
    independent legacy-grammar re-derivation)."""
    body = CANONICAL_BODY
    section = bvp.extract_verification_commands_section(body) or ""
    tuples, _ = bvp._command_entries_from_shared_parser(section)
    plan = bvp.compute_canonical_vc_plan(body)
    assert plan["command_occurrence_count"] == len(tuples) == 1


# ---------------------------------------------------------------------------
# AC3/AC6: explanatory non-$ line rejected consistently in all 3 paths
# ---------------------------------------------------------------------------


def test_explanatory_non_dollar_line_rejected_by_static_only():
    data = run_static_only(EXPLANATORY_NON_DOLLAR_BODY)
    assert data["status"] == "blocked"
    categories = {r["category"] for r in data["results"]}
    assert "non_dollar_command" in categories


def test_explanatory_non_dollar_line_rejected_by_normal_execution():
    """AC1/AC3: normal execution must reject the SAME body BEFORE any
    subprocess candidate is built -- not silently execute only the valid
    `$ echo canonical_command` while ignoring the malformed line."""
    data = run_normal(EXPLANATORY_NON_DOLLAR_BODY)
    assert data["status"] == "blocked"
    assert data["results"] == []
    assert any(e["kind"] == "extraction_error" for e in data["errors"])
    # AC13: no unhandled Python exception/traceback -- exit code is a clean,
    # deterministic classification, and stdout is well-formed JSON (already
    # implied by json.loads succeeding in run_normal()).


def test_explanatory_non_dollar_line_excluded_from_canonical_plan():
    """AC2/AC3/AC13 (fix_delta P1-B): `compute_canonical_vc_plan()` must
    converge on the SAME whole-body rejection normal execution applies to
    `EXPLANATORY_NON_DOLLAR_BODY` -- which contains BOTH the explanatory
    non-`$` line AND an otherwise well-formed `$ echo canonical_command`
    line. Normal execution rejects the WHOLE body (zero subprocess
    candidates, `results: []`) the instant any `non_dollar_command` error
    is present, so the canonical plan must ALSO produce ZERO command
    occurrences here -- not one occurrence for the still-well-formed `$`
    line -- otherwise the plan's occurrence/budget candidate set would
    drift from what normal execution actually runs, without raising any
    new exception type that would propagate to
    `contract_readiness_check.py` / `run_contract_review_once.py` /
    `run_root_review_pipeline.py` (outside this Issue's Allowed Paths)."""
    plan = bvp.compute_canonical_vc_plan(EXPLANATORY_NON_DOLLAR_BODY)
    assert plan["command_occurrence_count"] == 0
    assert plan["command_occurrences"] == []
    assert plan["command_budgets"] == []
    # No exception raised getting here -- this assertion IS the AC13 check.


def test_explanatory_non_dollar_line_excluded_from_distinct_command_texts():
    """AC12 (fix_delta P1-B): `_distinct_command_texts_from_body()` returns
    the SAME command population `compute_canonical_vc_plan()` / normal
    execution use. `EXPLANATORY_NON_DOLLAR_BODY` triggers a whole-body
    `non_dollar_command` rejection in BOTH of those -- zero commands, not
    just the explanatory line silently dropped while the otherwise
    well-formed `$ echo canonical_command` line is kept -- so this
    function must ALSO return an empty list for the SAME body, to keep its
    own AC12 "same population" claim true."""
    texts = bvp._distinct_command_texts_from_body(EXPLANATORY_NON_DOLLAR_BODY)
    assert texts == []


# ---------------------------------------------------------------------------
# AC4: canonical `$ command` fixture behaves identically across paths
# ---------------------------------------------------------------------------


def test_canonical_body_extracted_and_executed_consistently():
    static_data = run_static_only(CANONICAL_BODY)
    assert static_data["status"] == "ok"

    normal_data = run_normal(CANONICAL_BODY)
    assert len(normal_data["results"]) == 1
    assert normal_data["results"][0]["raw_command"] == "echo canonical_command"

    plan = bvp.compute_canonical_vc_plan(CANONICAL_BODY)
    assert plan["command_occurrence_count"] == 1


# ---------------------------------------------------------------------------
# AC9: quote-aware compound-shell detection unification
# ---------------------------------------------------------------------------


def test_quoted_regex_alternation_not_compound_shell_in_shared_parser():
    """AC9: `rg -n "foo|bar" PATH` (quoted regex alternation) must NOT be
    misclassified as `compound_shell` by `vc_contract_syntax.py`'s static
    parser (it delegates to the shlex-based quote-aware
    `detect_compound_command()`, not an independent regex)."""
    section = '```bash\n$ rg -n "foo|bar" some/path.py\n```\n'
    result = vcs.parse_verification_commands_section(section)
    assert not any(e.kind == "compound_shell" for e in result.errors)


def test_unquoted_pipe_is_still_compound_shell_in_shared_parser():
    """AC9 non-regression: an UNQUOTED `|` must still be detected as
    `compound_shell` by the shared parser."""
    section = "```bash\n$ echo a | grep b\n```\n"
    result = vcs.parse_verification_commands_section(section)
    assert any(e.kind == "compound_shell" for e in result.errors)


def test_detect_compound_command_is_the_single_shared_primitive():
    """AC9: `vc_contract_syntax.detect_compound_command` and
    `baseline_vc_preflight.detect_compound_command` (re-exported via import)
    are the SAME function object -- no second/third independent compound
    detector exists."""
    assert bvp.detect_compound_command is vcs.detect_compound_command


# ---------------------------------------------------------------------------
# fix_delta P1-A: punctuation-run false negatives in detect_compound_command()
# ---------------------------------------------------------------------------

_PUNCTUATION_RUN_COMPOUND_CASES = [
    pytest.param("echo hi 2>&1", id="stderr-to-stdout-2>&1"),
    pytest.param("echo hi &>out", id="redirect-both-streams-&>"),
    pytest.param("echo hi |& tee x", id="pipe-both-streams-|&"),
    pytest.param("echo hi >| out", id="clobber-redirect->|"),
    pytest.param("echo hi <> file", id="read-write-redirect-<>"),
]


@pytest.mark.parametrize("command", _PUNCTUATION_RUN_COMPOUND_CASES)
def test_punctuation_run_redirect_operators_detected_as_compound(command):
    """fix_delta P1-A: Bash-legal multi-character punctuation-run shell
    operators (`>&`, `&>`, `|&`, `>|`, `<>`) must be detected as compound,
    even though NONE of them exact-match the old finite operator set
    `{"&&", "||", "|", ";", "&", "<<", "<", ">", ">>", "<<<"}` --
    `shlex.shlex(..., punctuation_chars=True)` merges a contiguous run of
    punctuation characters into ONE multi-character token (e.g. `2>&1`
    tokenizes to `["2", ">&", "1"]`), so exact-matching against a finite
    set of known operator strings missed these."""
    assert vcs.detect_compound_command(command) is True

    section = f"```bash\n$ {command}\n```\n"
    result = vcs.parse_verification_commands_section(section)
    assert any(e.kind == "compound_shell" for e in result.errors), (
        f"expected compound_shell error for {command!r}, got errors={result.errors!r}"
    )


def test_quoted_regex_alternation_still_not_compound_after_punctuation_run_fix():
    """fix_delta P1-A non-regression: the #589 quoted-regex-alternation
    negative control (`rg -n "foo|bar" PATH`) must remain non-compound
    after generalizing punctuation-run detection -- the quoted token
    `foo|bar` mixes word characters with `|`, so it is never an
    all-operator-characters token."""
    assert vcs.detect_compound_command('rg -n "foo|bar" some/path.py') is False


# ---------------------------------------------------------------------------
# AC5/AC10: vc-regex-intent losslessness through the shared adapter
# ---------------------------------------------------------------------------


REGEX_NO_ANNOTATION_BODY = """## Verification Commands

```bash
# AC1
$ rg "foo\\\\|bar" some/path.py
```
"""

REGEX_WITH_ANNOTATION_BODY = """## Verification Commands

```bash
# AC1
# vc-regex-intent: literal-pipe-ok
$ rg "foo\\\\|bar" some/path.py
```
"""


def test_regex_literal_pipe_suspected_without_annotation():
    data = run_normal(REGEX_NO_ANNOTATION_BODY)
    assert data["results"][0]["category"] == "regex_literal_pipe_suspected"
    assert data["results"][0]["decision"] == "blocked"


def test_regex_literal_pipe_ok_annotation_permits_execution_after_migration():
    """AC5/AC10: with `# vc-regex-intent: literal-pipe-ok` immediately
    preceding the command, normal execution (via the shared adapter) must
    execute the command rather than blocking it as
    `regex_literal_pipe_suspected`."""
    data = run_normal(REGEX_WITH_ANNOTATION_BODY)
    assert data["results"][0]["category"] != "regex_literal_pipe_suspected"
    assert data["results"][0]["exit_code"] is not None


def test_annotation_source_line_and_raw_preserved_through_shared_parser():
    """AC5: `annotation_source.line` / `annotation_source.raw` (#889 AC11)
    must be preserved losslessly through the shared parser's
    `VcCommandEntry`, not merely parsed-and-discarded."""
    body = """## Verification Commands

```bash
# AC1
# baseline-expect: pass
$ true
```
"""
    data = run_normal(body)
    r = data["results"][0]
    assert r["annotations"]["baseline_expect"] == "pass"
    assert r["annotation_source"]["line"] is not None
    assert "baseline-expect: pass" in r["annotation_source"]["raw"]


# ---------------------------------------------------------------------------
# AC11: grouped AC -> one occurrence / one subprocess launch, deterministic
# ---------------------------------------------------------------------------


GROUPED_AC_BODY = """## Verification Commands

```bash
# AC2, AC3
# baseline-expect: pass
$ true
```
"""


def test_grouped_ac_produces_exactly_one_result():
    """AC11: a grouped AC marker (`# AC2, AC3`) must launch the command
    EXACTLY once -- AC association must never fan out into multiple
    subprocess launches for the same source command."""
    data = run_normal(GROUPED_AC_BODY)
    assert len(data["results"]) == 1
    plan = bvp.compute_canonical_vc_plan(GROUPED_AC_BODY)
    assert plan["command_occurrence_count"] == 1


def test_grouped_ac_scalar_label_is_deterministic_regardless_of_set_order():
    """AC11: the scalar `ac` field for a grouped marker is a deterministic,
    numerically-sorted string -- NOT dependent on Python `set` iteration
    order (which is insertion/hash-order dependent in general)."""
    data = run_normal(GROUPED_AC_BODY)
    assert data["results"][0]["ac"] == "AC2,AC3"

    # Directly exercise the scalar-label helper with reversed input ordering
    # to confirm the OUTPUT does not depend on set construction order.
    label_a = bvp._ac_refs_to_scalar_label({"AC3", "AC2"})
    label_b = bvp._ac_refs_to_scalar_label({"AC2", "AC3"})
    assert label_a == label_b == "AC2,AC3"


def test_single_ac_ref_scalar_label_unchanged():
    assert bvp._ac_refs_to_scalar_label({"AC1"}) == "AC1"


def test_empty_ac_refs_scalar_label_is_none():
    assert bvp._ac_refs_to_scalar_label(set()) is None


# ---------------------------------------------------------------------------
# AC12: _distinct_command_texts_from_body parity with normal execution
# ---------------------------------------------------------------------------


def test_distinct_command_texts_matches_normal_execution_command_set():
    body = """## Verification Commands

```bash
# AC1
$ echo one
# AC2
$ echo two
# AC3
$ echo one
```
"""
    texts = bvp._distinct_command_texts_from_body(body)
    # First-occurrence-ordered, DISTINCT -- "echo one" appears once despite
    # 2 occurrences in the body.
    assert texts == ["echo one", "echo two"]

    data = run_normal(body)
    executed_commands = [r["raw_command"] for r in data["results"]]
    assert set(texts) == set(executed_commands)
    # 2 distinct commands, but 3 result entries (one per occurrence).
    assert len(executed_commands) == 3


# ---------------------------------------------------------------------------
# AC13: compute_canonical_vc_plan() parse-error contract
# ---------------------------------------------------------------------------


def test_compute_canonical_vc_plan_never_raises_on_non_canonical_body():
    """AC13: a non-canonical body (explanatory non-$ line) must not cause
    `compute_canonical_vc_plan()` to raise any exception -- existing direct
    callers (`contract_readiness_check.py` / `run_contract_review_once.py`
    / `run_root_review_pipeline.py`, outside this Issue's Allowed Paths)
    must never observe an unhandled exception from this call."""
    # Should not raise.
    plan = bvp.compute_canonical_vc_plan(EXPLANATORY_NON_DOLLAR_BODY)
    # fix_delta P1-B: exact 0, not merely non-negative -- see
    # test_explanatory_non_dollar_line_excluded_from_canonical_plan() for
    # the full rationale (whole-body rejection convergence with normal
    # execution).
    assert plan["command_occurrence_count"] == 0


def test_compute_canonical_vc_plan_fully_non_canonical_body_zero_occurrences():
    """AC13: a body with ONLY non-canonical lines produces a plan with
    ZERO command occurrences (no subprocess candidates), not an exception
    and not a spurious occurrence for the malformed line."""
    body = """## Verification Commands

```bash
# AC1
Only explanatory prose here, no command at all.
```
"""
    plan = bvp.compute_canonical_vc_plan(body)
    assert plan["command_occurrence_count"] == 0
    assert plan["command_occurrences"] == []


# ---------------------------------------------------------------------------
# fix_delta P2: results[].line / annotation_source.line coordinate system
# ---------------------------------------------------------------------------

MULTI_FENCE_WITH_PROSE_BODY = """## Verification Commands

Some prose before the fence explaining context.

```bash
# AC1
$ echo first
```

More prose between fences.

```bash
# AC2
# baseline-expect: pass
$ echo second
```
"""


def test_result_line_is_block_relative_not_section_relative():
    """fix_delta P2: `results[].line` must be BLOCK-relative (relative to
    the first line after the command's OWN enclosing ```bash opening
    fence line) -- the EXACT pre-migration `parse_commands_from_block()`
    coordinate system -- not section-relative (relative to the whole
    `## Verification Commands` section, which is what the shared parser's
    OWN `VcCommandEntry.line_number` measures and which would be much
    larger here due to the leading prose line and the first bash fence).

    Fixture layout (`MULTI_FENCE_WITH_PROSE_BODY`):
      fence 1 content: line 1 = "# AC1", line 2 = "$ echo first"
      fence 2 content: line 1 = "# AC2", line 2 = "# baseline-expect: pass",
                        line 3 = "$ echo second"
    """
    data = run_normal(MULTI_FENCE_WITH_PROSE_BODY)
    assert len(data["results"]) == 2

    first, second = data["results"]
    assert first["raw_command"] == "echo first"
    assert first["line"] == 2
    assert first["annotation_source"]["line"] is None
    assert first["annotation_source"]["raw"] is None

    assert second["raw_command"] == "echo second"
    assert second["line"] == 3
    # The `# baseline-expect: pass` annotation line is block-relative line 2
    # (bash_block_lines[1]) in the SAME fence -- `line` (3) and
    # `annotation_source.line` (2) both use the SAME block-relative
    # coordinate system, so their difference (1) matches the actual
    # physical distance between the two source lines within fence 2.
    assert second["annotation_source"]["line"] == 2
    assert second["annotation_source"]["raw"] == "# baseline-expect: pass"


def test_command_entries_from_shared_parser_line_no_is_block_relative():
    """fix_delta P2: the legacy 9-tuple's `line_no` slot (index 2), as
    produced by `_command_entries_from_shared_parser()`, is sourced from
    `VcCommandEntry.block_line_number` -- exercised directly (without
    going through the CLI) against the SAME fixture."""
    section = bvp.extract_verification_commands_section(
        MULTI_FENCE_WITH_PROSE_BODY
    ) or ""
    tuples, parse_result = bvp._command_entries_from_shared_parser(section)
    assert [t[1] for t in tuples] == ["echo first", "echo second"]
    assert [t[2] for t in tuples] == [2, 3]

    # `VcCommandEntry.line_number` (section-relative) is a LARGER number
    # than `block_line_number` (block-relative) for both entries here,
    # confirming the two coordinate systems are genuinely different for
    # this fixture (i.e. this test is not accidentally vacuous).
    for entry in parse_result.commands:
        assert entry.line_number > entry.block_line_number
