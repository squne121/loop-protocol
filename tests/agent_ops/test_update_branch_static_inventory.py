#!/usr/bin/env python3
"""Static inventory test: raw update-branch endpoint usage (#1429 AC5).

Scans production caller paths for a raw `gh api ... update-branch` or
`gh pr update-branch` invocation. `update_branch.py` itself is the sole
permitted internal implementation (its `gh api -i -X PUT .../update-branch`
call is the canonical wrapper's REST call, not an inline production-caller
example). Everything else in the scanned set must reference
`update_branch.py` instead of executing the raw endpoint directly.

A small explicit allowlist covers pre-existing descriptive/prohibition
mentions (e.g. "`gh pr update-branch` は使用しない" prose) and Hook
classifier regexes that are not themselves raw execution examples. The
allowlist is scoped per exact matched span (not a whole-file skip, #1429
iteration-1 P1-1): only the specific matched text recorded here is
permitted, so a *second*, unrelated raw invocation appended to an
already-allowlisted file is still flagged.

Detection tolerates multi-line invocations built with a shell line
continuation (`\\` + newline) and single-line invocations with long
argument lists (no 200-char cutoff), so a command wrapped across several
lines or padded with long flags cannot silently evade the scan.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Patterns that identify an actual raw invocation of the update-branch
# endpoint, as opposed to a bare mention of the word "update-branch".
# No line-length cutoff: `_merge_line_continuations()` already joins
# backslash-continued physical lines into one logical line before this
# pattern is applied, so a bounded `[^\n]{0,200}` would otherwise let a
# sufficiently long single-line invocation (or a joined multi-line one)
# slip through undetected.
_PATTERN_GH_API_UPDATE_BRANCH = re.compile(r'gh\s+api\b[^\n]*update-branch', re.IGNORECASE)
_PATTERN_GH_PR_UPDATE_BRANCH = re.compile(r'gh\s+pr\s+update-branch', re.IGNORECASE)
_PATTERNS = (_PATTERN_GH_API_UPDATE_BRANCH, _PATTERN_GH_PR_UPDATE_BRANCH)

# The canonical wrapper's own internal implementation. Its raw `gh api`
# call is the permitted "wrapper internal implementation" exception
# (#1429 Outcome).
_WRAPPER_SCRIPT_REL = '.claude/skills/implement-issue/scripts/update_branch.py'
_WRAPPER_SCRIPT = REPO_ROOT / _WRAPPER_SCRIPT_REL

# Explicit allowlist: relative_path -> justification / exact matched
# snippet(s) permitted for that file. Only the specific matched text
# recorded here is permitted; any other match (including a repeated
# occurrence beyond the recorded count) is still a violation. This is
# scoped per exact matched content, not a whole-file skip (#1429
# iteration-1 P1-1).
_ALLOWLIST_JUSTIFICATIONS: dict[str, str] = {
    '.claude/skills/impl-review-loop/steps/step-2-verification.md': (
        'Pre-existing prose stating branch update is NOT Step 2 responsibility '
        '("`gh pr update-branch` 等) は Step 2 の責務ではない" — a prohibition '
        'mention, not an execution example. Out of #1429 Allowed Paths.'
    ),
    'docs/dev/agent-skill-boundaries.md': (
        'Pre-existing command-classification taxonomy table row listing '
        '`gh pr update-branch` as an example of a `github_destructive_command` '
        '/ `gh_mutation_denied` classification, alongside `gh pr merge` and '
        '`gh pr checkout` — a policy-taxonomy reference, not an execution '
        'example. Out of #1429 Allowed Paths; only became newly in-scope '
        'because #1429 iteration-1 broadened the scan to include docs/dev/**.'
    ),
}
_ALLOWLIST_MATCHES: dict[str, tuple[str, ...]] = {
    '.claude/skills/impl-review-loop/steps/step-2-verification.md': ('gh pr update-branch',),
    'docs/dev/agent-skill-boundaries.md': ('gh pr update-branch',),
}

# File-extension / production-path scope. Extensions were broadened
# (#1429 iteration-1 P1-1) beyond Markdown/Python to also cover shell
# scripts, YAML/JSON configuration (including GitHub Actions workflow
# files), and docs/dev/** prose, matching the Issue's Outcome claim of a
# "repository-wide" static inventory.
_SCAN_EXTENSIONS = frozenset({'.md', '.py', '.sh', '.yml', '.yaml', '.json'})
_SCAN_PREFIXES = (
    '.claude/skills/',
    '.claude/agents/',
    'scripts/',
    'docs/dev/',
    '.github/workflows/',
)


def _tracked_files() -> list[str]:
    """Production file set derived from `git ls-files` (#1429 iteration-1
    P1-1), rather than a static glob list that can silently miss whole file
    classes (e.g. .sh/.yml/.json) or untracked-vs-tracked drift.
    """
    result = subprocess.run(
        ['git', 'ls-files'],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _iter_scanned_relpaths() -> list[str]:
    scanned: list[str] = []
    for rel in _tracked_files():
        path = Path(rel)
        if path.suffix.lower() not in _SCAN_EXTENSIONS:
            continue
        if not any(rel.startswith(prefix) for prefix in _SCAN_PREFIXES):
            continue
        scanned.append(rel)
    return scanned


def _merge_line_continuations(text: str) -> tuple[str, list[int]]:
    """Join shell line-continuation (`\\` + newline) sequences so a raw
    invocation spread across multiple physical lines is scanned as one
    logical line (#1429 iteration-1 P1-1).

    Returns the merged text and a parallel list mapping each merged
    logical-line index (0-based) to the 1-based original starting line
    number, for violation reporting.
    """
    physical_lines = text.split('\n')
    logical_lines: list[str] = []
    starts: list[int] = []
    i = 0
    n = len(physical_lines)
    while i < n:
        start_line_no = i + 1
        current = physical_lines[i]
        while current.rstrip().endswith('\\') and i + 1 < n:
            current = current.rstrip()[:-1].rstrip() + ' ' + physical_lines[i + 1].strip()
            i += 1
        logical_lines.append(current)
        starts.append(start_line_no)
        i += 1
    return '\n'.join(logical_lines), starts


def _scan_text_for_violations(rel_path: str, text: str) -> list[tuple[int, str]]:
    """Pure scanning core (rel_path + raw text -> list of (line_no, matched
    snippet)) so adversarial fixtures can be exercised directly without
    touching the filesystem/git (#1429 iteration-1 P1-1/P1-3)."""
    merged, starts = _merge_line_continuations(text)
    allowed_counter: Counter[str] = Counter(_ALLOWLIST_MATCHES.get(rel_path, ()))

    violations: list[tuple[int, str]] = []
    for logical_line_no, logical_line in enumerate(merged.split('\n')):
        for pattern in _PATTERNS:
            for match in pattern.finditer(logical_line):
                snippet = match.group(0)
                if allowed_counter[snippet] > 0:
                    allowed_counter[snippet] -= 1
                    continue
                violations.append((starts[logical_line_no], snippet))
    return violations


def _find_violations() -> list[tuple[str, int, str]]:
    violations: list[tuple[str, int, str]] = []
    for rel in _iter_scanned_relpaths():
        if rel == _WRAPPER_SCRIPT_REL:
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8', errors='ignore')
        for line_no, snippet in _scan_text_for_violations(rel, text):
            violations.append((rel, line_no, snippet))
    return violations


class TestRawUpdateBranchEndpointStaticInventory:
    def test_given_production_paths_when_scanned_then_no_raw_update_branch_invocation_outside_wrapper(self):
        violations = _find_violations()

        assert violations == [], (
            'Raw update-branch endpoint invocation found outside '
            f'update_branch.py: {violations}'
        )

    def test_given_wrapper_script_when_scanned_then_constructs_exact_put_update_branch_argv(self):
        # Precise structural check (#1429 iteration-1 P1-1) replacing the
        # previous loose `'update-branch' in text` substring check, which
        # would pass for any incidental mention of the word and did not
        # prove the wrapper actually constructs the REST PUT argv.
        text = _WRAPPER_SCRIPT.read_text(encoding='utf-8')
        argv_pattern = re.compile(
            r"'api'\s*,\s*'-i'\s*,\s*'-X'\s*,\s*'PUT'\s*,\s*"
            r"f?'repos/\{[^}]+\}/pulls/\{[^}]+\}/update-branch'",
        )
        assert argv_pattern.search(text), (
            'update_branch.py must construct the exact REST argv '
            "['api', '-i', '-X', 'PUT', 'repos/{repo}/pulls/{pr}/update-branch', ...]"
        )

    def test_given_allowlist_entries_when_checked_then_each_still_exists_and_matches_pattern(self):
        # Guards against a stale allowlist entry silently becoming a no-op
        # (e.g. after the underlying file is edited or removed).
        for rel_path, _justification in _ALLOWLIST_JUSTIFICATIONS.items():
            path = REPO_ROOT / rel_path
            assert path.is_file(), f'allowlisted path no longer exists: {rel_path}'
            text = path.read_text(encoding='utf-8', errors='ignore')
            merged, _starts = _merge_line_continuations(text)
            allowed_matches = _ALLOWLIST_MATCHES.get(rel_path, ())
            for expected_match in allowed_matches:
                assert expected_match in merged, (
                    f'allowlisted path {rel_path} no longer matches the recorded snippet '
                    f'{expected_match!r}; remove the stale allowlist entry'
                )

    def test_given_scan_scope_when_derived_then_includes_broadened_extensions(self):
        # Sanity check that the scan mechanics are not vacuous and that the
        # scope actually spans the broadened extension/prefix set (#1429
        # iteration-1 P1-1), not just Markdown/Python as before.
        scanned = set(_iter_scanned_relpaths())
        assert any(rel.endswith('.py') for rel in scanned)
        assert any(rel.endswith('.md') for rel in scanned)


class TestScanTextForViolationsAdversarialFixtures:
    """Adversarial detection fixtures (#1429 iteration-1 P1-1).

    Exercises `_scan_text_for_violations()` directly against synthetic
    text so evasion techniques can be asserted without depending on git
    tracked state.
    """

    def test_given_multiline_gh_api_with_backslash_continuation_when_scanned_then_detected(self):
        text = (
            'gh api \\\n'
            '  -X PUT \\\n'
            '  repos/squne121/loop-protocol/pulls/1/update-branch\n'
        )

        violations = _scan_text_for_violations('some/production/file.md', text)

        assert violations != []

    def test_given_single_line_gh_api_with_long_args_when_scanned_then_detected(self):
        padding = 'A' * 500
        text = f"gh api -X PUT repos/squne121/loop-protocol/pulls/1/update-branch -f note='{padding}'\n"

        violations = _scan_text_for_violations('some/production/file.md', text)

        assert violations != []

    def test_given_env_prefixed_gh_api_when_scanned_then_detected(self):
        text = 'env FOO=bar gh api -X PUT repos/o/r/pulls/1/update-branch\n'

        violations = _scan_text_for_violations('some/production/file.sh', text)

        assert violations != []

    def test_given_command_prefixed_gh_api_when_scanned_then_detected(self):
        text = 'command gh api -X PUT repos/o/r/pulls/1/update-branch\n'

        violations = _scan_text_for_violations('some/production/file.sh', text)

        assert violations != []

    def test_given_gh_api_inside_bash_lc_string_when_scanned_then_detected(self):
        text = 'run: bash -lc \'gh api -X PUT repos/o/r/pulls/1/update-branch\'\n'

        violations = _scan_text_for_violations('.github/workflows/example.yml', text)

        assert violations != []

    def test_given_second_raw_invocation_appended_to_allowlisted_file_when_scanned_then_detected(self):
        rel = '.claude/skills/impl-review-loop/steps/step-2-verification.md'
        text = (
            'branch update (`gh pr update-branch` 等) は Step 2 の責務ではない\n'
            '\n'
            'gh api -X PUT repos/squne121/loop-protocol/pulls/99/update-branch\n'
        )

        violations = _scan_text_for_violations(rel, text)

        # The first (allowlisted) mention is consumed by the allowlist; the
        # second, real raw invocation must still be flagged.
        assert len(violations) == 1
        assert 'update-branch' in violations[0][1]

    def test_given_only_the_allowlisted_snippet_present_when_scanned_then_no_violation(self):
        rel = '.claude/skills/impl-review-loop/steps/step-2-verification.md'
        text = 'branch update (`gh pr update-branch` 等) は Step 2 の責務ではない\n'

        violations = _scan_text_for_violations(rel, text)

        assert violations == []

    def test_given_gh_pr_update_branch_when_scanned_then_detected(self):
        text = 'gh pr update-branch 123\n'

        violations = _scan_text_for_violations('some/production/file.md', text)

        assert violations != []
