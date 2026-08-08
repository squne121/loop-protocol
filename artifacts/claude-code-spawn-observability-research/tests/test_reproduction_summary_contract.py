"""Issue #2013 AC3: machine verification of ``reproduction-log.md``.

Every distribution row in the human-readable summary is re-derived here from
``reproduction-log.jsonl`` and compared cell by cell. A summary that is
missing a bucket, or that reports a number the raw ledger does not support,
fails -- so the summary cannot drift away from the evidence it claims to
summarise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _research_contract_support import (  # noqa: E402
    LANES,
    LIFECYCLE_CHECKPOINTS,
    SUMMARY_PATH,
    diagnostic_distribution,
    failure_class_distribution,
    hook_channel_identity_counts,
    lane_records,
    lane_status_counts,
    load_records,
    spawn_observed_counts,
    tool_result_identity_counts,
    valid_records,
)


@pytest.fixture(scope="module")
def summary_text() -> str:
    assert SUMMARY_PATH.is_file(), f"missing AC3 artifact: {SUMMARY_PATH}"
    text = SUMMARY_PATH.read_text(encoding="utf-8")
    assert text.strip(), "reproduction-log.md must not be empty"
    return text


@pytest.fixture(scope="module")
def records() -> list[dict]:
    return load_records()


STATUS_HEADING = "## lane 別 status 分布"
FAILURE_CLASS_HEADING = "## lane 別 failure_class 分布"
DIAGNOSTIC_HEADING = "## lane 別 diagnostic_cause 分布"
LIFECYCLE_HEADING = "## lane 別 lifecycle checkpoint 観測率"
IDENTITY_HEADING = "## identity evidence channel の突き合わせ"
SPAWN_RATE_HEADING = "## production 式 native_spawn_event_observed の成立率"
ROUTE_HEADING = "## production lane の route 別内訳"


def _section(text: str, heading: str) -> str:
    """The body between ``heading`` and the next ``## `` heading.

    Row lookups are scoped to a section so that a bucket label appearing in
    two different tables (``none`` is both a ``failure_class`` and a
    ``diagnostic_cause`` bucket) can never be matched against the wrong one.
    """
    assert heading in text, f"reproduction-log.md has no section {heading!r}"
    start = text.index(heading) + len(heading)
    rest = text[start:]
    match = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: match.start()] if match else rest


def _rows(text: str) -> list[list[str]]:
    parsed: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= {"-", ":"} and cell for cell in cells):
            continue
        parsed.append(cells)
    return parsed


def _find_row(rows: list[list[str]], *prefix: str) -> list[str] | None:
    for cells in rows:
        if len(cells) >= len(prefix) and cells[: len(prefix)] == list(prefix):
            return cells
    return None


def test_summary_declares_its_provenance(summary_text: str, records: list[dict]) -> None:
    assert "reproduction-log.jsonl" in summary_text, (
        "the summary must state that it is derived from the AC2 raw ledger"
    )
    assert "28394e226533cd59cdfc0f55602ac65e389a6600" in summary_text, (
        "historical baseline SHA is not recorded in the summary"
    )
    for sha in {r["tested_head_sha"] for r in records}:
        assert sha in summary_text, f"actual tested SHA {sha} is not recorded in the summary"
    for version in {r["claude_code_version"] for r in records}:
        assert version in summary_text, f"Claude Code version {version!r} is not recorded"


@pytest.mark.parametrize("lane", LANES)
def test_status_distribution_matches_raw_ledger(
    summary_text: str, records: list[dict], lane: str
) -> None:
    row = _find_row(_rows(_section(summary_text, STATUS_HEADING)), lane)
    assert row is not None, f"no status row for lane {lane}"
    counts = lane_status_counts(records, lane)
    total = len(lane_records(valid_records(records), lane))
    expected = [
        lane, str(counts.get("pass", 0)), str(counts.get("fail", 0)),
        str(counts.get("skip", 0)), str(total),
    ]
    assert row[:5] == expected, (
        f"lane {lane} status row {row[:5]} does not match the raw ledger {expected}"
    )


@pytest.mark.parametrize("lane", LANES)
def test_diagnostic_cause_distribution_matches_raw_ledger(
    summary_text: str, records: list[dict], lane: str
) -> None:
    rows = _rows(_section(summary_text, DIAGNOSTIC_HEADING))
    distribution = diagnostic_distribution(records, lane)
    assert distribution, f"lane {lane} has no trials to summarise"
    for cause, count in distribution.items():
        row = _find_row(rows, lane, cause)
        assert row is not None, f"summary omits diagnostic_cause bucket {lane}/{cause}"
        assert row[2] == str(count), (
            f"summary reports {lane}/{cause}={row[2]}, raw ledger says {count}"
        )
    assert sum(distribution.values()) == len(lane_records(valid_records(records), lane))


@pytest.mark.parametrize("lane", LANES)
def test_failure_class_distribution_matches_raw_ledger(
    summary_text: str, records: list[dict], lane: str
) -> None:
    rows = _rows(_section(summary_text, FAILURE_CLASS_HEADING))
    for failure_class, count in failure_class_distribution(records, lane).items():
        row = _find_row(rows, lane, failure_class)
        assert row is not None, f"summary omits failure_class bucket {lane}/{failure_class}"
        assert row[2] == str(count), (
            f"summary reports {lane}/{failure_class}={row[2]}, raw ledger says {count}"
        )


@pytest.mark.parametrize("lane", LANES)
def test_lifecycle_checkpoint_rates_match_raw_ledger(
    summary_text: str, records: list[dict], lane: str
) -> None:
    """All 12 checkpoints must be summarised per lane, each with the observed
    count the raw ledger supports."""
    rows = _rows(_section(summary_text, LIFECYCLE_HEADING))
    lane_rows = lane_records(valid_records(records), lane)
    for checkpoint in LIFECYCLE_CHECKPOINTS:
        row = _find_row(rows, lane, checkpoint)
        assert row is not None, f"summary omits lifecycle checkpoint {lane}/{checkpoint}"
        observed = sum(1 for r in lane_rows if r["lifecycle"][checkpoint] is True)
        assert row[2] == str(observed), (
            f"summary reports {lane}/{checkpoint} observed={row[2]}, raw ledger says {observed}"
        )
        assert row[3] == str(len(lane_rows))


@pytest.mark.parametrize("lane", LANES)
def test_cross_channel_identity_summary_matches_raw_ledger(
    summary_text: str, records: list[dict], lane: str
) -> None:
    """The tool_result-channel vs hook-channel comparison must be summarised
    and must match the ledger -- this is the evidence that hooks are not the
    sole ground truth."""
    lane_rows = lane_records(valid_records(records), lane)
    tool_result_observed, total = tool_result_identity_counts(records, lane)
    hook_observed, _ = hook_channel_identity_counts(records, lane)
    agreed = sum(
        1 for r in lane_rows
        if r["cross_channel_identity_agreement"]["agent_id_channels_agree"] is True
    )
    expected = [lane, str(tool_result_observed), str(hook_observed), str(agreed), str(total)]
    row = _find_row(_rows(_section(summary_text, IDENTITY_HEADING)), lane)
    assert row is not None, f"summary has no cross-channel identity row for lane {lane}"
    assert row == expected, (
        f"cross-channel identity row {row} does not match the raw ledger {expected}"
    )


@pytest.mark.parametrize("lane", LANES)
def test_native_spawn_rate_matches_raw_ledger(
    summary_text: str, records: list[dict], lane: str
) -> None:
    observed, total = spawn_observed_counts(records, lane)
    row = _find_row(_rows(_section(summary_text, SPAWN_RATE_HEADING)), lane)
    assert row is not None, f"summary has no native_spawn_event_observed row for {lane}"
    assert row == [lane, str(observed), str(total)], (
        f"native_spawn_event_observed row {row} does not match the raw ledger "
        f"observed={observed} total={total}"
    )


def test_production_route_breakdown_matches_raw_ledger(
    summary_text: str, records: list[dict]
) -> None:
    rows = _rows(_section(summary_text, ROUTE_HEADING))
    production = lane_records(valid_records(records), "production")
    routes = {r["route"] for r in production}
    assert routes, "no production trials to summarise"
    for route in routes:
        route_rows = [r for r in production if r["route"] == route]
        passed = sum(1 for r in route_rows if r["status"] == "pass")
        row = _find_row(rows, route)
        assert row is not None, f"summary omits production route {route}"
        assert row[1:4] == [str(passed), str(len(route_rows) - passed), str(len(route_rows))], (
            f"route {route} row {row[1:4]} does not match the raw ledger"
        )


def test_summary_reports_no_bucket_absent_from_the_ledger(
    summary_text: str, records: list[dict]
) -> None:
    """The inverse direction: a diagnostic_cause bucket claimed by the summary
    must actually exist in the raw ledger with that count."""
    rows = _rows(_section(summary_text, DIAGNOSTIC_HEADING))
    for lane in LANES:
        distribution = diagnostic_distribution(records, lane)
        for cells in rows:
            if len(cells) != 3 or cells[0] != lane:
                continue
            bucket = cells[1]
            assert bucket in distribution, (
                f"summary invents diagnostic_cause bucket {lane}/{bucket}, which the raw "
                f"ledger does not contain (ledger buckets: {sorted(distribution)})"
            )
            assert re.fullmatch(r"\d+", cells[2]), (
                f"summary bucket {lane}/{bucket} has a non-numeric count {cells[2]!r}"
            )
            assert cells[2] == str(distribution[bucket]), (
                f"summary reports {lane}/{bucket}={cells[2]}, ledger says {distribution[bucket]}"
            )


def test_summary_records_the_ordering_consequence(summary_text: str) -> None:
    """The summary must not present ``spawn_not_observed`` vs
    ``validation_failed`` as if it indicated whether a spawn happened."""
    assert "spawn_not_observed" in summary_text and "validation_failed" in summary_text
    assert "順 4" in summary_text and "順 5" in summary_text, (
        "the summary does not explain that the two failure classes are separated by "
        "evaluation order, not by spawn presence"
    )
