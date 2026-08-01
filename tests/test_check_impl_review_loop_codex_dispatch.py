from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "check_impl_review_loop_codex_dispatch.py"
spec = importlib.util.spec_from_file_location("check_impl_review_loop_codex_dispatch", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


def _write_config(tmp_path: Path, text: str) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(text, encoding="utf-8")
    return config_path


def _run_checker(config_path: Path, *assertions: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--config-path",
            str(config_path),
            *assertions,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_accepts_exact_multi_agent_v2_config(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        """[features.multi_agent_v2]
enabled = true
max_concurrent_threads_per_session = 4
""",
    )

    assert module.assert_project_declares_multi_agent_v2_enabled(config_path) == []
    assert module.assert_no_max_depth_setting(config_path) == []
    result = _run_checker(config_path, "--assert-project-multi-agent-v2-config")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("value", ["0", "3", "5", '\"4\"', "true"])
def test_rejects_non_integer_or_non_four_concurrency(tmp_path: Path, value: str):
    config_path = _write_config(
        tmp_path,
        """[features.multi_agent_v2]
enabled = true
max_concurrent_threads_per_session = %s
""" % value,
    )

    failures = module.assert_project_declares_multi_agent_v2_enabled(config_path)
    assert any("max_concurrent_threads_per_session must be strict integer 4" in failure for failure in failures)


@pytest.mark.parametrize("value", ["0", "1", "99", '\"1\"', "true"])
def test_rejects_any_max_depth_setting(tmp_path: Path, value: str):
    config_path = _write_config(
        tmp_path,
        """[features.multi_agent_v2]
enabled = true
max_concurrent_threads_per_session = 4

[agents]
max_depth = %s
""" % value,
    )

    failures = module.assert_no_max_depth_setting(config_path)
    assert failures == [".codex/config.toml: [agents].max_depth must be absent"]


def test_checker_accepts_injected_config_path(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        """[features.multi_agent_v2]
enabled = true
max_concurrent_threads_per_session = 4
""",
    )

    result = _run_checker(config_path, "--assert-project-multi-agent-v2-config")
    assert result.returncode == 0, result.stdout + result.stderr


def test_checker_requires_an_assertion_flag():
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert "specify at least one assertion flag" in result.stderr
    assert "OK:" not in result.stdout


def test_checker_rejects_malformed_toml_without_traceback(tmp_path: Path):
    config_path = _write_config(tmp_path, "[features.multi_agent_v2\nenabled = true\n")

    result = _run_checker(config_path, "--assert-project-multi-agent-v2-config")
    assert result.returncode == 1
    assert "malformed TOML" in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_checker_reports_missing_config_without_traceback(tmp_path: Path):
    config_path = tmp_path / "missing.toml"

    result = _run_checker(config_path, "--assert-project-multi-agent-v2-config")

    assert result.returncode == 1
    assert "TOML file not found" in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("text", "diagnostic"),
    [
        ('features = "string"\n', "[features] must be a table"),
        ("features = 1\n", "[features] must be a table"),
        ('features = ["array"]\n', "[features] must be a table"),
        ('[features]\nmulti_agent_v2 = "string"\n', "[features.multi_agent_v2] must be declared"),
    ],
)
def test_checker_rejects_wrong_shaped_toml_without_traceback(
    tmp_path: Path,
    text: str,
    diagnostic: str,
):
    config_path = _write_config(tmp_path, text)

    result = _run_checker(config_path, "--assert-project-multi-agent-v2-config")

    assert result.returncode == 1
    assert diagnostic in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_checker_rejects_wrong_shaped_agents_table_without_traceback(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        """agents = "string"

[features.multi_agent_v2]
enabled = true
max_concurrent_threads_per_session = 4
""",
    )

    result = _run_checker(config_path, "--assert-no-max-depth-setting")

    assert result.returncode == 1
    assert "[agents] must be a table" in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "text",
    [
        "[features]\nenabled = true\n",
        "[features.multi_agent_v2]\nenabled = false\nmax_concurrent_threads_per_session = 4\n",
        "[features.multi_agent_v2]\nenabled = \"true\"\nmax_concurrent_threads_per_session = 4\n",
        "[features.multi_agent_v2]\nenabled = true\n",
    ],
)
def test_rejects_missing_or_non_strict_v2_enabled_setting(tmp_path: Path, text: str):
    config_path = _write_config(tmp_path, text)

    failures = module.assert_project_declares_multi_agent_v2_enabled(config_path)
    assert failures


DISPATCH_SITE = ".claude/skills/impl-review-loop/steps/step-1-implementation.md"
DISPATCH_SITES = {
    DISPATCH_SITE: {
        "task_name_template": "implementation_i{iteration}",
        "agent_type": "implementation-worker",
        "message_binding_phrases": (
            "actual Issue number",
            "full Issue URL",
            "contract snapshot URL",
            "actual live Allowed Paths",
            "serialized fix_delta",
        ),
    }
}


def _write_dispatch_site(tmp_path: Path, text: str) -> None:
    path = tmp_path / DISPATCH_SITE
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")


def _valid_dispatch_block() -> str:
    return """```yaml
spawn_agent:
  task_name: implementation_i{iteration}
  agent_type: implementation-worker
  fork_turns: none
  message: |
    Objective: root materializes the actual Issue objective before spawn.
    Live reference: root binds the actual Issue number, full Issue URL, and contract snapshot URL before spawn.
    Bounded scope: root binds the actual live Allowed Paths and serialized fix_delta before spawn.
    Expected result: IMPLEMENT_RESULT_V1 with concrete execution facts.
```

See [Common Completion Protocol](#common-completion-protocol).
"""


def test_given_current_repository_when_native_v2_dispatch_contract_runs_then_it_passes():
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--assert-native-v2-dispatch-contract"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_given_current_repository_when_explicit_spawn_alias_runs_then_it_passes(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        """[features.multi_agent_v2]
enabled = true
max_concurrent_threads_per_session = 4
""",
    )

    result = _run_checker(config_path, "--assert-explicit-spawn-notes")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("replacement", "diagnostic"),
    [
        ("agent_type: test-runner", "agent_type must be 'implementation-worker'"),
        ("fork_turns: none", "fork_turns must be 'none'"),
        (
            "task_name: implementation_i{iteration}",
            "task_name must use materialization rule 'implementation_i{iteration}'",
        ),
    ],
)
def test_given_invalid_dispatch_field_when_static_contract_runs_then_it_rejects(
    tmp_path: Path,
    replacement: str,
    diagnostic: str,
):
    block = _valid_dispatch_block()
    if diagnostic == "fork_turns must be 'none'":
        block = block.replace(replacement, "fork_turns: all")
    elif "task_name must use materialization rule" in diagnostic:
        block = block.replace(replacement, "task_name: implementation-i0")
    else:
        block = block.replace("agent_type: implementation-worker", replacement)
    _write_dispatch_site(tmp_path, block)

    failures: list[str] = []
    module.assert_native_v2_dispatch_contract(
        failures,
        repo_root=tmp_path,
        dispatch_sites=DISPATCH_SITES,
    )

    assert any(diagnostic in failure for failure in failures)


def test_given_missing_fork_turns_when_static_contract_runs_then_it_rejects(tmp_path: Path):
    _write_dispatch_site(tmp_path, _valid_dispatch_block().replace("  fork_turns: none\n", ""))

    failures: list[str] = []
    module.assert_native_v2_dispatch_contract(
        failures,
        repo_root=tmp_path,
        dispatch_sites=DISPATCH_SITES,
    )

    assert any("spawn_agent keys must be exactly" in failure for failure in failures)


@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    [
        ("  unexpected_field: destructive\n", "spawn_agent keys must be exactly"),
        (
            "  task_name: implementation_i{iteration}\n",
            "malformed YAML (duplicate YAML key 'task_name')",
        ),
        ("  bad: [unclosed\n", "malformed YAML"),
        ("  task_name:\n    nested: implementation_i0\n", "task_name must use materialization rule"),
        ("  fork_turns: false\n", "fork_turns must be 'none'"),
    ],
)
def test_given_invalid_yaml_shape_when_static_contract_runs_then_it_rejects(
    tmp_path: Path,
    mutation: str,
    diagnostic: str,
):
    block = _valid_dispatch_block()
    if mutation.startswith("  task_name:\n"):
        block = block.replace("  task_name: implementation_i{iteration}\n", mutation)
    elif mutation.startswith("  fork_turns:"):
        block = block.replace("  fork_turns: none\n", mutation)
    else:
        block = block.replace("  agent_type: implementation-worker\n", mutation + "  agent_type: implementation-worker\n")
    _write_dispatch_site(tmp_path, block)

    failures: list[str] = []
    module.assert_native_v2_dispatch_contract(
        failures,
        repo_root=tmp_path,
        dispatch_sites=DISPATCH_SITES,
    )

    assert any(diagnostic in failure for failure in failures)


def test_given_retry_when_task_name_is_materialized_then_iteration_is_unique():
    assert module.materialize_task_name("implementation_i{iteration}", iteration=0) == "implementation_i0"
    assert module.materialize_task_name("implementation_i{iteration}", iteration=1) == "implementation_i1"


def test_given_stale_head_review_when_task_name_is_materialized_then_it_uses_next_iteration():
    assert module.materialize_task_name("pr_review_i{iteration}", iteration=1) == "pr_review_i1"


def test_given_cleanup_when_task_name_is_materialized_then_it_binds_actual_pr_number():
    task_name = module.materialize_task_name(
        "post_merge_cleanup_pr{merged_pr_number}_i{attempt}",
        merged_pr_number=1922,
        attempt=0,
    )
    assert task_name == "post_merge_cleanup_pr1922_i0"
    assert "pr1900" not in task_name


def test_given_reused_canonical_path_when_checked_then_it_is_rejected():
    used_task_names: set[str] = set()
    module.assert_unique_canonical_task_name("implementation_i0", used_task_names)

    with pytest.raises(ValueError, match="canonical task name already used"):
        module.assert_unique_canonical_task_name("implementation_i0", used_task_names)


def test_given_unresolved_symbolic_reference_when_static_contract_runs_then_it_rejects(
    tmp_path: Path,
):
    _write_dispatch_site(
        tmp_path,
        _valid_dispatch_block().replace("actual Issue number", "LOOP_STATE.issue_number"),
    )

    failures: list[str] = []
    module.assert_native_v2_dispatch_contract(
        failures,
        repo_root=tmp_path,
        dispatch_sites=DISPATCH_SITES,
    )

    assert any("concrete binding for 'actual Issue number'" in failure for failure in failures)
    assert any("unresolved reference 'LOOP_STATE'" in failure for failure in failures)


@pytest.mark.parametrize(
    "required_binding",
    [
        "actual Issue number",
        "PR number",
        "literal AC list",
        "literal Verification Commands",
        "contract body SHA",
        "diff head SHA",
    ],
)
def test_given_missing_concrete_verification_input_when_static_contract_runs_then_it_rejects(
    tmp_path: Path,
    required_binding: str,
):
    verification_site = ".claude/skills/impl-review-loop/steps/step-2-verification.md"
    text = (REPO_ROOT / verification_site).read_text(encoding="utf-8")
    _write_dispatch_site(tmp_path, text.replace(required_binding, "unresolved input", 1))
    verification_sites = {
        DISPATCH_SITE: {
            "task_name_template": "verification_i{iteration}",
            "agent_type": "test-runner",
            "message_binding_phrases": (
                "actual Issue number",
                "PR number",
                "contract body SHA",
                "diff head SHA",
                "literal AC list",
                "literal Verification Commands",
            ),
        }
    }

    failures: list[str] = []
    module.assert_native_v2_dispatch_contract(
        failures,
        repo_root=tmp_path,
        dispatch_sites=verification_sites,
    )

    assert any(f"concrete binding for '{required_binding}'" in failure for failure in failures)


@pytest.mark.parametrize("element", ["Objective", "Live reference", "Bounded scope", "Expected result"])
def test_given_missing_required_message_element_when_static_contract_runs_then_it_rejects(
    tmp_path: Path,
    element: str,
):
    block = "\n".join(
        line
        for line in _valid_dispatch_block().splitlines()
        if not line.startswith(f"    {element}:")
    )
    _write_dispatch_site(tmp_path, f"{block}\n")

    failures: list[str] = []
    module.assert_native_v2_dispatch_contract(
        failures,
        repo_root=tmp_path,
        dispatch_sites=DISPATCH_SITES,
    )

    assert any(f"message must include non-empty '{element}:'" in failure for failure in failures)


def test_given_duplicate_dispatch_blocks_when_static_contract_runs_then_it_rejects(tmp_path: Path):
    _write_dispatch_site(tmp_path, _valid_dispatch_block() * 2)

    failures: list[str] = []
    module.assert_native_v2_dispatch_contract(
        failures,
        repo_root=tmp_path,
        dispatch_sites=DISPATCH_SITES,
    )

    assert any("expected exactly one V2 dispatch block, found 2" in failure for failure in failures)


def test_given_legacy_phrase_only_when_static_contract_runs_then_it_rejects(tmp_path: Path):
    _write_dispatch_site(
        tmp_path,
        "Codex CLI: spawn the custom agent named implementation-worker for this step; the root thread must not.\n",
    )

    failures: list[str] = []
    module.assert_native_v2_dispatch_contract(
        failures,
        repo_root=tmp_path,
        dispatch_sites=DISPATCH_SITES,
    )

    assert any("expected exactly one V2 dispatch block, found 0" in failure for failure in failures)


def test_accepts_documented_v1_rollback_config(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        """[features.multi_agent_v2]
enabled = false
max_concurrent_threads_per_session = 4

[agents]
max_depth = 1
""",
    )

    assert module.assert_project_declares_multi_agent_v1_config(config_path) == []
    result = _run_checker(config_path, "--assert-project-multi-agent-v1-config")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("value", ["0", "2", '"1"', "true"])
def test_rejects_non_strict_v1_max_depth(tmp_path: Path, value: str):
    config_path = _write_config(
        tmp_path,
        """[features.multi_agent_v2]
enabled = false

[agents]
max_depth = %s
""" % value,
    )

    failures = module.assert_project_declares_multi_agent_v1_config(config_path)
    assert any("max_depth must be strict integer 1" in failure for failure in failures)
