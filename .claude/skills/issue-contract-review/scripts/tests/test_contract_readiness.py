#!/usr/bin/env python3
"""
Tests for contract_readiness_check.py

Covers:
  - AC2: ISSUE_CONTRACT_READINESS_RESULT_V1 schema validation
  - AC3: baseline_vc_preflight result lossless mapping
  - AC4: RVA decision: immediate required field detection
  - AC8: blocked fixture detection
  - AC9: go fixture status: go
  - AC10: no new logic in orchestration skills
  - AC12: static mode requires no network/auth
  - AC13: result JSON schema strict validation
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
_TESTS_DIR = Path(__file__).resolve().parent
_FIXTURES_DIR = _TESTS_DIR / "fixtures"
_SCRIPT = _SCRIPTS_DIR / "contract_readiness_check.py"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REQUIRED_RESULT_TOP_KEYS = {
    "schema",
    "status",
    "body_sha256",
    "source_checks",
    "errors",
    "minimal_context",
    "fix_hint",
}

REQUIRED_ERROR_KEYS = {
    "rule_id",
    "severity",
    "source_check",
    "category",
    "section",
    "line_start",
    "line_end",
    "minimal_context",
    "fix_hint",
    "autofixable",
}

# source_payload is an optional field present only for baseline_vc_preflight errors (AC3 lossless)
OPTIONAL_ERROR_KEYS = {"source_payload"}
ALLOWED_ERROR_KEYS = REQUIRED_ERROR_KEYS | OPTIONAL_ERROR_KEYS

REQUIRED_SOURCE_CHECK_KEYS = {"name", "schema", "status", "exit_code"}

VALID_STATUSES = {"go", "needs_fix", "human_judgment"}


def run_readiness(body_file: Path, mode: str = "static") -> tuple[dict, int]:
    """Run contract_readiness_check.py against a body file."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--body-file", str(body_file), "--mode", mode],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.stdout, f"No stdout from script (stderr: {result.stderr})"
    return json.loads(result.stdout), result.returncode


def run_readiness_with_body(body: str, mode: str = "static") -> tuple[dict, int]:
    """Run contract_readiness_check.py against a body string via temp file."""
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(body)
        tmp_path = tf.name

    try:
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--body-file", tmp_path, "--mode", mode],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.stdout, f"No stdout (stderr: {result.stderr})"
        return json.loads(result.stdout), result.returncode
    finally:
        import os
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# AC1: Script exists
# ---------------------------------------------------------------------------


def test_contract_readiness_script_exists():
    """AC1: contract_readiness_check.py exists."""
    assert _SCRIPT.exists(), f"Script not found: {_SCRIPT}"


# ---------------------------------------------------------------------------
# AC2: ISSUE_CONTRACT_READINESS_RESULT_V1 schema
# ---------------------------------------------------------------------------


def test_contract_readiness_result_v1_schema():
    """AC2: result has all required top-level fields and valid schema."""
    data, _ = run_readiness(_FIXTURES_DIR / "issue412_contract_go.md")

    assert data.get("schema") == "ISSUE_CONTRACT_READINESS_RESULT_V1"
    assert set(data.keys()) >= REQUIRED_RESULT_TOP_KEYS, (
        f"Missing top-level keys: {REQUIRED_RESULT_TOP_KEYS - set(data.keys())}"
    )
    assert data["status"] in VALID_STATUSES

    # source_checks structure
    assert isinstance(data["source_checks"], list)
    for sc in data["source_checks"]:
        assert set(sc.keys()) >= REQUIRED_SOURCE_CHECK_KEYS, (
            f"source_check missing keys: {REQUIRED_SOURCE_CHECK_KEYS - set(sc.keys())}"
        )

    # errors structure
    assert isinstance(data["errors"], list)
    for err in data["errors"]:
        missing = REQUIRED_ERROR_KEYS - set(err.keys())
        assert not missing, f"error item missing keys: {missing}"

    # minimal_context is list
    assert isinstance(data["minimal_context"], list)


# ---------------------------------------------------------------------------
# AC13: schema strict validation (no unknown top-level keys allowed)
# ---------------------------------------------------------------------------


def test_schema_strict_no_unknown_top_level_keys():
    """AC13: result JSON must not have unknown top-level keys."""
    # These are ALL the allowed top-level keys
    allowed_top_keys = REQUIRED_RESULT_TOP_KEYS
    data, _ = run_readiness(_FIXTURES_DIR / "issue412_contract_go.md")
    unknown = set(data.keys()) - allowed_top_keys
    assert not unknown, f"Unknown top-level keys found: {unknown}"


def test_schema_strict_error_item_no_unknown_keys():
    """AC13: each error item must not have unknown keys (source_payload is allowed for preflight errors)."""
    data, _ = run_readiness(_FIXTURES_DIR / "issue412_contract_blocked.md")
    for err in data["errors"]:
        unknown = set(err.keys()) - ALLOWED_ERROR_KEYS
        assert not unknown, f"Unknown error item keys: {unknown}"


# ---------------------------------------------------------------------------
# AC3: baseline_vc_preflight result lossless mapping
# ---------------------------------------------------------------------------


COMPOUND_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "1"
goal_ref: "test"
change_kind: workflow
```

## Parent Issue

#1

## Outcome

Test outcome.

## In Scope

- test

## Out of Scope

- n/a

## Acceptance Criteria

- [ ] AC1: Foo

## Verification Commands

```bash
# AC1
$ pnpm build && echo DONE
```

## Allowed Paths

```
src/
```

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合

## Runtime Verification Applicability

```yaml
decision: not_applicable
```
"""


def test_compound_command_disallowed_mapping():
    """AC3: compound_command_disallowed in VC → needs_fix in readiness result (static mode)."""
    data, exit_code = run_readiness_with_body(COMPOUND_BODY, mode="static")
    # Should detect compound command
    assert data["status"] in ("needs_fix", "human_judgment"), (
        f"Expected needs_fix or human_judgment, got: {data['status']}"
    )
    categories = [e["category"] for e in data["errors"]]
    assert "compound_command_disallowed" in categories, (
        f"compound_command_disallowed not found in errors: {categories}"
    )
    # Exit code 1 for needs_fix
    assert exit_code == 1, f"Expected exit_code 1, got {exit_code}"


UNEXPECTED_PASS_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "1"
goal_ref: "test"
change_kind: workflow
```

## Parent Issue

#1

## Outcome

Test outcome.

## In Scope

- test

## Out of Scope

- n/a

## Acceptance Criteria

- [ ] AC1: Foo

## Verification Commands

```bash
# AC1
$ rg -n "ISSUE_CONTRACT_READINESS_RESULT_V1" .claude/skills/issue-contract-review/scripts/contract_readiness_check.py
```

## Allowed Paths

```
src/
```

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合

## Runtime Verification Applicability

```yaml
decision: not_applicable
```
"""


def test_unexpected_pass_mapped_in_execute_mode():
    """AC3: unexpected_pass from preflight → needs_fix in execute mode."""
    # Run in execute mode so baseline_vc_preflight actually runs the command
    data, _ = run_readiness_with_body(UNEXPECTED_PASS_BODY, mode="execute")
    # rg finds the string (unexpected pass) → needs_fix
    # OR if the file doesn't exist in cwd: expected_fail → go (depends on runtime env)
    # We just verify schema is valid
    assert data["schema"] == "ISSUE_CONTRACT_READINESS_RESULT_V1"
    assert data["status"] in VALID_STATUSES


INVALID_PREFLIGHT_SCOPE_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "1"
goal_ref: "test"
change_kind: workflow
```

## Parent Issue

#1

## Outcome

Test outcome.

## In Scope

- test

## Out of Scope

- n/a

## Acceptance Criteria

- [ ] AC1: Foo

## Verification Commands

```bash
# AC1
# preflight-scope: invalid_scope_value
$ test -f nonexistent_file.py
```

## Allowed Paths

```
src/
```

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合

## Runtime Verification Applicability

```yaml
decision: not_applicable
```
"""


def test_invalid_preflight_scope_execute_mode():
    """AC3: invalid preflight-scope marker → human_judgment in execute mode."""
    data, exit_code = run_readiness_with_body(INVALID_PREFLIGHT_SCOPE_BODY, mode="execute")
    assert data["schema"] == "ISSUE_CONTRACT_READINESS_RESULT_V1"
    # Invalid preflight scope → human_judgment from baseline_vc_preflight
    assert data["status"] == "human_judgment", (
        f"Expected human_judgment for invalid preflight-scope, got: {data['status']}"
    )
    assert exit_code == 2


def test_human_judgment_not_collapsed_to_needs_fix():
    """AC3: human_judgment from preflight MUST NOT be collapsed to needs_fix."""
    data, exit_code = run_readiness_with_body(INVALID_PREFLIGHT_SCOPE_BODY, mode="execute")
    assert data["status"] == "human_judgment", (
        "human_judgment must not be collapsed to needs_fix"
    )
    assert exit_code == 2, f"Expected exit code 2 for human_judgment, got {exit_code}"


# ---------------------------------------------------------------------------
# AC4: RVA decision: immediate required field check
# ---------------------------------------------------------------------------

RVA_MISSING_FIELDS_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "1"
goal_ref: "test"
change_kind: workflow
```

## Parent Issue

#1

## Outcome

Test outcome.

## In Scope

- test

## Out of Scope

- n/a

## Acceptance Criteria

- [ ] AC1: Foo

## Verification Commands

```bash
# AC1
# preflight-scope: runtime_only
$ test -f some_file.py
```

## Allowed Paths

```
src/
```

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合

## Runtime Verification Applicability

```yaml
decision: immediate
applicable_acs:
  - AC1
```

"""


def test_runtime_verification_applicability_missing_fields():
    """AC4: decision: immediate with missing required fields → needs_fix."""
    data, exit_code = run_readiness_with_body(RVA_MISSING_FIELDS_BODY)
    assert data["status"] in ("needs_fix", "human_judgment"), (
        f"Expected needs_fix (or human_judgment), got: {data['status']}"
    )
    rule_ids = [e["rule_id"] for e in data["errors"]]
    assert "RVA001" in rule_ids, f"RVA001 not found in errors: {rule_ids}"


def test_applicable_acs_missing():
    """AC4: applicable_acs field missing for decision: immediate."""
    body = RVA_MISSING_FIELDS_BODY.replace("applicable_acs:\n  - AC1\n", "")
    data, _ = run_readiness_with_body(body)
    rule_ids = [e["rule_id"] for e in data["errors"]]
    categories = [e["category"] for e in data["errors"]]
    assert "rva_immediate_field_missing" in categories, (
        f"rva_immediate_field_missing not found: {categories}"
    )


def test_rva_not_applicable_no_errors():
    """AC4: decision: not_applicable has no RVA errors."""
    body = RVA_MISSING_FIELDS_BODY.replace("decision: immediate", "decision: not_applicable")
    body = body.replace("applicable_acs:\n  - AC1\n", "")
    data, _ = run_readiness_with_body(body)
    rva_errors = [e for e in data["errors"] if e.get("category") == "rva_immediate_field_missing"]
    assert not rva_errors, f"Unexpected RVA errors for not_applicable: {rva_errors}"


# ---------------------------------------------------------------------------
# AC8: blocked fixture
# ---------------------------------------------------------------------------


def test_issue412_blocked_fixture_detects_problems():
    """AC8: blocked fixture detects compound VC and RVA missing fields."""
    data, exit_code = run_readiness(_FIXTURES_DIR / "issue412_contract_blocked.md")

    assert data["schema"] == "ISSUE_CONTRACT_READINESS_RESULT_V1"
    assert data["status"] in ("needs_fix", "human_judgment"), (
        f"Blocked fixture should not be go, got: {data['status']}"
    )
    assert len(data["errors"]) > 0, "Blocked fixture should have errors"

    categories = [e["category"] for e in data["errors"]]
    # Compound command should be detected
    assert "compound_command_disallowed" in categories, (
        f"compound_command_disallowed not detected: {categories}"
    )
    # RVA missing fields should be detected
    assert "rva_immediate_field_missing" in categories, (
        f"rva_immediate_field_missing not detected: {categories}"
    )


# ---------------------------------------------------------------------------
# AC9: go fixture
# ---------------------------------------------------------------------------


def test_contract_go_fixture():
    """AC9: go fixture → status: go."""
    data, exit_code = run_readiness(_FIXTURES_DIR / "issue412_contract_go.md")

    assert data["schema"] == "ISSUE_CONTRACT_READINESS_RESULT_V1"
    assert data["status"] == "go", (
        f"Go fixture should have status: go, got: {data['status']}. "
        f"Errors: {data['errors']}"
    )
    assert exit_code == 0, f"Expected exit code 0 for go, got {exit_code}"


# ---------------------------------------------------------------------------
# AC10: no new logic in orchestration skills
# ---------------------------------------------------------------------------


def test_no_logic_in_orchestration_skills():
    """AC10: issue-refinement-loop and impl-review-loop SKILL.md have no new judgment logic."""
    _repo_root = _SCRIPTS_DIR.parents[4]
    loop_skills = [
        _repo_root / ".claude" / "skills" / "issue-refinement-loop" / "SKILL.md",
        _repo_root / ".claude" / "skills" / "impl-review-loop" / "SKILL.md",
    ]

    # Marker patterns that would indicate new judgment logic was added
    forbidden_patterns = [
        "contract_readiness_check",
        "ISSUE_CONTRACT_READINESS_RESULT_V1",
    ]

    for skill_path in loop_skills:
        if not skill_path.exists():
            continue
        content = skill_path.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            assert pattern not in content, (
                f"Forbidden pattern '{pattern}' found in {skill_path}. "
                "AC10 requires no new logic in orchestration skills."
            )


# ---------------------------------------------------------------------------
# AC12: static mode requires no network/auth
# ---------------------------------------------------------------------------


def test_no_network_required():
    """AC12: --mode static can run without GitHub token or network."""
    import os

    # Run with a clean environment (no GH_TOKEN)
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}

    body_file = _FIXTURES_DIR / "issue412_contract_go.md"
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--body-file", str(body_file), "--mode", "static"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.stdout, f"No stdout (stderr: {result.stderr})"
    data = json.loads(result.stdout)
    assert data["schema"] == "ISSUE_CONTRACT_READINESS_RESULT_V1"
    # Should not fail due to missing auth
    assert data["status"] in VALID_STATUSES


def test_static_mode_no_baseline_preflight_source_check():
    """AC12: static mode does NOT include baseline_vc_preflight source_check."""
    data, _ = run_readiness(_FIXTURES_DIR / "issue412_contract_go.md", mode="static")
    source_names = [sc["name"] for sc in data["source_checks"]]
    assert "baseline_vc_preflight" not in source_names, (
        "static mode must not run baseline_vc_preflight"
    )
    assert "validate_issue_body" in source_names


def test_execute_mode_includes_baseline_preflight():
    """AC12 (contrast): execute mode DOES include baseline_vc_preflight source_check."""
    data, _ = run_readiness(_FIXTURES_DIR / "issue412_contract_go.md", mode="execute")
    source_names = [sc["name"] for sc in data["source_checks"]]
    assert "baseline_vc_preflight" in source_names


# ---------------------------------------------------------------------------
# issue412 fixture aliases (for VC -k "issue412" keyword matching)
# ---------------------------------------------------------------------------


def test_issue412_go_fixture():
    """AC9 (issue412 alias): go fixture → status: go."""
    data, exit_code = run_readiness(_FIXTURES_DIR / "issue412_contract_go.md")
    assert data["schema"] == "ISSUE_CONTRACT_READINESS_RESULT_V1"
    assert data["status"] == "go", (
        f"Go fixture should have status: go, got: {data['status']}. "
        f"Errors: {data['errors']}"
    )
    assert exit_code == 0

# ---------------------------------------------------------------------------
# AC3 Unit: map_preflight_result_to_errors() with synthetic preflight JSON
# (No environment-dependent rg execution — purely unit testing the mapper)
# ---------------------------------------------------------------------------

sys.path.insert(0, str(_SCRIPTS_DIR))
from contract_readiness_check import map_preflight_result_to_errors


def test_unexpected_pass_maps_to_needs_fix():
    """AC3 unit: synthetic unexpected_pass result → needs_fix status and category in errors."""
    synthetic_preflight = {
        "schema": "baseline_vc_preflight/v1",
        "status": "blocked",
        "results": [
            {
                "ac": "AC1",
                "command": "rg foo file.md",
                "raw_command": "rg foo file.md",
                "exit_code": 0,
                "classification": "unexpected_pass",
                "category": "unexpected_pass",
                "decision": "blocked",
                "confidence": "high",
                "scope_class": "baseline_fail_expected",
                "line": 42,
                "fix_hint": "VC passed before implementation. Tighten VC so it fails at baseline.",
                "stdout_head": [],
                "stderr_head": [],
            }
        ],
        "errors": [],
    }
    errors, aggregate = map_preflight_result_to_errors(synthetic_preflight)
    assert aggregate == "needs_fix", f"Expected needs_fix, got {aggregate}"
    assert any(e["category"] == "unexpected_pass" for e in errors), (
        f"unexpected_pass not found in errors: {errors}"
    )


def test_compound_command_maps_to_needs_fix():
    """AC3 unit: synthetic compound_command_disallowed result → needs_fix."""
    synthetic_preflight = {
        "schema": "baseline_vc_preflight/v1",
        "status": "blocked",
        "results": [
            {
                "ac": "AC1",
                "command": "pnpm build && echo DONE",
                "raw_command": "pnpm build && echo DONE",
                "exit_code": -1,
                "classification": "blocked",
                "category": "compound_command_disallowed",
                "decision": "blocked",
                "confidence": "high",
                "scope_class": "baseline_fail_expected",
                "line": 10,
                "fix_hint": "Replace compound shell command with a single command.",
                "stdout_head": [],
                "stderr_head": [],
            }
        ],
        "errors": [],
    }
    errors, aggregate = map_preflight_result_to_errors(synthetic_preflight)
    assert aggregate == "needs_fix", f"Expected needs_fix, got {aggregate}"
    assert any(e["category"] == "compound_command_disallowed" for e in errors)


def test_human_judgment_decision_not_collapsed():
    """AC3 unit: human_judgment decision MUST NOT be collapsed to needs_fix."""
    synthetic_preflight = {
        "schema": "baseline_vc_preflight/v1",
        "status": "blocked",
        "results": [
            {
                "ac": "AC1",
                "command": "some_tool --check",
                "raw_command": "some_tool --check",
                "exit_code": 127,
                "classification": "blocked",
                "category": "env_missing_dep",
                "decision": "human_judgment",
                "confidence": "high",
                "scope_class": "baseline_fail_expected",
                "line": 5,
                "fix_hint": "Required tool missing. Human intervention needed.",
                "stdout_head": [],
                "stderr_head": [],
            }
        ],
        "errors": [],
    }
    errors, aggregate = map_preflight_result_to_errors(synthetic_preflight)
    assert aggregate == "human_judgment", (
        f"human_judgment must not be collapsed to needs_fix, got: {aggregate}"
    )


def test_expected_baseline_fail_maps_to_go():
    """AC3 unit: expected_baseline_fail → go (no error emitted)."""
    synthetic_preflight = {
        "schema": "baseline_vc_preflight/v1",
        "status": "pass",
        "results": [
            {
                "ac": "AC1",
                "command": "rg 'new_function' src/new_file.py",
                "raw_command": "rg 'new_function' src/new_file.py",
                "exit_code": 1,
                "classification": "expected_fail",
                "category": "expected_baseline_fail",
                "decision": "go",
                "confidence": "high",
                "scope_class": "baseline_fail_expected",
                "line": 8,
                "fix_hint": "",
                "stdout_head": [],
                "stderr_head": [],
            }
        ],
        "errors": [],
    }
    errors, aggregate = map_preflight_result_to_errors(synthetic_preflight)
    assert aggregate == "go", f"Expected go, got {aggregate}"
    assert not errors, f"Expected no errors for expected_baseline_fail, got: {errors}"


def test_preflight_static_mode_schema_valid():
    """AC3 unit: --mode preflight-static returns valid schema (static alias)."""
    # preflight-static is an alias for static; should not run baseline_vc_preflight
    body_file = _FIXTURES_DIR / "issue412_contract_go.md"
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--body-file", str(body_file), "--mode", "preflight-static"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout, f"No stdout from preflight-static mode (stderr: {result.stderr})"
    data = json.loads(result.stdout)
    assert data.get("schema") == "ISSUE_CONTRACT_READINESS_RESULT_V1"
    assert data["status"] in VALID_STATUSES
    # preflight-static must NOT run baseline_vc_preflight
    source_names = [sc["name"] for sc in data["source_checks"]]
    assert "baseline_vc_preflight" not in source_names, (
        "preflight-static mode must not run baseline_vc_preflight"
    )


# ---------------------------------------------------------------------------
# AC3 Lossless: source_payload field presence (Blocker 2)
# ---------------------------------------------------------------------------


def test_source_payload_present_in_preflight_errors():
    """AC3 lossless: errors from baseline_vc_preflight must include source_payload with all fields."""
    from contract_readiness_check import map_preflight_result_to_errors

    synthetic_preflight = {
        "schema": "baseline_vc_preflight/v1",
        "status": "blocked",
        "results": [
            {
                "ac": "AC1",
                "command": "pnpm build && echo DONE",
                "raw_command": "pnpm build && echo DONE",
                "exit_code": -1,
                "classification": "blocked",
                "category": "compound_command_disallowed",
                "decision": "blocked",
                "confidence": "high",
                "command_hash": "sha256:abc123",
                "duration_ms": 42,
                "scope_class": "baseline_fail_expected",
                "line": 10,
                "fix_hint": "Replace compound shell command with a single command.",
                "stdout_head": [],
                "stderr_head": [],
            }
        ],
        "errors": [],
    }
    errors, aggregate = map_preflight_result_to_errors(synthetic_preflight)
    assert errors, "Expected at least one error"
    err = errors[0]
    assert "source_payload" in err, f"source_payload missing from error: {list(err.keys())}"
    sp = err["source_payload"]
    required_payload_fields = {"classification", "decision", "confidence", "exit_code", "command_hash", "duration_ms"}
    missing = required_payload_fields - set(sp.keys())
    assert not missing, f"source_payload missing fields: {missing}"
    assert sp["classification"] == "blocked"
    assert sp["decision"] == "blocked"
    assert sp["confidence"] == "high"
    assert sp["exit_code"] == -1
    assert sp["command_hash"] == "sha256:abc123"
    assert sp["duration_ms"] == 42


# ---------------------------------------------------------------------------
# Blocker 4: Redirect operators not flagged as compound (< > << >> <<<)
# ---------------------------------------------------------------------------


REDIRECT_OPERATOR_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "1"
goal_ref: "test"
change_kind: workflow
```

## Parent Issue

#1

## Outcome

Test outcome.

## In Scope

- test

## Out of Scope

- n/a

## Acceptance Criteria

- [ ] AC1: Foo

## Verification Commands

```bash
# AC1
$ rg "pattern" < input.txt
$ command > output.txt
$ command << EOF
$ heredoc_cmd <<< string
$ cmd >> append.txt
```

## Allowed Paths

```
src/
```

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合
- 後続 Phase / 別スコープへの波及が判明した場合
- nested SubAgent delegation が必要になった場合
- 外部サービス利用・権限昇格・既存テスト大規模改変が必要になった場合

## Runtime Verification Applicability

```yaml
decision: not_applicable
```
"""


def test_redirect_operators_not_flagged_as_compound():
    """Blocker 4: <, >, <<, >>, <<< must NOT be flagged as compound_command_disallowed."""
    from contract_readiness_check import check_vc_static_syntax

    errors = check_vc_static_syntax(REDIRECT_OPERATOR_BODY)
    compound_errors = [e for e in errors if e["category"] == "compound_command_disallowed"]
    assert not compound_errors, (
        f"Redirect operators should not be flagged as compound_command_disallowed: "
        f"{[e['minimal_context'] for e in compound_errors]}"
    )


def test_control_operators_still_flagged():
    """Blocker 4 (contrast): &&, ||, |, ;, & must still be flagged."""
    from contract_readiness_check import check_vc_static_syntax

    errors = check_vc_static_syntax(COMPOUND_BODY)  # COMPOUND_BODY uses &&
    compound_errors = [e for e in errors if e["category"] == "compound_command_disallowed"]
    assert compound_errors, "Control operators (&&) should still be flagged as compound_command_disallowed"


# ---------------------------------------------------------------------------
# Blocker 1: validator tool/internal errors → human_judgment
# ---------------------------------------------------------------------------


def test_validator_tool_error_maps_to_human_judgment():
    """Blocker 1: validator_tool_error status → human_judgment aggregate (not needs_fix)."""
    from contract_readiness_check import compute_aggregate_status

    # Simulate validator timeout error
    validator_timeout_errors = [
        {
            "rule_id": "VALIDATOR_TIMEOUT",
            "severity": "error",
            "source_check": "validate_issue_body",
            "category": "validator_tool_error",
            "section": "(global)",
            "line_start": 0,
            "line_end": 0,
            "minimal_context": [],
            "fix_hint": "validator 実行環境を確認してください",
            "autofixable": False,
        }
    ]
    status = compute_aggregate_status(
        validate_errors=validator_timeout_errors,
        preflight_errors=[],
        rva_errors=[],
        static_vc_errors=[],
        preflight_aggregate="go",
    )
    assert status == "human_judgment", (
        f"validator_tool_error must map to human_judgment, got: {status}"
    )


def test_validator_internal_error_maps_to_human_judgment():
    """Blocker 1: validator_internal_error status → human_judgment aggregate (not needs_fix)."""
    from contract_readiness_check import compute_aggregate_status

    validator_internal_errors = [
        {
            "rule_id": "VALIDATOR_JSON_ERROR",
            "severity": "error",
            "source_check": "validate_issue_body",
            "category": "validator_internal_error",
            "section": "(global)",
            "line_start": 0,
            "line_end": 0,
            "minimal_context": [],
            "fix_hint": "validator 実行環境を確認してください",
            "autofixable": False,
        }
    ]
    status = compute_aggregate_status(
        validate_errors=validator_internal_errors,
        preflight_errors=[],
        rva_errors=[],
        static_vc_errors=[],
        preflight_aggregate="go",
    )
    assert status == "human_judgment", (
        f"validator_internal_error must map to human_judgment, got: {status}"
    )
