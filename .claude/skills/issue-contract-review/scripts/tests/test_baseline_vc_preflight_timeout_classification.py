"""Issue #1892: VCP_TIMEOUT false-positive regression tests.

`classify_result()` in baseline_vc_preflight.py previously classified any
command whose stderr merely *contained* the substring "timeout" as a real
timeout. This misclassified fast, correctly-completed pytest baseline
failures (exit_code=4, "file or directory not found") whose test node-id
happened to include the word "timeout" (e.g.
`test_agy_real_subprocess_timeout_classified`) as `VCP_TIMEOUT` /
`human_judgment`, even though the command finished in milliseconds.
"""

import sys
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
        "test_agy_real_subprocess.py::test_agy_real_subprocess_timeout_classified"
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
    """Regression guard: a non-pytest exit_code=0-adjacent command whose real stderr
    happens to contain the substring "timeout" (but is not the exact run_command()
    sentinel) must not be classified as a real timeout.
    """
    classification, category, decision, fix_hint, scope_class = baseline_vc_preflight.classify_result(
        exit_code=4,
        stdout="",
        stderr='ERROR: file or directory not found: tests/test_request_timeout_handling.py::test_foo',
        command="uv run --locked pytest tests/test_request_timeout_handling.py::test_foo -q",
        cwd=str(_REPO_ROOT),
    )
    assert category != "timeout"
    assert decision != "blocked" or category == "expected_baseline_fail"


def test_contract_readiness_check_execute_mode_no_false_positive_vcp_timeout():
    """AC3: contract_readiness_check.py --mode execute (via run_baseline_vc_preflight(),
    a real subprocess invocation of baseline_vc_preflight.py) does not emit
    rule_id VCP_TIMEOUT for a fast, correctly-completed missing-file pytest VC
    whose test node-id contains the word "timeout".
    """
    sys.path.insert(0, str(_SCRIPTS_DIR))
    import contract_readiness_check  # noqa: E402

    node_id = (
        ".claude/skills/gemini-cli-headless-delegation/tests/"
        "test_agy_real_subprocess.py::test_agy_real_subprocess_timeout_classified"
    )
    body = (
        "## Verification Commands\n\n"
        "```bash\n"
        "# AC1\n"
        "# baseline-expect: fail\n"
        f"$ uv run --locked pytest {node_id} -q\n"
        "```\n"
    )

    preflight_result, exit_code = contract_readiness_check.run_baseline_vc_preflight(body)
    errors, aggregate_status = contract_readiness_check.map_preflight_result_to_errors(preflight_result)

    rule_ids = [e.get("rule_id") for e in errors]
    assert "VCP_TIMEOUT" not in rule_ids, f"unexpected VCP_TIMEOUT in errors: {errors}"
