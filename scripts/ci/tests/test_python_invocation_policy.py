"""Tests for the governed Python invocation policy checker (Issue #1193).

VC -> test selector mapping (each AC's `uv run --locked pytest ... -k <sel>`):

  AC1  surface_registry              AC4c schema_validation
  AC2  pytest                        AC4d exact_argv_prefix
  AC2a locked_python_m_pytest        AC5  parser
  AC3  python_script                 AC6a fixture_exclusion
  AC3a unlocked_script               AC6b markdown_policy_example
  AC4  exception_registry            AC6c false_positive
  AC4a exception_schema              AC7  repo_scan_clean
  AC4b governed_surface              AC8  ssot_targets_preserved
                                     AC9  performance_decision_recorded

The checker classifies invocations from text alone (no import analysis): the
allowed pytest form is `uv run --locked pytest`, and the allowed script form is
`uv run --locked python[3] <script.py>`. Direct interpreter use is permitted
only via the exact-prefix exceptions registry (stdlib_only | bootstrap).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# scripts/ci/tests/<this> -> parents[1] == scripts/ci (checker dir),
# parents[3] == repo root.
_CHECKER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_CHECKER_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKER_DIR))

import check_python_invocation_policy as checker  # noqa: E402

FIXTURE_DIR = _CHECKER_DIR / "fixtures" / "python_invocation_policy"


def _argv(line: str) -> list[str]:
    tokens = checker._extract_argv_from_line(line)
    assert tokens is not None, f"no invocation extracted from: {line!r}"
    return tokens


def _classify(line: str, exceptions: list[dict] | None = None) -> tuple[bool, str]:
    return checker.classify_invocation(_argv(line), exceptions or [])


# ---------------------------------------------------------------------------
# AC1 surface_registry
# ---------------------------------------------------------------------------

def test_surface_registry_includes_required_globs():
    globs = set(checker.SURFACE_GLOBS)
    assert any(g.startswith(".github/workflows/") for g in globs)
    assert any(g.startswith(".github/actions/") for g in globs)
    assert any(g.startswith("docs/dev/") for g in globs)
    assert "package.json" in globs
    assert checker.SKILL_MD_PATTERN == ".claude/skills/**/SKILL.md"


def test_surface_registry_collects_real_governed_files():
    files = checker.collect_surface_files(REPO_ROOT)
    rels = {Path(f).relative_to(REPO_ROOT).as_posix() for f in files}
    assert "package.json" in rels
    assert ".github/workflows/ci.yml" in rels
    assert ".claude/skills/open-pr/SKILL.md" in rels
    assert "docs/dev/test-lane-policy.md" in rels


# ---------------------------------------------------------------------------
# AC2 pytest / AC2a locked_python_m_pytest
# ---------------------------------------------------------------------------

def test_uv_run_pytest_without_locked_is_pytest_violation():
    is_v, vt = _classify("uv run pytest scripts/x_test.py")
    assert is_v is True
    assert vt == "uv_run_pytest_no_locked"


def test_uv_run_locked_pytest_is_allowed():
    is_v, _ = _classify("uv run --locked pytest scripts/x_test.py")
    assert is_v is False


def test_direct_python3_m_pytest_is_violation():
    is_v, vt = _classify("python3 -m pytest scripts/x_test.py")
    assert is_v is True
    assert vt == "direct_python_m_pytest"


def test_direct_python_m_pytest_is_violation():
    is_v, vt = _classify("python -m pytest scripts/x_test.py")
    assert is_v is True
    assert vt == "direct_python_m_pytest"


def test_locked_python_m_pytest_is_violation():
    # AC2a: even with --locked, the `-m pytest` form is rejected.
    is_v, vt = _classify("uv run --locked python -m pytest scripts/x_test.py")
    assert is_v is True
    assert vt == "uv_run_python_m_pytest"


def test_locked_python3_m_pytest_is_violation():
    is_v, vt = _classify("uv run --locked python3 -m pytest scripts/x_test.py")
    assert is_v is True
    assert vt == "uv_run_python_m_pytest"


# ---------------------------------------------------------------------------
# AC3 python_script / AC3a unlocked_script
# ---------------------------------------------------------------------------

def test_uv_run_locked_python_script_is_allowed():
    is_v, _ = _classify("uv run --locked python scripts/foo.py --bar")
    assert is_v is False


def test_uv_run_locked_python3_script_via_python_script_form_is_allowed():
    is_v, _ = _classify("uv run --locked python3 scripts/foo.py --bar")
    assert is_v is False


def test_unlocked_script_python3_is_violation():
    # AC3a: `uv run python3 <script>` without --locked is rejected.
    is_v, vt = _classify("uv run python3 scripts/foo.py --bar")
    assert is_v is True
    assert vt == "uv_run_python_script_no_locked"


def test_unlocked_script_python_is_violation():
    is_v, vt = _classify("uv run python scripts/foo.py")
    assert is_v is True
    assert vt == "uv_run_python_script_no_locked"


# ---------------------------------------------------------------------------
# AC4 exception_registry
# ---------------------------------------------------------------------------

def test_exception_registry_real_file_loads_and_is_valid():
    exceptions = checker.load_exceptions(REPO_ROOT)
    assert len(exceptions) >= 1
    patterns = {e["exact_argv_pattern"] for e in exceptions}
    assert "python3 -c" in patterns


def test_exception_registry_gated_direct_interpreter_is_allowed():
    exceptions = checker.load_exceptions(REPO_ROOT)
    # A registered stdlib-only bare interpreter invocation is not a violation.
    is_v, vt = _classify(
        'python3 .claude/scripts/secret_exposure_scanner.py --local .claude/scripts',
        exceptions,
    )
    assert is_v is False
    assert vt == "exception_match"


def test_exception_registry_unregistered_direct_interpreter_is_violation():
    exceptions = checker.load_exceptions(REPO_ROOT)
    is_v, vt = _classify("python3 scripts/not_registered.py", exceptions)
    assert is_v is True
    assert vt == "direct_python_script"


def test_yaml_dependent_scripts_are_not_stdlib_only_exceptions():
    """Adversarial guard (iteration-1, #1227): dependency-bearing (yaml) scripts
    must not be registered as stdlib_only exceptions, because the registry would
    then silently exempt their bare `python3 <script>` invocations from the
    uv-run-locked requirement — the policy hole this PR closes.
    """
    exceptions = checker.load_exceptions(REPO_ROOT)
    patterns = {e["exact_argv_pattern"] for e in exceptions}
    # These two scripts import yaml (non-stdlib); they must NOT be exempted.
    assert "python3 .claude/scripts/kill_switch_runtime_smoke.py" not in patterns
    assert "python3 .claude/scripts/check_session_recording_policy.py" not in patterns


def test_unregistered_yaml_script_bare_invocation_is_violation():
    """With the yaml-dependent scripts removed from the registry, their bare
    `python3 <script>` invocations are classified as violations (must migrate to
    `uv run --locked python3 ...`).
    """
    exceptions = checker.load_exceptions(REPO_ROOT)
    for line in (
        "python3 .claude/scripts/kill_switch_runtime_smoke.py "
        "--fixtures tests/fixtures/session-recording",
        "python3 .claude/scripts/check_session_recording_policy.py "
        "docs/dev/session-recording-policy.md",
    ):
        is_v, vt = _classify(line, exceptions)
        assert is_v is True, f"expected violation for: {line!r}"
        assert vt == "direct_python_script"


# ---------------------------------------------------------------------------
# AC4a exception_schema
# ---------------------------------------------------------------------------

def test_exception_schema_requires_three_fields():
    full = {"exact_argv_pattern": "python3 -c", "reason": "r", "scope": "stdlib_only"}
    assert checker.validate_exceptions_schema({"exceptions": [full]}) == []
    for missing in ("exact_argv_pattern", "reason", "scope"):
        entry = dict(full)
        del entry[missing]
        errors = checker.validate_exceptions_schema({"exceptions": [entry]})
        assert errors, f"missing {missing} should fail-closed"
        assert any(missing in e for e in errors)


def test_exception_schema_rejects_unknown_scope():
    bad = {"exact_argv_pattern": "python3 -c", "reason": "r", "scope": "whatever"}
    errors = checker.validate_exceptions_schema({"exceptions": [bad]})
    assert any("scope" in e for e in errors)


# ---------------------------------------------------------------------------
# AC4b governed_surface (path glob + text pattern; no import analysis)
# ---------------------------------------------------------------------------

def test_governed_surface_uses_path_globs_only():
    # All surface entries are path globs, not python module names.
    for g in checker.SURFACE_GLOBS:
        assert "/" in g or g.endswith(".json"), g
    assert "**" in checker.SKILL_MD_PATTERN


def test_governed_surface_classification_is_text_only():
    # Classification works on a raw string with no filesystem/import access:
    # the script target need not exist on disk to be classified.
    is_v, vt = _classify("uv run python3 does/not/exist/anywhere.py")
    assert is_v is True
    assert vt == "uv_run_python_script_no_locked"


# ---------------------------------------------------------------------------
# AC4c schema_validation (fail-closed loader)
# ---------------------------------------------------------------------------

def test_schema_validation_loader_fails_closed(tmp_path):
    bad_registry = {"exceptions": [{"reason": "no pattern", "scope": "stdlib_only"}]}
    root = tmp_path
    (root / "scripts" / "ci").mkdir(parents=True)
    (root / checker.EXCEPTIONS_PATH).write_text(json.dumps(bad_registry), encoding="utf-8")
    with pytest.raises(ValueError):
        checker.load_exceptions(root)


def test_schema_validation_passes_for_real_registry():
    data = json.loads((REPO_ROOT / checker.EXCEPTIONS_PATH).read_text(encoding="utf-8"))
    assert checker.validate_exceptions_schema(data) == []


# ---------------------------------------------------------------------------
# AC4d exact_argv_prefix (prefix match, no glob/regex)
# ---------------------------------------------------------------------------

def test_exact_argv_prefix_matches_leading_tokens():
    exceptions = [{"exact_argv_pattern": "python3 scripts/ci/python_test_plan.py",
                   "reason": "r", "scope": "bootstrap"}]
    argv = ["python3", "scripts/ci/python_test_plan.py", "--emit", "run-argv"]
    assert checker.matches_exception(argv, exceptions) is True


def test_exact_argv_prefix_rejects_non_prefix():
    exceptions = [{"exact_argv_pattern": "python3 scripts/ci/python_test_plan.py",
                   "reason": "r", "scope": "bootstrap"}]
    # Different second token -> not a prefix match.
    argv = ["python3", "scripts/ci/other.py"]
    assert checker.matches_exception(argv, exceptions) is False


def test_exact_argv_prefix_is_not_glob_or_regex():
    # A glob/regex-looking pattern is matched literally, not interpreted.
    exceptions = [{"exact_argv_pattern": "python3 scripts/.*",
                   "reason": "r", "scope": "stdlib_only"}]
    assert checker.matches_exception(["python3", "scripts/foo.py"], exceptions) is False
    assert checker.matches_exception(["python3", "scripts/.*"], exceptions) is True


# ---------------------------------------------------------------------------
# AC5 parser (markdown fenced, yaml run:, package.json scripts)
# ---------------------------------------------------------------------------

def test_parser_yaml_run_block_extracts_command():
    content = (
        "jobs:\n"
        "  x:\n"
        "    steps:\n"
        "      - name: t\n"
        "        run: |\n"
        "          uv run pytest a_test.py\n"
        "      - name: u\n"
        "        run: uv run --locked pytest b_test.py\n"
    )
    lines = [t for _, t in checker.iter_yaml_run_lines(content)]
    assert any("uv run pytest a_test.py" in line for line in lines)
    assert any("uv run --locked pytest b_test.py" in line for line in lines)


def test_parser_markdown_fenced_block_extracts_command():
    content = "text\n\n```bash\nuv run pytest a_test.py\n```\n\nmore\n"
    lines = [t for _, t in checker.iter_markdown_code_lines(content)]
    assert any("uv run pytest a_test.py" in line for line in lines)


def test_parser_package_json_scripts_values():
    content = json.dumps(
        {"scripts": {"x": "uv run python3 scripts/foo.py --json"}}, indent=2
    )
    lines = [t for _, t in checker.iter_package_json_lines(content)]
    assert any("uv run python3 scripts/foo.py" in line for line in lines)


# ---------------------------------------------------------------------------
# AC6a fixture_exclusion
# ---------------------------------------------------------------------------

def test_fixture_exclusion_from_surface_scan():
    files = checker.collect_surface_files(REPO_ROOT)
    rels = {Path(f).relative_to(REPO_ROOT).as_posix() for f in files}
    assert not any(r.startswith(checker.FIXTURE_PREFIX) for r in rels)
    assert checker.should_exclude(str(FIXTURE_DIR / "violations.md"), str(REPO_ROOT))


def test_fixture_violations_file_detects_negative_cases_when_scanned_directly():
    vios = checker.scan_file(
        str(FIXTURE_DIR / "violations.md"),
        str(REPO_ROOT),
        checker.load_exceptions(REPO_ROOT),
    )
    types = {v.violation_type for v in vios}
    assert "uv_run_pytest_no_locked" in types
    assert "uv_run_python_script_no_locked" in types
    assert "direct_python_m_pytest" in types
    assert "uv_run_python_m_pytest" in types


def test_fixture_compliant_file_has_no_violations_when_scanned_directly():
    vios = checker.scan_file(
        str(FIXTURE_DIR / "compliant.md"),
        str(REPO_ROOT),
        checker.load_exceptions(REPO_ROOT),
    )
    assert vios == []


# ---------------------------------------------------------------------------
# AC6b markdown_policy_example
# ---------------------------------------------------------------------------

def test_markdown_policy_example_block_is_excluded():
    content = (
        "<!-- policy-example -->\n"
        "```bash\n"
        "uv run pytest a_test.py\n"
        "```\n"
    )
    lines = list(checker.iter_markdown_code_lines(content))
    assert lines == []


def test_markdown_unmarked_block_is_scanned():
    content = "```bash\nuv run pytest a_test.py\n```\n"
    lines = [t for _, t in checker.iter_markdown_code_lines(content)]
    assert any("uv run pytest" in line for line in lines)


def test_markdown_policy_example_fixture_only_flags_unmarked_block():
    vios = checker.scan_file(
        str(FIXTURE_DIR / "policy_example.md"),
        str(REPO_ROOT),
        checker.load_exceptions(REPO_ROOT),
    )
    # The marked block (uv run pytest) is excluded; the unmarked block is the
    # compliant `uv run --locked pytest`, so no violations remain.
    assert vios == []


# ---------------------------------------------------------------------------
# AC6c false_positive
# ---------------------------------------------------------------------------

def test_false_positive_uv_python_install_not_flagged():
    # `uv python install` is a uv subcommand, not an interpreter invocation.
    assert checker._extract_argv_from_line("run_timed uv_python_install uv python install") is None


def test_false_positive_japanese_prose_not_flagged():
    # Prose like "python3 不在" (python3 absent) is not a script invocation.
    is_v, _ = _classify("python3 不在など自身がエラーになった場合も fail-closed")
    assert is_v is False


def test_false_positive_test_file_excluded_from_surface_scan():
    # AC6c: this test file's own string literals must not be reported.
    files = checker.collect_surface_files(REPO_ROOT)
    rels = {Path(f).relative_to(REPO_ROOT).as_posix() for f in files}
    assert checker.TEST_FILE_EXCL not in rels
    assert checker.should_exclude(str(Path(__file__).resolve()), str(REPO_ROOT))


# ---------------------------------------------------------------------------
# AC7 repo_scan_clean
# ---------------------------------------------------------------------------

def test_repo_scan_clean_zero_violations():
    result = checker.run_check(REPO_ROOT)
    detail = "\n".join(
        f"{v.file}:{v.line_num} [{v.violation_type}] {v.line_text}"
        for v in result.violations
    )
    assert result.violations == [], f"governed surface not clean:\n{detail}"


# ---------------------------------------------------------------------------
# AC8 ssot_targets_preserved
# ---------------------------------------------------------------------------

def test_ssot_targets_preserved():
    plan = json.loads(
        (REPO_ROOT / ".github" / "ci" / "python-test-plan.json").read_text(encoding="utf-8")
    )
    targets = plan["targets"]
    assert isinstance(targets, list) and targets
    # The SSOT still routes this checker's own test directory through CI.
    assert "scripts/ci/tests/" in targets
    assert "schemas/tests/" in targets


# ---------------------------------------------------------------------------
# AC9 performance_decision_recorded
# ---------------------------------------------------------------------------

def test_performance_decision_recorded():
    text = (REPO_ROOT / "docs" / "dev" / "test-lane-policy.md").read_text(encoding="utf-8")
    assert "CI_TEST_PERFORMANCE_DECISION_V1 (#1193)" in text
    assert "issue_number: 1193" in text
    assert "contract_artifact" in text
    assert "python_unit" in text
