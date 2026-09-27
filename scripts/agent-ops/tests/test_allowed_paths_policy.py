"""Issue #2783 AC1/AC8: unit tests for
``scripts/agent-ops/allowed_paths_policy.py::is_no_path_marker()``.

These tests exercise the raw-token-level marker predicate directly and
hermetically. AC1 specifically requires that ``(none)`` and the legacy
marker are recognized BEFORE any consumer-side annotation normalization is
applied -- a false-green caused by an existing consumer's annotation
normalization accidentally reducing the entry to an empty string does not
count as evidence this predicate works (see module docstring / Issue #2783
In Scope section).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "allowed_paths_policy.py"
_MODULE_NAME = "allowed_paths_policy_issue_2783"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
policy = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = policy
_spec.loader.exec_module(policy)


def test_is_no_path_marker_recognizes_canonical_and_legacy():
    # AC1: raw token stage (no annotation normalization applied here at all
    # -- these are the exact raw strings, not routed through any consumer's
    # _normalize_allowed_path_entry()-style helper).
    assert policy.is_no_path_marker("(none)") is True
    assert policy.is_no_path_marker("読み取り専用。リポジトリ変更なし（既定）") is True


def test_is_no_path_marker_false_for_real_paths():
    assert policy.is_no_path_marker("scripts/agent-ops/allowed_paths_policy.py") is False
    assert policy.is_no_path_marker("docs/日本語。md") is False


def test_is_no_path_marker_false_for_annotated_real_path():
    # AC7-adjacent: an entry that is a real path plus trailing annotation is
    # not a no-path marker, since it does not exact-match either marker
    # after wrapper stripping (before annotation normalization).
    assert policy.is_no_path_marker("some/path.py（読み取り専用）") is False


def test_is_no_path_marker_false_for_unrelated_prose():
    assert (
        policy.is_no_path_marker("調査対象として `.claude/hooks/` を参照可能（変更は行わない）")
        is False
    )


def test_is_no_path_marker_true_for_bullet_wrapped_canonical():
    # AC8: bullet-wrapped `(none)`.
    assert policy.is_no_path_marker("- (none)") is True
    assert policy.is_no_path_marker("* (none)") is True
    assert policy.is_no_path_marker("+ (none)") is True


def test_is_no_path_marker_true_for_backtick_wrapped_canonical():
    assert policy.is_no_path_marker("`(none)`") is True
    assert policy.is_no_path_marker("- `(none)`") is True


def test_is_no_path_marker_true_for_code_fence_wrapped_canonical():
    # AC8: code-fence-wrapped `(none)` judged identically to bullet-wrapped.
    assert policy.is_no_path_marker("```\n(none)\n```") is True
    assert policy.is_no_path_marker("```text\n(none)\n```") is True


def test_is_no_path_marker_true_for_bullet_wrapped_legacy():
    assert policy.is_no_path_marker("- 読み取り専用。リポジトリ変更なし（既定）") is True


def test_is_no_path_marker_false_for_none_and_non_string():
    assert policy.is_no_path_marker(None) is False  # type: ignore[arg-type]
    assert policy.is_no_path_marker("") is False


def test_is_no_path_marker_false_for_legacy_marker_prefix_without_suffix():
    # Exact-match only: a partial/legacy-like prefix without the full
    # "（既定）" suffix must NOT be recognized (no lexical heuristic).
    assert policy.is_no_path_marker("読み取り専用。リポジトリ変更なし") is False
