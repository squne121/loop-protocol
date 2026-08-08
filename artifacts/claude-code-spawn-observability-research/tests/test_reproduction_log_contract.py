"""Issue #2013 AC2: machine verification of ``reproduction-log.jsonl``.

The raw ledger must carry, per trial, every metadata field AC2 enumerates,
the full 12-checkpoint lifecycle map, and a ``diagnostic_cause`` drawn from
the fixed taxonomy -- with the fixed 15+15 trial counts intact and no
cherry-picking (removed failures would show up as a count shortfall, and an
invalidated trial must still carry its record plus an exclusion reason).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _research_contract_support import (  # noqa: E402
    ARTIFACT_DIR,
    DIAGNOSTIC_CAUSES,
    FAILURE_CLASSES,
    LANES,
    LEDGER_PATH,
    LIFECYCLE_CHECKPOINTS,
    lane_records,
    load_records,
    valid_records,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

REQUIRED_KEYS = (
    "trial_id",
    "lane",
    "route",
    "claude_code_version",
    "tested_head_sha",
    "prompt_sha256",
    "agent_definition_sha256",
    "effective_settings_digest",
    "timeout_seconds",
    "max_turns",
    "start_time",
    "end_time",
    "lifecycle",
    "failure_class",
    "diagnostic_cause",
)


@pytest.fixture(scope="module")
def records() -> list[dict]:
    assert LEDGER_PATH.is_file(), f"missing AC2 artifact: {LEDGER_PATH}"
    loaded = load_records()
    assert loaded, "reproduction-log.jsonl is empty"
    return loaded


def test_ledger_is_one_json_object_per_line() -> None:
    text = LEDGER_PATH.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines, "reproduction-log.jsonl is empty"
    for index, line in enumerate(lines, start=1):
        assert line.lstrip().startswith("{") and line.rstrip().endswith("}"), (
            f"line {index} is not a single-line JSON object"
        )


def test_fixed_trial_counts_are_intact(records: list[dict]) -> None:
    """15 control + 15 production, exactly as pre-registered. A deleted
    failure would show here."""
    assert len(records) >= 30, f"expected at least 30 raw records, got {len(records)}"
    for lane in LANES:
        valid_in_lane = lane_records(valid_records(records), lane)
        assert len(valid_in_lane) == 15, (
            f"lane {lane} has {len(valid_in_lane)} valid trials, expected exactly 15"
        )


def test_trial_ids_are_unique(records: list[dict]) -> None:
    ids = [r["trial_id"] for r in records]
    assert len(ids) == len(set(ids)), "duplicate trial_id in reproduction-log.jsonl"


def test_invalid_trials_keep_their_record_and_reason(records: list[dict]) -> None:
    """A trial that broke as an experiment must not be deleted: its record
    survives with ``trial_valid: false`` and a non-empty exclusion reason."""
    for record in records:
        assert "trial_valid" in record, f"{record['trial_id']}: missing trial_valid"
        if record["trial_valid"] is False:
            reason = record.get("excluded_reason")
            assert isinstance(reason, str) and reason.strip(), (
                f"{record['trial_id']}: invalidated without an excluded_reason"
            )


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_required_key_present_in_every_record(records: list[dict], key: str) -> None:
    for record in records:
        assert key in record, f"{record.get('trial_id')}: missing required key {key!r}"


def test_metadata_fields_are_well_formed(records: list[dict]) -> None:
    for record in records:
        trial = record["trial_id"]
        assert record["lane"] in LANES, f"{trial}: bad lane {record['lane']!r}"
        assert isinstance(record["route"], str) and record["route"], f"{trial}: empty route"
        version = record["claude_code_version"]
        assert isinstance(version, str) and version.strip(), f"{trial}: empty claude_code_version"
        assert "Claude Code" in version or re.match(r"^\d+\.\d+", version), (
            f"{trial}: implausible claude_code_version {version!r}"
        )
        assert _SHA1_RE.match(record["tested_head_sha"]), f"{trial}: bad tested_head_sha"
        for digest_key in ("prompt_sha256", "agent_definition_sha256", "effective_settings_digest"):
            value = record[digest_key]
            assert isinstance(value, str) and _SHA256_RE.match(value), (
                f"{trial}: {digest_key} is not a sha256 hex digest: {value!r}"
            )
        assert isinstance(record["timeout_seconds"], int) and record["timeout_seconds"] > 0
        assert isinstance(record["max_turns"], int) and record["max_turns"] > 0
        assert _TIMESTAMP_RE.match(record["start_time"]), f"{trial}: bad start_time"
        assert _TIMESTAMP_RE.match(record["end_time"]), f"{trial}: bad end_time"
        assert record["end_time"] >= record["start_time"], f"{trial}: end_time precedes start_time"
        assert isinstance(record["api_retry_count"], int) and record["api_retry_count"] >= 0, (
            f"{trial}: api_retry_count must be a non-negative int"
        )


def test_lifecycle_is_the_full_twelve_checkpoint_map(records: list[dict]) -> None:
    """Not a single collapsed boolean: exactly the 12 pre-registered
    checkpoints, each an independent bool."""
    for record in records:
        lifecycle = record["lifecycle"]
        assert isinstance(lifecycle, dict), f"{record['trial_id']}: lifecycle is not an object"
        assert set(lifecycle) == set(LIFECYCLE_CHECKPOINTS), (
            f"{record['trial_id']}: lifecycle key set drifted: "
            f"missing={set(LIFECYCLE_CHECKPOINTS) - set(lifecycle)} "
            f"extra={set(lifecycle) - set(LIFECYCLE_CHECKPOINTS)}"
        )
        for key, value in lifecycle.items():
            assert isinstance(value, bool), f"{record['trial_id']}: lifecycle[{key}] is not a bool"


def test_failure_class_uses_the_unchanged_existing_schema(records: list[dict]) -> None:
    for record in records:
        failure_class = record["failure_class"]
        assert failure_class is None or failure_class in FAILURE_CLASSES, (
            f"{record['trial_id']}: failure_class {failure_class!r} is outside the existing schema"
        )
        assert record["status"] in ("pass", "fail", "skip"), record["trial_id"]
        if record["status"] == "pass":
            assert failure_class is None, f"{record['trial_id']}: pass with a failure_class"
        else:
            assert failure_class is not None, f"{record['trial_id']}: non-pass without failure_class"


def test_diagnostic_cause_uses_the_fixed_taxonomy(records: list[dict]) -> None:
    for record in records:
        cause = record["diagnostic_cause"]
        assert cause is None or cause in DIAGNOSTIC_CAUSES, (
            f"{record['trial_id']}: diagnostic_cause {cause!r} is outside the taxonomy"
        )
        if record["status"] == "pass":
            assert cause is None, f"{record['trial_id']}: passing trial carries a diagnostic_cause"
        else:
            assert cause is not None, (
                f"{record['trial_id']}: failing trial has no diagnostic_cause "
                "(the outer failure_class must never be the only classification)"
            )


def test_spawn_lifecycle_and_downstream_route_are_separate_fields(records: list[dict]) -> None:
    """AGY / Serena / GitHub outcomes must be preserved beside the spawn
    evidence, never collapsed into it."""
    for record in records:
        assert "downstream" in record, f"{record['trial_id']}: no downstream field"
        downstream = record["downstream"]
        assert isinstance(downstream, dict)
        for key in ("selected_provider", "request_validation", "gemini_sentinel_hits"):
            assert key in downstream, f"{record['trial_id']}: downstream lacks {key!r}"
        assert "native_spawn_event_observed" in record
        assert isinstance(record["native_spawn_event_observed"], bool)


def test_downstream_failures_are_not_reclassified_as_spawn_failures(records: list[dict]) -> None:
    """A trial whose spawn lifecycle fully succeeded but whose AGY/Serena/
    GitHub route failed must be diagnosed as a downstream cause, never as a
    spawn cause."""
    spawn_causes = {
        "spawn_not_attempted",
        "subagent_start_not_observed",
        "subagent_completion_timeout",
        "tool_result_identity_not_observed",
        "agent_type_mismatch",
    }
    for record in records:
        lifecycle = record["lifecycle"]
        spawn_fully_observed = (
            lifecycle["agent_tool_use_observed"]
            and lifecycle["tool_result_observed"]
            and lifecycle["tool_result_agent_id_observed"]
            and lifecycle["agent_type_matches_requested"]
        )
        if spawn_fully_observed:
            assert record["diagnostic_cause"] not in spawn_causes, (
                f"{record['trial_id']}: spawn was fully observed yet diagnosed as "
                f"{record['diagnostic_cause']!r}"
            )


def test_api_retry_timeout_is_distinguished_from_plain_absence(records: list[dict]) -> None:
    """``runtime_api_retry_timeout`` may only be used where a
    ``system/api_retry`` event was genuinely observed, and any timeout with
    such an event must not be filed as a plain completion timeout."""
    for record in records:
        cause = record["diagnostic_cause"]
        if cause == "runtime_api_retry_timeout":
            assert record["api_retry_count"] > 0, (
                f"{record['trial_id']}: runtime_api_retry_timeout without an api_retry event"
            )
        if record.get("timed_out") and record["api_retry_count"] > 0:
            assert cause == "runtime_api_retry_timeout", (
                f"{record['trial_id']}: timeout with api_retry filed as {cause!r}"
            )


def test_identity_evidence_is_never_assumed(records: list[dict]) -> None:
    """``agent_type_matches_requested`` must be backed by an observed agent
    type -- never by substituting the requested value."""
    for record in records:
        lifecycle = record["lifecycle"]
        if lifecycle["agent_type_matches_requested"]:
            observed = record.get("observed_agent_type")
            assert isinstance(observed, str) and observed, (
                f"{record['trial_id']}: identity match claimed with no observed agent type"
            )
            assert observed == record["requested_agent_type"], record["trial_id"]
        if not lifecycle["tool_result_agent_type_observed"]:
            assert not lifecycle["agent_type_matches_requested"], (
                f"{record['trial_id']}: identity match claimed without agent-type evidence"
            )


def test_native_spawn_flag_matches_the_production_formula(records: list[dict]) -> None:
    """``native_spawn_event_observed`` must equal the production conjunction,
    so the ledger cannot quietly relax the evidence bar."""
    for record in records:
        expected = bool(
            record["parent_session_id_observed"]
            and record["child_session_id_observed"]
            and record["lifecycle"]["agent_type_matches_requested"]
        )
        assert record["native_spawn_event_observed"] == expected, (
            f"{record['trial_id']}: native_spawn_event_observed diverges from the "
            "production formula"
        )


def test_raw_evidence_is_persisted_for_every_trial(records: list[dict]) -> None:
    """Every derived flag must remain independently recomputable."""
    for record in records:
        for key in ("raw_stdout_path", "raw_stderr_path"):
            rel = record[key]
            assert isinstance(rel, str) and rel, f"{record['trial_id']}: no {key}"
            path = ARTIFACT_DIR / rel
            assert path.is_file(), f"{record['trial_id']}: raw evidence missing at {path}"
        stdout_path = ARTIFACT_DIR / record["raw_stdout_path"]
        assert stdout_path.stat().st_size > 0, (
            f"{record['trial_id']}: raw stdout evidence is empty"
        )


def test_trial_conditions_were_fixed_before_execution(records: list[dict]) -> None:
    """A single frozen trial-plan digest across all trials, and per-lane
    constant timeout / max-turns: the run conditions were not re-tuned after
    seeing results."""
    digests = {record["trial_plan_sha256"] for record in records}
    assert len(digests) == 1, f"trial plan digest changed mid-run: {digests}"
    plan_path = ARTIFACT_DIR / "trial-plan.json"
    assert plan_path.is_file(), "the pre-registered trial-plan.json is missing"
    import json

    frozen = json.loads(plan_path.read_text(encoding="utf-8"))
    assert frozen["trial_plan_sha256"] == digests.pop(), (
        "reproduction-log.jsonl was produced under a different plan than trial-plan.json"
    )
    for lane in LANES:
        rows = lane_records(records, lane)
        assert len({r["timeout_seconds"] for r in rows}) == 1, f"{lane}: timeout varied"
        assert len({r["max_turns"] for r in rows}) == 1, f"{lane}: max_turns varied"
        assert len({r["prompt_sha256"] for r in rows}) == 1 or lane == "production", (
            f"{lane}: prompt varied between trials"
        )


def test_production_lane_targets_the_real_custom_agents(records: list[dict]) -> None:
    production = lane_records(records, "production")
    agents = {record["requested_agent_type"] for record in production}
    assert agents == {"codebase-investigator", "web-researcher"}, agents
    for record in production:
        assert record["route"].startswith("claude_code:"), record["trial_id"]


def test_control_lane_needs_no_external_provider(records: list[dict]) -> None:
    """The control lane must be genuinely isolated: no AGY delegation
    evidence, no gemini invocation, no direct web fallback."""
    for record in lane_records(records, "control"):
        downstream = record["downstream"]
        assert downstream["gemini_sentinel_hits"] == 0, record["trial_id"]
        assert downstream["direct_web_tool_event_count"] == 0, record["trial_id"]
        assert downstream["delegation_request_present"] is False, record["trial_id"]
