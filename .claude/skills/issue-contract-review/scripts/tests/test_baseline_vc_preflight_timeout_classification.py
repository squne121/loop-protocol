"""Issue #1892: VCP_TIMEOUT false-positive regression tests.

`classify_result()` in baseline_vc_preflight.py previously classified any
command whose stderr merely *contained* the substring "timeout" as a real
timeout. This misclassified fast, correctly-completed pytest baseline
failures (exit_code=4, "file or directory not found") whose test node-id
happened to include the word "timeout" (e.g.
`test_agy_timeout_baseline_fixture_missing`) as `VCP_TIMEOUT` /
`human_judgment`, even though the command finished in milliseconds.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import baseline_vc_preflight  # noqa: E402

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _SCRIPTS_DIR.parents[3]


def test_classify_result_real_timeout_sentinel_still_blocked():
    """AC1: a genuine subprocess.TimeoutExpired sentinel is still classified as timeout/blocked."""
    classification, category, decision, fix_hint, scope_class = baseline_vc_preflight.classify_result(
        exit_code=-1,
        stdout="",
        stderr="timeout",
        command='rg -n "pattern" some_file.py',
    )
    assert classification == "blocked"
    assert category == "timeout"
    assert decision == "blocked"
    assert fix_hint == "Command exceeded timeout"


def test_classify_result_pytest_missing_file_with_timeout_in_test_name_not_misclassified():
    """AC2: a fast exit_code=4 file-not-found failure is not misclassified as timeout
    merely because the (not-yet-created) test node-id contains the word "timeout".
    """
    node_id = (
        ".claude/skills/gemini-cli-headless-delegation/tests/"
        "test_agy_timeout_baseline_fixture.py::test_agy_timeout_baseline_fixture_missing"
    )
    command = f"uv run --locked pytest {node_id} -q"
    stderr = f"ERROR: file or directory not found: {node_id}"

    classification, category, decision, fix_hint, scope_class = baseline_vc_preflight.classify_result(
        exit_code=4,
        stdout="\nno tests ran in 0.01s\n",
        stderr=stderr,
        command=command,
        cwd=str(_REPO_ROOT),
    )
    assert classification == "expected_fail"
    assert category == "expected_baseline_fail"
    assert decision == "go"
    assert category != "timeout"


def test_classify_result_non_pytest_stderr_containing_timeout_word_not_misclassified():
    """Regression guard: a genuinely non-pytest command whose real stderr happens to
    contain the substring "timeout" (but is not the exact run_command() sentinel,
    exit_code == -1 and stderr.strip() == "timeout") must not be classified as a
    real timeout, and must resolve to the specific file_not_found_unrunnable
    category (the referenced script does not exist).
    """
    classification, category, decision, fix_hint, scope_class = baseline_vc_preflight.classify_result(
        exit_code=2,
        stdout="",
        stderr=(
            "python3: can't open file 'missing_timeout_script.py': "
            "[Errno 2] No such file or directory"
        ),
        command="python3 missing_timeout_script.py",
    )
    assert classification == "blocked"
    assert category == "file_not_found_unrunnable"
    assert decision == "blocked"


_TIMEOUT_NODE_ID = (
    ".claude/skills/gemini-cli-headless-delegation/tests/"
    "test_agy_timeout_baseline_fixture.py::test_agy_timeout_baseline_fixture_missing"
)

_VC_ONLY_BODY = (
    "## Verification Commands\n\n"
    "```bash\n"
    "# AC1\n"
    "# baseline-expect: fail\n"
    f"$ uv run --locked pytest {_TIMEOUT_NODE_ID} -q\n"
    "```\n"
)

_FULL_ISSUE_BODY = f"""\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "1892"
goal_ref: "test"
change_kind: workflow
```

## Parent Issue

#1892

## Outcome

Test outcome for VCP_TIMEOUT false-positive regression coverage.

## In Scope

- test

## Out of Scope

- n/a

## Acceptance Criteria

- [ ] AC1: Foo

## Verification Commands

```bash
# AC1
# baseline-expect: fail
$ uv run --locked pytest {_TIMEOUT_NODE_ID} -q
```

## Allowed Paths

```
.claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py
```

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合
- In Scope の固定契約の変更が必要になった場合
- 新規 Issue の起票が必要と判断した場合

## Runtime Verification Applicability

```yaml
decision: not_applicable
reason: >
  RVA section was missing in fixture and caused false-positive failure.
  This contract uses this VC only to validate baseline timeout false-positive
  classification and does not require runtime verification.
```
"""


def test_contract_readiness_check_execute_mode_no_false_positive_vcp_timeout():
    """AC3: contract_readiness_check.py --mode execute (via run_baseline_vc_preflight(),
    a real subprocess invocation of baseline_vc_preflight.py) does not emit
    rule_id VCP_TIMEOUT for a fast, correctly-completed missing-file pytest VC
    whose test node-id contains the word "timeout", and the underlying
    preflight result is deterministically classified as an expected baseline
    fail (go), not merely "anything but VCP_TIMEOUT".
    """
    sys.path.insert(0, str(_SCRIPTS_DIR))
    import contract_readiness_check  # noqa: E402

    preflight_result, exit_code = contract_readiness_check.run_baseline_vc_preflight(_VC_ONLY_BODY)
    errors, aggregate_status = contract_readiness_check.map_preflight_result_to_errors(preflight_result)

    assert exit_code == 0
    assert preflight_result["status"] == "pass"
    assert len(preflight_result["results"]) == 1
    result = preflight_result["results"][0]
    assert (result["classification"], result["category"], result["decision"]) == (
        "expected_fail",
        "expected_baseline_fail",
        "go",
    )
    assert errors == []
    assert aggregate_status == "go"

    rule_ids = [e.get("rule_id") for e in errors]
    assert "VCP_TIMEOUT" not in rule_ids, f"unexpected VCP_TIMEOUT in errors: {errors}"


def test_contract_readiness_check_cli_execute_mode_no_false_positive_vcp_timeout():
    """AC3 (CLI integration): the full contract_readiness_check.py CLI, invoked as a
    subprocess in --mode execute against a complete valid Issue body, exits 0 with
    a final status of "go", no errors, and a "pass" baseline_vc_preflight
    source_check, for a missing-file pytest VC whose node-id contains "timeout".
    """
    script = _SCRIPTS_DIR / "contract_readiness_check.py"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(_FULL_ISSUE_BODY)
        tmp_path = tf.name

    try:
        result = subprocess.run(
            [sys.executable, str(script), "--body-file", tmp_path, "--mode", "execute"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (
            f"expected exit_code 0, got {result.returncode}; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        data = json.loads(result.stdout)
        assert data["status"] == "go"
        assert data["errors"] == []
        source_check_by_name = {sc["name"]: sc for sc in data["source_checks"]}
        assert source_check_by_name["baseline_vc_preflight"]["status"] == "pass"
        rule_ids = [e.get("rule_id") for e in data["errors"]]
        assert "VCP_TIMEOUT" not in rule_ids
    finally:
        os.unlink(tmp_path)
