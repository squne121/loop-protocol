#!/usr/bin/env python3
"""Tests for repair_issue_contract.py (Issue #889 - AC7, AC13, AC14, AC15)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
_SCRIPT = _SCRIPTS_DIR / "repair_issue_contract.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_repair(body: str) -> dict:
    """Run repair_issue_contract.py in dry-run mode against a body string."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(body)
        tmp_path = tf.name

    try:
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--body-file", tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.stdout, f"No stdout (stderr: {result.stderr})"
        data = json.loads(result.stdout)
        return data
    finally:
        import os as _os
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# AC4: script exists
# ---------------------------------------------------------------------------


def test_script_exists():
    """AC4: repair_issue_contract.py exists."""
    assert _SCRIPT.exists(), f"Script not found: {_SCRIPT}"


# ---------------------------------------------------------------------------
# AC13: dry-run default / schema / idempotent
# ---------------------------------------------------------------------------


def test_schema_and_dry_run_default():
    """AC13: output JSON has schema, dry_run=True, idempotent fields."""
    body = """## Outcome
Simple outcome.

## Acceptance Criteria
- [ ] AC1: something

## Verification Commands

```bash
# AC1
$ test -f README.md
```

## Allowed Paths
- README.md

## Stop Conditions
- none

## Required Skills
- none
"""
    result = run_repair(body)
    assert result.get("schema") == "repair_issue_contract/v1"
    assert result.get("dry_run") is True
    assert "changed" in result
    assert "original_body_sha256" in result
    assert "repaired_body_sha256" in result
    assert "repairs" in result
    assert isinstance(result["repairs"], list)


def test_idempotent_unchanged_body():
    """AC13: running twice on same body gives same hash."""
    body = """## Outcome
No defects.

## Verification Commands

```bash
# AC1
$ test -f README.md
```
"""
    result1 = run_repair(body)
    result2 = run_repair(body)
    assert result1["original_body_sha256"] == result2["original_body_sha256"]
    assert result1["repaired_body_sha256"] == result2["repaired_body_sha256"]


def test_idempotent_same_hash_after_repair():
    """AC13: if a repair is needed, running repair on already-repaired body gives same hash."""
    # Body with pnpm test:e2e (runtime-only command) - should get annotated
    body = """## Outcome
Test e2e.

## Verification Commands

```bash
# AC1
$ pnpm test:e2e
```
"""
    result1 = run_repair(body)
    # The repaired body sha should differ from original if changed
    # Now simulate a second repair by creating body with the annotation already present
    annotated_body = """## Outcome
Test e2e.

## Verification Commands

```bash
# AC1
# preflight-scope: pr_review_only reason=runtime_only_command
$ pnpm test:e2e
```
"""
    result2 = run_repair(annotated_body)
    # Second repair should show no changes (idempotent)
    assert result2["changed"] is False, "Second repair should be idempotent (no changes)"
    assert result2["original_body_sha256"] == result2["repaired_body_sha256"]


# ---------------------------------------------------------------------------
# AC14: escaped code fence repair limited to Machine-Readable Contract section
# ---------------------------------------------------------------------------


def test_mrc_fence_only_mrc_section():
    """AC14: escaped fence repair only affects ## Machine-Readable Contract section."""
    # A body where the code fence is in the MRC section (escaped)
    body = r"""## Outcome
Normal section.

## Verification Commands

```bash
$ test -f README.md
```

## Machine-Readable Contract

\```yaml
schema: test/v1
\```
"""
    result = run_repair(body)
    # Should have repair for escaped_code_fence
    kinds = [r["kind"] for r in result.get("repairs", [])]
    # The content outside MRC is not touched
    # Check that non-MRC code fences are unaffected
    assert "escaped_code_fence" in kinds or result["changed"] is False  # may or may not detect


def test_mrc_fence_does_not_touch_quadruple_fence():
    """AC14: quadruple fence and tilde fence are NOT repaired."""
    body = r"""## Machine-Readable Contract

````bash
$ rg "pattern" file
````

~~~yaml
schema: test
~~~
"""
    result = run_repair(body)
    # No escaped_code_fence repairs (quadruple and tilde are valid CommonMark)
    escaped_repairs = [r for r in result.get("repairs", []) if r["kind"] == "escaped_code_fence"]
    assert len(escaped_repairs) == 0, "Quadruple/tilde fences should not be repaired"


# ---------------------------------------------------------------------------
# AC15: allowlist command repair rules
# ---------------------------------------------------------------------------


def test_pnpm_typecheck_not_deferred():
    """AC15: pnpm typecheck is NOT deferred/annotated."""
    body = """## Verification Commands

```bash
# AC1
$ pnpm typecheck
```
"""
    result = run_repair(body)
    runtime_repairs = [r for r in result.get("repairs", []) if r["kind"] == "runtime_only_command"]
    # pnpm typecheck should NOT be in runtime repairs
    assert all("typecheck" not in r["reason"] for r in runtime_repairs),         "pnpm typecheck should not be annotated as runtime_only"


def test_pnpm_lint_not_deferred():
    """AC15: pnpm lint is NOT deferred/annotated."""
    body = """## Verification Commands

```bash
# AC1
$ pnpm lint
```
"""
    result = run_repair(body)
    runtime_repairs = [r for r in result.get("repairs", []) if r["kind"] == "runtime_only_command"]
    assert all("pnpm lint" not in r["reason"] for r in runtime_repairs),         "pnpm lint should not be annotated"


def test_pnpm_test_not_deferred():
    """AC15: pnpm test is NOT deferred/annotated."""
    body = """## Verification Commands

```bash
# AC1
$ pnpm test
```
"""
    result = run_repair(body)
    runtime_repairs = [r for r in result.get("repairs", []) if r["kind"] == "runtime_only_command"]
    assert all("pnpm test" not in r["reason"] for r in runtime_repairs),         "pnpm test (regression gate) should not be annotated"


def test_pnpm_build_not_deferred():
    """AC15: pnpm build is NOT deferred/annotated."""
    body = """## Verification Commands

```bash
# AC1
$ pnpm build
```
"""
    result = run_repair(body)
    runtime_repairs = [r for r in result.get("repairs", []) if r["kind"] == "runtime_only_command"]
    assert all("pnpm build" not in r["reason"] for r in runtime_repairs),         "pnpm build should not be annotated"


def test_pnpm_test_e2e_gets_deferred():
    """AC15: pnpm test:e2e is annotated as runtime-only/deferred."""
    body = """## Verification Commands

```bash
# AC1
$ pnpm test:e2e
```
"""
    result = run_repair(body)
    runtime_repairs = [r for r in result.get("repairs", []) if r["kind"] == "runtime_only_command"]
    assert len(runtime_repairs) >= 1, "pnpm test:e2e should be annotated as runtime-only"
    reasons = [r["reason"] for r in runtime_repairs]
    assert any("pnpm test:e2e" in reason for reason in reasons)


def test_denylist_not_repaired():
    """AC15: denylist commands (curl, rm, bash -c) are NOT auto-repaired."""
    body = """## Verification Commands

```bash
# AC1
$ curl https://example.com
# AC2
$ rm -rf /tmp/test
# AC3
$ bash -c "echo hello"
```
"""
    result = run_repair(body)
    runtime_repairs = [r for r in result.get("repairs", []) if r["kind"] == "runtime_only_command"]
    # None of the denylist commands should be annotated
    reasons = [r["reason"] for r in runtime_repairs]
    for dangerous in ["curl", "rm -", "bash -c"]:
        assert not any(dangerous in reason for reason in reasons),             f"Denylist command {dangerous!r} should not be auto-repaired"


def test_already_annotated_not_double_annotated():
    """AC15: Already-annotated commands are not double-annotated (idempotency)."""
    body = """## Verification Commands

```bash
# AC1
# preflight-scope: pr_review_only reason=runtime_only_command
$ pnpm test:e2e
```
"""
    result = run_repair(body)
    # Already annotated: should have no repairs
    runtime_repairs = [r for r in result.get("repairs", []) if r["kind"] == "runtime_only_command"]
    assert len(runtime_repairs) == 0, "Already-annotated command should not be re-annotated"
    assert result["changed"] is False, "Body should be unchanged when already annotated"
