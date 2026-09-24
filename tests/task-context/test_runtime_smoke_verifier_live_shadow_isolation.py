"""Issue #2744 AC2: live shadow-canonical isolation verification.

``orchestrate_runtime_smoke()`` (the ONE canonical Task Context runtime-smoke
orchestration entrypoint, Issue #2568 / PR #2708 fix_delta item 2) is
supposed to keep every synthetic/runtime-hook write of a real runtime-smoke
run confined to its own run-scoped isolated state root, never touching the
canonical Task Context DB except for the one deliberate, bounded
``execution_runs`` roll-up row (AC5). Before this Issue, that guarantee had
never actually been exercised against a REAL ``claude`` subprocess launch --
Issue #2569/#2570 AC8 observed exactly the failure mode this test protects
against (unscoped writes landing in the real canonical DB) because the
acceptance procedure at the time bypassed this entrypoint entirely.

Runtime Verification Applicability (Issue #2744 body): ``immediate`` for
AC2. This test drives a REAL ``run_worktree_agent_runtime_smoke.py`` launch
(via ``orchestrate_runtime_smoke()``'s own, unmodified ``invoke_generic_
runner``) against a genuinely disposable shadow canonical DB rooted at a
temporary ``$XDG_STATE_HOME`` -- never the real
``~/.local/state/loop-protocol/...`` canonical DB. Project hooks are left
fully enabled (no ``--safe-mode``/``--restricted``/custom ``--setting-
sources``) -- only the STATE is isolated, never the hook wiring itself. When
no working ``claude`` CLI is available in this environment, this test SKIPs
(``pytest.skip()``, mapped from the runner's own capability-unavailable exit
code 77 convention per Issue #2231/#2744) rather than fabricating a PASS. A
result carrying any ``_*_fallback: true``-shaped field is treated as FAIL,
never PASS (Issue #2744 fallback_policy).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

import task_context_config as config  # noqa: E402
import task_context_db as db  # noqa: E402
import task_context_migration_runner as migration_runner  # noqa: E402
import task_context_runtime_smoke_verifier as verifier  # noqa: E402
import task_context_service as service  # noqa: E402

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parents[1]
_RUNNER_PATH = _REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"

_MODULE_NAME = "run_worktree_agent_runtime_smoke_issue_2744_live_shadow_isolation"
_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _RUNNER_PATH)
assert _spec is not None and _spec.loader is not None
_runner_module = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = _runner_module
_spec.loader.exec_module(_runner_module)

# The AC2 canonical-side table set (Issue #2744) -- deliberately DIFFERENT
# from this module's own FORBIDDEN_TABLES (tasks/activities/tab_bindings/
# task_ref_claims): this set is the one the live Issue names explicitly, and
# is checked independently below rather than through
# ``verifier.canonical_delta_contract`` (which does not track these four).
_AC2_TABLE_PRIMARY_KEYS: dict[str, str] = {
    "execution_runs": "id",
    "events": "id",
    "runtime_locations": "id",
    "projection_outbox": "projection_key",
}


def _native_claude_available() -> tuple[bool, str]:
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return False, "native 'claude' binary not found on PATH"
    try:
        result = subprocess.run([claude_bin, "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"native 'claude --version' failed: {exc}"
    if result.returncode != 0:
        return False, f"native 'claude --version' exited {result.returncode}"
    return True, claude_bin


def _snapshot_ac2_tables(conn) -> dict[str, list[dict[str, Any]]]:
    snapshot: dict[str, list[dict[str, Any]]] = {}
    for table, pk in _AC2_TABLE_PRIMARY_KEYS.items():
        rows = db.execute_readonly(conn, f"SELECT * FROM {table} ORDER BY {pk}").fetchall()  # noqa: S608
        snapshot[table] = [dict(row) for row in rows]
    return snapshot


def _diff_by_key(before: list[dict[str, Any]], after: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """Row identity/content delta between two snapshots of the SAME table,
    keyed by ``key`` (this table's own primary key column -- ``id`` for
    execution_runs/events/runtime_locations, ``projection_key`` for
    projection_outbox, which has no ``id`` column at all). Deliberately a
    fresh, table-generic implementation (not a reuse of this module's own
    ``_diff_rows``, which hardcodes ``row["id"]`` and would KeyError on
    ``projection_outbox``) -- see the module docstring above for why this
    table set needs its own diff rather than extending
    ``verifier.FORBIDDEN_TABLES``'s existing ``id``-only contract."""
    before_by_key = {row[key]: row for row in before}
    after_by_key = {row[key]: row for row in after}
    added = [row for k, row in after_by_key.items() if k not in before_by_key]
    removed = [row for k, row in before_by_key.items() if k not in after_by_key]
    mutated = [
        (before_by_key[k], after_by_key[k])
        for k in (before_by_key.keys() & after_by_key.keys())
        if before_by_key[k] != after_by_key[k]
    ]
    return {"added": added, "removed": removed, "mutated": mutated}


def _assert_no_fallback_field(payload: Any, *, path: str = "") -> None:
    """Issue #2744 fallback_policy: any ``_*_fallback: true``-shaped field
    anywhere in the observed evidence must FAIL this test, never be silently
    accepted as a passing result."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.startswith("_") and key.endswith("_fallback") and value is True:
                pytest.fail(f"fallback field observed at {path}.{key} -- treating as FAIL, not PASS")
            _assert_no_fallback_field(value, path=f"{path}.{key}")
    elif isinstance(payload, list):
        for idx, item in enumerate(payload):
            _assert_no_fallback_field(item, path=f"{path}[{idx}]")


def _seed_canonical_task_activity(conn) -> tuple[str, str]:
    task = service.create_task(conn, title="issue-2744 live shadow isolation parent task")
    activity = service.transition_activity(conn, task["id"], kind="verification")
    return task["id"], activity["id"]


def test_given_disposable_shadow_canonical_root_when_orchestrate_runtime_smoke_runs_live_then_canonical_tables_unaffected_and_isolated_root_receives_writes(
    tmp_path, monkeypatch
):
    """GIVEN a disposable shadow canonical Task Context DB rooted at a
    temporary $XDG_STATE_HOME (never the real canonical DB)
    WHEN orchestrate_runtime_smoke() drives one REAL runtime-smoke run
    (real claude subprocess, real generic runner, project hooks enabled)
    THEN the shadow canonical DB's execution_runs/events/runtime_locations/
    projection_outbox tables show ONLY the one expected execution_runs
    roll-up addition (row identity/content delta, not just row count), and
    the run-scoped isolated state root actually received the synthetic
    smoke-seed writes (tasks/activities/execution_runs) that never touched
    the shadow canonical side."""
    available, detail = _native_claude_available()
    if not available:
        pytest.skip(f"native Claude Code live environment unavailable: {detail}")

    # --- disposable shadow canonical root -----------------------------------
    xdg_state_home = tmp_path / "xdg-state-home"
    monkeypatch.setenv(config.XDG_STATE_HOME_ENV_VAR, str(xdg_state_home))
    monkeypatch.delenv(config.STATE_ROOT_ENV_VAR, raising=False)
    monkeypatch.delenv(config.SCOPE_ENV_VAR, raising=False)

    canonical_db_file = config.db_path()
    assert str(canonical_db_file).startswith(str(xdg_state_home)), (
        "sanity check: the shadow canonical DB path must resolve under the "
        f"disposable $XDG_STATE_HOME, got {canonical_db_file}"
    )
    real_canonical_default = Path.home() / ".local" / "state"
    assert not str(canonical_db_file).startswith(str(real_canonical_default)), (
        "refusing to proceed: resolved 'shadow' canonical path is under the "
        "REAL default XDG state home -- this must never touch the actual "
        f"developer machine canonical DB. Resolved path: {canonical_db_file}"
    )

    canonical_conn = db.connect(canonical_db_file)
    migration_runner.migrate(canonical_conn)
    try:
        canonical_task_id, canonical_activity_id = _seed_canonical_task_activity(canonical_conn)
        before = _snapshot_ac2_tables(canonical_conn)

        nonce = "RUNTIME_SMOKE_ISSUE_2744_" + uuid.uuid4().hex[:16]
        prompt_path = tmp_path / "prompt.txt"
        prompt_path.write_text(
            f"Respond with exactly this single line and nothing else: {nonce}. "
            "Do not use any tool. Answer directly in this same turn.\n",
            encoding="utf-8",
        )
        base_dir = tmp_path / "runtime-smoke-base"
        base_dir.mkdir()
        output_dir = tmp_path / "runtime-smoke-output"
        run_id = "live-shadow-isolation-" + uuid.uuid4().hex[:12]

        try:
            result = verifier.orchestrate_runtime_smoke(
                canonical_conn,
                worktree=str(_REPO_ROOT),
                base_dir=base_dir,
                prompt_file=str(prompt_path),
                output_dir=str(output_dir),
                run_id=run_id,
                timeout_seconds=90.0,
                runner_argv_extra=["--max-turns", "2"],
                canonical_task_id=canonical_task_id,
                canonical_activity_id=canonical_activity_id,
            )
        except subprocess.TimeoutExpired as exc:
            pytest.fail(
                "orchestrate_runtime_smoke() (real subprocess launch) did not "
                f"complete within its bounded timeout: {exc}"
            )

        if result.runner_returncode == _runner_module.EXIT_SKIP:
            pytest.skip(
                "generic runner itself reported capability-unavailable "
                f"(exit {_runner_module.EXIT_SKIP}) -- runner_evidence={result.runner_evidence!r}"
            )

        assert result.runner_returncode == 0, (
            f"real generic runner launch did not exit 0: returncode={result.runner_returncode} "
            f"runner_evidence={result.runner_evidence!r}"
        )
        _assert_no_fallback_field(result.runner_evidence, path="runner_evidence")
        _assert_no_fallback_field(result.to_dict(), path="result")

        # --- isolated side: synthetic writes must have actually landed -----
        state_root = verifier.build_isolated_state_root(base_dir, run_id=run_id)
        isolated_db_file = state_root / config.DB_FILE_NAME
        assert isolated_db_file.is_file(), (
            f"expected the run-scoped isolated Task Context DB to be materialized at "
            f"{isolated_db_file}, but it was not"
        )
        isolated_conn = db.connect_readonly(isolated_db_file)
        assert isolated_conn is not None
        try:
            seed_task_id = result.seed["data"]["task_id"]
            seed_activity_id = result.seed["data"]["activity_id"]
            isolated_task_row = db.execute_readonly(
                isolated_conn, "SELECT * FROM tasks WHERE id = ?", (seed_task_id,)
            ).fetchone()
            isolated_activity_row = db.execute_readonly(
                isolated_conn, "SELECT * FROM activities WHERE id = ?", (seed_activity_id,)
            ).fetchone()
            assert isolated_task_row is not None, (
                f"expected the synthetic smoke-seed task {seed_task_id!r} to exist in the "
                "ISOLATED DB (it must never be written to the shadow canonical DB)"
            )
            assert isolated_activity_row is not None, (
                f"expected the synthetic smoke-seed activity {seed_activity_id!r} to exist "
                "in the ISOLATED DB"
            )
        finally:
            isolated_conn.close()

        # --- shadow canonical side: only the ONE expected delta -------------
        after = _snapshot_ac2_tables(canonical_conn)

        for table in ("events", "runtime_locations", "projection_outbox"):
            delta = _diff_by_key(before[table], after[table], _AC2_TABLE_PRIMARY_KEYS[table])
            assert not delta["added"], f"unexpected row(s) added to shadow canonical {table}: {delta['added']}"
            assert not delta["removed"], f"unexpected row(s) removed from shadow canonical {table}: {delta['removed']}"
            assert not delta["mutated"], f"unexpected row(s) mutated in shadow canonical {table}: {delta['mutated']}"

        execution_runs_delta = _diff_by_key(
            before["execution_runs"], after["execution_runs"], _AC2_TABLE_PRIMARY_KEYS["execution_runs"]
        )
        assert not execution_runs_delta["removed"], (
            f"unexpected row(s) removed from shadow canonical execution_runs: "
            f"{execution_runs_delta['removed']}"
        )
        assert not execution_runs_delta["mutated"], (
            f"unexpected row(s) mutated in shadow canonical execution_runs: "
            f"{execution_runs_delta['mutated']}"
        )
        assert len(execution_runs_delta["added"]) == 1, (
            "expected exactly ONE execution_runs row added to the shadow canonical DB "
            f"(the deliberate AC5 roll-up), got {len(execution_runs_delta['added'])}: "
            f"{execution_runs_delta['added']}"
        )
        added_run = execution_runs_delta["added"][0]
        assert added_run["run_kind"] == "runtime_smoke"
        assert added_run["binding_id"] is None
        assert added_run["task_id"] == canonical_task_id
        assert added_run["activity_id"] == canonical_activity_id

        # Cross-check against this module's own independent contract for the
        # SAME execution_runs delta, reusing rather than re-deriving its
        # attribution assertion.
        assert result.canonical_delta.execution_runs.status == "pass", result.canonical_delta.to_dict()
        assert result.canonical_delta.status == "pass", result.canonical_delta.to_dict()
    finally:
        canonical_conn.close()
