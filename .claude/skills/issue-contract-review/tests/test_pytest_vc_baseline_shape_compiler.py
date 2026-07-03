"""
Tests for vc_baseline_shape_compiler.py (Issue #1285)

Covers:
  AC1: existing_test_file.py -k test_new_name -> rewrite hint (bare identifier only)
  AC2: existing_test_file.py::test_missing_name -> rewrite hint (simple function node-id only)
  AC3: complex -k expression / Allowed Paths outside -> not_autofixable
  AC4: missing_new_test_file.py::test_name -> already_canonical
  AC5: issue_contract_hygiene_autofix.py wiring + sha256 no_change on 2nd run
"""

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

COMPILER_SCRIPT = Path(__file__).parent.parent / "scripts" / "vc_baseline_shape_compiler.py"
AUTOFIX_SCRIPT = (
    Path(__file__).parent.parent.parent / "edit-issue" / "scripts" / "issue_contract_hygiene_autofix.py"
)


def _load_compiler():
    spec = importlib.util.spec_from_file_location("vc_baseline_shape_compiler", COMPILER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_body(vc_bash_block: str, allowed_paths: list[str]) -> str:
    allowed_paths_block = "\n".join(f"- {p}" for p in allowed_paths)
    return (
        "## Verification Commands\n"
        "\n"
        "```bash\n"
        f"{vc_bash_block}\n"
        "```\n"
        "\n"
        "## Allowed Paths\n"
        f"{allowed_paths_block}\n"
    )


# ---------------------------------------------------------------------------
# AC1: -k new test name on existing file -> rewrite
# ---------------------------------------------------------------------------


def test_ac1_dash_k_new_test_name_rewritten(tmp_path: Path):
    """GIVEN existing_test_file.py -k test_new_name (bare identifier, not in file) /
    WHEN compiled / THEN a missing-file node-id rewrite hint is produced."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")

    candidate = "some_dir/test_existing_new_test.py"
    body = _make_body(
        vc_bash_block="$ pytest some_dir/test_existing.py -k test_new_name",
        allowed_paths=["some_dir/test_existing.py", candidate],
    )

    result = compiler.compile_body(body, tmp_path)

    assert result["schema"] == "vc_baseline_shape_compiler/v1"
    assert result["status"] == "changed"
    assert len(result["rewrites"]) == 1
    rw = result["rewrites"][0]
    assert rw["reason_code"] == "pytest_dash_k_new_test_on_existing_file"
    assert candidate in rw["suggested_command"]
    assert "::test_new_name" in rw["suggested_command"]
    assert "-k" not in rw["suggested_command"]


def test_ac1_dash_k_on_existing_test_name_is_not_rewritten(tmp_path: Path):
    """GIVEN -k selects a test name that already exists in the file /
    WHEN compiled / THEN no rewrite is produced (out of scope, not a baseline-fail shape)."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")

    body = _make_body(
        vc_bash_block="$ pytest some_dir/test_existing.py -k test_alpha",
        allowed_paths=["some_dir/test_existing.py"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "already_canonical"
    assert result["rewrites"] == []


# ---------------------------------------------------------------------------
# AC2: missing node-id on existing file -> rewrite
# ---------------------------------------------------------------------------


def test_ac2_missing_node_id_on_existing_file_rewritten(tmp_path: Path):
    """GIVEN existing_test_file.py::test_missing_name (simple function, not in file, verified
    via ast.parse) / WHEN compiled / THEN a missing-file node-id rewrite hint is produced."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")

    candidate = "some_dir/test_existing_new_test.py"
    body = _make_body(
        vc_bash_block="$ pytest some_dir/test_existing.py::test_missing_name",
        allowed_paths=["some_dir/test_existing.py", candidate],
    )

    result = compiler.compile_body(body, tmp_path)

    assert result["status"] == "changed"
    assert len(result["rewrites"]) == 1
    rw = result["rewrites"][0]
    assert rw["reason_code"] == "pytest_missing_node_id_on_existing_file"
    assert rw["suggested_command"].endswith(f"{candidate}::test_missing_name")


def test_ac2_existing_node_id_is_not_rewritten(tmp_path: Path):
    """GIVEN existing_test_file.py::test_alpha where test_alpha genuinely exists /
    WHEN compiled / THEN no rewrite (out of scope)."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    assert True\n", encoding="utf-8")

    body = _make_body(
        vc_bash_block="$ pytest some_dir/test_existing.py::test_alpha",
        allowed_paths=["some_dir/test_existing.py"],
    )
    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "already_canonical"
    assert result["rewrites"] == []


# ---------------------------------------------------------------------------
# AC3: not_autofixable — complex -k expression / complex node-id
# ---------------------------------------------------------------------------


def test_ac3_not_autofixable_complex_expression(tmp_path: Path):
    """GIVEN a boolean -k expression (`test_a or test_b`) on an existing file /
    WHEN compiled / THEN not_autofixable (body untouched, no rewrite)."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_a():\n    pass\n\n\ndef test_b():\n    pass\n", encoding="utf-8")

    body = _make_body(
        vc_bash_block='$ pytest some_dir/test_existing.py -k "test_a or test_b"',
        allowed_paths=["some_dir/test_existing.py"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "not_autofixable"
    assert result["rewrites"] == []
    assert len(result["warnings"]) == 1
    assert "pytest_dash_k_complex_expression" in result["warnings"][0]


def test_ac3_not_autofixable_class_selector_node_id(tmp_path: Path):
    """GIVEN a class::method node-id on an existing file /
    WHEN compiled / THEN not_autofixable (class/method selectors are out of scope)."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text(
        "class TestFoo:\n    def test_bar(self):\n        pass\n", encoding="utf-8"
    )

    body = _make_body(
        vc_bash_block="$ pytest some_dir/test_existing.py::TestFoo::test_bar_missing",
        allowed_paths=["some_dir/test_existing.py"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "not_autofixable"
    assert result["rewrites"] == []


def test_ac3_not_autofixable_parametrized_node_id(tmp_path: Path):
    """GIVEN a parametrized node-id ([...]) on an existing file /
    WHEN compiled / THEN not_autofixable."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    pass\n", encoding="utf-8")

    body = _make_body(
        vc_bash_block="$ pytest some_dir/test_existing.py::test_alpha[case1]",
        allowed_paths=["some_dir/test_existing.py"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "not_autofixable"
    assert result["rewrites"] == []


def test_ac3_not_autofixable_when_no_safe_candidate_in_allowed_paths(tmp_path: Path):
    """GIVEN a rewritable shape but no candidate missing-file path listed in Allowed Paths /
    WHEN compiled / THEN not_autofixable (never invent a path outside Allowed Paths)."""
    compiler = _load_compiler()

    pkg_dir = tmp_path / "some_dir"
    pkg_dir.mkdir()
    existing = pkg_dir / "test_existing.py"
    existing.write_text("def test_alpha():\n    pass\n", encoding="utf-8")

    body = _make_body(
        vc_bash_block="$ pytest some_dir/test_existing.py -k test_new_name",
        allowed_paths=["some_dir/test_existing.py"],  # no missing-file candidate listed
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "not_autofixable"
    assert result["rewrites"] == []


# ---------------------------------------------------------------------------
# AC4: missing_new_test_file.py::test_name -> already_canonical
# ---------------------------------------------------------------------------


def test_ac4_missing_file_node_id_already_canonical(tmp_path: Path):
    """GIVEN missing_new_test_file.py::test_name where the file does not exist on baseline /
    WHEN compiled / THEN already_canonical (no rewrite, hygiene autofix is a no-op)."""
    compiler = _load_compiler()

    body = _make_body(
        vc_bash_block="$ pytest some_dir/missing_new_test_file.py::test_name",
        allowed_paths=["some_dir/missing_new_test_file.py"],
    )

    result = compiler.compile_body(body, tmp_path)
    assert result["status"] == "already_canonical"
    assert result["rewrites"] == []
    assert result["warnings"] == []


def test_invalid_input_missing_vc_section(tmp_path: Path):
    """GIVEN a body without ## Verification Commands / WHEN compiled / THEN invalid_input."""
    compiler = _load_compiler()
    result = compiler.compile_body("## Outcome\nsomething\n", tmp_path)
    assert result["status"] == "invalid_input"
    assert result["errors"]


# ---------------------------------------------------------------------------
# AC5: issue_contract_hygiene_autofix.py wiring + sha256 no_change on rerun
# ---------------------------------------------------------------------------


def _run_autofix(body: str) -> tuple[int, str, str]:
    result = subprocess.run(
        [sys.executable, str(AUTOFIX_SCRIPT)],
        input=body,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def test_ac5_hygiene_autofix_applies_vc_shape_then_no_change():
    """GIVEN a contract body with a forbidden -k VC shape on a real repo test file /
    WHEN issue_contract_hygiene_autofix.py runs / THEN it rewrites the VC line to
    canonical form, and running it again on the repaired body is a sha256 no_change."""
    # Candidate must be a real (currently nonexistent) repo-relative path so the
    # compiler's "missing baseline file" safety check passes against the real repo tree.
    existing_repo_file = "some_dir_ac5_probe/test_existing_ac5_probe.py"
    candidate = "some_dir_ac5_probe/test_existing_ac5_probe_new_test.py"

    # Create the "existing" file relative to the real repo root (parents[3] of the
    # autofix script, i.e. .claude/skills/edit-issue/scripts/../../../..).
    repo_root = AUTOFIX_SCRIPT.parents[4]
    probe_dir = repo_root / "some_dir_ac5_probe"
    probe_dir.mkdir(exist_ok=True)
    probe_file = probe_dir / "test_existing_ac5_probe.py"
    probe_file.write_text("def test_alpha():\n    pass\n", encoding="utf-8")
    try:
        body = textwrap.dedent(
            f"""\
            ## Outcome
            Test outcome.

            ## Acceptance Criteria
            - [ ] AC1: file exists

            ## Stop Conditions
            - エラー時は停止する。

            ## Verification Commands

            ```bash
            # AC1
            $ pytest {existing_repo_file} -k test_new_ac5_probe
            ```

            ## Allowed Paths
            - {existing_repo_file}
            - {candidate}

            ## Runtime Verification Applicability
            ```yaml
            decision: not_applicable
            reason: "test"
            ```

            ## Delivery Rule
            1 Issue = 1 PR
            """
        )

        code1, out1, err1 = _run_autofix(body)
        assert code1 == 0, f"Expected exit 0 (repaired), got {code1}. stderr={err1}"
        assert candidate in out1
        assert "::test_new_ac5_probe" in out1
        assert "-k" not in out1.split("## Allowed Paths")[0].split("Verification Commands")[1]

        # Second run on the repaired body: sha256 guard -> no_change (exit 1)
        code2, out2, err2 = _run_autofix(out1)
        assert code2 == 1, f"Expected exit 1 (no_change) on 2nd run, got {code2}. stderr={err2}"
        assert "no_change" in err2
    finally:
        probe_file.unlink(missing_ok=True)
        try:
            probe_dir.rmdir()
        except OSError:
            pass
