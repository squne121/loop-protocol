#!/usr/bin/env python3
"""Static inventory test: raw update-branch endpoint usage (#1429 AC5).

Scans production caller paths for a raw `gh api ... update-branch` or
`gh pr update-branch` invocation. `update_branch.py` itself is the sole
permitted internal implementation (its `gh api -i -X PUT .../update-branch`
call is the canonical wrapper's REST call, not an inline production-caller
example). Everything else in the scanned globs must reference
`update_branch.py` instead of executing the raw endpoint directly.

A small explicit allowlist covers pre-existing descriptive/prohibition
mentions (e.g. "`gh pr update-branch` は使用しない" prose) and Hook
classifier regexes that are not themselves raw execution examples. These
files are out of this Issue's Allowed Paths and predate this contract; the
allowlist documents *why* each is safe rather than silently skipping them.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Patterns that identify an actual raw invocation of the update-branch
# endpoint, as opposed to a bare mention of the word "update-branch".
_PATTERN_GH_API_UPDATE_BRANCH = re.compile(r'gh\s+api\b[^\n]{0,200}update-branch', re.IGNORECASE)
_PATTERN_GH_PR_UPDATE_BRANCH = re.compile(r'gh\s+pr\s+update-branch', re.IGNORECASE)

# The canonical wrapper's own internal implementation. Its raw `gh api`
# call is the permitted "wrapper internal implementation" exception
# (#1429 Outcome).
_WRAPPER_SCRIPT = REPO_ROOT / '.claude' / 'skills' / 'implement-issue' / 'scripts' / 'update_branch.py'

# Explicit allowlist: (relative_path, justification). Only files that are
# provably NOT raw production-caller invocation examples belong here.
_ALLOWLIST: dict[str, str] = {
    '.claude/skills/impl-review-loop/steps/step-2-verification.md': (
        'Pre-existing prose stating branch update is NOT Step 2 responsibility '
        '("`gh pr update-branch` 等) は Step 2 の責務ではない" — a prohibition '
        'mention, not an execution example. Out of #1429 Allowed Paths.'
    ),
}

_GLOBS = (
    '.claude/skills/**/*.md',
    '.claude/agents/*.md',
    '.claude/skills/**/scripts/*.py',
    'scripts/**/*.py',
)


def _iter_scanned_files() -> list[Path]:
    seen: set[Path] = set()
    files: list[Path] = []
    for pattern in _GLOBS:
        for path in REPO_ROOT.glob(pattern):
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            files.append(path)
    return files


def _find_violations() -> list[tuple[str, int, str]]:
    violations: list[tuple[str, int, str]] = []
    for path in _iter_scanned_files():
        if path == _WRAPPER_SCRIPT:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in _ALLOWLIST:
            continue
        text = path.read_text(encoding='utf-8', errors='ignore')
        for pattern in (_PATTERN_GH_API_UPDATE_BRANCH, _PATTERN_GH_PR_UPDATE_BRANCH):
            for match in pattern.finditer(text):
                line_no = text.count('\n', 0, match.start()) + 1
                violations.append((rel, line_no, match.group(0)))
    return violations


class TestRawUpdateBranchEndpointStaticInventory:
    def test_given_production_paths_when_scanned_then_no_raw_update_branch_invocation_outside_wrapper(self):
        violations = _find_violations()

        assert violations == [], (
            'Raw update-branch endpoint invocation found outside '
            f'update_branch.py: {violations}'
        )

    def test_given_wrapper_script_when_scanned_then_still_contains_the_internal_rest_call(self):
        # Sanity check that the scan mechanics themselves are not vacuous:
        # the wrapper script DOES contain a raw update-branch call, and it
        # is the one permitted exception.
        text = _WRAPPER_SCRIPT.read_text(encoding='utf-8')
        assert 'update-branch' in text

    def test_given_allowlist_entries_when_checked_then_each_still_exists_and_matches_pattern(self):
        # Guards against a stale allowlist entry silently becoming a no-op
        # (e.g. after the underlying file is edited or removed).
        for rel_path, _justification in _ALLOWLIST.items():
            path = REPO_ROOT / rel_path
            assert path.is_file(), f'allowlisted path no longer exists: {rel_path}'
            text = path.read_text(encoding='utf-8', errors='ignore')
            matched = bool(
                _PATTERN_GH_API_UPDATE_BRANCH.search(text) or _PATTERN_GH_PR_UPDATE_BRANCH.search(text)
            )
            assert matched, (
                f'allowlisted path {rel_path} no longer matches the inventory pattern; '
                'remove the stale allowlist entry'
            )
