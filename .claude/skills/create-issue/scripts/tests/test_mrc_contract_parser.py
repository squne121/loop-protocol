"""Tests for mrc_contract_parser (Issue #1135 P0 / P1b).

Verifies the shared, section-bound, strict Machine-Readable Contract parser:
  - decoy YAML outside the MRC section is ignored (AC1)
  - duplicate mapping keys fail closed (AC2)
  - structural failures (0/multiple sections, 0/multiple fences, non-mapping root)
    fail closed (AC3)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mrc_contract_parser as mp  # noqa: E402


def _body(*sections: str) -> str:
    return "\n\n".join(sections)


MRC_CODE = (
    "## Machine-Readable Contract\n\n"
    "```yaml\n"
    "contract_schema_version: v1\n"
    "issue_kind: implementation\n"
    "change_kind: code\n"
    "```"
)


class TestDecoyYamlOutsideMrc:
    def test_decoy_yaml_outside_mrc_ignored(self):
        """A `change_kind: docs` YAML fence under ## Notes must NOT be read; the
        real MRC (change_kind: code) is authoritative."""
        notes = (
            "## Notes\n\n"
            "```yaml\n"
            "contract_schema_version: v1\n"
            "change_kind: docs\n"
            "```"
        )
        result = mp.parse_machine_readable_contract(_body(notes, MRC_CODE))
        assert result.ok is True
        assert result.get("change_kind") == "code", (
            f"decoy YAML must be ignored; got {result.get('change_kind')!r}"
        )

    def test_decoy_yaml_outside_mrc_ignored_order_independent(self):
        """Even when the decoy appears AFTER the MRC, only the MRC is parsed."""
        notes = "## Appendix\n\n```yaml\nchange_kind: docs\n```"
        result = mp.parse_machine_readable_contract(_body(MRC_CODE, notes))
        assert result.ok is True
        assert result.get("change_kind") == "code"


class TestDuplicateKeyFailsClosed:
    def test_duplicate_key_fails_closed(self):
        """change_kind declared twice (code -> docs) must fail closed, not last-wins."""
        body = (
            "## Machine-Readable Contract\n\n"
            "```yaml\n"
            "contract_schema_version: v1\n"
            "issue_kind: implementation\n"
            "change_kind: code\n"
            "change_kind: docs\n"
            "```"
        )
        result = mp.parse_machine_readable_contract(body)
        assert result.ok is False
        assert result.reason == mp.REASON_DUPLICATE_KEY
        assert result.duplicate_key == "change_kind"
        # None-safe accessor returns the default on failure (no silent docs leak).
        assert result.get("change_kind") is None

    @pytest.mark.parametrize("dup_key", ["requirement_id", "source_task_id", "product_spec_id"])
    def test_duplicate_trace_key_fails_closed(self, dup_key):
        body = (
            "## Machine-Readable Contract\n\n"
            "```yaml\n"
            "contract_schema_version: v1\n"
            "issue_kind: implementation\n"
            f"{dup_key}: REQ-001\n"
            f"{dup_key}: REQ-002\n"
            "```"
        )
        result = mp.parse_machine_readable_contract(body)
        assert result.ok is False
        assert result.reason == mp.REASON_DUPLICATE_KEY
        assert result.duplicate_key == dup_key
        assert dup_key in mp.SENSITIVE_DUPLICATE_KEYS


class TestStructuralFailuresFailClosed:
    def test_structural_failures_fail_closed_missing_section(self):
        result = mp.parse_machine_readable_contract("## Outcome\n\ndo X")
        assert result.ok is False
        assert result.reason == mp.REASON_MISSING

    def test_structural_failures_fail_closed_multiple_sections(self):
        body = _body(MRC_CODE, MRC_CODE)
        result = mp.parse_machine_readable_contract(body)
        assert result.ok is False
        assert result.reason == mp.REASON_MULTIPLE_SECTIONS

    def test_structural_failures_fail_closed_no_fence(self):
        body = "## Machine-Readable Contract\n\nchange_kind: docs (no fence)"
        result = mp.parse_machine_readable_contract(body)
        assert result.ok is False
        assert result.reason == mp.REASON_NO_FENCE

    def test_structural_failures_fail_closed_multiple_fences(self):
        body = (
            "## Machine-Readable Contract\n\n"
            "```yaml\ncontract_schema_version: v1\n```\n\n"
            "```yaml\nchange_kind: docs\n```"
        )
        result = mp.parse_machine_readable_contract(body)
        assert result.ok is False
        assert result.reason == mp.REASON_MULTIPLE_FENCES

    def test_structural_failures_fail_closed_syntax_error(self):
        body = (
            "## Machine-Readable Contract\n\n"
            "```yaml\n"
            "contract_schema_version: v1\n"
            'change_kind: "unterminated\n'
            "```"
        )
        result = mp.parse_machine_readable_contract(body)
        assert result.ok is False
        assert result.reason == mp.REASON_YAML_ERROR

    def test_structural_failures_fail_closed_non_mapping_root(self):
        body = (
            "## Machine-Readable Contract\n\n"
            "```yaml\n"
            "- contract_schema_version: v1\n"
            "- change_kind: docs\n"
            "```"
        )
        result = mp.parse_machine_readable_contract(body)
        assert result.ok is False
        assert result.reason == mp.REASON_ROOT_NOT_MAPPING

    def test_valid_single_mrc_parses(self):
        result = mp.parse_machine_readable_contract(MRC_CODE)
        assert result.ok is True
        assert result.data["issue_kind"] == "implementation"
