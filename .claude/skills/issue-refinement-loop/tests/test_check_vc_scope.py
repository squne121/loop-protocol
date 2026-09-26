"""Unit tests for `.claude/skills/issue-refinement-loop/scripts/check_vc_scope.py`
(new test file, Issue #2783 AC12).

AC12: `_extract_allowed_paths()` must resolve the canonical no-path marker
`(none)` and the legacy exact marker `読み取り専用。リポジトリ変更なし（既定）`
to `[]`, via the shared `scripts/agent-ops/allowed_paths_policy.py::is_no_path_marker()`
helper (previously the legacy marker's trailing full-width paren annotation
was stripped by this module's own annotation normalizer into a bogus
non-empty "path" string).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_vc_scope as cvs  # noqa: E402


def test_extract_allowed_paths_no_path_marker_returns_empty():
    body_canonical = "## Allowed Paths\n\n- (none)\n"
    assert cvs._extract_allowed_paths(body_canonical) == []

    body_legacy = "## Allowed Paths\n\n- 読み取り専用。リポジトリ変更なし（既定）\n"
    assert cvs._extract_allowed_paths(body_legacy) == []


def test_extract_allowed_paths_real_path_still_preserved():
    body = "## Allowed Paths\n\n- scripts/example.py\n"
    assert cvs._extract_allowed_paths(body) == ["scripts/example.py"]


def test_extract_allowed_paths_annotated_real_path_still_preserved():
    body = "## Allowed Paths\n\n- scripts/example.py（新規）\n"
    assert cvs._extract_allowed_paths(body) == ["scripts/example.py"]
