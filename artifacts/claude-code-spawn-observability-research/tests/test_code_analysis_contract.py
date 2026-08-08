"""Issue #2013 AC1: machine verification of ``code-analysis.md``.

Three things are checked, and each is cross-validated against the *real*
production source rather than only against the Markdown's own prose:

(a) the spawn-time / completion-time classification of the three extractors,
(b) the actual ``_run_route_once()`` failure evaluation order, with
    ``file:line`` references that must genuinely point at the described
    condition in ``run_agent_provider_route_smoke.py``,
(c) the 12-value extended ``diagnostic_cause`` taxonomy.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _research_contract_support import (  # noqa: E402
    CODE_ANALYSIS_PATH,
    DIAGNOSTIC_CAUSES,
    EXTRACTORS,
    LIFECYCLE_CHECKPOINTS,
    PRODUCTION_FAILURE_LADDER,
    ROUTE_SMOKE_PATH,
    RUNTIME_SMOKE_PATH,
    source_line,
)


@pytest.fixture(scope="module")
def analysis_text() -> str:
    assert CODE_ANALYSIS_PATH.is_file(), f"missing AC1 artifact: {CODE_ANALYSIS_PATH}"
    text = CODE_ANALYSIS_PATH.read_text(encoding="utf-8")
    assert text.strip(), "code-analysis.md must not be empty"
    return text


# --- (a) extractor evidence classification ---------------------------------


@pytest.mark.parametrize(("extractor", "evidence_kind"), EXTRACTORS)
def test_extractor_evidence_classification_recorded(
    analysis_text: str, extractor: str, evidence_kind: str
) -> None:
    """Each extractor must be named AND classified as spawn-time or
    completion-time evidence in its own section (not merely mentioned)."""
    heading = re.search(
        rf"^###\s+`{re.escape(extractor)}\(\)`.*$", analysis_text, re.MULTILINE
    )
    assert heading, f"code-analysis.md has no dedicated section for {extractor}()"
    start = heading.end()
    next_heading = re.search(r"^###\s", analysis_text[start:], re.MULTILINE)
    section = analysis_text[start: start + (next_heading.start() if next_heading else len(analysis_text))]
    assert "区分:" in section, f"{extractor}() section records no evidence-kind classification"
    kind_line = next(line for line in section.splitlines() if "区分:" in line)
    assert evidence_kind in kind_line, (
        f"{extractor}() must be classified as {evidence_kind} evidence, got: {kind_line!r}"
    )


def test_extractor_sections_cite_real_source_lines(analysis_text: str) -> None:
    """Every ``run_worktree_agent_runtime_smoke.py:<n>`` citation must point at
    a line that genuinely exists in the production file."""
    citations = re.findall(r"run_worktree_agent_runtime_smoke\.py:(\d+)(?:-(\d+))?", analysis_text)
    assert citations, "code-analysis.md cites no runtime-smoke source lines"
    total_lines = len(RUNTIME_SMOKE_PATH.read_text(encoding="utf-8").splitlines())
    for start, end in citations:
        for value in (start, end):
            if not value:
                continue
            assert 1 <= int(value) <= total_lines, (
                f"run_worktree_agent_runtime_smoke.py:{value} is out of range (file has {total_lines} lines)"
            )


def test_parent_session_id_short_circuit_defect_recorded(analysis_text: str) -> None:
    """The known information-destroying short circuit in
    ``extract_claude_child_session_id()`` must be recorded, and must still be
    真 in the production source at the cited line."""
    assert "if not parent_session_id:" in analysis_text, (
        "code-analysis.md does not record the parent_session_id short-circuit defect"
    )
    body = RUNTIME_SMOKE_PATH.read_text(encoding="utf-8")
    marker = body.index("def extract_claude_child_session_id(")
    tail = body[marker: marker + 2500]
    assert "if not parent_session_id:" in tail and "return None" in tail, (
        "the recorded short-circuit no longer exists in the production source; "
        "code-analysis.md is stale"
    )


def test_tested_and_baseline_sha_both_recorded(analysis_text: str) -> None:
    assert "28394e226533cd59cdfc0f55602ac65e389a6600" in analysis_text, (
        "historical baseline SHA (PR #2005 merge commit) is not recorded"
    )
    tested = re.search(r"actual tested SHA.*?`([0-9a-f]{40})`", analysis_text)
    assert tested, "actual tested SHA is not recorded"
    ledger_shas = {
        record["tested_head_sha"] for record in __import__(
            "_research_contract_support"
        ).load_records()
    }
    assert ledger_shas == {tested.group(1)}, (
        "code-analysis.md's recorded tested SHA does not match the SHA every trial "
        f"was actually run against: doc={tested.group(1)!r} ledger={sorted(ledger_shas)!r}"
    )


# --- (b) _run_route_once() failure evaluation order ------------------------


@pytest.mark.parametrize(
    ("step", "line_number", "failure_class", "condition_fragment"), PRODUCTION_FAILURE_LADDER
)
def test_failure_ladder_step_recorded_and_matches_source(
    analysis_text: str, step: int, line_number: int, failure_class: str, condition_fragment: str
) -> None:
    """Each ladder step must be recorded in code-analysis.md with a
    ``run_agent_provider_route_smoke.py:<line>`` reference, and that line in
    the real production file must genuinely carry the described condition."""
    citation = f"run_agent_provider_route_smoke.py:{line_number}"
    assert citation in analysis_text, (
        f"ladder step {step} ({failure_class}) is not recorded with a {citation} reference"
    )
    real_line = source_line(ROUTE_SMOKE_PATH, line_number)
    assert condition_fragment in real_line, (
        f"code-analysis.md cites {citation} for {failure_class}, but that source line is "
        f"{real_line!r} and does not contain {condition_fragment!r}"
    )
    row = next(
        (line for line in analysis_text.splitlines()
         if citation in line and line.lstrip().startswith("|")),
        None,
    )
    assert row is not None, f"ladder step {step} is not recorded as a table row"
    assert f"`{failure_class}`" in row, (
        f"ladder row for {citation} does not record failure_class {failure_class}: {row!r}"
    )


def test_failure_ladder_records_ordering_consequence(analysis_text: str) -> None:
    """The core diagnostic consequence -- harness non-zero (step 4) being
    evaluated before spawn evidence (step 5) -- must be recorded explicitly."""
    assert "順 4" in analysis_text and "順 5" in analysis_text, (
        "code-analysis.md does not discuss the step-4 / step-5 ordering"
    )
    assert "spawn の有無を推測できない" in analysis_text, (
        "code-analysis.md does not record that failure_class alone cannot imply spawn presence"
    )


def test_failure_ladder_source_order_is_monotonic() -> None:
    """Guard against the recorded ladder drifting from the production source:
    the cited line numbers must be strictly increasing, matching a single
    if/elif chain."""
    line_numbers = [entry[1] for entry in PRODUCTION_FAILURE_LADDER]
    assert line_numbers == sorted(set(line_numbers)), line_numbers


def test_issue_body_ladder_drift_recorded(analysis_text: str) -> None:
    """Issue #2013's Notes recorded an 8-step ladder; current main has 9
    (``route_evidence_schema_mismatch``). The drift must be recorded."""
    assert "route_evidence_schema_mismatch" in analysis_text
    assert "drift" in analysis_text.lower(), "the Issue-vs-current-code drift is not recorded"


# --- (c) extended diagnostic_cause taxonomy --------------------------------


@pytest.mark.parametrize("cause", DIAGNOSTIC_CAUSES)
def test_diagnostic_cause_defined(analysis_text: str, cause: str) -> None:
    """Each of the 12 taxonomy values must be *defined*, i.e. appear in a
    table row that also carries a definition cell -- not merely listed."""
    rows = [
        line for line in analysis_text.splitlines()
        if line.lstrip().startswith("|") and f"`{cause}`" in line
    ]
    assert rows, f"diagnostic_cause `{cause}` has no definition row in code-analysis.md"
    for row in rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0] == f"`{cause}`" and len(cells[1]) >= 15:
            return
    pytest.fail(f"diagnostic_cause `{cause}` row carries no substantive definition: {rows!r}")


def test_taxonomy_is_exactly_twelve(analysis_text: str) -> None:
    assert len(DIAGNOSTIC_CAUSES) == 12
    assert len(set(DIAGNOSTIC_CAUSES)) == 12


def test_existing_failure_class_schema_declared_unchanged(analysis_text: str) -> None:
    assert "一切変更しない" in analysis_text, (
        "code-analysis.md must state that the existing failure_class schema is unchanged"
    )


@pytest.mark.parametrize("checkpoint", LIFECYCLE_CHECKPOINTS)
def test_lifecycle_checkpoint_recorded(analysis_text: str, checkpoint: str) -> None:
    assert f"`{checkpoint}`" in analysis_text, (
        f"lifecycle checkpoint `{checkpoint}` is not recorded in code-analysis.md"
    )


def test_lifecycle_checkpoint_count_is_twelve() -> None:
    assert len(LIFECYCLE_CHECKPOINTS) == 12
    assert len(set(LIFECYCLE_CHECKPOINTS)) == 12


def test_hook_is_not_sole_ground_truth(analysis_text: str) -> None:
    """The design decision to cross-check hook evidence against the
    tool-result channel (rather than trusting hooks alone) must be recorded,
    together with the upstream report's non-contractual status."""
    assert "27755" in analysis_text
    assert "唯一の ground truth" in analysis_text
    assert "公式契約ではない" in analysis_text
