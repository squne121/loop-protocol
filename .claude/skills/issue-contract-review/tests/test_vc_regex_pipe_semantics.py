#!/usr/bin/env python3
"""
Tests for VC regex pipe semantics (#589).

Validates that the VC analysis engine does not misidentify the `|` character
inside quoted regex patterns (rg, grep -E, egrep) as a shell pipeline operator.

AC1: rg -n "foo|bar" must NOT trigger VCS001 (compound_command_disallowed)
     -- it is regex alternation inside a quoted pattern, not a shell pipeline.
AC2: cmd1 | cmd2 must STILL trigger VCS001 (shell pipeline between commands).
AC3: rg -n "foo\|bar" must be classified as regex_literal_pipe_suspected and
     return decision: blocked (unless # vc-regex-intent: literal-pipe-ok annotation
     is present on the preceding line).
AC4: test-runner.md states that VC commands must be executed verbatim (checked
     via rg search over the file content).
AC5: baseline_vc_preflight.py returns regex_literal_pipe_suspected category and
     decision: blocked for rg -n "foo\|bar" -- verified by direct unit test.
AC6: #578-style fixture tests: rg -n "foo|bar" passes, rg -n "foo\|bar" -> blocked.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Paths to the scripts under test
_TESTS_DIR = Path(__file__).parent
_SKILLS_DIR = _TESTS_DIR.parent
_SCRIPTS_DIR = _SKILLS_DIR / "scripts"

PREFLIGHT_SCRIPT = _SCRIPTS_DIR / "baseline_vc_preflight.py"
CONTRACT_READINESS_SCRIPT = _SCRIPTS_DIR / "contract_readiness_check.py"

# Add scripts dir to sys.path for direct imports
sys.path.insert(0, str(_SCRIPTS_DIR))

# Locate test-runner.md (relative to repo root)
_REPO_ROOT = _SKILLS_DIR.parents[2]  # .claude/skills/issue-contract-review -> repo root
TEST_RUNNER_MD = _REPO_ROOT / ".claude" / "agents" / "test-runner.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_preflight(body_content: str, issue_num: int = 999) -> dict:
    """Run baseline_vc_preflight on a string of body content via a temp file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body_content)
        fixture_file = f.name
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(PREFLIGHT_SCRIPT),
                "--body-file",
                fixture_file,
                "--issue",
                str(issue_num),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.stdout, f"No output from preflight: stderr={result.stderr}"
        return json.loads(result.stdout)
    finally:
        os.unlink(fixture_file)


def run_contract_readiness_static(body_content: str) -> dict:
    """Run contract_readiness_check in static mode on a body string."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body_content)
        fixture_file = f.name
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(CONTRACT_READINESS_SCRIPT),
                "--body-file",
                fixture_file,
                "--mode",
                "static",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.stdout, f"No output from contract_readiness_check: stderr={result.stderr}"
        return json.loads(result.stdout)
    finally:
        os.unlink(fixture_file)


def _make_body_with_vc(vc_lines: str) -> str:
    """Wrap vc_lines in a minimal issue body with Verification Commands section."""
    return (
        "## Outcome\n\nSome outcome.\n\n"
        "## Acceptance Criteria\n\n- [ ] AC1: some condition\n\n"
        "## Verification Commands\n\n"
        "```bash\n"
        f"{vc_lines}\n"
        "```\n\n"
        "## Runtime Verification Applicability\n\n"
        "- decision: not_applicable\n"
    )


# ---------------------------------------------------------------------------
# AC1: rg "foo|bar" inside quotes must NOT trigger compound_command_disallowed
# ---------------------------------------------------------------------------


class TestAC1QuotedPipeIsNotShellPipeline:
    """AC1: Quoted pattern | in rg must not trigger VCS001."""

    def test_rg_double_quoted_alternation_passes_static_check(self):
        """AC1: rg -n "foo|bar" must not produce compound_command_disallowed error."""
        body = _make_body_with_vc('# AC1\n$ rg -n "foo|bar" .claude/')
        data = run_contract_readiness_static(body)
        compound_errors = [
            e
            for e in data.get("errors", [])
            if e.get("category") == "compound_command_disallowed"
        ]
        assert compound_errors == [], (
            f"rg with quoted alternation should NOT trigger compound_command_disallowed. "
            f"Got errors: {compound_errors}"
        )

    def test_rg_single_quoted_alternation_passes_static_check(self):
        """AC1: rg -n 'foo|bar' (single quotes) must not produce compound_command_disallowed."""
        body = _make_body_with_vc("# AC1\n$ rg -n 'foo|bar' .claude/")
        data = run_contract_readiness_static(body)
        compound_errors = [
            e
            for e in data.get("errors", [])
            if e.get("category") == "compound_command_disallowed"
        ]
        assert compound_errors == [], (
            f"rg with single-quoted alternation should NOT trigger compound_command_disallowed. "
            f"Got errors: {compound_errors}"
        )

    def test_rg_quoted_alternation_passes_preflight_classification(self):
        """AC1: preflight classify_static_command returns None for rg "foo|bar" (proceed to execute)."""
        from baseline_vc_preflight import classify_static_command

        # rg with quoted alternation should proceed to execution (not be statically blocked)
        result = classify_static_command('rg -n "foo|bar" .claude/', Path("."))
        assert result is None, (
            f'rg with quoted alternation should be allowed (return None), got: {result}'
        )

    def test_rg_quoted_alternation_full_preflight_not_blocked_by_pipe_policy(self):
        """AC1: end-to-end preflight on rg "foo|bar" must not be blocked as compound_command."""
        body = _make_body_with_vc('# AC1\n$ rg -n "foo|bar" .claude/skills/')
        data = run_preflight(body)
        results = data.get("results", [])
        assert len(results) >= 1, f"Expected at least 1 result, got: {results}"
        r = results[0]
        assert r["category"] != "compound_command_disallowed", (
            f"rg with quoted alternation must not be compound_command_disallowed. "
            f"Got category={r['category']}"
        )


# ---------------------------------------------------------------------------
# AC2: Unquoted pipe between commands must still be VCS001
# ---------------------------------------------------------------------------


class TestAC2UnquotedPipeIsShellPipeline:
    """AC2: cmd1 | cmd2 must still trigger compound_command_disallowed (VCS001)."""

    def test_unquoted_pipe_triggers_vcs001_in_static_check(self):
        """AC2: 'rg pattern . | grep foo' must trigger compound_command_disallowed."""
        body = _make_body_with_vc("# AC1\n$ rg pattern . | grep foo")
        data = run_contract_readiness_static(body)
        compound_errors = [
            e
            for e in data.get("errors", [])
            if e.get("category") == "compound_command_disallowed"
        ]
        assert compound_errors, (
            f"Unquoted shell pipe must trigger compound_command_disallowed. "
            f"Got errors: {data.get('errors', [])}"
        )
        assert data.get("status") in ("needs_fix", "human_judgment"), (
            f"Status must be needs_fix or human_judgment for compound_command. "
            f"Got: {data.get('status')}"
        )

    def test_unquoted_pipe_between_commands_static(self):
        """AC2: 'rg some-pattern src/ | sort' must trigger compound_command_disallowed."""
        body = _make_body_with_vc("# AC1\n$ rg some-pattern src/ | sort")
        data = run_contract_readiness_static(body)
        compound_errors = [
            e
            for e in data.get("errors", [])
            if e.get("category") == "compound_command_disallowed"
        ]
        assert compound_errors, (
            f"'rg ... | sort' must trigger compound_command_disallowed. "
            f"Got errors: {data.get('errors', [])}"
        )

    def test_unquoted_pipe_triggers_compound_in_preflight_classification(self):
        """AC2: classify_static_command returns compound_command_disallowed for unquoted pipe."""
        from baseline_vc_preflight import classify_static_command

        result = classify_static_command("rg pattern . | grep foo", Path("."))
        assert result is not None, "Unquoted pipe must be blocked by classify_static_command"
        classification, category, decision, fix_hint, scope_class = result
        assert category == "compound_command_disallowed", (
            f"Unquoted pipe must be compound_command_disallowed, got {category}"
        )
        assert decision == "blocked"


# ---------------------------------------------------------------------------
# AC3: rg "foo\|bar" must be regex_literal_pipe_suspected -> blocked
# ---------------------------------------------------------------------------


class TestAC3BackslashPipeIsRegexLiteralPipeSuspected:
    r"""AC3: rg pattern with \| must be classified as regex_literal_pipe_suspected and blocked."""

    def test_rg_backslash_pipe_is_regex_literal_pipe_suspected_in_preflight(self):
        r"""AC3: classify_static_command returns regex_literal_pipe_suspected for rg -n "foo\|bar"."""
        from baseline_vc_preflight import classify_static_command

        result = classify_static_command(r'rg -n "foo\|bar" .', Path("."))
        assert result is not None, (
            r'rg -n "foo\|bar" should be blocked as regex_literal_pipe_suspected'
        )
        classification, category, decision, fix_hint, scope_class = result
        assert category == "regex_literal_pipe_suspected", (
            f"Expected category=regex_literal_pipe_suspected, got {category}"
        )
        assert decision == "blocked", f"Expected decision=blocked, got {decision}"
        assert fix_hint is not None and len(fix_hint) > 0

    def test_rg_backslash_pipe_blocked_in_full_preflight(self):
        r"""AC3: end-to-end preflight on rg "foo\|bar" returns regex_literal_pipe_suspected."""
        body = _make_body_with_vc('# AC1\n$ rg -n "foo\\|bar" .')
        data = run_preflight(body)
        results = data.get("results", [])
        assert len(results) >= 1
        r = results[0]
        assert r["category"] == "regex_literal_pipe_suspected", (
            f'Expected regex_literal_pipe_suspected, got {r["category"]}'
        )
        assert r["decision"] == "blocked", f'Expected decision=blocked, got {r["decision"]}'
        assert data.get("status") == "blocked"

    def test_literal_pipe_ok_annotation_in_body_exempts_from_blocked(self):
        r"""AC3: vc-regex-intent: literal-pipe-ok annotation in body prevents blocked."""
        body = (
            "## Outcome\n\nSome outcome.\n\n"
            "## Acceptance Criteria\n\n- [ ] AC1: some condition\n\n"
            "## Verification Commands\n\n"
            "```bash\n"
            "# AC1\n"
            '# vc-regex-intent: literal-pipe-ok reason="rg uses BRE, \\| is literal"\n'
            '$ rg -n "foo\\|bar" .\n'
            "```\n\n"
            "## Runtime Verification Applicability\n\n"
            "- decision: not_applicable\n"
        )
        data = run_preflight(body)
        results = data.get("results", [])
        assert len(results) >= 1
        r = results[0]
        # With annotation, should NOT be regex_literal_pipe_suspected / blocked
        assert not (r["category"] == "regex_literal_pipe_suspected" and r["decision"] == "blocked"), (
            f"With literal-pipe-ok annotation, rg with \\| should not be blocked as "
            f"regex_literal_pipe_suspected. Got category={r['category']}, decision={r['decision']}"
        )

    def test_backslash_pipe_in_egrep_also_suspected(self):
        r"""AC3: egrep "foo\|bar" also triggers regex_literal_pipe_suspected."""
        from baseline_vc_preflight import classify_static_command

        result = classify_static_command(r'egrep "foo\|bar" somefile.txt', Path("."))
        assert result is not None, r'egrep "foo\|bar" should be blocked'
        classification, category, decision, fix_hint, scope_class = result
        assert category == "regex_literal_pipe_suspected", (
            f"egrep with \\| should be regex_literal_pipe_suspected, got {category}"
        )
        assert decision == "blocked"


# ---------------------------------------------------------------------------
# AC4: test-runner.md must mention VC verbatim execution requirement
# ---------------------------------------------------------------------------


class TestAC4TestRunnerMdVerbatim:
    """AC4: test-runner.md must explicitly state that VC commands must be executed verbatim."""

    def test_test_runner_md_mentions_verbatim_execution(self):
        """AC4: test-runner.md contains the VC verbatim execution requirement."""
        assert TEST_RUNNER_MD.exists(), f"test-runner.md not found at {TEST_RUNNER_MD}"
        content = TEST_RUNNER_MD.read_text(encoding="utf-8")
        import re
        markers = [
            r"逐語実行",
            r"verbatim",
            r"省略禁止",
            r"パターン削除.*禁止",
            r"簡略化.*禁止",
            r"置換.*禁止",
        ]
        found = any(re.search(marker, content) for marker in markers)
        assert found, (
            f"test-runner.md must mention VC verbatim execution requirement. "
            f"Searched for markers: {markers}. "
            f"File: {TEST_RUNNER_MD}"
        )


# ---------------------------------------------------------------------------
# AC5: baseline_vc_preflight classify_static_command unit tests
# ---------------------------------------------------------------------------


class TestAC5BaselineVcPreflightRegexLiteralPipe:
    r"""AC5: baseline_vc_preflight.py returns regex_literal_pipe_suspected for \| in rg pattern."""

    def test_classify_static_command_returns_regex_literal_pipe_for_rg(self):
        r"""AC5: classify_static_command('rg -n "foo\|bar" .', ...) -> regex_literal_pipe_suspected."""
        from baseline_vc_preflight import classify_static_command

        result = classify_static_command(r'rg -n "foo\|bar" .', Path("."))
        assert result is not None, "Must return a tuple (not None) for blocked command"
        classification, category, decision, fix_hint, scope_class = result
        assert category == "regex_literal_pipe_suspected"
        assert decision == "blocked"

    def test_classify_static_command_no_annotation_means_blocked(self):
        r"""AC5: without annotation, rg "foo\|bar" -> decision: blocked."""
        from baseline_vc_preflight import classify_static_command

        result = classify_static_command(r'rg "foo\|bar" src/', Path("."))
        assert result is not None
        _, category, decision, _, _ = result
        assert category == "regex_literal_pipe_suspected"
        assert decision == "blocked"

    def test_classify_static_command_rg_without_backslash_pipe_is_allowed(self):
        r"""AC5: rg without \| is allowed (returns None, proceeds to execute)."""
        from baseline_vc_preflight import classify_static_command

        result = classify_static_command('rg "foo|bar" src/', Path("."))
        assert result is None, (
            f'rg "foo|bar" should be allowed (None), got: {result}'
        )


# ---------------------------------------------------------------------------
# AC6: #578-style fixture tests (combined AC1 + AC3 fixtures)
# ---------------------------------------------------------------------------


class TestAC6Fixture578Style:
    r"""AC6: #578-style fixture tests -- rg "foo|bar" passes, rg "foo\|bar" -> blocked."""

    def test_rg_alternation_passes_classification(self):
        r"""AC6: rg -n "foo|bar" -> classify_static_command returns None (allowed)."""
        from baseline_vc_preflight import classify_static_command

        result = classify_static_command('rg -n "foo|bar" .', Path("."))
        assert result is None, (
            f'rg -n "foo|bar" (regex alternation) must be allowed; got: {result}'
        )

    def test_rg_backslash_pipe_blocked_classification(self):
        r"""AC6: rg -n "foo\|bar" -> classify_static_command returns regex_literal_pipe_suspected."""
        from baseline_vc_preflight import classify_static_command

        result = classify_static_command(r'rg -n "foo\|bar" .', Path("."))
        assert result is not None, r'rg -n "foo\|bar" must be blocked'
        _, category, decision, _, _ = result
        assert category == "regex_literal_pipe_suspected"
        assert decision == "blocked"

    def test_full_preflight_alternation_is_not_compound_blocked(self):
        """AC6: end-to-end preflight on rg "foo|bar" is not blocked as compound_command."""
        body = _make_body_with_vc('# AC1\n$ rg -n "foo|bar" .claude/skills/')
        data = run_preflight(body)
        results = data.get("results", [])
        assert len(results) >= 1
        r = results[0]
        assert r["category"] != "compound_command_disallowed", (
            f'rg "foo|bar" must not be compound_command_disallowed; got {r["category"]}'
        )

    def test_full_preflight_backslash_pipe_is_blocked(self):
        r"""AC6: end-to-end preflight on rg "foo\|bar" -> blocked / regex_literal_pipe_suspected."""
        body = _make_body_with_vc('# AC1\n$ rg -n "foo\\|bar" .claude/skills/')
        data = run_preflight(body)
        results = data.get("results", [])
        assert len(results) >= 1
        r = results[0]
        assert r["category"] == "regex_literal_pipe_suspected", (
            f'rg "foo\\|bar" must be regex_literal_pipe_suspected; got {r["category"]}'
        )
        assert r["decision"] == "blocked"
        assert data.get("status") == "blocked"
