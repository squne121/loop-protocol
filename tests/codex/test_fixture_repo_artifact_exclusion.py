"""Regression coverage for `_fixture_repo()`'s `.claude/artifacts` exclusion (Issue #2078).

`_fixture_repo()` in `test_codex_permission_profile_config.py` copies the
repository into a `tmp_path` sandbox via `shutil.copytree()` for the Codex
permission-profile validator tests. Its `exclude_relpaths` set previously
contained only a top-level `"artifacts"` entry, which matches root-level
`artifacts/` but not the nested `.claude/artifacts/` tree. Because
`.claude/artifacts/issue-refinement-loop/<issue>/` is continuously rewritten
by `run_refinement_preflight.py` during CI's parallel test execution, copying
it under `shutil.copytree()` could intermittently raise `shutil.Error` for a
file that vanished mid-copy.

This module verifies that the `exact entry` fix (`.claude/artifacts` added to
`exclude_relpaths`) actually excludes the nested directory from the fixture
copy, without asserting anything about `.claude/artifacts` being
repository-wide disposable (it is not -- see `_fixture_repo()`'s docstring).
"""

from __future__ import annotations

from pathlib import Path

from tests.codex.test_codex_permission_profile_config import REPO_ROOT, _fixture_repo


def test_fixture_repo_excludes_claude_artifacts(tmp_path: Path) -> None:
    # GIVEN a live repository whose `.claude/artifacts` directory exists
    # (this is a precondition for the test to be meaningful: if the source
    # directory did not exist, its absence from the copy would prove nothing).
    source = REPO_ROOT / ".claude" / "artifacts"
    assert source.is_dir(), (
        "precondition failed: REPO_ROOT/.claude/artifacts must exist for "
        "this exclusion test to be meaningful"
    )

    # WHEN `_fixture_repo()` copies the repository into an isolated tmp_path
    dest = _fixture_repo(tmp_path)

    # THEN the fixture copy's `.claude/artifacts` directory itself is absent
    copied_artifacts = dest / ".claude" / "artifacts"
    assert not copied_artifacts.exists(), (
        f"expected {copied_artifacts} to be excluded from the fixture copy, "
        "but it was present"
    )

    # AND the rest of `.claude` (e.g. skill canonical bodies) is still copied,
    # confirming the exclusion is scoped to `.claude/artifacts` only.
    copied_claude_dir = dest / ".claude"
    assert copied_claude_dir.is_dir(), (
        f"expected {copied_claude_dir} to exist (only .claude/artifacts and "
        ".claude/worktrees should be excluded, not all of .claude)"
    )

    # AND a real, stable file elsewhere under `.claude` (a skill canonical
    # body) was actually copied -- not merely an empty `.claude` directory
    # left behind by a broken exclusion implementation that drops everything
    # under `.claude` instead of just `.claude/artifacts`.
    copied_skill_script = (
        dest
        / ".claude"
        / "skills"
        / "issue-refinement-loop"
        / "scripts"
        / "run_refinement_preflight.py"
    )
    assert copied_skill_script.is_file(), (
        f"expected {copied_skill_script} to be copied as part of the "
        "fixture repo, confirming .claude contents other than "
        ".claude/artifacts are preserved"
    )
