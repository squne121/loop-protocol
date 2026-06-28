"""Tests for the governed Python invocation policy checker (Issue #1193).

VC -> test selector mapping (each AC's `uv run --locked pytest ... -k <sel>`):

  AC1  surface_registry              AC6a fixture_exclusion
  AC2  pytest                        AC6b markdown_policy_example
  AC2a locked_python_m_pytest        AC6c false_positive
  AC3  python_script                 AC7  repo_scan_clean
  AC3a unlocked_script               AC8  ssot_targets_preserved
  AC4  exception_registry            AC9  performance_decision_recorded
  AC4a exception_schema              AC14 heredoc_c_dependency
  AC4b governed_surface              AC15 compound_command
  AC4c schema_validation             AC16 unsupported_shell_grammar
  AC4d exact_argv_prefix             AC17 exact_argv_variant
  AC5  parser                        AC18 adversarial

Hardening (OWNER REQUEST_CHANGES): direct interpreter exceptions match the
*complete* argv token list exactly (no prefix/glob/regex); stdlib_only script
exceptions are AST import-proven; heredoc / ``-c`` inline code is AST scanned;
every simple command in a compound line is classified; unsupported shell grammar
is reported fail-closed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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


def _types(
    line: str,
    body: str | None = None,
    exceptions: list[dict] | None = None,
    repo_root: Path | None = None,
) -> list[str]:
    return checker.scan_command_types(line, body, exceptions or [], repo_root)


# A schema-valid sample entry (no_target proof avoids AST/filesystem access).
_VALID_ENTRY = {
    "id": "sample",
    "scope": "stdlib_only",
    "exact_argv": ["python3", "scripts/foo.py"],
    "reason": "sample reason",
    "proof": "no_target",
}


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
    is_v, vt = _classify("uv run python3 scripts/foo.py --bar")
    assert is_v is True
    assert vt == "uv_run_python_script_no_locked"


def test_unlocked_script_python_is_violation():
    is_v, vt = _classify("uv run python scripts/foo.py")
    assert is_v is True
    assert vt == "uv_run_python_script_no_locked"


# ---------------------------------------------------------------------------
# AC4 exception_registry (new schema: id/scope/exact_argv/reason/proof)
# ---------------------------------------------------------------------------

def test_exception_registry_real_file_loads_and_is_valid():
    exceptions = checker.load_exceptions(REPO_ROOT)
    assert len(exceptions) >= 1
    ids = {e["id"] for e in exceptions}
    assert "python_test_plan_workers" in ids
    for e in exceptions:
        assert isinstance(e["exact_argv"], list) and e["exact_argv"]
        assert e["proof"] in checker.EXCEPTION_ALLOWED_PROOFS


def test_exception_registry_gated_direct_interpreter_is_allowed():
    exceptions = checker.load_exceptions(REPO_ROOT)
    line = (
        "python3 .claude/scripts/secret_exposure_scanner.py "
        "--local tests/fixtures/session-recording/valid --fail-on-finding"
    )
    assert _types(line, exceptions=exceptions, repo_root=REPO_ROOT) == []


def test_exception_registry_unregistered_direct_interpreter_is_violation():
    exceptions = checker.load_exceptions(REPO_ROOT)
    assert _types(
        "python3 scripts/not_registered.py", exceptions=exceptions, repo_root=REPO_ROOT
    ) == ["direct_python_script"]


def test_yaml_dependent_scripts_are_not_stdlib_only_exceptions():
    """Adversarial guard (#1227): dependency-bearing (yaml) scripts must not be
    registered as stdlib_only exceptions, else their bare ``python3 <script>``
    invocations would be silently exempted from the uv-run-locked requirement.
    """
    exceptions = checker.load_exceptions(REPO_ROOT)
    argv_blobs = [" ".join(e["exact_argv"]) for e in exceptions]
    for script in (
        ".claude/scripts/kill_switch_runtime_smoke.py",
        ".claude/scripts/check_session_recording_policy.py",
    ):
        assert not any(script in blob for blob in argv_blobs)


# ---------------------------------------------------------------------------
# AC4a exception_schema
# ---------------------------------------------------------------------------

def test_exception_schema_requires_all_fields():
    assert checker.validate_exceptions_schema({"exceptions": [_VALID_ENTRY]}) == []
    for missing in checker.EXCEPTION_REQUIRED_FIELDS:
        entry = dict(_VALID_ENTRY)
        del entry[missing]
        errors = checker.validate_exceptions_schema({"exceptions": [entry]})
        assert errors, f"missing {missing} should fail-closed"
        assert any(missing in e for e in errors)


def test_exception_schema_rejects_unknown_scope_and_proof():
    bad_scope = dict(_VALID_ENTRY, scope="whatever")
    assert any("scope" in e for e in checker.validate_exceptions_schema(
        {"exceptions": [bad_scope]}))
    bad_proof = dict(_VALID_ENTRY, proof="trust_me")
    assert any("proof" in e for e in checker.validate_exceptions_schema(
        {"exceptions": [bad_proof]}))


def test_exception_schema_rejects_duplicate_ids():
    dup = [dict(_VALID_ENTRY), dict(_VALID_ENTRY)]
    errors = checker.validate_exceptions_schema({"exceptions": dup})
    assert any("unique" in e for e in errors)


def test_exception_schema_exact_argv_must_be_nonempty_string_array():
    bad = dict(_VALID_ENTRY, exact_argv="python3 scripts/foo.py")
    assert any("exact_argv" in e for e in checker.validate_exceptions_schema(
        {"exceptions": [bad]}))
    empty = dict(_VALID_ENTRY, exact_argv=[])
    assert any("exact_argv" in e for e in checker.validate_exceptions_schema(
        {"exceptions": [empty]}))


def test_exception_schema_callsite_bound_requires_surface_and_locator():
    # A heredoc / -c (callsite-bound) entry needs surface + locator.
    cb = {
        "id": "heredoc_x",
        "scope": "stdlib_only",
        "exact_argv": ["python3", "-c"],
        "reason": "inline",
        "proof": "code_hash",
    }
    errors = checker.validate_exceptions_schema({"exceptions": [cb]})
    assert any("surface" in e for e in errors)
    assert any("locator" in e for e in errors)
    cb_ok = dict(cb, surface=".github/workflows/ci.yml", locator="step:foo")
    assert checker.validate_exceptions_schema({"exceptions": [cb_ok]}) == []


# ---------------------------------------------------------------------------
# AC4b governed_surface (path glob selection + AST import proof)
# ---------------------------------------------------------------------------

def test_governed_surface_uses_path_globs_only():
    for g in checker.SURFACE_GLOBS:
        assert "/" in g or g.endswith(".json"), g
    assert "**" in checker.SKILL_MD_PATTERN


def test_governed_surface_classification_is_text_only_for_uv_run():
    is_v, vt = _classify("uv run python3 does/not/exist/anywhere.py")
    assert is_v is True
    assert vt == "uv_run_python_script_no_locked"


def test_governed_surface_stdlib_proof_rejects_nonstdlib_target(tmp_path):
    (tmp_path / "scripts").mkdir(parents=True)
    (tmp_path / "scripts" / "x.py").write_text("import yaml\n", encoding="utf-8")
    exc = [{
        "id": "x", "scope": "stdlib_only",
        "exact_argv": ["python3", "scripts/x.py"],
        "reason": "claims stdlib", "proof": "stdlib_import_scan",
    }]
    is_v, vt = checker.classify_invocation(
        ["python3", "scripts/x.py"], exc, repo_root=tmp_path
    )
    assert is_v is True
    assert vt == "exception_proof_failed"


def test_governed_surface_stdlib_proof_accepts_stdlib_only_target(tmp_path):
    (tmp_path / "scripts").mkdir(parents=True)
    (tmp_path / "scripts" / "x.py").write_text(
        "import json, sys\nprint(sys.argv)\n", encoding="utf-8"
    )
    exc = [{
        "id": "x", "scope": "stdlib_only",
        "exact_argv": ["python3", "scripts/x.py"],
        "reason": "stdlib", "proof": "stdlib_import_scan",
    }]
    is_v, _ = checker.classify_invocation(
        ["python3", "scripts/x.py"], exc, repo_root=tmp_path
    )
    assert is_v is False


# ---------------------------------------------------------------------------
# AC4c schema_validation (fail-closed loader)
# ---------------------------------------------------------------------------

def test_schema_validation_loader_fails_closed(tmp_path):
    bad_registry = {"exceptions": [{"id": "x", "scope": "stdlib_only",
                                    "exact_argv": ["python3", "y.py"],
                                    "reason": "no proof"}]}
    (tmp_path / "scripts" / "ci").mkdir(parents=True)
    (tmp_path / checker.EXCEPTIONS_PATH).write_text(
        json.dumps(bad_registry), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        checker.load_exceptions(tmp_path)


def test_schema_validation_passes_for_real_registry():
    data = json.loads((REPO_ROOT / checker.EXCEPTIONS_PATH).read_text(encoding="utf-8"))
    assert checker.validate_exceptions_schema(data) == []


# ---------------------------------------------------------------------------
# AC4d exact_argv_prefix (now an EXACT full-argv match, not a prefix)
# ---------------------------------------------------------------------------

def test_exact_argv_prefix_requires_full_match():
    exc = [dict(_VALID_ENTRY, exact_argv=["python3", "scripts/p.py", "--emit", "workers"])]
    assert checker.matches_exception(
        ["python3", "scripts/p.py", "--emit", "workers"], exc) is True


def test_exact_argv_prefix_rejects_extra_trailing_token():
    # Former prefix-match behaviour is removed: surplus tokens no longer match.
    exc = [dict(_VALID_ENTRY, exact_argv=["python3", "scripts/p.py", "--emit", "workers"])]
    assert checker.matches_exception(
        ["python3", "scripts/p.py", "--emit", "workers", "--extra"], exc) is False


def test_exact_argv_prefix_rejects_reorder_and_duplicate_and_path_qualified():
    exc = [dict(_VALID_ENTRY, exact_argv=["python3", "scripts/p.py", "--a", "--b"])]
    assert checker.matches_exception(
        ["python3", "scripts/p.py", "--b", "--a"], exc) is False  # reordered
    assert checker.matches_exception(
        ["python3", "scripts/p.py", "--a", "--a", "--b"], exc) is False  # duplicate
    assert checker.matches_exception(
        ["/usr/bin/python3", "scripts/p.py", "--a", "--b"], exc) is False  # path-qualified


def test_exact_argv_prefix_is_not_glob_or_regex():
    exc = [dict(_VALID_ENTRY, exact_argv=["python3", "scripts/.*"])]
    assert checker.matches_exception(["python3", "scripts/foo.py"], exc) is False
    assert checker.matches_exception(["python3", "scripts/.*"], exc) is True


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


def test_parser_yaml_heredoc_body_is_captured():
    content = (
        "jobs:\n  x:\n    steps:\n      - run: |\n"
        "          python3 - <<'PY'\n"
        "          import yaml\n"
        "          print(yaml)\n"
        "          PY\n"
    )
    units = list(checker._iter_yaml_units(content))
    bodies = [b for _, _, b in units if b is not None]
    assert any("import yaml" in b for b in bodies)


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
    assert list(checker.iter_markdown_code_lines(content)) == []


def test_markdown_unmarked_block_is_scanned():
    content = "```bash\nuv run pytest a_test.py\n```\n"
    lines = [t for _, t in checker.iter_markdown_code_lines(content)]
    assert any("uv run pytest" in line for line in lines)


def test_markdown_non_shell_language_block_is_not_scanned():
    # A ```yaml block is data, not shell, and must not be parsed as commands.
    content = '```yaml\n- "uv run python3 scripts/foo.py"\n```\n'
    assert list(checker.iter_markdown_code_lines(content)) == []


def test_markdown_policy_example_fixture_only_flags_unmarked_block():
    vios = checker.scan_file(
        str(FIXTURE_DIR / "policy_example.md"),
        str(REPO_ROOT),
        checker.load_exceptions(REPO_ROOT),
    )
    assert vios == []


# ---------------------------------------------------------------------------
# AC6c false_positive
# ---------------------------------------------------------------------------

def test_false_positive_uv_python_install_not_flagged():
    assert checker._extract_argv_from_line(
        "run_timed uv_python_install uv python install") is None


def test_false_positive_japanese_prose_not_flagged():
    is_v, _ = _classify("python3 不在など自身がエラーになった場合も fail-closed")
    assert is_v is False


def test_false_positive_comment_line_not_flagged():
    # A shell comment mentioning a script path is not an invocation.
    assert _types("# python3 を使う場合 guard-issue-body.py 参照") == []


def test_false_positive_test_file_excluded_from_surface_scan():
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


# ---------------------------------------------------------------------------
# AC14 heredoc_c_dependency (AST import scan of heredoc body / -c code)
# ---------------------------------------------------------------------------

def test_heredoc_c_dependency_heredoc_body_with_yaml_is_violation():
    types = _types("python3 - ", body="import yaml\nprint(yaml)\n")
    assert types == ["heredoc_c_dependency"]


def test_heredoc_c_dependency_dash_c_with_yaml_is_violation():
    assert _types('python3 -c "import yaml; print(1)"') == ["heredoc_c_dependency"]


def test_heredoc_c_dependency_stdlib_heredoc_is_allowed():
    assert _types("python3 - ", body="import json, os, sys\nprint(json)\n") == []


def test_heredoc_c_dependency_stdlib_dash_c_is_allowed():
    assert _types('python3 -c "import json,sys; print(1)"') == []


def test_heredoc_c_dependency_locked_uv_run_inline_yaml_is_allowed():
    # Under uv run --locked the dependency is resolved from the lockfile.
    assert _types('uv run --locked python -c "import yaml; print(1)"') == []


def test_heredoc_c_dependency_unlocked_uv_run_inline_yaml_is_violation():
    assert _types('uv run python -c "import yaml; print(1)"') == [
        "uv_run_inline_no_locked_dependency"
    ]


# ---------------------------------------------------------------------------
# AC15 compound_command (every simple command in a line is classified)
# ---------------------------------------------------------------------------

def test_compound_command_trailing_violation_after_and():
    exc = checker.load_exceptions(REPO_ROOT)
    assert _types(
        "uv run --locked pytest -q && python3 scripts/not_registered.py",
        exceptions=exc, repo_root=REPO_ROOT,
    ) == ["direct_python_script"]


def test_compound_command_leading_violation_before_and():
    exc = checker.load_exceptions(REPO_ROOT)
    assert _types(
        "python3 scripts/not_registered.py && uv run --locked pytest -q",
        exceptions=exc, repo_root=REPO_ROOT,
    ) == ["direct_python_script"]


def test_compound_command_trailing_unlocked_pytest_after_semicolon():
    assert _types("uv run --locked pytest -q; uv run pytest -q") == [
        "uv_run_pytest_no_locked"
    ]


def test_compound_command_pipeline_segment_is_classified():
    assert _types("echo hi | uv run pytest -q") == ["uv_run_pytest_no_locked"]


# ---------------------------------------------------------------------------
# AC16 unsupported_shell_grammar (fail-closed; no .split() fallback)
# ---------------------------------------------------------------------------

def test_unsupported_shell_grammar_unterminated_substitution():
    assert _types("echo $(uv run pytest -q") == ["unsupported_shell_grammar"]


def test_unsupported_shell_grammar_unbalanced_quote_on_launcher_line():
    assert _types('python3 scripts/x.py "unterminated') == [
        "unsupported_shell_grammar"
    ]


def test_unsupported_shell_grammar_process_substitution_hidden_violation_caught():
    # A violation hidden inside process substitution must NOT be false green.
    assert _types("cat <(uv run pytest tests/)") == ["uv_run_pytest_no_locked"]


def test_unsupported_shell_grammar_no_split_fallback_for_benign_substitution():
    # Benign command substitution without a launcher is not flagged.
    assert _types("_x=$(date +%s)") == []


# ---------------------------------------------------------------------------
# AC17 exact_argv_variant (bootstrap/stdlib exceptions are exact argv shapes)
# ---------------------------------------------------------------------------

def test_exact_argv_variant_registered_shape_is_allowed():
    exc = checker.load_exceptions(REPO_ROOT)
    assert _types(
        "python3 scripts/ci/python_test_plan.py --emit workers --format lines",
        exceptions=exc, repo_root=REPO_ROOT,
    ) == []


def test_exact_argv_variant_different_option_shape_is_violation():
    # Same script, a shape that is NOT a registered exact_argv variant.
    exc = checker.load_exceptions(REPO_ROOT)
    assert _types(
        "python3 scripts/ci/python_test_plan.py --emit workers",
        exceptions=exc, repo_root=REPO_ROOT,
    ) == ["direct_python_script"]


def test_exact_argv_variant_real_registry_entries_are_exact_argv_arrays():
    exc = checker.load_exceptions(REPO_ROOT)
    plan_entries = [e for e in exc if "python_test_plan.py" in " ".join(e["exact_argv"])]
    assert plan_entries
    for e in plan_entries:
        assert e["exact_argv"][0] == "python3"
        assert "--emit" in e["exact_argv"]


# ---------------------------------------------------------------------------
# AC18 adversarial (the six required adversarial cases)
# ---------------------------------------------------------------------------

def test_adversarial_case1_heredoc_yaml_violation():
    assert _types("python3 - ", body="import yaml\n") == ["heredoc_c_dependency"]


def test_adversarial_case2_dash_c_yaml_violation():
    assert _types('python3 -c "import yaml"') == ["heredoc_c_dependency"]


def test_adversarial_case3_trailing_unregistered_script_after_locked_pytest():
    exc = checker.load_exceptions(REPO_ROOT)
    assert _types(
        "uv run --locked pytest -q && python3 scripts/not_registered.py",
        exceptions=exc, repo_root=REPO_ROOT,
    ) == ["direct_python_script"]


def test_adversarial_case4_leading_unregistered_script_before_locked_pytest():
    exc = checker.load_exceptions(REPO_ROOT)
    assert _types(
        "python3 scripts/not_registered.py && uv run --locked pytest -q",
        exceptions=exc, repo_root=REPO_ROOT,
    ) == ["direct_python_script"]


def test_adversarial_case5_trailing_non_locked_pytest_after_semicolon():
    assert _types("uv run --locked pytest -q; uv run pytest -q") == [
        "uv_run_pytest_no_locked"
    ]


def test_adversarial_case6_command_process_substitution_not_false_green():
    # Hidden violation inside command substitution is detected ...
    assert _types("x=$(uv run pytest -q)") == ["uv_run_pytest_no_locked"]
    # ... and an unparseable (unterminated) substitution fails closed.
    assert _types("x=$(uv run pytest -q") == ["unsupported_shell_grammar"]
