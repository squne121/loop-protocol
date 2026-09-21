"""Task Context runtime-smoke verifier (Issue #2568).

This module owns every Task Context-*specific* semantic piece of the
``worktree-agent-runtime-smoke`` runtime smoke: canonical forbidden-table
before/after snapshots, run-scoped isolated Task Context state-root/env
construction, ``smoke seed`` invocation against that isolated state, the
canonical DB table-scoped delta contract (AC3/AC5), and the deterministic
scenario-evidence assertions (wrong-primary-target advisory / ``/clear``
session continuity) that a caller (a fresh, real Claude runtime driven
by the generic ``scripts/agent-ops/run_worktree_agent_runtime_smoke.py``
runner) feeds real observed values into.

Responsibility boundary (Issue #2568 In Scope): the generic runner owns
ONLY environment/carrier passthrough, Native/Claude-GPT child launch,
explicit isolated Herdr ``--session`` transport, and generic hook-chain
evidence (Issue #2663/PR #2668, reused as-is). It never imports this module
and never embeds Task Context PASS/FAIL semantic classification. This
module never launches a Claude/Herdr process itself -- it only prepares the
isolated environment a caller launches a real runtime *into*, and evaluates
before/after DB state a caller captured around that real runtime.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import task_context_config as config  # noqa: E402
import task_context_db as db  # noqa: E402
import task_context_envelope as envelope  # noqa: E402
import task_context_service as service  # noqa: E402

# ---------------------------------------------------------------------------
# Canonical forbidden-table / execution_runs delta contract (AC3, AC5)
# ---------------------------------------------------------------------------

# AC3: these four tables must be byte-identical (not one row added, changed,
# or removed) across a runtime-smoke run against the CANONICAL DB.
FORBIDDEN_TABLES: tuple[str, ...] = ("tasks", "activities", "tab_bindings", "task_ref_claims")

# AC3/AC5: execution_runs is the ONLY table a runtime-smoke run may add rows
# to in the canonical DB, and only under this exact fixed contract.
EXECUTION_RUNS_TABLE = "execution_runs"
RUNTIME_SMOKE_RUN_KIND = "runtime_smoke"
MAX_RUNTIME_SMOKE_ROWS_ADDED = 1

_SNAPSHOT_TABLES: tuple[str, ...] = (*FORBIDDEN_TABLES, EXECUTION_RUNS_TABLE)


def snapshot_canonical_tables(conn) -> dict[str, list[dict[str, Any]]]:
    """Read-only snapshot of ``FORBIDDEN_TABLES`` + ``execution_runs``,
    keyed by table name, each value a list of row dicts ordered by ``id``.
    Callers take one snapshot before a runtime-smoke run and one after, and
    pass both into ``execution_runs_delta_contract``/
    ``assert_forbidden_tables_byte_identical`` below. Never mutates the
    connection or its transaction state."""
    snapshot: dict[str, list[dict[str, Any]]] = {}
    for table in _SNAPSHOT_TABLES:
        rows = db.execute_readonly(conn, f"SELECT * FROM {table} ORDER BY id").fetchall()  # noqa: S608
        snapshot[table] = [dict(row) for row in rows]
    return snapshot


@dataclass
class TableDiffResult:
    table: str
    status: str  # "pass" | "fail"
    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    mutated: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


def _index_by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in rows}


def _diff_rows(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[tuple[dict[str, Any], dict[str, Any]]]
]:
    before_by_id = _index_by_id(before)
    after_by_id = _index_by_id(after)
    added = [row for row_id, row in after_by_id.items() if row_id not in before_by_id]
    removed = [row for row_id, row in before_by_id.items() if row_id not in after_by_id]
    mutated = [
        (before_by_id[row_id], after_by_id[row_id])
        for row_id in (before_by_id.keys() & after_by_id.keys())
        if before_by_id[row_id] != after_by_id[row_id]
    ]
    return added, removed, mutated


def assert_forbidden_tables_byte_identical(
    before: dict[str, list[dict[str, Any]]], after: dict[str, list[dict[str, Any]]]
) -> dict[str, TableDiffResult]:
    """AC3: ``tasks``/``activities``/``tab_bindings``/``task_ref_claims``
    must be byte-identical (no add/remove/mutate) between ``before`` and
    ``after`` snapshots of the CANONICAL DB. Returns one ``TableDiffResult``
    per forbidden table -- ``status == "fail"`` for any table that changed
    at all."""
    results: dict[str, TableDiffResult] = {}
    for table in FORBIDDEN_TABLES:
        added, removed, mutated = _diff_rows(before.get(table, []), after.get(table, []))
        violations: list[str] = []
        if added:
            violations.append(f"{len(added)} row(s) added")
        if removed:
            violations.append(f"{len(removed)} row(s) removed")
        if mutated:
            violations.append(f"{len(mutated)} row(s) mutated")
        results[table] = TableDiffResult(
            table=table,
            status="fail" if violations else "pass",
            added=added,
            removed=removed,
            mutated=mutated,
            violations=violations,
        )
    return results


def assert_execution_runs_delta_contract(
    before: dict[str, list[dict[str, Any]]], after: dict[str, list[dict[str, Any]]]
) -> TableDiffResult:
    """AC3/AC5: the ONLY permitted ``execution_runs`` delta is the addition
    of at most ``MAX_RUNTIME_SMOKE_ROWS_ADDED`` row(s), each with
    ``run_kind == RUNTIME_SMOKE_RUN_KIND`` and ``binding_id IS NULL``. Any
    removal, mutation of an existing row, addition beyond that bound, or an
    added row that does not match the fixed shape is an unexpected
    mutation -> ``status == "fail"``."""
    added, removed, mutated = _diff_rows(
        before.get(EXECUTION_RUNS_TABLE, []), after.get(EXECUTION_RUNS_TABLE, [])
    )
    violations: list[str] = []
    if removed:
        violations.append(f"{len(removed)} row(s) removed (execution_runs must never lose rows here)")
    if mutated:
        violations.append(f"{len(mutated)} existing row(s) mutated (execution_runs rows are append-only here)")
    if len(added) > MAX_RUNTIME_SMOKE_ROWS_ADDED:
        violations.append(
            f"{len(added)} row(s) added, exceeds max {MAX_RUNTIME_SMOKE_ROWS_ADDED} permitted"
        )
    for row in added:
        if row.get("run_kind") != RUNTIME_SMOKE_RUN_KIND:
            violations.append(
                f"added row {row.get('id')!r} has run_kind={row.get('run_kind')!r}, "
                f"expected {RUNTIME_SMOKE_RUN_KIND!r}"
            )
        if row.get("binding_id") is not None:
            violations.append(f"added row {row.get('id')!r} has non-NULL binding_id={row.get('binding_id')!r}")
    return TableDiffResult(
        table=EXECUTION_RUNS_TABLE,
        status="fail" if violations else "pass",
        added=added,
        removed=removed,
        mutated=mutated,
        violations=violations,
    )


@dataclass
class CanonicalDeltaContractResult:
    status: str  # "pass" | "fail"
    forbidden_tables: dict[str, TableDiffResult]
    execution_runs: TableDiffResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "forbidden_tables": {
                table: {"status": r.status, "violations": r.violations} for table, r in self.forbidden_tables.items()
            },
            "execution_runs": {"status": self.execution_runs.status, "violations": self.execution_runs.violations},
        }


def canonical_delta_contract(
    before: dict[str, list[dict[str, Any]]], after: dict[str, list[dict[str, Any]]]
) -> CanonicalDeltaContractResult:
    """AC3 aggregate: byte-identical forbidden tables AND the bounded
    execution_runs contract must BOTH hold for the aggregate to be
    ``"pass"``."""
    forbidden = assert_forbidden_tables_byte_identical(before, after)
    runs = assert_execution_runs_delta_contract(before, after)
    aggregate_status = (
        "pass"
        if runs.status == "pass" and all(r.status == "pass" for r in forbidden.values())
        else "fail"
    )
    return CanonicalDeltaContractResult(status=aggregate_status, forbidden_tables=forbidden, execution_runs=runs)


# ---------------------------------------------------------------------------
# Run-scoped isolated Task Context state root / env (In Scope)
# ---------------------------------------------------------------------------


def build_isolated_state_root(base_dir: Path, run_id: str | None = None) -> Path:
    """A run-scoped, absolute, not-yet-materialized isolated state-root
    path under ``base_dir`` (a caller-owned worktree-local directory, e.g.
    ``artifacts/runtime-smoke/task-context/<run>``). The directory itself is
    created lazily by whatever first opens the DB at this root (AC6:
    ``task-contextctl`` only ever does that when
    ``LOOP_TASK_CONTEXT_SCOPE=runtime_smoke``) -- this function does not
    create it, so a caller can assert non-materialization before that first
    open."""
    run_id = run_id or uuid.uuid4().hex
    return (base_dir / f"task-context-smoke-{run_id}").resolve()


def build_isolated_env(
    state_root: Path, *, base_env: dict[str, str] | None = None, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """The exact env-var pair the Issue #2568 In Scope contract requires a
    runtime-smoke child to receive: ``LOOP_TASK_CONTEXT_SCOPE=runtime_smoke``
    and ``LOOP_TASK_CONTEXT_STATE_ROOT=<state_root>`` (an absolute path),
    merged additively on top of ``base_env`` (defaults to a copy of the
    current process env) and ``extra`` (any further caller-owned overrides,
    e.g. ``LOOP_TASK_CONTEXT_RUNTIME_VARIANT`` for the Claude-GPT lane)."""
    if not state_root.is_absolute():
        raise ValueError(f"state_root must be absolute, got {state_root!r}")
    env = dict(base_env) if base_env is not None else dict(os.environ)
    env[config.SCOPE_ENV_VAR] = config.RUNTIME_SMOKE_SCOPE_VALUE
    env[config.STATE_ROOT_ENV_VAR] = str(state_root)
    if extra:
        env.update(extra)
    return env


def is_state_root_materialized(state_root: Path) -> bool:
    """AC6 proof helper: whether the isolated state-root directory (and, by
    construction, any DB file inside it) exists yet."""
    return state_root.exists()


# ---------------------------------------------------------------------------
# smoke seed invocation against the isolated DB (In Scope)
# ---------------------------------------------------------------------------

_CLI_PATH = Path(__file__).resolve().parent / "task_contextctl.py"


def invoke_smoke_seed(
    env: dict[str, str], *, title: str | None = None, timeout_seconds: float = 30.0
) -> dict[str, Any]:
    """Run the real ``task_contextctl.py smoke seed`` CLI as a subprocess
    against ``env`` (built via ``build_isolated_env`` above) and return the
    parsed result envelope. Raises ``RuntimeError`` on a non-OK envelope or
    non-zero exit so a caller never silently treats a rejected/failed seed
    as evidence of success."""
    request = envelope.build_request("smoke_seed", {"title": title} if title else {})
    proc = subprocess.run(
        [sys.executable, str(_CLI_PATH), "smoke", "seed"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout_seconds,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError(
            f"expected exactly one stdout line from smoke seed, got: {proc.stdout!r} (stderr={proc.stderr!r})"
        )
    result = json.loads(lines[0])
    if proc.returncode != 0 or result.get("status") != "ok":
        raise RuntimeError(f"smoke seed rejected/failed: exit={proc.returncode} result={result!r}")
    return result


# ---------------------------------------------------------------------------
# Canonical execution_runs roll-up (AC5)
# ---------------------------------------------------------------------------


def roll_up_runtime_smoke_execution_run(
    conn,
    *,
    task_id: str | None,
    activity_id: str | None,
    runtime_profile: str | None = None,
    resume_profile: str | None = None,
) -> dict[str, Any]:
    """AC5: append (start + immediately end) at most one canonical
    ``execution_runs`` row for this smoke run, under the caller's own
    current parent Task/Activity, with ``run_kind='runtime_smoke'`` and
    ``binding_id=NULL`` (this run's Herdr Tab/child is never registered as
    an operator TabBinding -- AC4). Reuses ``runtime_profile``/
    ``resume_profile`` -- current fixed schema columns -- rather than adding
    any new column. The caller is responsible for recording the
    bounded/public-safe evidence ref (this run's id, or an Issue/PR comment
    attachment) -- this function itself stores no evidence payload."""
    run = service.start_execution_run(
        conn,
        run_kind=RUNTIME_SMOKE_RUN_KIND,
        task_id=task_id,
        activity_id=activity_id,
        binding_id=None,
        runtime_profile=runtime_profile,
        resume_profile=resume_profile,
    )
    return service.end_execution_run(conn, run["id"])


# ---------------------------------------------------------------------------
# Isolated synthetic fixture <-> canonical DB non-leakage (In Scope "E")
# ---------------------------------------------------------------------------


def assert_isolated_fixture_absent_from_canonical(
    canonical_conn, isolated_task_id: str, isolated_binding_id: str
) -> bool:
    """AC4/E: the isolated DB's synthetic Task id / Binding id must never
    appear in the canonical DB's ``tasks``/``tab_bindings`` tables. Returns
    ``True`` when absent (the expected/passing state)."""
    task_row = db.execute_readonly(canonical_conn, "SELECT id FROM tasks WHERE id = ?", (isolated_task_id,)).fetchone()
    binding_row = db.execute_readonly(
        canonical_conn, "SELECT id FROM tab_bindings WHERE id = ?", (isolated_binding_id,)
    ).fetchone()
    return task_row is None and binding_row is None


# ---------------------------------------------------------------------------
# Scenario evidence assertions (AC7, AC8) -- pure functions over caller-
# supplied REAL observed values. This module never drives a Claude/Herdr
# process itself; the generic runner (or its caller) captures the real
# session ids / DB rows around a real runtime turn and passes them in here.
# ---------------------------------------------------------------------------


@dataclass
class WrongPrimaryTargetEvidence:
    status: str  # "pass" | "fail"
    violations: list[str] = field(default_factory=list)


def assert_wrong_primary_target_advisory(
    hook_result: dict[str, Any],
    *,
    binding_before: dict[str, Any] | None,
    binding_after: dict[str, Any] | None,
    activity_before: dict[str, Any] | None,
    activity_after: dict[str, Any] | None,
    claim_before: list[dict[str, Any]],
    claim_after: list[dict[str, Any]],
) -> WrongPrimaryTargetEvidence:
    """AC7: a wrong-primary-target ``UserPromptSubmit`` against an ACTIVE
    synthetic Task must be observed as advisory (never a hard block), and
    must leave the isolated DB's Binding/Activity/claim rows unchanged.
    ``hook_result`` is the real ``task-contextctl hook UserPromptSubmit``
    result envelope's ``data`` observed for this real prompt turn."""
    violations: list[str] = []
    if hook_result.get("decision") != "pass":
        violations.append(
            f"decision={hook_result.get('decision')!r}, expected 'pass' (Claude processing must continue)"
        )
    if not hook_result.get("advisory"):
        violations.append("advisory flag not set true -- must be observed as advisory, not silent/hard-block")
    if binding_before != binding_after:
        violations.append("binding row changed across the wrong-primary-target turn")
    if activity_before != activity_after:
        violations.append("activity row changed across the wrong-primary-target turn")
    if claim_before != claim_after:
        violations.append("task_ref_claims changed across the wrong-primary-target turn")
    return WrongPrimaryTargetEvidence(status="fail" if violations else "pass", violations=violations)


@dataclass
class ClearScenarioEvidence:
    status: str  # "pass" | "fail"
    violations: list[str] = field(default_factory=list)


def assert_clear_scenario_evidence(
    *,
    pre_clear_session_id: str | None,
    post_clear_session_id: str | None,
    task_id_before: str | None,
    task_id_after: str | None,
    activity_id_before: str | None,
    activity_id_after: str | None,
    binding_id_before: str | None,
    binding_id_after: str | None,
) -> ClearScenarioEvidence:
    """AC8: ``/clear`` causal evidence. Model self-report / a plain
    conversation marker is never sufficient -- the caller must supply REAL
    observed pre/post Claude session ids (distinct) plus identical
    task_id/activity_id/binding_id in the isolated DB across the clear."""
    violations: list[str] = []
    if not pre_clear_session_id or not post_clear_session_id:
        violations.append(
            "missing pre_clear_session_id or post_clear_session_id (both must be observed, not assumed)"
        )
    elif pre_clear_session_id == post_clear_session_id:
        violations.append(
            "pre_clear_session_id == post_clear_session_id -- /clear did not actually start a new session"
        )
    if not task_id_before or task_id_before != task_id_after:
        violations.append(f"task_id changed across /clear: before={task_id_before!r} after={task_id_after!r}")
    if not activity_id_before or activity_id_before != activity_id_after:
        violations.append(
            f"activity_id changed across /clear: before={activity_id_before!r} after={activity_id_after!r}"
        )
    if not binding_id_before or binding_id_before != binding_id_after:
        violations.append(f"binding_id changed across /clear: before={binding_id_before!r} after={binding_id_after!r}")
    return ClearScenarioEvidence(status="fail" if violations else "pass", violations=violations)
