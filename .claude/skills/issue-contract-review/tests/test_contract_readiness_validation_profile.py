"""Regression coverage for existing-issue validation profile selection (#1844)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent
_SKILL_DIR = _HERE.parent
_SCRIPT_PATH = _SKILL_DIR / "scripts" / "contract_readiness_check.py"
_GO_FIXTURE = _SKILL_DIR / "scripts" / "tests" / "fixtures" / "issue412_contract_go.md"

_SPEC = importlib.util.spec_from_file_location("contract_readiness_check_1844", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_CRC = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CRC)  # type: ignore[union-attr]


def _mrc(issue_kind: str) -> str:
    return f"""## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: {issue_kind}
goal_ref: validation profile regression
change_kind: workflow
```
"""


def _parent_body() -> str:
    return _mrc("parent") + """
## Summary

Parent tracker summary.

## Goal

Keep parent edits safe.

## Desired Destination

Safe parent patching.

## Current Validated Scope

Readiness selection only.

## Decisions Fixed

- 2026-07-29: use the parent profile only for a strictly parsed parent MRC.

## Quality Decision Record

N/A

## Parent Closure Rule

- Close after the child work is complete.

## Child Issues

- [ ] #1

## Remaining Parent Gaps

- [ ] none

## Phase Handoff Contract

- Parent handoff is tracked here.

## Acceptance Criteria

- [ ] AC1: parent validation can run.
"""


def _missing_implementation_sections(issue_kind: str) -> str:
    return _mrc(issue_kind) + """
## Acceptance Criteria

- [ ] AC1: preserve the legacy fallback.
"""


def _lp001_missing_sections(result: dict) -> set[str]:
    return {
        error["fix_hint"]
        for error in result["errors"]
        if error.get("rule_id") == "LP001"
    }


def test_parent_profile_forwards_only_parent_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIVEN a valid parent MRC WHEN readiness runs THEN it invokes only `--kind parent`."""
    captured: list[list[str]] = []

    class _Completed:
        stdout = json.dumps({"schema": "loop_body_lint/v1", "status": "pass", "errors": []})
        stderr = ""

    def _run(args: list[str], **_kwargs: object) -> _Completed:
        captured.append(args)
        return _Completed()

    monkeypatch.setattr(_CRC.subprocess, "run", _run)

    assert _CRC.run_validate_issue_body(_parent_body())["status"] == "pass"
    assert captured[0][-2:] == ["--kind", "parent"]


def test_parent_kind_no_lp001_for_implementation_only_sections() -> None:
    """GIVEN a parent body WHEN validated THEN missing VC/Allowed Paths are not LP001."""
    result = _CRC.run_validate_issue_body(_parent_body())

    assert "Add '## Verification Commands' section to the Issue body." not in _lp001_missing_sections(result)
    assert "Add '## Allowed Paths' section to the Issue body." not in _lp001_missing_sections(result)


@pytest.mark.parametrize(
    "body",
    [
        _missing_implementation_sections("implementation"),
        _missing_implementation_sections("future-kind"),
        "## Machine-Readable Contract\n\n```yaml\nissue_kind: parent\nissue_kind: implementation\n```\n",
    ],
    ids=["implementation", "unknown_kind", "mrc_parse_failure"],
)
def test_fallback_not_regressed_without_supported_profile(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """GIVEN an unsupported or invalid MRC WHEN readiness runs THEN legacy no-kind LP001 remains."""
    captured: list[list[str]] = []
    original_run = _CRC.subprocess.run

    def _run(args: list[str], **kwargs: object):
        captured.append(args)
        return original_run(args, **kwargs)

    monkeypatch.setattr(_CRC.subprocess, "run", _run)
    result = _CRC.run_validate_issue_body(body)

    assert "--kind" not in captured[0]
    missing_sections = _lp001_missing_sections(result)
    assert "Add '## Verification Commands' section to the Issue body." in missing_sections
    assert "Add '## Allowed Paths' section to the Issue body." in missing_sections


def test_existing_go_fixture_keeps_kind_agnostic_rdr_behavior() -> None:
    """GIVEN the legacy implementation go fixture WHEN checked THEN it stays go without RDR LP001."""
    body = _GO_FIXTURE.read_text(encoding="utf-8")
    validate_result = _CRC.run_validate_issue_body(body)
    readiness = _CRC.build_result(body, "static", validate_result, None, None)

    assert validate_result["status"] == "pass"
    assert readiness["status"] == "go"
    assert not any(error["rule_id"] == "RDR001" for error in readiness["errors"])
