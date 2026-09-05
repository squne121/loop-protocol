"""Issue #2252 AC3: a stale, pre-existing `.guard_shadow_log.jsonl` (a file
left over from before all production producers were removed) must not block
the canonical preflight executed via `scripts/agent-guards/skill_runtime_exec.py`,
as long as the file itself is left unchanged by the wrapped command. Now that
the typed exact-file shadow-log special case has been fully removed from
`skill_runtime_exec.py`, this file is a generic, untracked repository path:
the executor's ordinary before/after snapshot diff must observe it as
byte-identical across the run and therefore never report it as an
unauthorized_write_path.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from test_skill_runtime_exec_unauthorized_write_path import (  # noqa: E402
    _install_skill_runtime_exec_fixture,
    _make_repo,
    _materialize_execution_root,
    _run_executor,
)


def test_stale_pre_existing_shadow_log_does_not_block_preflight(tmp_path: Path) -> None:
    """GIVEN a stale `.guard_shadow_log.jsonl` already exists at repo root
    before the executor runs (left over from a producer that has since been
    removed)
    WHEN the wrapped child command does not touch that file at all
    THEN skill_runtime_exec.py's canonical preflight completes successfully
    (exit 0) and the stale file's content is left byte-for-byte unchanged."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    # Issue #2199: `preflight.run` now dispatches its child (and its own
    # before/after snapshot) under the dedicated worktree, not `repo` --
    # the stale file must genuinely predate the real dispatch's
    # before-snapshot (materialized via a real throwaway dispatch first) to
    # faithfully exercise "already existed, left untouched", not "appeared
    # mid-run".
    execution_root = _materialize_execution_root(repo)

    stale_shadow_log = execution_root / ".guard_shadow_log.jsonl"
    stale_content = (
        '{"schema_version":"1","timestamp":"2026-01-01T00:00:00Z","event":"stale"}\n'
    )
    stale_shadow_log.write_text(stale_content)

    result = _run_executor(repo)

    assert result.returncode == 0, result.stderr
    assert stale_shadow_log.exists()
    assert stale_shadow_log.read_text() == stale_content
    artifact = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1228" / "preflight.json"
    assert artifact.exists()
