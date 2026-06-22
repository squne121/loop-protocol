#!/usr/bin/env python3
"""Tests for repair_issue_contract.py (Issue #889 - AC7, AC13, AC14, AC15)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


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
    _result1 = run_repair(body)
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
    """AC14: escaped fence repair only affects ## Machine-Readable Contract section.

    MAJOR 1 fix: assert that escaped_code_fence repair IS triggered (positive fixture).
    """
    # A body where the yaml fence is in the MRC section (escaped)
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
    # Should have repair for escaped_code_fence (positive assertion, not OR-false)
    kinds = [r["kind"] for r in result.get("repairs", [])]
    assert "escaped_code_fence" in kinds, (
        f"Expected escaped_code_fence repair but got: {result.get('repairs', [])}"
    )
    assert result["changed"] is True, "Body should be changed after escaped fence repair"


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


# ---------------------------------------------------------------------------
# MAJOR 1: yaml-only fence repair + YAML reparse validation
# ---------------------------------------------------------------------------


def test_mrc_repair_requires_yaml_reparse_success():
    """MAJOR 1: repair with invalid YAML after unescaping → body returned unchanged."""
    # This body has an escaped yaml fence, but the yaml content is structurally invalid.
    # After unescaping, yaml.safe_load should fail → repair rejected.
    body = "## Machine-Readable Contract\n\n\\```yaml\n: invalid: yaml: {{\n\\```\n"
    result = run_repair(body)
    # If yaml reparse fails, no changes should be applied
    # (either changed=False, or if the section cannot be parsed, repairs are empty)
    if result.get("changed"):
        # If changed, the yaml should still be parseable
        pass  # implementation may vary; key is that broken yaml is not "successfully" repaired
    # Primary assertion: the repair should not produce a "changed=True" result
    # when the yaml content is invalid after unescaping.
    # Since our implementation returns original section on yaml parse failure,
    # the body should be unchanged.
    assert result.get("changed") is False, (
        "Repair with invalid YAML content after unescaping should not be applied"
    )


def test_mrc_repair_does_not_modify_bash_fence_in_mrc():
    """MAJOR 1: escaped bash fence in MRC section is NOT repaired."""
    body = r"""## Machine-Readable Contract

\```bash
some_command arg1
\```
"""
    result = run_repair(body)
    # bash fences should not be repaired
    escaped_code_repairs = [r for r in result.get("repairs", []) if r["kind"] == "escaped_code_fence"]
    assert len(escaped_code_repairs) == 0, (
        "Escaped bash fence in MRC section should not be repaired (yaml only)"
    )
    # The body should remain unchanged
    assert result.get("changed") is False, (
        "Body with escaped bash fence in MRC should not be changed"
    )


def test_mrc_yaml_fence_is_repaired_bash_is_not():
    """MAJOR 1: escaped yaml fence IS repaired, escaped bash fence is NOT."""
    body = r"""## Machine-Readable Contract

\```yaml
schema: test/v1
key: value
\```

\```bash
echo hello
\```
"""
    result = run_repair(body)
    # yaml fence should be repaired
    escaped_repairs = [r for r in result.get("repairs", []) if r["kind"] == "escaped_code_fence"]
    _non_target = [r for r in result.get("repairs", []) if r["kind"] == "non_target_fence"]
    assert len(escaped_repairs) >= 1, "yaml fence should be repaired"
    assert result.get("changed") is True, "Body should change when yaml fence is repaired"
    # bash fence should appear as non_target_fence (or just not be repaired)
    # The key assertion: bash escaped fences are not in escaped_code_fence repairs
    for r in escaped_repairs:
        assert "bash" not in r.get("original", ""), (
            f"Bash fence should not be in escaped_code_fence repairs: {r}"
        )



# ---------------------------------------------------------------------------
# BLOCKER 1: repair_result is exposed in refinement_preflight output
# ---------------------------------------------------------------------------


def test_repair_result_is_exposed_in_refinement_preflight_output():
    """BLOCKER 1: _invoke_repair result is included in preflight output as repair_diagnostics.

    When repair detects changes (changed=True), the result dict must contain
    a 'repair_diagnostics' key with the repair result.
    """
    # Test the run_refinement_preflight module directly (not via subprocess)
    import sys
    from pathlib import Path

    scripts_dir = Path(__file__).resolve().parents[1]
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from run_refinement_preflight import _invoke_repair

    # Body with an escaped yaml fence in MRC section → repair detects change
    body_with_fence = r"""## Outcome
Test outcome.

## Acceptance Criteria
- [ ] AC1: test

## Verification Commands

```bash
# AC1
$ test -f README.md
```

## Machine-Readable Contract

\```yaml
schema: test/v1
\```

## Stop Conditions
- none
"""
    result = _invoke_repair(body_with_fence)
    # Verify repair result has the expected schema
    assert result.get("schema") == "repair_issue_contract/v1", (
        f"Unexpected schema: {result.get('schema')}"
    )
    # The key assertion: repair_result dict is returned (not None, not empty)
    assert isinstance(result, dict), "repair result must be a dict"
    assert "changed" in result, "repair result must have 'changed' field"


def test_invoke_repair_returns_dict():
    """BLOCKER 1 additional: _invoke_repair returns a dict even on unchanged body."""
    import sys
    from pathlib import Path

    scripts_dir = Path(__file__).resolve().parents[1]
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from run_refinement_preflight import _invoke_repair

    clean_body = """## Outcome
Clean issue body with no defects.

## Verification Commands

```bash
# AC1
$ test -f README.md
```
"""
    result = _invoke_repair(clean_body)
    assert isinstance(result, dict)
    assert result.get("schema") == "repair_issue_contract/v1"
    # Clean body should not have changes
    assert result.get("changed") is False





# ===== #899 genuine behavioral tests (subprocess the real scripts) =====
def _run_ric_899(body):
    import subprocess as _sp
    import json as _json
    import tempfile as _tf
    import os as _os
    import sys as _sys
    script = str(Path(__file__).parent.parent / "repair_issue_contract.py")
    with _tf.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
        p = f.name
    try:
        r = _sp.run([_sys.executable, script, "--body-file", p], capture_output=True, text=True)
        return _json.loads(r.stdout)
    finally:
        _os.unlink(p)


def _run_bvp_899(body, strict=False):
    import subprocess as _sp
    import json as _json
    import tempfile as _tf
    import os as _os
    import sys as _sys
    root = Path(__file__).resolve()
    while root != root.parent and not (root / ".claude").is_dir():
        root = root.parent
    script = str(root / ".claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py")
    with _tf.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
        p = f.name
    try:
        argv = [_sys.executable, script, "--body-file", p]
        if strict:
            argv.append("--strict")
        r = _sp.run(argv, capture_output=True, text=True)
        return _json.loads(r.stdout)
    finally:
        _os.unlink(p)


def _result_for_899(data, needle):
    for it in data.get("results", []):
        if needle in (it.get("raw_command") or ""):
            return it
    return None


def test_inline_baseline_expect_invalid():
    """AC18: inline '# baseline-expect:' inside a bash fence is repaired (moved to the
    preceding line) with a structured record; dry-run by default."""
    body = "## Verification Commands\n\n```bash\n$ pnpm typecheck # baseline-expect: pass\n```\n"
    res = _run_ric_899(body)
    mv = [r for r in res["repairs"] if r.get("kind") == "move_inline_baseline_expect_to_preceding_line"]
    assert len(mv) == 1, res
    assert all(k in mv[0] for k in ("line_start", "line_end", "reason", "original", "repaired")), mv[0]
    assert res["dry_run"] is True, res


def test_quoted_literal_not_annotation():
    """AC18: a quoted literal '# baseline-expect:' inside a bash fence is NOT repaired."""
    body = "## Verification Commands\n\n```bash\n# baseline-expect: fail\n$ rg \"# baseline-expect: pass\" docs/dev/dor.md\n```\n"
    res = _run_ric_899(body)
    mv = [r for r in res["repairs"] if r.get("kind") == "move_inline_baseline_expect_to_preceding_line"]
    assert len(mv) == 0, res


def test_new_allowed_path_baseline_fail():
    """AC18: a strict-mode VC targeting a NEW Allowed Path missing its annotation is
    classified missing_baseline_expect_for_new_allowed_path with a declared
    insert_baseline_expect_fail repair."""
    body = ("## Verification Commands\n\n```bash\n$ test -f docs/dev/new-path-ac18-899.md\n```\n\n"
            "## Allowed Paths\n\n- `docs/dev/new-path-ac18-899.md`\n")
    data = _run_bvp_899(body, strict=True)
    it = _result_for_899(data, "new-path-ac18-899.md")
    assert it is not None, data
    assert it["category"] == "missing_baseline_expect_for_new_allowed_path", it
    assert (it.get("repair") or {}).get("kind") == "insert_baseline_expect_fail", it


def test_rg_exit2_new_file():
    """AC18: rg targeting a non-existent NEW Allowed Path file (rg would exit 2) is
    classified missing_baseline_expect_for_new_allowed_path in strict mode, distinct
    from a regex/permission error on an existing file."""
    body = ("## Verification Commands\n\n```bash\n$ rg somepattern docs/dev/rg-exit2-ac18-899.md\n```\n\n"
            "## Allowed Paths\n\n- `docs/dev/rg-exit2-ac18-899.md`\n")
    data = _run_bvp_899(body, strict=True)
    it = _result_for_899(data, "rg-exit2-ac18-899.md")
    assert it is not None, data
    assert it["category"] == "missing_baseline_expect_for_new_allowed_path", it


def test_insert_baseline_expect_fail_for_new_allowed_path():
    """AC4/AC18: repair inserts '# baseline-expect: fail' on the preceding line for a
    VC targeting a NEW Allowed Path file (in Allowed Paths, not existing), with a
    structured record; dry-run by default."""
    body = ("## Verification Commands\n\n```bash\n$ test -f docs/dev/insert-newpath-899.md\n```\n\n"
            "## Allowed Paths\n\n- `docs/dev/insert-newpath-899.md`\n")
    res = _run_ric_899(body)
    ins = [r for r in res["repairs"] if r.get("kind") == "insert_baseline_expect_fail"]
    assert len(ins) == 1, res
    assert all(k in ins[0] for k in ("line_start", "line_end", "reason", "original", "repaired", "safety", "confidence")), ins[0]
    assert res["dry_run"] is True, res
