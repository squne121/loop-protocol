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
import json
import platform
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parents[1]
_RUNNER_PATH = _REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"

# PR #2745 OWNER review (F5): mirrors tests/task-context/conftest.py's own
# bare-module sys.path bootstrap (scripts/task-context is a hyphenated
# directory name and therefore cannot be imported as a normal Python
# package). Under ordinary pytest collection, conftest.py already performs
# this exact insertion before this module is ever imported, so this is a
# harmless, idempotent no-op duplicate in that mode (the ``not in sys.path``
# guard below prevents a second insertion). It exists here too ONLY so this
# module can ALSO run standalone as a script (``python3
# test_runtime_smoke_verifier_live_shadow_isolation.py``, see the
# machine-readable SKIP(77)/FAIL(1)/PASS(0) verification entrypoint at the
# bottom of this file), which never loads conftest.py at all.
_SCRIPTS_DIR = _REPO_ROOT / "scripts" / "task-context"
_MIGRATIONS_DIR = _SCRIPTS_DIR / "migrations"
for _dir in (str(_SCRIPTS_DIR), str(_MIGRATIONS_DIR)):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import task_context_config as config  # noqa: E402
import task_context_db as db  # noqa: E402
import task_context_migration_runner as migration_runner  # noqa: E402
import task_context_runtime_smoke_verifier as verifier  # noqa: E402
import task_context_service as service  # noqa: E402

_MODULE_NAME = "run_worktree_agent_runtime_smoke_issue_2744_live_shadow_isolation"
_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _RUNNER_PATH)
assert _spec is not None and _spec.loader is not None
_runner_module = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = _runner_module
_spec.loader.exec_module(_runner_module)

# PR #2745 OWNER review (F3, https://github.com/squne121/loop-protocol/pull/2745#issuecomment-5816561294):
# this module drives a REAL `claude` CLI subprocess (via
# `orchestrate_runtime_smoke()`'s own unmodified `invoke_generic_runner`).
# `pyproject.toml`'s default addopts already deselect `claude_live`-marked
# tests (`-m 'not github_live and not claude_live'`) -- without this marker,
# a bare `uv run pytest tests/task-context/ -v` (no `-m` override) would
# still collect and execute this module and could launch a real Claude
# model call in an environment where a working `claude` CLI happens to be
# present. Marking this module `claude_live` keeps it opt-in, exactly like
# every other real-CLI-invoking test in this repository (see
# `.claude/skills/agent-retrospective/scripts/tests/verify_run_retrospective_live_cli.sh`).
pytestmark = pytest.mark.claude_live

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


# PR #2745 OWNER review (F1): `task-contextctl`'s own dispatcher
# (`_dispatch()`) short-circuits EVERY `hook` operation with
# `{"decision": "pass", "reason_code": "observe_only_non_herdr"}` *before*
# ever opening the DB whenever the hook payload's `herdr_tab_id` is falsy
# (`.claude/hooks/task_context/hook_entry.py::_build_base_payload` reads
# `HERDR_TAB_ID` straight out of the child process's own environment). The
# generic runner's STRUCTURED lane (`run_structured_claude()`) never strips
# inherited `HERDR_*` env vars (that stripping -- `_isolated_env()` -- is
# only ever applied to the separate INTERACTIVE herdr lane), so whatever
# `HERDR_TAB_ID` this test process itself happens to have is what the real
# `claude` child (and therefore every hook subprocess it spawns) inherits.
# Rather than depend on the ambient invoking shell's own Herdr Tab identity
# (which may or may not be set, making this test's central isolation
# guarantee non-deterministic across environments), this test deliberately
# sets a SYNTHETIC, run-scoped `HERDR_TAB_ID` before driving the real run --
# `task_context_hook_flows.py` only ever treats this value as an opaque
# locator string keyed into the ISOLATED DB's own `tab_bindings` table; it
# never itself shells out to the real `herdr` CLI/session machinery.
_SYNTHETIC_HERDR_TAB_ID_ENV_VAR = "HERDR_TAB_ID"


def _assert_isolated_runtime_hook_write_observed(isolated_conn) -> list[dict[str, Any]]:
    """PR #2745 OWNER review (F1): positive proof that a REAL runtime hook
    (not just the pre-launch `smoke seed`, which never itself calls
    `service.append_event`) actually reached `task_context_hook_flows.py`'s
    real DB-mutating dispatch path in the ISOLATED DB -- i.e. that
    `herdr_tab_id` was non-empty and the dispatcher did NOT take its
    `observe_only_non_herdr` early-return (which returns before ever
    opening the DB, so it can never itself produce an `events` row). Any
    row in the isolated DB's `events` table is unambiguous evidence of this,
    since `smoke seed` (`task_contextctl.py`'s `smoke_seed` operation)
    creates only `tasks`/`activities`/`tab_bindings`/`execution_runs` rows
    and never an `events` row itself. Raises via a plain assertion (not a
    silent False) when no such row exists, so a caller cannot mistake
    "hook never actually wrote" for a passing isolation result."""
    rows = [
        dict(row)
        for row in db.execute_readonly(
            isolated_conn, "SELECT * FROM events ORDER BY occurred_at"
        ).fetchall()
    ]
    assert rows, (
        "expected at least one real runtime-hook-authored row in the ISOLATED "
        "DB's events table (proof the hook chain actually reached its "
        "DB-mutating dispatch path, not the 'observe_only_non_herdr' "
        "early-return) -- got none. This means this run's positive isolation "
        "evidence would otherwise rest ONLY on the pre-launch smoke-seed rows, "
        "which is not evidence that any runtime hook write happened at all."
    )
    return rows


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


_ARTIFACTS_DIR = _REPO_ROOT / "artifacts"


def _write_runtime_verification_log(
    *, verdict: str, exit_code: int, reason: str, vc_input: dict[str, Any], vc_output: dict[str, Any]
) -> Path:
    """``docs/dev/runtime-verification-policy.md`` ## 4 証跡保存フォーマット:
    write this run's AC2 evidence under worktree-local ``artifacts/`` (never
    committed -- see repo root ``.gitignore``'s ``artifacts/`` entry)."""
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = _ARTIFACTS_DIR / f"runtime-verification-AC2-{timestamp}.log"
    claude_bin = shutil.which("claude") or "unavailable"
    lines = [
        "=== Runtime Verification Log ===",
        "AC: AC2 (Issue #2744) -- disposable shadow canonical root live isolation",
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}",
        f"Environment: {platform.platform()} / python {platform.python_version()} / claude_bin={claude_bin}",
        "",
        "--- Input ---",
        json.dumps(vc_input, indent=2, sort_keys=True, default=str),
        "",
        "--- Output ---",
        json.dumps(vc_output, indent=2, sort_keys=True, default=str)[:20000],
        "",
        "--- Verdict ---",
        f"Result: {verdict}",
        f"Exit Code: {exit_code}",
        f"Reason: {reason}",
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


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
        # PR #2745 OWNER review (F5): a SKIP this early (before any DB is
        # even opened) previously left no runtime-verification-policy.md
        # ## 4 evidence log behind at all. Persist a minimal SKIP record so
        # every terminal outcome of this test -- PASS, FAIL, and SKIP alike
        # -- is captured under artifacts/, not just the PASS/FAIL branches
        # reached once a run actually starts.
        _write_runtime_verification_log(
            verdict="SKIP",
            exit_code=77,
            reason=f"native Claude Code live environment unavailable: {detail}",
            vc_input={},
            vc_output={},
        )
        pytest.skip(f"native Claude Code live environment unavailable: {detail}")

    # PR #2745 OWNER review (F1): see _SYNTHETIC_HERDR_TAB_ID_ENV_VAR's own
    # docstring-comment above -- without a non-empty HERDR_TAB_ID reaching
    # the real claude child's own environment, every runtime hook this run
    # fires takes task-contextctl's `observe_only_non_herdr` early-return
    # and never writes to the isolated DB at all, making this test's
    # positive isolation evidence rest solely on the pre-launch smoke-seed
    # rows (PR #2745 review finding). Set BEFORE build_isolated_env()/the
    # real subprocess launch below so it is inherited all the way down:
    # this test process -> invoke_generic_runner's subprocess.run(env=None)
    # -> run_structured_claude()'s own os.environ.copy() -> the real
    # `claude` child -> its own hook subprocess.
    synthetic_herdr_tab_id = "issue-2744-live-shadow-isolation-" + uuid.uuid4().hex[:12]
    monkeypatch.setenv(_SYNTHETIC_HERDR_TAB_ID_ENV_VAR, synthetic_herdr_tab_id)

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

        # PR #2745 OWNER review (F5): built BEFORE the real subprocess launch
        # (every field here is already known) so the TimeoutExpired/EXIT_SKIP
        # branches immediately below can also persist a
        # runtime-verification-policy.md ## 4 evidence log -- previously only
        # the PASS/FAIL branches further down did.
        vc_input = {
            "xdg_state_home": str(xdg_state_home),
            "canonical_db_file": str(canonical_db_file),
            "canonical_task_id": canonical_task_id,
            "canonical_activity_id": canonical_activity_id,
            "base_dir": str(base_dir),
            "run_id": run_id,
            "worktree": str(_REPO_ROOT),
            "runner_argv_extra": ["--max-turns", "2"],
            "synthetic_herdr_tab_id": synthetic_herdr_tab_id,
        }

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
            _write_runtime_verification_log(
                verdict="FAIL",
                exit_code=1,
                reason=f"orchestrate_runtime_smoke() did not complete within its bounded timeout: {exc}",
                vc_input=vc_input,
                vc_output={},
            )
            pytest.fail(
                "orchestrate_runtime_smoke() (real subprocess launch) did not "
                f"complete within its bounded timeout: {exc}"
            )

        if result.runner_returncode == _runner_module.EXIT_SKIP:
            _write_runtime_verification_log(
                verdict="SKIP",
                exit_code=77,
                reason=(
                    "generic runner itself reported capability-unavailable "
                    f"(exit {_runner_module.EXIT_SKIP})"
                ),
                vc_input=vc_input,
                vc_output={"runner_returncode": result.runner_returncode, "runner_evidence": result.runner_evidence},
            )
            pytest.skip(
                "generic runner itself reported capability-unavailable "
                f"(exit {_runner_module.EXIT_SKIP}) -- runner_evidence={result.runner_evidence!r}"
            )

        vc_output: dict[str, Any] = {"runner_returncode": result.runner_returncode}
        try:
            assert result.runner_returncode == 0, (
                f"real generic runner launch did not exit 0: returncode={result.runner_returncode} "
                f"runner_evidence={result.runner_evidence!r}"
            )
            _assert_no_fallback_field(result.runner_evidence, path="runner_evidence")
            _assert_no_fallback_field(result.to_dict(), path="result")

            # PR #2745 OWNER review (F1): the aggregate verdict itself (which
            # already folds in the canonical_delta/statusline/scenario
            # sub-verdicts) and the presence of SOME runner evidence dict are
            # both part of the AND this test's overall PASS rests on -- not
            # previously asserted directly.
            vc_output["result_status"] = result.status
            vc_output["result_violations"] = result.violations
            # Issue #2747: orchestrate_runtime_smoke() now re-interprets a
            # completed structured runtime-smoke session's post-session
            # statusLine "executed_degenerate" observation as not_applicable
            # (see task_context_runtime_smoke_verifier.py::orchestrate_
            # runtime_smoke()), so this real single-turn structured live run
            # no longer needs the #2745-era known-timing-limitation
            # tolerance -- the ordinary aggregate oracle below is sufficient
            # and any OTHER violation still fails closed as a genuine
            # regression.
            assert result.status == "pass", (
                f"orchestrate_runtime_smoke() aggregate result.status={result.status!r} with "
                f"violations={result.violations!r}"
            )
            assert result.runner_evidence is not None, (
                "expected SOME runner_evidence dict to have been observed for this real run, got None"
            )

            # --- isolated side: synthetic writes must have actually landed -
            state_root = verifier.build_isolated_state_root(base_dir, run_id=run_id)
            isolated_db_file = state_root / config.DB_FILE_NAME
            vc_output["isolated_db_file"] = str(isolated_db_file)
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
                vc_output["isolated_task_row_present"] = isolated_task_row is not None
                vc_output["isolated_activity_row_present"] = isolated_activity_row is not None
                assert isolated_task_row is not None, (
                    f"expected the synthetic smoke-seed task {seed_task_id!r} to exist in the "
                    "ISOLATED DB (it must never be written to the shadow canonical DB)"
                )
                assert isolated_activity_row is not None, (
                    f"expected the synthetic smoke-seed activity {seed_activity_id!r} to exist "
                    "in the ISOLATED DB"
                )

                # PR #2745 OWNER review (F1): positive proof that a REAL
                # runtime hook (not just the pre-launch smoke seed) actually
                # wrote to the isolated DB -- see
                # _assert_isolated_runtime_hook_write_observed's own
                # docstring for why an events-table row is the right,
                # seed-independent signal.
                hook_events = _assert_isolated_runtime_hook_write_observed(isolated_conn)
                vc_output["isolated_hook_events_count"] = len(hook_events)
                vc_output["isolated_hook_event_types"] = sorted({row["event_type"] for row in hook_events})
            finally:
                isolated_conn.close()

            # --- shadow canonical side: only the ONE expected delta --------
            after = _snapshot_ac2_tables(canonical_conn)

            for table in ("events", "runtime_locations", "projection_outbox"):
                delta = _diff_by_key(before[table], after[table], _AC2_TABLE_PRIMARY_KEYS[table])
                vc_output[f"{table}_delta"] = delta
                assert not delta["added"], f"unexpected row(s) added to shadow canonical {table}: {delta['added']}"
                assert not delta["removed"], (
                    f"unexpected row(s) removed from shadow canonical {table}: {delta['removed']}"
                )
                assert not delta["mutated"], (
                    f"unexpected row(s) mutated in shadow canonical {table}: {delta['mutated']}"
                )

            execution_runs_delta = _diff_by_key(
                before["execution_runs"], after["execution_runs"], _AC2_TABLE_PRIMARY_KEYS["execution_runs"]
            )
            vc_output["execution_runs_delta"] = execution_runs_delta
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

            # Cross-check against this module's own independent contract for
            # the SAME execution_runs delta, reusing rather than re-deriving
            # its attribution assertion.
            vc_output["canonical_delta"] = result.canonical_delta.to_dict()
            assert result.canonical_delta.execution_runs.status == "pass", result.canonical_delta.to_dict()
            assert result.canonical_delta.status == "pass", result.canonical_delta.to_dict()
        except BaseException as exc:
            _write_runtime_verification_log(
                verdict="FAIL",
                exit_code=1,
                reason=f"{type(exc).__name__}: {exc}",
                vc_input=vc_input,
                vc_output=vc_output,
            )
            raise
        else:
            _write_runtime_verification_log(
                verdict="PASS",
                exit_code=0,
                reason="all canonical-side isolation deltas matched the expected AC5-only roll-up",
                vc_input=vc_input,
                vc_output=vc_output,
            )
    finally:
        canonical_conn.close()


# ---------------------------------------------------------------------------
# Machine-readable SKIP(77)/FAIL(1)/PASS(0) verification entrypoint
# (PR #2745 OWNER review, F5, https://github.com/squne121/loop-protocol/pull/2745#issuecomment-5816561294)
# ---------------------------------------------------------------------------
#
# Ordinary pytest collection/execution of this module (e.g. the Issue #2744
# Verification Command `uv run pytest
# tests/task-context/test_runtime_smoke_verifier_live_shadow_isolation.py -v`)
# is COMPLETELY UNCHANGED by everything below: `pytest.skip()` inside the
# test function above still reports pytest's own SKIPPED / exit-0 semantics
# in that mode, exactly as before this fix_delta. `docs/dev/runtime-
# verification-policy.md`'s exit-code convention (SKIP == 77, distinct from
# a plain pytest exit 0 that could just as easily mean "everything passed")
# only applies to the SEPARATE, explicit entrypoint below, reached ONLY by
# invoking this file directly as a script:
#   uv run python3 tests/task-context/test_runtime_smoke_verifier_live_shadow_isolation.py
#
# This deliberately lives inside this SAME Allowed-Paths file (Issue #2744's
# Allowed Paths list this file by its exact path, not a new one) rather than
# as a new standalone shell/py wrapper script -- no new global pytest plugin
# or generic pass/fail framework is introduced; this is a narrow, local
# dual-purpose (importable pytest module + invokable script) entrypoint,
# modeled on `.claude/skills/agent-retrospective/scripts/tests/
# verify_run_retrospective_live_cli.sh`'s own SKIP(77)/FAIL(1)/PASS(0)
# contract (that script itself cannot be reused here -- it targets a
# different test file with different skip_conditions -- so its CONTRACT,
# not its code, is what is reused).


class _OutcomeCollector:
    """An in-process pytest plugin (never registered globally -- passed only
    via ``pytest.main(..., plugins=[...])`` for this one nested invocation)
    that records each collected test's terminal outcome (``"passed"`` /
    ``"failed"`` / ``"skipped"``) so ``_run_as_verification_entrypoint``
    below can distinguish a genuine SKIP from a genuine PASS/FAIL -- a plain
    pytest process exit code alone conflates "all skipped" with "all
    passed" (both exit 0)."""

    def __init__(self) -> None:
        self.outcomes: list[str] = []

    def pytest_runtest_logreport(self, report) -> None:  # noqa: ANN001 - pytest hook signature
        if report.when == "call" or (report.when == "setup" and report.skipped):
            self.outcomes.append(report.outcome)


def _run_as_verification_entrypoint() -> int:
    """SKIP (77) / FAIL (1) / PASS (0), per docs/dev/runtime-verification-
    policy.md's exit-code convention:

    - the two documented skip_conditions (Issue #2744 body's
      ``## Runtime Verification Applicability`` block) -- no working
      ``claude`` CLI, or this test itself reporting SKIPPED for any reason
      (including the generic runner's own capability-unavailable exit) --
      map to SKIP (77), with a leading ``SKIP: `` stdout line.
    - any real assertion failure, or the nested pytest run reporting ANY
      failed outcome, is FAIL (1).
    - only a genuine PASS outcome (with no failures and no skips) is PASS
      (0)."""
    available, detail = _native_claude_available()
    if not available:
        print(f"SKIP: native Claude Code live environment unavailable: {detail}")
        return 77

    collector = _OutcomeCollector()
    pytest.main(["-o", "addopts=", "-m", "claude_live", "-q", __file__], plugins=[collector])

    if any(outcome == "failed" for outcome in collector.outcomes):
        print("FAIL: live shadow isolation verification failed (see pytest output above)")
        return 1
    if collector.outcomes and all(outcome == "skipped" for outcome in collector.outcomes):
        print("SKIP: nested pytest run reported SKIPPED (see pytest output above for reason)")
        return 77
    if any(outcome == "passed" for outcome in collector.outcomes):
        print("PASS: live shadow isolation verification succeeded")
        return 0
    print("FAIL: no test outcomes observed from the nested pytest run")
    return 1


if __name__ == "__main__":
    sys.exit(_run_as_verification_entrypoint())
