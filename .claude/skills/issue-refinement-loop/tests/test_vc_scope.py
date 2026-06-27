"""
test_vc_scope.py - pytest tests for check_vc_scope.py

Tests cover:
- AC1: VC_MISSING_DOLLAR_PREFIX warn (exit 1) with line numbers
- AC2: VC_LEGACY_PYTHON3 blocked (exit 2)
- AC3: VC_SCOPE_OUTSIDE_ALLOWED_PATH / VC_SCOPE_BROAD_SEARCH_PATH blocked (exit 2)
- AC4: prose note only does not block (exit 0/1)
- AC5: stdout only contains allowed keys
- False positive / negative prevention
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = (
    Path(__file__).parent.parent / "scripts" / "check_vc_scope.py"
).resolve()

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "vc_scope"

ALLOWED_STDOUT_KEYS = {"STATUS", "SUMMARY", "NEXT_ACTION", "EVIDENCE", "BLOCKERS", "ARTIFACT"}


def run_checker(
    fixture_name: str | None = None,
    body: str | None = None,
    extra_args: list[str] | None = None,
) -> tuple[int, str, str, dict]:
    """Run check_vc_scope.py and return (exit_code, stdout, stderr, artifact_data)."""
    cmd = [sys.executable, str(SCRIPT)]
    if extra_args:
        cmd.extend(extra_args)

    if fixture_name is not None:
        fixture_path = FIXTURES_DIR / fixture_name
        cmd.extend(["--issue-body-file", str(fixture_path)])
        input_data = None
    elif body is not None:
        input_data = body.encode()
    else:
        raise ValueError("Either fixture_name or body must be provided")

    result = subprocess.run(
        cmd,
        input=input_data,
        capture_output=True,
        timeout=30,
    )
    stdout = result.stdout.decode()
    stderr = result.stderr.decode()
    exit_code = result.returncode

    # Find artifact path from stdout
    artifact_data = {}
    for line in stdout.splitlines():
        if line.startswith("ARTIFACT: "):
            artifact_path = line[len("ARTIFACT: "):].strip()
            try:
                with open(artifact_path) as f:
                    artifact_data = json.load(f)
            except Exception:
                pass
            break

    return exit_code, stdout, stderr, artifact_data


def parse_findings_from_artifact(artifact_data: dict) -> list[dict]:
    return artifact_data.get("findings", [])


def has_reason_code(findings: list[dict], reason_code: str) -> bool:
    return any(f["reason_code"] == reason_code for f in findings)


def findings_with_code(findings: list[dict], reason_code: str) -> list[dict]:
    return [f for f in findings if f["reason_code"] == reason_code]


# ---------------------------------------------------------------------------
# AC1: VC_MISSING_DOLLAR_PREFIX warn (exit 1) - with line number
# ---------------------------------------------------------------------------

class TestMissingDollarPrefix:
    """AC1: VC command line that does not start with '$ ' is flagged as warn."""

    def test_missing_dollar_prefix_is_warn_exit1(self):
        """GIVEN a VC section with a command missing '$ ' prefix
        WHEN check_vc_scope.py is run
        THEN exit code is 1 (warn) and VC_MISSING_DOLLAR_PREFIX finding is present
        """
        exit_code, stdout, stderr, artifact = run_checker("missing_dollar_prefix.md")
        assert exit_code == 1, f"Expected exit 1 (warn), got {exit_code}. stderr: {stderr}"
        findings = parse_findings_from_artifact(artifact)
        assert has_reason_code(findings, "VC_MISSING_DOLLAR_PREFIX"), (
            f"Expected VC_MISSING_DOLLAR_PREFIX in findings: {findings}"
        )

    def test_missing_dollar_prefix_includes_line_number(self):
        """GIVEN a VC section with a command missing '$ ' prefix
        WHEN check_vc_scope.py is run
        THEN the finding includes a line number (line > 0)
        """
        exit_code, stdout, stderr, artifact = run_checker("missing_dollar_prefix.md")
        findings = parse_findings_from_artifact(artifact)
        missing_dollar = findings_with_code(findings, "VC_MISSING_DOLLAR_PREFIX")
        assert missing_dollar, "No VC_MISSING_DOLLAR_PREFIX findings found"
        for f in missing_dollar:
            assert "line" in f, f"Finding missing 'line' key: {f}"
            assert isinstance(f["line"], int) and f["line"] > 0, (
                f"Expected positive line number, got {f['line']}"
            )

    def test_missing_dollar_prefix_level_is_warn(self):
        """GIVEN a VC_MISSING_DOLLAR_PREFIX finding
        WHEN checking level
        THEN level is 'warn' (not 'blocked')
        """
        exit_code, stdout, stderr, artifact = run_checker("missing_dollar_prefix.md")
        findings = parse_findings_from_artifact(artifact)
        missing_dollar = findings_with_code(findings, "VC_MISSING_DOLLAR_PREFIX")
        assert missing_dollar, "No VC_MISSING_DOLLAR_PREFIX findings found"
        for f in missing_dollar:
            assert f["level"] == "warn", f"Expected level='warn', got {f['level']}"

    def test_artifact_exit_code_matches_process_exit(self):
        """GIVEN a VC_MISSING_DOLLAR_PREFIX finding
        WHEN checking artifact JSON
        THEN artifact exit_code matches the actual process exit code
        """
        exit_code, stdout, stderr, artifact = run_checker("missing_dollar_prefix.md")
        assert artifact.get("exit_code") == exit_code, (
            f"Artifact exit_code {artifact.get('exit_code')} != process exit {exit_code}"
        )


# ---------------------------------------------------------------------------
# AC2: VC_LEGACY_PYTHON3 blocked (exit 2)
# ---------------------------------------------------------------------------

class TestLegacyPython3:
    """AC2: Bare python3 (without uv run) triggers VC_LEGACY_PYTHON3 blocked."""

    def test_legacy_python3_is_blocked_exit2(self):
        """GIVEN a VC with 'python3 script.py' (no uv run)
        WHEN check_vc_scope.py is run
        THEN exit code is 2 (blocked) and VC_LEGACY_PYTHON3 finding is present
        """
        exit_code, stdout, stderr, artifact = run_checker("legacy_python3.md")
        assert exit_code == 2, f"Expected exit 2 (blocked), got {exit_code}. stderr: {stderr}"
        findings = parse_findings_from_artifact(artifact)
        assert has_reason_code(findings, "VC_LEGACY_PYTHON3"), (
            f"Expected VC_LEGACY_PYTHON3 in findings: {findings}"
        )

    def test_legacy_python3_level_is_blocked(self):
        """GIVEN a VC_LEGACY_PYTHON3 finding
        WHEN checking level
        THEN level is 'blocked'
        """
        exit_code, stdout, stderr, artifact = run_checker("legacy_python3.md")
        findings = parse_findings_from_artifact(artifact)
        legacy = findings_with_code(findings, "VC_LEGACY_PYTHON3")
        assert legacy, "No VC_LEGACY_PYTHON3 findings found"
        for f in legacy:
            assert f["level"] == "blocked", f"Expected level='blocked', got {f['level']}"

    def test_uv_run_python3_is_not_flagged(self):
        """GIVEN a VC with 'uv run python3 script.py'
        WHEN check_vc_scope.py is run
        THEN VC_LEGACY_PYTHON3 is NOT present in findings
        """
        exit_code, stdout, stderr, artifact = run_checker("uv_run_allowed.md")
        findings = parse_findings_from_artifact(artifact)
        assert not has_reason_code(findings, "VC_LEGACY_PYTHON3"), (
            f"uv run python3 should not trigger VC_LEGACY_PYTHON3: {findings}"
        )

    def test_uv_run_locked_pytest_is_not_flagged(self):
        """GIVEN a VC with 'uv run --locked pytest ...'
        WHEN check_vc_scope.py is run
        THEN VC_LEGACY_PYTHON3 is NOT present in findings
        """
        body = """## Verification Commands

```bash
$ uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_vc_scope.py -v
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/fixtures/`
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        findings = parse_findings_from_artifact(artifact)
        assert not has_reason_code(findings, "VC_LEGACY_PYTHON3"), (
            f"uv run --locked pytest should not trigger VC_LEGACY_PYTHON3: {findings}"
        )

    def test_rg_regex_string_python3_not_flagged(self):
        """GIVEN a VC with rg -F '^python3 ' (python3 is inside a regex string, not a command)
        WHEN check_vc_scope.py is run
        THEN VC_LEGACY_PYTHON3 is NOT present in findings
        """
        exit_code, stdout, stderr, artifact = run_checker("rg_regex_false_positive.md")
        findings = parse_findings_from_artifact(artifact)
        assert not has_reason_code(findings, "VC_LEGACY_PYTHON3"), (
            f"rg -F '^python3 ' should not trigger VC_LEGACY_PYTHON3: {findings}"
        )

    def test_python3_in_background_section_not_flagged(self):
        """GIVEN a body where 'python3' appears in Background section (not in VC)
        WHEN check_vc_scope.py is run
        THEN VC_LEGACY_PYTHON3 is NOT present in findings
        """
        body = """## Background

The problem is that python3 direct calls waste context. Use uv run instead.

## Verification Commands

```bash
$ uv run pytest .claude/skills/issue-refinement-loop/tests/test_vc_scope.py -v
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/fixtures/`
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        findings = parse_findings_from_artifact(artifact)
        assert not has_reason_code(findings, "VC_LEGACY_PYTHON3"), (
            f"python3 in Background section should not trigger VC_LEGACY_PYTHON3: {findings}"
        )

    def test_artifact_exit_code_matches_process_exit(self):
        """GIVEN a VC_LEGACY_PYTHON3 blocked finding
        WHEN checking artifact JSON
        THEN artifact exit_code matches the actual process exit code
        """
        exit_code, stdout, stderr, artifact = run_checker("legacy_python3.md")
        assert artifact.get("exit_code") == exit_code, (
            f"Artifact exit_code {artifact.get('exit_code')} != process exit {exit_code}"
        )


# ---------------------------------------------------------------------------
# AC3: VC_SCOPE_OUTSIDE_ALLOWED_PATH / VC_SCOPE_BROAD_SEARCH_PATH blocked (exit 2)
# ---------------------------------------------------------------------------

class TestScopeChecks:
    """AC3: Paths outside Allowed Paths or broad search paths are blocked."""

    def test_outside_allowed_path_blocked_exit2(self):
        """GIVEN a VC referencing a path outside Allowed Paths
        WHEN check_vc_scope.py is run
        THEN exit code is 2 and VC_SCOPE_OUTSIDE_ALLOWED_PATH is present
        """
        exit_code, stdout, stderr, artifact = run_checker("outside_allowed_path.md")
        assert exit_code == 2, f"Expected exit 2 (blocked), got {exit_code}. stderr: {stderr}"
        findings = parse_findings_from_artifact(artifact)
        assert has_reason_code(findings, "VC_SCOPE_OUTSIDE_ALLOWED_PATH"), (
            f"Expected VC_SCOPE_OUTSIDE_ALLOWED_PATH in findings: {findings}"
        )

    def test_broad_search_path_blocked_exit2(self):
        """GIVEN a VC with a broad glob path '.claude/skills/**/scripts/**'
        WHEN check_vc_scope.py is run
        THEN exit code is 2 and VC_SCOPE_BROAD_SEARCH_PATH is present
        """
        exit_code, stdout, stderr, artifact = run_checker("broad_search_path.md")
        assert exit_code == 2, f"Expected exit 2 (blocked), got {exit_code}. stderr: {stderr}"
        findings = parse_findings_from_artifact(artifact)
        assert has_reason_code(findings, "VC_SCOPE_BROAD_SEARCH_PATH"), (
            f"Expected VC_SCOPE_BROAD_SEARCH_PATH in findings: {findings}"
        )

    def test_path_within_allowed_directory_not_flagged(self):
        """GIVEN a VC with a path inside Allowed Paths directory
        WHEN check_vc_scope.py is run
        THEN VC_SCOPE_OUTSIDE_ALLOWED_PATH is NOT present
        """
        exit_code, stdout, stderr, artifact = run_checker("allowed_path_within.md")
        findings = parse_findings_from_artifact(artifact)
        assert not has_reason_code(findings, "VC_SCOPE_OUTSIDE_ALLOWED_PATH"), (
            f"Path within Allowed Paths should not trigger VC_SCOPE_OUTSIDE_ALLOWED_PATH: {findings}"
        )

    def test_absolute_path_is_blocked(self):
        """GIVEN a VC with an absolute path
        WHEN check_vc_scope.py is run
        THEN VC_SCOPE_OUTSIDE_ALLOWED_PATH is present (absolute paths are blocked)
        """
        exit_code, stdout, stderr, artifact = run_checker("absolute_path.md")
        findings = parse_findings_from_artifact(artifact)
        assert has_reason_code(findings, "VC_SCOPE_OUTSIDE_ALLOWED_PATH"), (
            f"Absolute path should trigger VC_SCOPE_OUTSIDE_ALLOWED_PATH: {findings}"
        )

    def test_parent_traversal_is_blocked(self):
        """GIVEN a VC with a '../' parent traversal path
        WHEN check_vc_scope.py is run
        THEN VC_SCOPE_OUTSIDE_ALLOWED_PATH is present
        """
        exit_code, stdout, stderr, artifact = run_checker("parent_traversal.md")
        findings = parse_findings_from_artifact(artifact)
        assert has_reason_code(findings, "VC_SCOPE_OUTSIDE_ALLOWED_PATH"), (
            f"Parent traversal path should trigger VC_SCOPE_OUTSIDE_ALLOWED_PATH: {findings}"
        )

    def test_pass_clean_fixture_is_pass(self):
        """GIVEN a VC section with all clean commands within Allowed Paths
        WHEN check_vc_scope.py is run
        THEN exit code is 0 (pass)
        """
        exit_code, stdout, stderr, artifact = run_checker("pass_clean.md")
        assert exit_code == 0, f"Expected exit 0 (pass), got {exit_code}. stdout: {stdout}. stderr: {stderr}"

    def test_outside_allowed_path_level_is_blocked(self):
        """GIVEN a VC_SCOPE_OUTSIDE_ALLOWED_PATH finding
        WHEN checking level
        THEN level is 'blocked'
        """
        exit_code, stdout, stderr, artifact = run_checker("outside_allowed_path.md")
        findings = parse_findings_from_artifact(artifact)
        outside = findings_with_code(findings, "VC_SCOPE_OUTSIDE_ALLOWED_PATH")
        assert outside, "No VC_SCOPE_OUTSIDE_ALLOWED_PATH findings found"
        for f in outside:
            assert f["level"] == "blocked", f"Expected level='blocked', got {f['level']}"


# ---------------------------------------------------------------------------
# AC4: prose note only does not block
# ---------------------------------------------------------------------------

class TestProseNoteOnly:
    """AC4: VC sections with only prose/comments do not produce blocked exit."""

    def test_prose_note_only_does_not_block(self):
        """GIVEN a VC section with only comment lines (no executable commands)
        WHEN check_vc_scope.py is run
        THEN exit code is 0 or 1 (not 2/blocked)
        """
        exit_code, stdout, stderr, artifact = run_checker("prose_note_only.md")
        assert exit_code in (0, 1), (
            f"Prose-only VC should not block (exit 2), got {exit_code}. stdout: {stdout}"
        )

    def test_prose_note_python3_in_background_does_not_block(self):
        """GIVEN a body where python3 appears only in Background / Out of Scope sections
        WHEN check_vc_scope.py is run
        THEN VC_LEGACY_PYTHON3 is NOT present in findings
        """
        body = """## Background

python3 calls are slow. We use uv run instead.

## Out of Scope

- Direct python3 invocations

## Verification Commands

```bash
# Only a comment here
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/fixtures/`
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        findings = parse_findings_from_artifact(artifact)
        assert not has_reason_code(findings, "VC_LEGACY_PYTHON3"), (
            f"python3 in prose should not trigger VC_LEGACY_PYTHON3: {findings}"
        )

    def test_prose_vc_prose_reference_only_info(self):
        """GIVEN a body with no commands in VC section
        WHEN checking artifact status
        THEN status is 'pass' and no blocking findings
        """
        body = """## Verification Commands

```bash
# This section is intentionally empty - verification is manual
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/fixtures/`
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        assert exit_code == 0, f"Empty VC should be pass (exit 0), got {exit_code}"
        findings = parse_findings_from_artifact(artifact)
        blocked = [f for f in findings if f["level"] == "blocked"]
        assert not blocked, f"No blocked findings expected: {blocked}"


# ---------------------------------------------------------------------------
# AC5: stdout only contains allowed keys
# ---------------------------------------------------------------------------

class TestStdoutSchema:
    """AC5: stdout must only contain lines with allowed key prefixes."""

    def _check_stdout_keys(self, stdout: str) -> list[str]:
        """Return list of lines with disallowed keys."""
        bad_lines = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            # Find key (part before ':')
            parts = line.split(":", 1)
            if len(parts) >= 1:
                key = parts[0].strip()
                if key not in ALLOWED_STDOUT_KEYS:
                    bad_lines.append(line)
        return bad_lines

    def test_stdout_only_allowed_keys_pass(self):
        """GIVEN a clean issue body
        WHEN check_vc_scope.py runs
        THEN stdout contains only allowed key lines
        """
        exit_code, stdout, stderr, artifact = run_checker("pass_clean.md")
        bad = self._check_stdout_keys(stdout)
        assert not bad, f"Disallowed stdout lines found: {bad}"

    def test_stdout_only_allowed_keys_blocked(self):
        """GIVEN an issue body with blocked findings
        WHEN check_vc_scope.py runs
        THEN stdout contains only allowed key lines
        """
        exit_code, stdout, stderr, artifact = run_checker("legacy_python3.md")
        bad = self._check_stdout_keys(stdout)
        assert not bad, f"Disallowed stdout lines found: {bad}"

    def test_stdout_only_allowed_keys_warn(self):
        """GIVEN an issue body with warn findings
        WHEN check_vc_scope.py runs
        THEN stdout contains only allowed key lines
        """
        exit_code, stdout, stderr, artifact = run_checker("missing_dollar_prefix.md")
        bad = self._check_stdout_keys(stdout)
        assert not bad, f"Disallowed stdout lines found: {bad}"

    def test_stdout_contains_status_line(self):
        """GIVEN any issue body
        WHEN check_vc_scope.py runs
        THEN stdout contains a STATUS line
        """
        exit_code, stdout, stderr, artifact = run_checker("pass_clean.md")
        assert any(line.startswith("STATUS:") for line in stdout.splitlines()), (
            f"stdout missing STATUS line: {stdout}"
        )

    def test_stdout_contains_artifact_line(self):
        """GIVEN any issue body
        WHEN check_vc_scope.py runs
        THEN stdout contains an ARTIFACT line
        """
        exit_code, stdout, stderr, artifact = run_checker("pass_clean.md")
        assert any(line.startswith("ARTIFACT:") for line in stdout.splitlines()), (
            f"stdout missing ARTIFACT line: {stdout}"
        )

    def test_artifact_json_exit_code_matches_process_exit(self):
        """GIVEN various fixture files
        WHEN check_vc_scope.py runs
        THEN artifact JSON exit_code always matches actual process exit code
        """
        fixtures_to_test = [
            ("pass_clean.md", 0),
            ("missing_dollar_prefix.md", 1),
            ("legacy_python3.md", 2),
            ("outside_allowed_path.md", 2),
            ("broad_search_path.md", 2),
        ]
        for fixture_name, expected_exit in fixtures_to_test:
            exit_code, stdout, stderr, artifact = run_checker(fixture_name)
            assert exit_code == expected_exit, (
                f"{fixture_name}: expected exit {expected_exit}, got {exit_code}"
            )
            assert artifact.get("exit_code") == exit_code, (
                f"{fixture_name}: artifact exit_code {artifact.get('exit_code')} != process exit {exit_code}"
            )

    def test_no_ansi_escape_in_stdout(self):
        """GIVEN any issue body
        WHEN check_vc_scope.py runs
        THEN stdout contains no ANSI escape sequences
        """
        import re as _re
        ansi_re = _re.compile(r"\x1b\[[0-9;]*[mGKH]")
        for fixture in ["pass_clean.md", "legacy_python3.md", "missing_dollar_prefix.md"]:
            exit_code, stdout, stderr, artifact = run_checker(fixture)
            assert not ansi_re.search(stdout), (
                f"{fixture}: ANSI escape found in stdout: {repr(stdout)}"
            )


# ---------------------------------------------------------------------------
# Additional false positive / negative tests
# ---------------------------------------------------------------------------

class TestFalsePositiveNegative:
    """Tests to prevent false positives and false negatives per Notes for Reviewer."""

    def test_tc4_python3_script_in_allowed_path_triggers_legacy(self):
        """TC4: python3 .claude/skills/issue-refinement-loop/scripts/foo.py -> VC_LEGACY_PYTHON3 blocked."""
        body = """## Verification Commands

```bash
$ python3 .claude/skills/issue-refinement-loop/scripts/foo.py
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/fixtures/`
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        assert exit_code == 2, f"Expected exit 2 (blocked), got {exit_code}"
        findings = parse_findings_from_artifact(artifact)
        assert has_reason_code(findings, "VC_LEGACY_PYTHON3"), (
            f"Expected VC_LEGACY_PYTHON3: {findings}"
        )

    def test_tc5_broad_glob_triggers_broad_search(self):
        """TC5: .claude/skills/**/scripts/** -> VC_SCOPE_BROAD_SEARCH_PATH blocked."""
        exit_code, stdout, stderr, artifact = run_checker("broad_search_path.md")
        assert exit_code == 2, f"Expected exit 2 (blocked), got {exit_code}"
        findings = parse_findings_from_artifact(artifact)
        assert has_reason_code(findings, "VC_SCOPE_BROAD_SEARCH_PATH"), (
            f"Expected VC_SCOPE_BROAD_SEARCH_PATH: {findings}"
        )

    def test_tc6_allowed_subdirectory_path_passes(self):
        """TC6: .claude/skills/issue-refinement-loop/tests/fixtures/vc_scope/sample.md -> allowed."""
        body = """## Verification Commands

```bash
$ rg -n "foo" .claude/skills/issue-refinement-loop/tests/fixtures/vc_scope/sample.md
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/fixtures/`
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        findings = parse_findings_from_artifact(artifact)
        assert not has_reason_code(findings, "VC_SCOPE_OUTSIDE_ALLOWED_PATH"), (
            f"Allowed subdirectory path should not be flagged: {findings}"
        )

    def test_tc7_other_skill_path_is_blocked(self):
        """TC7: .claude/skills/other-skill/scripts/foo.py -> VC_SCOPE_OUTSIDE_ALLOWED_PATH blocked."""
        exit_code, stdout, stderr, artifact = run_checker("outside_allowed_path.md")
        assert exit_code == 2, f"Expected exit 2 (blocked), got {exit_code}"
        findings = parse_findings_from_artifact(artifact)
        assert has_reason_code(findings, "VC_SCOPE_OUTSIDE_ALLOWED_PATH"), (
            f"Expected VC_SCOPE_OUTSIDE_ALLOWED_PATH: {findings}"
        )

    def test_tc8_parent_traversal_is_blocked(self):
        """TC8: ../.claude/skills/... -> blocked."""
        exit_code, stdout, stderr, artifact = run_checker("parent_traversal.md")
        findings = parse_findings_from_artifact(artifact)
        assert has_reason_code(findings, "VC_SCOPE_OUTSIDE_ALLOWED_PATH"), (
            f"Parent traversal should trigger VC_SCOPE_OUTSIDE_ALLOWED_PATH: {findings}"
        )

    def test_tc8_absolute_path_is_blocked(self):
        """TC8: absolute path -> blocked."""
        exit_code, stdout, stderr, artifact = run_checker("absolute_path.md")
        findings = parse_findings_from_artifact(artifact)
        assert has_reason_code(findings, "VC_SCOPE_OUTSIDE_ALLOWED_PATH"), (
            f"Absolute path should trigger VC_SCOPE_OUTSIDE_ALLOWED_PATH: {findings}"
        )

    def test_tc9_prose_python3_in_background_not_blocked(self):
        """TC9: Background section python3 does not block."""
        body = """## Background

Example of bad practice: python3 script.py or calling .claude/skills/other-skill/foo.py

## Out of Scope

- python3 .claude/skills/other-skill/bad.py

## Verification Commands

```bash
# No executable commands
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/fixtures/`
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        assert exit_code in (0, 1), f"Prose python3/path should not block, got exit {exit_code}"
        findings = parse_findings_from_artifact(artifact)
        assert not has_reason_code(findings, "VC_LEGACY_PYTHON3"), (
            f"prose python3 should not trigger VC_LEGACY_PYTHON3: {findings}"
        )
        assert not has_reason_code(findings, "VC_SCOPE_OUTSIDE_ALLOWED_PATH"), (
            f"prose path should not trigger VC_SCOPE_OUTSIDE_ALLOWED_PATH: {findings}"
        )

    def test_tc10_parse_indeterminate_is_warn(self):
        """TC10: Unparseable/complex quoted command -> VC_PARSE_INDETERMINATE warn."""
        exit_code, stdout, stderr, artifact = run_checker("parse_indeterminate.md")
        # Should be warn or pass, not blocked
        # Note: parse_indeterminate fixture has unclosed quote which raises ValueError
        findings = parse_findings_from_artifact(artifact)
        # If any parse indeterminate finding exists, it should be warn
        parse_findings = findings_with_code(findings, "VC_PARSE_INDETERMINATE")
        for f in parse_findings:
            assert f["level"] == "warn", f"VC_PARSE_INDETERMINATE should be warn: {f}"

    def test_tc11_stdout_only_allowed_keys_all_fixtures(self):
        """TC11: stdout is permitted keys only across all fixture files."""
        fixture_files = list(FIXTURES_DIR.glob("*.md"))
        assert fixture_files, "No fixture files found"
        for fixture_path in fixture_files:
            exit_code, stdout, stderr, artifact = run_checker(fixture_path.name)
            for line in stdout.splitlines():
                if not line.strip():
                    continue
                parts = line.split(":", 1)
                key = parts[0].strip()
                assert key in ALLOWED_STDOUT_KEYS, (
                    f"{fixture_path.name}: disallowed stdout key '{key}' in line: {line!r}"
                )

    def test_tc12_artifact_exit_code_consistency_all_fixtures(self):
        """TC12: artifact JSON exit_code matches process exit across all fixture files."""
        fixture_files = list(FIXTURES_DIR.glob("*.md"))
        assert fixture_files, "No fixture files found"
        for fixture_path in fixture_files:
            exit_code, stdout, stderr, artifact = run_checker(fixture_path.name)
            if artifact:
                assert artifact.get("exit_code") == exit_code, (
                    f"{fixture_path.name}: artifact exit_code {artifact.get('exit_code')} "
                    f"!= process exit {exit_code}"
                )

    def test_tc2_rg_regex_python3_false_positive(self):
        """TC2: rg -F "^python3 " regex string should not be mistaken for legacy python."""
        exit_code, stdout, stderr, artifact = run_checker("rg_regex_false_positive.md")
        findings = parse_findings_from_artifact(artifact)
        assert not has_reason_code(findings, "VC_LEGACY_PYTHON3"), (
            f"rg -F string should not trigger VC_LEGACY_PYTHON3: {findings}"
        )

    def test_tc3_uv_run_locked_pytest_false_positive(self):
        """TC3: uv run --locked pytest should not trigger VC_LEGACY_PYTHON3."""
        body = """## Verification Commands

```bash
$ uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_vc_scope.py
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/fixtures/`
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        findings = parse_findings_from_artifact(artifact)
        assert not has_reason_code(findings, "VC_LEGACY_PYTHON3"), (
            f"uv run --locked pytest should not trigger VC_LEGACY_PYTHON3: {findings}"
        )

    def test_artifact_schema_version(self):
        """GIVEN any issue body
        WHEN check_vc_scope.py runs
        THEN artifact has correct schema_version
        """
        exit_code, stdout, stderr, artifact = run_checker("pass_clean.md")
        assert artifact.get("schema_version") == "vc_scope_check.v1", (
            f"Unexpected schema_version: {artifact.get('schema_version')}"
        )

    def test_artifact_allowed_paths_populated(self):
        """GIVEN an issue body with Allowed Paths
        WHEN check_vc_scope.py runs
        THEN artifact.allowed_paths is populated
        """
        exit_code, stdout, stderr, artifact = run_checker("pass_clean.md")
        assert artifact.get("allowed_paths"), f"artifact.allowed_paths should not be empty: {artifact}"


# ---------------------------------------------------------------------------
# Fix 1: Allowed Paths bullet format without backtick code span
# ---------------------------------------------------------------------------

class TestAllowedPathsBulletFormat:
    """Fix 1: Bullet entries without backtick code spans are parsed correctly."""

    def test_bullet_without_backtick_parses_path(self):
        """GIVEN Allowed Paths entries without backtick code spans (e.g. Issue #793 form)
        WHEN check_vc_scope.py runs
        THEN allowed_paths are populated and path check works correctly
        """
        body = """## Verification Commands

```bash
$ rg -n "foo" .claude/skills/issue-refinement-loop/scripts/check_vc_scope.py
```

## Allowed Paths

- .claude/skills/issue-refinement-loop/scripts/check_vc_scope.py（新規）
- .claude/skills/issue-refinement-loop/tests/test_vc_scope.py（新規）
- .claude/skills/issue-refinement-loop/tests/fixtures/（fixture 追加のみ）
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        assert artifact.get("allowed_paths"), (
            f"allowed_paths should be populated from bullet-without-backtick form: {artifact}"
        )
        paths = artifact.get("allowed_paths", [])
        # Annotations like （新規） must be stripped
        for p in paths:
            assert "（" not in p, f"Full-width paren annotation not stripped from path: {p!r}"
            assert "(" not in p, f"ASCII paren annotation not stripped from path: {p!r}"
        # Correct path should appear
        assert ".claude/skills/issue-refinement-loop/scripts/check_vc_scope.py" in paths, (
            f"Expected path not found in: {paths}"
        )

    def test_bullet_without_backtick_scope_check_works(self):
        """GIVEN Allowed Paths without backtick and a path in the VC that is outside Allowed Paths
        WHEN check_vc_scope.py runs
        THEN VC_SCOPE_OUTSIDE_ALLOWED_PATH is still detected (scope check is functional)
        """
        body = """## Verification Commands

```bash
$ rg -n "foo" .claude/skills/other-skill/scripts/foo.py
```

## Allowed Paths

- .claude/skills/issue-refinement-loop/scripts/check_vc_scope.py（新規）
- .claude/skills/issue-refinement-loop/tests/test_vc_scope.py（新規）
- .claude/skills/issue-refinement-loop/tests/fixtures/（fixture 追加のみ）
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        assert exit_code == 2, f"Expected exit 2 (blocked), got {exit_code}"
        findings = parse_findings_from_artifact(artifact)
        assert has_reason_code(findings, "VC_SCOPE_OUTSIDE_ALLOWED_PATH"), (
            f"Expected VC_SCOPE_OUTSIDE_ALLOWED_PATH with bullet-without-backtick Allowed Paths: {findings}"
        )

    def test_bullet_without_backtick_path_within_allowed_passes(self):
        """GIVEN Allowed Paths without backtick and a VC path within the allowed dir
        WHEN check_vc_scope.py runs
        THEN no VC_SCOPE_OUTSIDE_ALLOWED_PATH (path is correctly allowed)
        """
        body = """## Verification Commands

```bash
$ rg -n "foo" .claude/skills/issue-refinement-loop/tests/fixtures/vc_scope/sample.md
```

## Allowed Paths

- .claude/skills/issue-refinement-loop/scripts/check_vc_scope.py（新規）
- .claude/skills/issue-refinement-loop/tests/test_vc_scope.py（新規）
- .claude/skills/issue-refinement-loop/tests/fixtures/（fixture 追加のみ）
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        findings = parse_findings_from_artifact(artifact)
        assert not has_reason_code(findings, "VC_SCOPE_OUTSIDE_ALLOWED_PATH"), (
            f"Path within allowed dir should not be blocked: {findings}"
        )

    def test_fixture_pass_clean_uses_annotation_form(self):
        """GIVEN pass_clean.md fixture which uses （注記） annotation form
        WHEN check_vc_scope.py runs
        THEN allowed_paths are parsed correctly (no （ in paths)
        """
        exit_code, stdout, stderr, artifact = run_checker("pass_clean.md")
        for p in artifact.get("allowed_paths", []):
            assert "（" not in p, f"Full-width paren annotation not stripped: {p!r}"
        assert exit_code == 0, f"pass_clean.md should exit 0, got {exit_code}"


# ---------------------------------------------------------------------------
# Fix 2: VC_PARSE_INDETERMINATE is emitted (not silently swallowed)
# ---------------------------------------------------------------------------

class TestParseIndeterminateEmitted:
    """Fix 2: tokenizer failure must emit VC_PARSE_INDETERMINATE finding."""

    def test_parse_indeterminate_finding_present(self):
        """GIVEN a VC command with unclosed quote
        WHEN check_vc_scope.py runs
        THEN VC_PARSE_INDETERMINATE finding is present in artifact
        """
        exit_code, stdout, stderr, artifact = run_checker("parse_indeterminate.md")
        findings = parse_findings_from_artifact(artifact)
        assert has_reason_code(findings, "VC_PARSE_INDETERMINATE"), (
            f"Expected VC_PARSE_INDETERMINATE finding: {findings}"
        )

    def test_parse_indeterminate_exit_is_warn(self):
        """GIVEN a VC command that triggers VC_PARSE_INDETERMINATE
        WHEN check_vc_scope.py runs
        THEN exit code is 1 (warn), not 0 or 2
        """
        exit_code, stdout, stderr, artifact = run_checker("parse_indeterminate.md")
        findings = parse_findings_from_artifact(artifact)
        parse_findings = findings_with_code(findings, "VC_PARSE_INDETERMINATE")
        assert parse_findings, "No VC_PARSE_INDETERMINATE findings found"
        assert exit_code == 1, (
            f"VC_PARSE_INDETERMINATE should yield exit 1 (warn), got {exit_code}"
        )

    def test_parse_indeterminate_level_is_warn(self):
        """GIVEN a VC_PARSE_INDETERMINATE finding
        WHEN checking level
        THEN level is 'warn' not 'blocked'
        """
        exit_code, stdout, stderr, artifact = run_checker("parse_indeterminate.md")
        findings = parse_findings_from_artifact(artifact)
        for f in findings_with_code(findings, "VC_PARSE_INDETERMINATE"):
            assert f["level"] == "warn", f"Expected level=warn, got {f['level']}"


# ---------------------------------------------------------------------------
# Fix 3: Compound command with bare python3 after && is detected
# ---------------------------------------------------------------------------

class TestCompoundCommandLegacyPython:
    """Fix 3: Bare python3 after && in a compound command is flagged."""

    def test_uv_run_and_python3_compound_is_blocked(self):
        """GIVEN 'uv run pytest ... && python3 ...' compound command
        WHEN check_vc_scope.py runs
        THEN VC_LEGACY_PYTHON3 is present (python3 after && is bare)
        """
        body = """## Verification Commands

```bash
$ uv run pytest .claude/skills/issue-refinement-loop/tests/test_vc_scope.py \
  && python3 .claude/skills/issue-refinement-loop/scripts/check_vc_scope.py
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/fixtures/`
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        assert exit_code == 2, f"Expected exit 2 (blocked), got {exit_code}"
        findings = parse_findings_from_artifact(artifact)
        assert has_reason_code(findings, "VC_LEGACY_PYTHON3"), (
            f"Expected VC_LEGACY_PYTHON3 for python3 after &&: {findings}"
        )

    def test_compound_uv_run_only_is_not_blocked(self):
        """GIVEN 'uv run pytest ... && uv run pytest ...' compound (no bare python3)
        WHEN check_vc_scope.py runs
        THEN VC_LEGACY_PYTHON3 is NOT present
        """
        body = """## Verification Commands

```bash
$ uv run pytest .claude/skills/issue-refinement-loop/tests/test_vc_scope.py -v \
  && uv run pytest .claude/skills/issue-refinement-loop/tests/test_vc_scope.py -k "not slow"
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/fixtures/`
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        findings = parse_findings_from_artifact(artifact)
        assert not has_reason_code(findings, "VC_LEGACY_PYTHON3"), (
            f"uv run && uv run should not trigger VC_LEGACY_PYTHON3: {findings}"
        )

    def test_compound_with_fixture(self):
        """GIVEN compound_legacy_python3.md fixture
        WHEN check_vc_scope.py runs
        THEN exit 2 and VC_LEGACY_PYTHON3 is blocked
        """
        exit_code, stdout, stderr, artifact = run_checker("compound_legacy_python3.md")
        assert exit_code == 2, f"Expected exit 2 (blocked), got {exit_code}. stderr: {stderr}"
        findings = parse_findings_from_artifact(artifact)
        assert has_reason_code(findings, "VC_LEGACY_PYTHON3"), (
            f"Expected VC_LEGACY_PYTHON3 in findings: {findings}"
        )


# ---------------------------------------------------------------------------
# Fix 4: File entry exact match (no pseudo-subpath)
# ---------------------------------------------------------------------------

class TestFileEntryExactMatch:
    """Fix 4: file entry in Allowed Paths only allows exact match, not pseudo-subpath."""

    def test_file_entry_pseudo_subpath_is_blocked(self):
        """GIVEN Allowed Paths contains a file entry (no trailing /)
        WHEN a VC references a pseudo-subpath of that file entry
        THEN VC_SCOPE_OUTSIDE_ALLOWED_PATH is triggered (file entry is exact-match only)
        """
        body = """## Verification Commands

```bash
$ rg -n "foo" .claude/skills/issue-refinement-loop/scripts/check_vc_scope.py/evil
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/fixtures/`
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        assert exit_code == 2, (
            f"Pseudo-subpath of file entry should be blocked (exit 2), got {exit_code}"
        )
        findings = parse_findings_from_artifact(artifact)
        assert has_reason_code(findings, "VC_SCOPE_OUTSIDE_ALLOWED_PATH"), (
            f"Pseudo-subpath should trigger VC_SCOPE_OUTSIDE_ALLOWED_PATH: {findings}"
        )

    def test_file_entry_exact_match_passes(self):
        """GIVEN Allowed Paths contains a file entry
        WHEN a VC references exactly that file
        THEN no VC_SCOPE_OUTSIDE_ALLOWED_PATH
        """
        body = """## Verification Commands

```bash
$ rg -n "foo" .claude/skills/issue-refinement-loop/scripts/check_vc_scope.py
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/fixtures/`
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        findings = parse_findings_from_artifact(artifact)
        assert not has_reason_code(findings, "VC_SCOPE_OUTSIDE_ALLOWED_PATH"), (
            f"Exact file entry match should not be blocked: {findings}"
        )

    def test_dir_entry_subpath_passes(self):
        """GIVEN Allowed Paths contains a dir entry (trailing /)
        WHEN a VC references a subpath within that directory
        THEN no VC_SCOPE_OUTSIDE_ALLOWED_PATH (dir entry allows descendants)
        """
        body = """## Verification Commands

```bash
$ rg -n "foo" .claude/skills/issue-refinement-loop/tests/fixtures/vc_scope/sample.md
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/fixtures/`
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        findings = parse_findings_from_artifact(artifact)
        assert not has_reason_code(findings, "VC_SCOPE_OUTSIDE_ALLOWED_PATH"), (
            f"Dir entry subpath should not be blocked: {findings}"
        )


# ---------------------------------------------------------------------------
# Fix 5: shlex-based tokenizer - URL and regex pattern false positive prevention
# ---------------------------------------------------------------------------

class TestShlexTokenizerFalsePositives:
    """Fix 5: shlex-based tokenizer does not confuse URLs or regex patterns for paths."""

    def test_url_in_rg_pattern_not_flagged_as_path(self):
        """GIVEN a VC command that uses rg with a URL-like string as a pattern
        WHEN check_vc_scope.py runs
        THEN the URL is not treated as a file path (no false VC_SCOPE_OUTSIDE_ALLOWED_PATH)
        """
        body = """## Verification Commands

```bash
$ rg -n "https://example.com/api" .claude/skills/issue-refinement-loop/scripts/check_vc_scope.py
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/fixtures/`
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        findings = parse_findings_from_artifact(artifact)
        outside = findings_with_code(findings, "VC_SCOPE_OUTSIDE_ALLOWED_PATH")
        # If outside findings exist, none should be due to the URL
        for f in outside:
            assert "https://" not in f.get("evidence", ""), (
                f"URL should not be treated as file path: {f}"
            )

    def test_rg_e_pattern_option_not_treated_as_path(self):
        """GIVEN a VC with 'rg -e PATTERN path' where PATTERN contains /
        WHEN check_vc_scope.py runs
        THEN the PATTERN is skipped (flag -e takes next token as value)
        and the actual path is the only one checked
        """
        body = """## Verification Commands

```bash
$ rg -e "some/pattern" .claude/skills/issue-refinement-loop/scripts/check_vc_scope.py
```

## Allowed Paths

- `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/test_vc_scope.py`
- `.claude/skills/issue-refinement-loop/tests/fixtures/`
"""
        exit_code, stdout, stderr, artifact = run_checker(body=body)
        findings = parse_findings_from_artifact(artifact)
        # "some/pattern" should not be flagged as outside allowed paths
        outside = findings_with_code(findings, "VC_SCOPE_OUTSIDE_ALLOWED_PATH")
        pattern_blocked = [f for f in outside if "some/pattern" in f.get("evidence", "")]
        assert not pattern_blocked, (
            f"rg -e pattern should not be treated as file path: {pattern_blocked}"
        )
