#!/usr/bin/env python3
"""Tests for scripts/pr_body_validator.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "pr_body_validator.py"
FIXTURE_DIR = Path(__file__).resolve().parents[2] / ".claude" / "skills" / "open-pr" / "scripts" / "tests" / "fixtures" / "pr_body"


def _run_cli(body: str, *, changed_paths_fixture: str = "non_safety_paths.txt", schema_change: str | None = None, linked_issue: int = 469) -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False) as body_file:
        body_file.write(body)
        body_path = body_file.name
    try:
        cmd = [sys.executable, str(SCRIPT_PATH), "--body-file", body_path, "--changed-paths-file", str(FIXTURE_DIR / changed_paths_fixture), "--linked-issue", str(linked_issue)]
        if schema_change:
            cmd.extend(["--schema-change", schema_change])
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_wrapper_emits_error_code_for_parse_failure():
    body = """## Summary

- test

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: not_schema_change

## Schema Consumer Inventory

N/A
reason: none

## Safety Claim Matrix

```yaml
# SAFETY_CLAIMS_V1
safety_claims: [
```

## Notes

- Related issue: #469
"""
    result = _run_cli(body, changed_paths_fixture="safety_paths.txt")
    assert result.returncode == 1
    assert "ERROR=E_SAFETY_CLAIMS_PARSE_ERROR" in result.stdout


def test_wrapper_schema_change_flag_mismatch():
    body = """## Summary

- test

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: not_schema_change

## Schema Consumer Inventory

N/A
reason: none

## Safety Claim Matrix

N/A
reason: docs only

## Notes

- Related issue: #469
"""
    result = _run_cli(body, schema_change="schema_change")
    assert result.returncode == 1
    assert "ERROR=E_SCHEMA_CHANGE_FLAG_MISMATCH" in result.stdout


def test_wrapper_success_path_outputs_json_only():
    body = (FIXTURE_DIR / "valid_not_schema_change.md").read_text(encoding="utf-8")
    result = _run_cli(body, linked_issue=330)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "pass"


def test_wrapper_emits_error_code_for_missing_safety_claim_matrix():
    body = """## Summary

- test

## Checks

- check

## Schema Change Applicability

- decision: not_schema_change

## Schema Consumer Inventory

N/A
reason: none

## Notes

- Related issue: #469
"""
    result = _run_cli(body, changed_paths_fixture="safety_paths.txt")
    assert result.returncode == 1
    assert "ERROR=E_SAFETY_CLAIM_MATRIX_MISSING" in result.stdout


def test_wrapper_schema_change_flag_requires_inventory_when_body_decision_invalid():
    body = """## Summary

- test

## Checks

- check

## Schema Change Applicability

- decision: maybe

## Schema Consumer Inventory

N/A
reason: none

## Safety Claim Matrix

N/A
reason: docs only

## Notes

- Related issue: #469
"""
    result = _run_cli(body, schema_change="schema_change")
    assert result.returncode == 1
    payload = json.loads(result.stdout.split("\nERROR=", 1)[0])
    rule_ids = {error["rule_id"] for error in payload["errors"]}
    assert "LP050" in rule_ids
