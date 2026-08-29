"""Shared, assertion-free helpers for the Issue #2013 research contract tests.

Every derived number a contract test checks against a Markdown artifact is
recomputed here from ``reproduction-log.jsonl`` (AC2 raw evidence), never
read back from the Markdown it is supposed to validate.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ARTIFACT_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = ARTIFACT_DIR.parent.parent
AGENT_OPS_DIR = REPO_ROOT / "scripts" / "agent-ops"
ROUTE_SMOKE_PATH = AGENT_OPS_DIR / "run_agent_provider_route_smoke.py"
RUNTIME_SMOKE_PATH = AGENT_OPS_DIR / "run_worktree_agent_runtime_smoke.py"

LEDGER_PATH = ARTIFACT_DIR / "reproduction-log.jsonl"
CODE_ANALYSIS_PATH = ARTIFACT_DIR / "code-analysis.md"
SUMMARY_PATH = ARTIFACT_DIR / "reproduction-log.md"
RETRY_POLICY_PATH = ARTIFACT_DIR / "retry-policy-assessment.md"
CONCLUSION_PATH = ARTIFACT_DIR / "conclusion.md"

LANES = ("control", "production")

LIFECYCLE_CHECKPOINTS = (
    "process_started",
    "system_init_observed",
    "agent_tool_use_observed",
    "subagent_start_hook_observed",
    "subagent_stop_hook_observed",
    "tool_result_observed",
    "tool_result_agent_id_observed",
    "tool_result_agent_type_observed",
    "agent_type_matches_requested",
    "terminal_event_observed",
    "expected_marker_observed",
    "delegation_request_validated",
)

DIAGNOSTIC_CAUSES = (
    "spawn_not_attempted",
    "subagent_start_not_observed",
    "subagent_completion_timeout",
    "tool_result_identity_not_observed",
    "agent_type_mismatch",
    "runtime_api_retry_timeout",
    "runtime_nonzero",
    "terminal_event_missing",
    "marker_not_observed",
    "request_validation_failed",
    "delegation_wrapper_failed",
    "downstream_route_failed",
)

# The existing (unchanged by Issue #2013) failure_class vocabulary of
# run_agent_provider_route_smoke._run_route_once().
FAILURE_CLASSES = (
    "gemini_invoked",
    "direct_fallback_invoked",
    "agy_unavailable",
    "validation_failed",
    "spawn_not_observed",
    "provider_mismatch",
    "route_evidence_schema_mismatch",
    "timeout",
    "other",
)

CONCLUSION_CATEGORIES = (
    "repo_observability_defect",
    "upstream_runtime_contract_or_bug",
    "transient_infrastructure",
    "model_orchestration_nonspawn",
    "downstream_route_failure",
    "inconclusive",
)

# The production failure ladder, as reconstructed in code-analysis.md. Each
# entry is (step_number, source_line, failure_class, a substring that must
# genuinely appear on that line of the real production source file).
#
# Issue #2161: native Codex CLI (codex_cli) route removal shifted every
# line number in this ladder down by 1; the citations below were
# re-derived from the current source, not merely renumbered blindly.
PRODUCTION_FAILURE_LADDER = (
    (1, 910, "gemini_invoked", "gemini_hits > 0"),
    (2, 913, "direct_fallback_invoked", "fallback_hits > 0"),
    (3, 916, "agy_unavailable", "harness_exit == 77"),
    (4, 919, "validation_failed", "harness_exit != 0"),
    (5, 922, "spawn_not_observed", "native_spawn_event_observed"),
    (6, 925, "validation_failed", "request_validation"),
    (7, 928, "provider_mismatch", "selected_provider"),
    (8, 931, "route_evidence_schema_mismatch", "route_evidence_sha256"),
    (9, 934, "validation_failed", "wrapper_ok"),
)

EXTRACTORS = (
    ("extract_claude_parent_session_id", "spawn-time"),
    ("extract_claude_child_agent_type", "completion-time"),
    ("extract_claude_child_session_id", "completion-time"),
)


def load_records() -> list[dict]:
    text = LEDGER_PATH.read_text(encoding="utf-8")
    records: list[dict] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"line {line_number} is not a JSON object")
        records.append(payload)
    return records


def valid_records(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("trial_valid") is True]


def lane_records(records: list[dict], lane: str) -> list[dict]:
    return [r for r in records if r.get("lane") == lane]


def diagnostic_distribution(records: list[dict], lane: str) -> Counter:
    """``diagnostic_cause`` histogram for one lane. Passing trials are
    bucketed under the literal key ``none``."""
    counter: Counter = Counter()
    for record in lane_records(valid_records(records), lane):
        cause = record.get("diagnostic_cause")
        counter[cause if cause is not None else "none"] += 1
    return counter


def failure_class_distribution(records: list[dict], lane: str) -> Counter:
    counter: Counter = Counter()
    for record in lane_records(valid_records(records), lane):
        failure_class = record.get("failure_class")
        counter[failure_class if failure_class is not None else "none"] += 1
    return counter


def lane_status_counts(records: list[dict], lane: str) -> Counter:
    counter: Counter = Counter()
    for record in lane_records(valid_records(records), lane):
        counter[record.get("status")] += 1
    return counter


def spawn_observed_counts(records: list[dict], lane: str) -> tuple[int, int]:
    """``(observed, total)`` for the production ``native_spawn_event_observed``
    formula in one lane."""
    rows = lane_records(valid_records(records), lane)
    return sum(1 for r in rows if r.get("native_spawn_event_observed") is True), len(rows)


def hook_channel_identity_counts(records: list[dict], lane: str) -> tuple[int, int]:
    """``(hook_channel_had_agent_type, total)`` in one lane."""
    rows = lane_records(valid_records(records), lane)
    return sum(1 for r in rows if r.get("hook_agent_type_observed")), len(rows)


def tool_result_identity_counts(records: list[dict], lane: str) -> tuple[int, int]:
    """``(tool_result_channel_had_agent_type, total)`` in one lane."""
    rows = lane_records(valid_records(records), lane)
    return (
        sum(1 for r in rows if r["lifecycle"].get("tool_result_agent_type_observed") is True),
        len(rows),
    )


def source_line(path: Path, line_number: int) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not 1 <= line_number <= len(lines):
        return ""
    return lines[line_number - 1]
