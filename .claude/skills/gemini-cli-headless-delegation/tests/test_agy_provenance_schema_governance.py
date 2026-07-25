"""Schema governance tests for `agy_tool_provenance_v1` (Issue #1708 AC4 / AC11).

Hermetic: no live AGY execution, no network. Validates (a) the fail-closed field
validator rejects any missing required field, and (b) the provider contract docs
carry a schema governance section (consumer inventory / compatibility decision /
closed-schema test reference) for the new schema.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agy_tool_provenance as prov  # noqa: E402

_REFERENCES_DIR = Path(__file__).resolve().parent.parent / "references"


def _valid_event():
    return prov.build_provenance_event(
        event="PreToolUse",
        tool_name="search_web",
        tool_args={"query": "loop protocol"},
        step_idx=3,
        conversation_id="conv-123",
        transcript_path="/home/user/repo/.gemini/antigravity-cli/transcript.jsonl",
        transcript_sha256="a" * 64,
        parent_run_id="run-abc",
        subtask_id="subtask-1",
        attempt_id="attempt-1",
        tool_profile="grounded_research",
    )


@pytest.mark.parametrize(
    "field",
    [
        "schema",
        "version",
        "event",
        "toolCall",
        "stepIdx",
        "conversationId",
        "transcript_path_ref",
        "transcript_sha256",
        "parent_run_id",
        "subtask_id",
        "attempt_id",
        "provider",
        "tool_profile",
        "monotonic_ns",
        "utc",
    ],
)
def test_required_field_missing_is_rejected(field):
    event = _valid_event()
    del event[field]
    ok, violations = prov.validate_provenance_event(event)
    assert ok is False
    assert any(v.startswith(f"missing_required_field:{field}") for v in violations)


def test_wrong_schema_name_is_rejected():
    event = _valid_event()
    event["schema"] = "some_other_schema_v1"
    ok, violations = prov.validate_provenance_event(event)
    assert ok is False
    assert "wrong_schema" in violations


def test_wrong_version_is_rejected():
    event = _valid_event()
    event["version"] = 2
    ok, violations = prov.validate_provenance_event(event)
    assert ok is False
    assert "wrong_version" in violations


def test_fully_valid_event_is_accepted():
    ok, violations = prov.validate_provenance_event(_valid_event())
    assert ok is True, violations


def test_non_dict_event_is_rejected():
    ok, violations = prov.validate_provenance_event(["not", "a", "dict"])
    assert ok is False
    assert violations == ["event_not_object"]


# ---------------------------------------------------------------------------
# AC11: schema governance section present in provider contract docs
# ---------------------------------------------------------------------------


def test_schema_governance_section_present():
    usage_contract = (_REFERENCES_DIR / "usage-contract.md").read_text(encoding="utf-8")
    assert "agy_tool_provenance_v1" in usage_contract
    # Governance keywords: consumer inventory, compatibility decision, closed-schema tests.
    assert "consumer inventory" in usage_contract.lower() or "consumer" in usage_contract.lower()
    assert "compatibility" in usage_contract.lower()
    assert "closed-schema" in usage_contract.lower() or "closed schema" in usage_contract.lower()
    assert "test_agy_tool_provenance.py" in usage_contract
    assert "test_agy_provenance_schema_governance.py" in usage_contract


def test_canonical_tool_names_documented_in_provider_mapping():
    provider_mapping = (_REFERENCES_DIR / "provider-mapping.md").read_text(encoding="utf-8")
    assert "search_web" in provider_mapping
    assert "read_url_content" in provider_mapping
    assert "agy_tool_provenance_v1" in provider_mapping
