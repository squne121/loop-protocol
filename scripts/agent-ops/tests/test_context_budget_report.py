"""Issue #2052 AC7: ``scripts/agent-ops/context_budget_report.py`` must
report ONLY the actually-observed ``fetch_count`` / ``emitted_utf8_bytes`` /
``snapshot_reuse_count`` / ``duplicate_projection_count`` metrics per phase,
and must never emit a fabricated token / model-turn value.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_CBR_MODULE_PATH = Path(__file__).resolve().parent.parent / "context_budget_report.py"
_CBR_MODULE_NAME = "context_budget_report_issue_2052"
_spec = importlib.util.spec_from_file_location(_CBR_MODULE_NAME, _CBR_MODULE_PATH)
assert _spec is not None and _spec.loader is not None
context_budget_report = importlib.util.module_from_spec(_spec)
sys.modules[_CBR_MODULE_NAME] = context_budget_report
_spec.loader.exec_module(context_budget_report)

_EI_MODULE_PATH = Path(__file__).resolve().parent.parent / "evidence_index.py"
_EI_MODULE_NAME = "evidence_index_for_context_budget_report_issue_2052"
_ei_spec = importlib.util.spec_from_file_location(_EI_MODULE_NAME, _EI_MODULE_PATH)
assert _ei_spec is not None and _ei_spec.loader is not None
evidence_index = importlib.util.module_from_spec(_ei_spec)
sys.modules[_EI_MODULE_NAME] = evidence_index
_ei_spec.loader.exec_module(evidence_index)


def test_report_only_observed_metrics_no_fabricated_tokens():
    report = context_budget_report.ContextBudgetReport(consumer="run_refinement_preflight.py")
    report.record_phase(
        "preflight_fetch",
        {"fetch_count": 2, "emitted_utf8_bytes": 4096, "snapshot_reuse_count": 1, "duplicate_projection_count": 1},
    )
    payload = report.to_dict()

    assert payload["schema"] == "CONTEXT_BUDGET_REPORT_V1"
    assert payload["consumer"] == "run_refinement_preflight.py"
    assert set(payload["phases"]["preflight_fetch"].keys()) == set(context_budget_report.OBSERVED_METRIC_FIELDS)

    # The closed field set never contains any token/model-turn naming.
    rendered = json.dumps(payload)
    for forbidden in ("token", "model_turn", "model-turn", "turns_consumed", "tokens_saved"):
        assert forbidden not in rendered.lower().replace(" ", "_") or forbidden not in rendered.lower(), (
            f"fabricated/unobserved metric leaked into report: {forbidden!r}"
        )

    assert payload["totals"] == {
        "fetch_count": 2,
        "emitted_utf8_bytes": 4096,
        "snapshot_reuse_count": 1,
        "duplicate_projection_count": 1,
    }


def test_record_phase_rejects_unknown_metric_fields():
    report = context_budget_report.ContextBudgetReport(consumer="test")
    with pytest.raises(ValueError):
        report.record_phase("preflight_fetch", {"fetch_count": 1, "tokens_saved": 500})


def test_record_phase_rejects_non_int_and_negative_values():
    report = context_budget_report.ContextBudgetReport(consumer="test")
    with pytest.raises(ValueError):
        report.record_phase("preflight_fetch", {"fetch_count": 1.5})
    with pytest.raises(ValueError):
        report.record_phase("preflight_fetch", {"fetch_count": -1})
    with pytest.raises(ValueError):
        report.record_phase("preflight_fetch", {"fetch_count": True})


def test_multiple_phases_are_kept_separate_and_summed_in_totals():
    report = context_budget_report.ContextBudgetReport(consumer="test")
    report.record_phase("preflight_fetch", {"fetch_count": 2, "emitted_utf8_bytes": 100})
    report.record_phase("post_repair_readback", {"fetch_count": 1, "emitted_utf8_bytes": 50})

    assert report.phases() == ["preflight_fetch", "post_repair_readback"]
    totals = report.totals()
    assert totals["fetch_count"] == 3
    assert totals["emitted_utf8_bytes"] == 150
    # Untouched fields default to zero, never fabricated non-zero guesses.
    assert totals["snapshot_reuse_count"] == 0
    assert totals["duplicate_projection_count"] == 0


def test_record_from_evidence_index_uses_only_observed_counters():
    index = evidence_index.EvidenceIndex()
    index.begin_phase("preflight_fetch")
    index.get_or_fetch(
        repository="squne121/loop-protocol",
        resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052,
        fetch_fn=lambda: ({"body": "hello"}, ""),
        # Issue #2052 fix_delta D: duplicate_projection_count only counts a
        # cache hit that ALSO supplied a project_fn.
        project_fn=lambda raw: raw,
    )
    index.get_or_fetch(
        repository="squne121/loop-protocol",
        resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052,
        fetch_fn=lambda: ({"body": "hello"}, ""),
        project_fn=lambda raw: raw,
    )

    report = context_budget_report.ContextBudgetReport(consumer="run_refinement_preflight.py")
    report.record_from_evidence_index("preflight_fetch", index)

    payload = report.to_dict()["phases"]["preflight_fetch"]
    assert payload["fetch_count"] == 1
    assert payload["snapshot_reuse_count"] == 1
    assert payload["duplicate_projection_count"] == 1
    assert payload["emitted_utf8_bytes"] > 0


def test_record_from_evidence_index_across_phase_transition_does_not_double_count():
    """Issue #2052 fix_delta D regression: recording phase A's metrics,
    then transitioning to phase B and recording ITS metrics, must never
    have phase B's recorded counters "carry over" phase A's activity --
    `EvidenceIndex.begin_phase()` resets its own counters to phase-local
    values on a genuine transition, so `totals()` across both recorded
    phases reflects exactly the real total work done (not a doubled
    count for phase A's contribution)."""
    index = evidence_index.EvidenceIndex()
    report = context_budget_report.ContextBudgetReport(consumer="run_refinement_preflight.py")

    index.begin_phase("phase_a")
    index.get_or_fetch(
        repository="squne121/loop-protocol",
        resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=1,
        fetch_fn=lambda: ({"body": "a"}, ""),
    )
    report.record_from_evidence_index("phase_a", index)

    index.begin_phase("phase_b")
    index.get_or_fetch(
        repository="squne121/loop-protocol",
        resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2,
        fetch_fn=lambda: ({"body": "b"}, ""),
    )
    report.record_from_evidence_index("phase_b", index)

    payload = report.to_dict()
    assert payload["phases"]["phase_a"]["fetch_count"] == 1
    assert payload["phases"]["phase_b"]["fetch_count"] == 1
    # Exactly 2 real fetches happened in total -- never 3 (1 + (1+1)).
    assert payload["totals"]["fetch_count"] == 2


def test_write_json_and_from_dict_roundtrip(tmp_path):
    report = context_budget_report.ContextBudgetReport(consumer="run_refinement_preflight.py")
    report.record_phase("preflight_fetch", {"fetch_count": 5, "emitted_utf8_bytes": 999})
    out_path = report.write_json(tmp_path / "context_budget_report.json")

    assert out_path.is_file()
    loaded_payload = json.loads(out_path.read_text(encoding="utf-8"))
    reloaded = context_budget_report.ContextBudgetReport.from_dict(loaded_payload)
    assert reloaded.to_dict() == report.to_dict()


def test_from_dict_rejects_wrong_schema():
    with pytest.raises(ValueError):
        context_budget_report.ContextBudgetReport.from_dict({"schema": "SOMETHING_ELSE_V1"})
