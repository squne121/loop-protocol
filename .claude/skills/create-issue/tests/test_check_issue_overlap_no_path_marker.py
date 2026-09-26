"""AC11 (Issue #2783): two unrelated read-only research Issues whose bodies
declare Allowed Paths as ONLY the canonical no-path marker `(none)` or the
legacy exact marker `読み取り専用。リポジトリ変更なし（既定）` must not be
flagged as `duplicate` / `same_path_set()` overlap merely because they both
declare "no paths" the same way (before Issue #2783's fix, the legacy
marker's trailing full-width paren annotation was accidentally stripped by
the existing annotation normalizer into a non-empty bogus "path" string
identical across any two legacy-marker bodies, which `same_path_set()`
would then flag as an exact duplicate).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_issue_overlap as cio  # noqa: E402


def _read_only_body(marker: str) -> str:
    return f"""## Outcome

Investigation-only research.

## Acceptance Criteria

- [ ] AC1: findings documented.

## Allowed Paths

- {marker}

## Stop Conditions

- none
"""


def test_no_path_marker_alone_not_duplicate():
    # GIVEN: two entirely unrelated read-only research Issues, both
    # declaring Allowed Paths as ONLY the legacy no-path marker.
    current = cio.IssueScope(
        title="research: 完全に無関係な調査 A",
        number=3001,
        body=_read_only_body("読み取り専用。リポジトリ変更なし（既定）"),
        goal="調査Aの目的",
    )
    candidate = cio.IssueScope(
        title="research: 完全に無関係な調査 B",
        number=3002,
        body=_read_only_body("読み取り専用。リポジトリ変更なし（既定）"),
        goal="調査Bの目的",
        state="OPEN",
    )

    # WHEN: same_path_set() is asked whether their effective Allowed Paths
    # collide.
    same = cio.same_path_set(
        current.effective_allowed_paths(), candidate.effective_allowed_paths()
    )

    # THEN: no-path-marker-only agreement is NOT a path-set match (both
    # sides normalize to the empty set, and same_path_set() requires a
    # non-empty set on both sides).
    assert same is False
    assert current.effective_allowed_paths() == ()
    assert candidate.effective_allowed_paths() == ()

    # AND: the full classify_overlap() routing does not classify these two
    # unrelated Issues as duplicate/overlap via allowed_paths matching
    # (title/goal are deliberately disjoint, so no other matched_fields
    # should fire either).
    result = cio.classify_overlap(current, [candidate])
    assert result.verdict != cio.DUPLICATE, result
    assert not any(
        "allowed_paths" in ev.matched_fields for ev in result.candidates
    ), result.candidates


def test_no_path_marker_canonical_alone_not_duplicate():
    """Same scenario with the canonical marker `(none)`."""
    current = cio.IssueScope(
        title="research: 完全に無関係な調査 C",
        number=3003,
        body=_read_only_body("(none)"),
        goal="調査Cの目的",
    )
    candidate = cio.IssueScope(
        title="research: 完全に無関係な調査 D",
        number=3004,
        body=_read_only_body("(none)"),
        goal="調査Dの目的",
        state="OPEN",
    )

    same = cio.same_path_set(
        current.effective_allowed_paths(), candidate.effective_allowed_paths()
    )
    assert same is False

    result = cio.classify_overlap(current, [candidate])
    assert result.verdict != cio.DUPLICATE, result


def test_no_path_marker_mixed_canonical_and_legacy_not_duplicate():
    """One side declares the canonical marker, the other the legacy marker
    -- still not a duplicate via allowed_paths (both normalize to 0
    paths)."""
    current = cio.IssueScope(
        title="research: 無関係な調査 E",
        number=3005,
        body=_read_only_body("(none)"),
        goal="調査Eの目的",
    )
    candidate = cio.IssueScope(
        title="research: 無関係な調査 F",
        number=3006,
        body=_read_only_body("読み取り専用。リポジトリ変更なし（既定）"),
        goal="調査Fの目的",
        state="OPEN",
    )

    result = cio.classify_overlap(current, [candidate])
    assert result.verdict != cio.DUPLICATE, result
