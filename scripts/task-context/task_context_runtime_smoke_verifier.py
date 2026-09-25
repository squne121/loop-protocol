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
and never embeds Task Context PASS/FAIL semantic classification.

PR #2708 REQUEST_CHANGES fix_delta item 2: ``orchestrate_runtime_smoke``
below is the ONE canonical Task Context orchestration entrypoint. It is the
only place in this module that itself launches the generic runner (as a
subprocess -- the runner's own transport-only contract is reused as-is,
never modified/duplicated) and collects statusLine evidence (by invoking
the already-configured ``.claude/hooks/task_context/statusline.py`` command
directly, reused as-is). Every OTHER function in this module remains a pure
function over caller-supplied observed values, unchanged. The Task Context
semantic PASS/FAIL verdict lives ONLY here/in this module -- never in the
generic runner.
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
    before: dict[str, list[dict[str, Any]]],
    after: dict[str, list[dict[str, Any]]],
    *,
    expected_task_id: str,
    expected_activity_id: str,
) -> TableDiffResult:
    """AC3/AC5: the ONLY permitted ``execution_runs`` delta is the addition
    of at most ``MAX_RUNTIME_SMOKE_ROWS_ADDED`` row(s), each with
    ``run_kind == RUNTIME_SMOKE_RUN_KIND``, ``binding_id IS NULL``, AND
    ``task_id``/``activity_id`` matching ``expected_task_id``/
    ``expected_activity_id`` exactly (Issue #2568 PR #2708 REQUEST_CHANGES
    fix_delta item 3: AC5 parent Task/Activity attribution must be directly
    asserted in the DB delta, not merely typed by the caller -- a
    runtime-smoke row attached to no Task/Activity, or to the WRONG one,
    must fail here). Any removal, mutation of an existing row, addition
    beyond that bound, or an added row that does not match the fixed shape
    is an unexpected mutation -> ``status == "fail"``.

    ``expected_task_id``/``expected_activity_id`` are required (not
    Optional/defaulted) -- there is no meaningful "don't check attribution"
    mode for this assertion."""
    if not expected_task_id:
        raise ValueError("expected_task_id is required and must be non-empty")
    if not expected_activity_id:
        raise ValueError("expected_activity_id is required and must be non-empty")
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
        if row.get("task_id") != expected_task_id:
            violations.append(
                f"added row {row.get('id')!r} has task_id={row.get('task_id')!r}, "
                f"expected {expected_task_id!r}"
            )
        if row.get("activity_id") != expected_activity_id:
            violations.append(
                f"added row {row.get('id')!r} has activity_id={row.get('activity_id')!r}, "
                f"expected {expected_activity_id!r}"
            )
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
    before: dict[str, list[dict[str, Any]]],
    after: dict[str, list[dict[str, Any]]],
    *,
    expected_task_id: str,
    expected_activity_id: str,
) -> CanonicalDeltaContractResult:
    """AC3 aggregate: byte-identical forbidden tables AND the bounded
    execution_runs contract (including AC5 Task/Activity attribution,
    fix_delta item 3) must BOTH hold for the aggregate to be ``"pass"``."""
    forbidden = assert_forbidden_tables_byte_identical(before, after)
    runs = assert_execution_runs_delta_contract(
        before, after, expected_task_id=expected_task_id, expected_activity_id=expected_activity_id
    )
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
    e.g. ``LOOP_TASK_CONTEXT_RUNTIME_VARIANT`` for the Claude-GPT lane).

    Issue #2568 PR #2708 REQUEST_CHANGES fix_delta item 1 (atomic carrier
    integrity): ``extra`` is applied BEFORE the two reserved keys are set,
    so a caller-supplied ``extra`` can never silently overwrite either
    reserved key afterward -- the two reserved keys always win and are
    always exactly ``config.RUNTIME_SMOKE_SCOPE_VALUE`` /
    ``str(state_root)`` on return, regardless of ``extra``'s contents.
    Every other key in ``extra`` is unaffected."""
    if not state_root.is_absolute():
        raise ValueError(f"state_root must be absolute, got {state_root!r}")
    env = dict(base_env) if base_env is not None else dict(os.environ)
    if extra:
        env.update(extra)
    env[config.SCOPE_ENV_VAR] = config.RUNTIME_SMOKE_SCOPE_VALUE
    env[config.STATE_ROOT_ENV_VAR] = str(state_root)
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
    task_id: str,
    activity_id: str,
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
    attachment) -- this function itself stores no evidence payload.

    Issue #2568 PR #2708 REQUEST_CHANGES fix_delta item 3: ``task_id``/
    ``activity_id`` are required (not Optional) -- a runtime-smoke
    ExecutionRun attached to no parent Task/Activity would be unattributed
    and is rejected here rather than silently accepted as ``NULL``."""
    if not task_id:
        raise ValueError("task_id is required and must be non-empty")
    if not activity_id:
        raise ValueError("activity_id is required and must be non-empty")
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


# Issue #2568 PR #2708 REQUEST_CHANGES fix_delta item 4 (P2): the canonical
# normalized representation this codebase already records for a real
# Claude Code `SessionStart` hook fired with `source == "clear"`
# (`task_context_hook_flows.on_session_start`'s `_RECOVERABLE_SOURCES`
# branch) -- see `hook:SessionStart` events with this `reason_code` in
# their metadata, appended via `task_context_service.append_event`.
CLEAR_RESTORED_BINDING_REASON_CODE = "clear_restored_binding"


def _event_reason_code(event: dict[str, Any]) -> str | None:
    """Extract ``reason_code`` from an ``events`` row -- accepts either the
    raw DB row shape (``metadata_json`` as a JSON string) or an
    already-parsed shape (``metadata`` as a dict), so a caller can pass
    either a raw ``SELECT * FROM events`` row or a pre-parsed dict without
    this module re-deriving the parsing convention twice."""
    metadata = event.get("metadata")
    if metadata is None and event.get("metadata_json") is not None:
        try:
            metadata = json.loads(event["metadata_json"])
        except (TypeError, ValueError):
            metadata = None
    if not isinstance(metadata, dict):
        return None
    return metadata.get("reason_code")


def assert_clear_causal_evidence(
    *,
    pre_clear_execution_run: dict[str, Any] | None,
    clear_event: dict[str, Any] | None,
    post_clear_execution_run: dict[str, Any] | None,
) -> ClearScenarioEvidence:
    """AC8 strengthening (Issue #2568 PR #2708 REQUEST_CHANGES fix_delta
    item 4): two unrelated sessions sharing the same Binding (same
    task_id/activity_id/binding_id, distinct session ids -- the shape
    ``assert_clear_scenario_evidence`` above already accepts) is NOT
    sufficient evidence of a REAL causal ``/clear``. This assertion
    additionally requires, and checks the actual timestamp ordering of, a
    genuine clear-associated ``events`` row:

    - ``clear_event`` must exist and carry
      ``reason_code == CLEAR_RESTORED_BINDING_REASON_CODE`` (the real,
      already-recorded normalized representation of a Claude Code
      ``SessionStart`` hook fired with ``source == "clear"`` -- see
      ``task_context_hook_flows.on_session_start``). No new hook/event
      recorder is added here (Issue #2568 explicit constraint) -- this
      function only reads the existing recorded shape.
    - ``pre_clear_execution_run['ended_at']``, ``clear_event['occurred_at']``,
      and ``post_clear_execution_run['started_at']`` must be observed and
      causally ordered (pre-clear run ended at or before the clear event,
      which occurred at or before the post-clear run started) -- checked as
      an actual ISO-8601 string/timestamp comparison, never inferred from
      value presence alone. All three timestamps are produced by
      ``task_context_service.now_iso()`` (fixed UTC offset), so lexical
      string ordering is a valid proxy for chronological ordering.

    Callers combine this with ``assert_clear_scenario_evidence`` above (same
    call site) -- this function does not repeat the session-id/identity
    checks that function already performs."""
    violations: list[str] = []

    pre_ended_at = (pre_clear_execution_run or {}).get("ended_at")
    if pre_clear_execution_run is None or not pre_ended_at:
        violations.append("missing pre_clear_execution_run or its ended_at timestamp")

    post_started_at = (post_clear_execution_run or {}).get("started_at")
    if post_clear_execution_run is None or not post_started_at:
        violations.append("missing post_clear_execution_run or its started_at timestamp")

    clear_occurred_at = None
    if clear_event is None:
        violations.append(
            "missing clear-associated event evidence (a real hook:SessionStart "
            f"events row with reason_code={CLEAR_RESTORED_BINDING_REASON_CODE!r})"
        )
    else:
        reason_code = _event_reason_code(clear_event)
        if reason_code != CLEAR_RESTORED_BINDING_REASON_CODE:
            violations.append(
                f"clear_event reason_code={reason_code!r}, expected "
                f"{CLEAR_RESTORED_BINDING_REASON_CODE!r} -- two unrelated sessions on the "
                "same Binding with no real causal clear-event must not pass"
            )
        clear_occurred_at = clear_event.get("occurred_at")
        if not clear_occurred_at:
            violations.append("clear_event missing occurred_at timestamp")

    if not violations:
        if not (pre_ended_at <= clear_occurred_at <= post_started_at):
            violations.append(
                "causal ordering violated: expected pre_clear ended_at <= clear_event "
                f"occurred_at <= post_clear started_at, got "
                f"pre_ended_at={pre_ended_at!r}, clear_occurred_at={clear_occurred_at!r}, "
                f"post_started_at={post_started_at!r}"
            )

    return ClearScenarioEvidence(status="fail" if violations else "pass", violations=violations)


# ---------------------------------------------------------------------------
# Canonical Task Context runtime-smoke orchestration entrypoint (Issue #2568
# PR #2708 REQUEST_CHANGES fix_delta item 2)
# ---------------------------------------------------------------------------

_RUNNER_SCRIPT = Path(__file__).resolve().parent.parent / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
_STATUSLINE_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent / ".claude" / "hooks" / "task_context" / "statusline.py"
)
_DEGENERATE_STATUSLINE_RENDERS = frozenset({"Unbound", "Degraded", ""})


def invoke_generic_runner(argv: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess:
    """Invoke the generic ``worktree-agent-runtime-smoke`` runner
    (``scripts/agent-ops/run_worktree_agent_runtime_smoke.py``) as a
    subprocess -- the exact same canonical live invocation shape documented
    in ``.claude/skills/worktree-agent-runtime-smoke/SKILL.md``. This never
    duplicates or modifies that runner's own launch/transport logic
    (Responsibility boundary docstring at the top of this module); it only
    adds the ``--task-context-scope``/``--task-context-state-root``
    passthrough flags that runner already exposes as purely additive."""
    return subprocess.run(
        [sys.executable, str(_RUNNER_SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def _read_evidence_json(evidence_json_path: Path) -> dict[str, Any] | None:
    """Best-effort read of the runner's own ``--evidence-json`` machine
    dump. Returns ``None`` (never raises) when absent or unparsable -- a
    caller must treat that as "no runner evidence observed", not as a
    reason to crash this orchestration."""
    if not evidence_json_path.exists():
        return None
    try:
        return json.loads(evidence_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def collect_statusline_evidence(
    env: dict[str, str], *, claude_session_id: str | None, timeout_seconds: float = 5.0
) -> dict[str, Any]:
    """Issue #2568 AC2 statusLine runtime evidence (fix_delta item 2's
    dedicated statusLine requirement): run the EXACT already-configured
    statusLine command (``.claude/hooks/task_context/statusline.py``, wired
    from ``.claude/settings.json``'s own ``statusLine.command`` -- reused
    as-is; no new UI/daemon machinery is added here) against ``env`` (the
    SAME isolated Task Context env the real runtime-smoke child received)
    with the real ``claude_session_id`` this runtime-smoke run actually
    observed (via the generic runner's own ``parent_session_id`` evidence
    field). This is mechanical evidence -- not settings-JSON-file
    existence, not a code review -- that the statusLine command, invoked
    with this session's real identity against this run's real isolated DB,
    actually queries and renders this run's seeded Task/Activity: a
    non-degenerate rendered line (neither ``"Unbound"`` nor ``"Degraded"``
    nor empty) is the passing signal; anything else fails closed rather
    than being silently treated as a pass."""
    if not claude_session_id:
        return {
            "status": "skipped",
            "reason": "no claude_session_id observed for this run (see runner's parent_session_id evidence field)",
            "rendered": None,
        }
    proc = subprocess.run(
        [sys.executable, str(_STATUSLINE_SCRIPT)],
        input=json.dumps({"session_id": claude_session_id}),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout_seconds,
    )
    rendered = proc.stdout.strip()
    if proc.returncode != 0:
        return {"status": "failed", "rendered": rendered, "returncode": proc.returncode, "stderr": proc.stderr}
    if rendered in _DEGENERATE_STATUSLINE_RENDERS:
        return {"status": "executed_degenerate", "rendered": rendered, "returncode": proc.returncode}
    return {"status": "executed", "rendered": rendered, "returncode": proc.returncode}


@dataclass
class RuntimeSmokeOrchestrationResult:
    status: str  # "pass" | "fail"
    seed: dict[str, Any]
    canonical_delta: CanonicalDeltaContractResult
    runner_returncode: int | None
    runner_evidence: dict[str, Any] | None
    statusline_evidence: dict[str, Any]
    scenario_evidence: dict[str, Any]
    violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "canonical_delta": self.canonical_delta.to_dict(),
            "runner_returncode": self.runner_returncode,
            "statusline_evidence": self.statusline_evidence,
            "scenario_evidence": self.scenario_evidence,
            "violations": self.violations,
        }


def orchestrate_runtime_smoke(
    canonical_conn,
    *,
    worktree: str,
    base_dir: Path,
    prompt_file: str,
    output_dir: str,
    canonical_task_id: str,
    canonical_activity_id: str,
    run_id: str | None = None,
    timeout_seconds: float = 180.0,
    runner_argv_extra: list[str] | None = None,
    wrong_primary_target_evidence: WrongPrimaryTargetEvidence | None = None,
    clear_scenario_evidence: ClearScenarioEvidence | None = None,
    clear_causal_evidence: ClearScenarioEvidence | None = None,
) -> RuntimeSmokeOrchestrationResult:
    """The ONE canonical Task Context orchestration entrypoint (Issue #2568
    PR #2708 REQUEST_CHANGES fix_delta item 2). Owns, by calling existing
    primitives only (never duplicating their logic):

    - run-scoped isolated state-root/env preparation (``build_isolated_
      state_root``/``build_isolated_env``)
    - synthetic fixture / smoke seed (``invoke_smoke_seed``)
    - driving a fresh REAL runtime launch via the existing generic runner
      (``invoke_generic_runner``, transport-only contract unchanged)
    - the canonical ``execution_runs`` roll-up with AC5 Task/Activity
      attribution (``roll_up_runtime_smoke_execution_run``)
    - before/after canonical DB assertion, including the AC5 attribution
      check (``canonical_delta_contract``)
    - bounded/public-safe statusLine evidence (``collect_statusline_
      evidence``)
    - an aggregate verdict combining all of the above, living ONLY here --
      never inside the generic runner (Responsibility boundary docstring).

    The wrong-primary-target and ``/clear`` scenarios (``assert_wrong_
    primary_target_advisory``/``assert_clear_scenario_evidence``/
    ``assert_clear_causal_evidence``) each require a caller to have already
    driven their own specific multi-turn real session and captured the
    observed values those pure functions take -- this orchestration accepts
    their PRE-COMPUTED ``*Evidence`` results (still produced by this same
    module's own functions) as optional parameters, and reports each as
    ``"skipped"`` (never fabricated as ``"pass"``) when a caller does not
    supply one for a given orchestration call, consistent with Issue #2568's
    own runtime-verification skip_conditions.

    ``canonical_task_id``/``canonical_activity_id`` (Issue #2744, added for
    live-runtime testability; PR #2745 OWNER review F4 made both REQUIRED,
    not Optional/defaulted): ``roll_up_runtime_smoke_execution_run``'s own
    docstring requires the canonical ``execution_runs`` roll-up row to be
    attributed to "the caller's own current parent Task/Activity" -- i.e. a
    Task/Activity pair that ALREADY exists in ``canonical_conn`` before this
    call (so it is already present in both the ``before`` and ``after``
    ``snapshot_canonical_tables`` snapshots and therefore never itself trips
    the AC3 ``tasks``/``activities`` byte-identical check below). This
    orchestration always attributes the roll-up (and the
    ``canonical_delta_contract`` expected-attribution check) to exactly this
    caller-supplied pair -- there is no "reuse this run's OWN isolated
    smoke-seed ids for the canonical roll-up" fallback mode: PR #2708's
    original default-path reuse only ever succeeded against a
    ``canonical_conn`` that happened to already contain a Task/Activity with
    those exact (isolated, randomly-generated) ids, which no real canonical
    DB does, so making the parameters Optional/defaulted only deferred a
    guaranteed ``NotFoundError`` from call time to roll-up time, AFTER the
    real seed/subprocess launch had already run (PR #2745 review finding).
    Both ids are validated -- via ``task_context_service.get_task``/
    ``get_activity`` against ``canonical_conn`` (each raising
    ``errors.NotFoundError`` if absent) plus an explicit
    ``activity["task_id"] == canonical_task_id`` cross-check -- BEFORE the
    ``smoke seed`` subprocess or the real runtime-smoke subprocess launch
    below, so an invalid pair is rejected before either side effect, never
    after."""
    violations: list[str] = []

    if not canonical_task_id:
        raise ValueError("canonical_task_id is required and must be non-empty")
    if not canonical_activity_id:
        raise ValueError("canonical_activity_id is required and must be non-empty")
    # PR #2745 OWNER review (F4): validated against canonical_conn BEFORE any
    # seed/subprocess side effect below -- service.get_task/get_activity each
    # raise errors.NotFoundError when the id does not exist in this
    # canonical DB, and the explicit task_id cross-check below catches a
    # syntactically-valid-but-mismatched pair (an Activity that belongs to a
    # DIFFERENT Task than the supplied canonical_task_id).
    service.get_task(canonical_conn, canonical_task_id)
    canonical_activity_row = service.get_activity(canonical_conn, canonical_activity_id)
    if canonical_activity_row["task_id"] != canonical_task_id:
        raise ValueError(
            f"canonical_activity_id {canonical_activity_id!r} belongs to task_id "
            f"{canonical_activity_row['task_id']!r}, not the supplied canonical_task_id "
            f"{canonical_task_id!r}"
        )

    state_root = build_isolated_state_root(base_dir, run_id=run_id)
    if is_state_root_materialized(state_root):
        raise RuntimeError(f"refusing to reuse an already-materialized state root: {state_root}")
    env = build_isolated_env(state_root)

    seed = invoke_smoke_seed(env, title="runtime-smoke orchestration seed")
    # PR #2745 OWNER review (F4): the run's OWN isolated smoke-seed task_id
    # is still needed below (evidence_json_path naming); its activity_id is
    # no longer read here now that canonical_task_id/canonical_activity_id
    # are required and always used for the roll-up (no more "reuse the
    # run's own isolated seed ids" fallback -- see docstring above).
    task_id = seed["data"]["task_id"]

    before = snapshot_canonical_tables(canonical_conn)

    evidence_json_path = Path(base_dir) / f"runner-evidence-{run_id or task_id}.json"
    # PR #2745 OWNER review (F2, https://github.com/squne121/loop-protocol/pull/2745#issuecomment-5816561294):
    # the two reserved carrier flags (``--task-context-scope``/
    # ``--task-context-state-root``) are placed LAST, strictly AFTER
    # ``runner_argv_extra``, not before it. The generic runner's own
    # ``argparse``-based CLI (unmodified -- Out of Scope, #2568/PR #2708
    # responsibility boundary) resolves a repeated option to its LAST
    # occurrence ("last flag wins"); putting the reserved pair first (as
    # before this fix) meant a caller-supplied ``runner_argv_extra``
    # containing the SAME option names could silently shadow the canonical
    # scope/state-root values the child generic runner actually acts on --
    # while a naive check of "is the flag present in argv" (its FIRST
    # occurrence) would still find the canonical pair and wrongly report
    # this canonical carrier contract intact. Ordering the reserved pair
    # last, using the exact same standard argparse last-occurrence-wins
    # behavior (not a new validation/rejection mechanism), makes the
    # canonical values the ones the child generic runner actually parses,
    # regardless of what ``runner_argv_extra`` contains -- this is a narrow
    # ordering fix to THIS module's own argv construction, not a change to
    # the generic runner's own option parsing/defaults.
    argv = [
        "--runtime", "claude",
        "--mode", "structured",
        "--worktree", worktree,
        "--prompt-file", prompt_file,
        "--output-dir", output_dir,
        "--timeout-seconds", str(int(timeout_seconds)),
        "--evidence-json", str(evidence_json_path),
        *(runner_argv_extra or []),
        "--task-context-scope", config.RUNTIME_SMOKE_SCOPE_VALUE,
        "--task-context-state-root", str(state_root),
    ]
    proc = invoke_generic_runner(argv, timeout_seconds=timeout_seconds + 60.0)
    runner_evidence = _read_evidence_json(evidence_json_path)
    if proc.returncode != 0:
        violations.append(
            f"generic runner exit_code={proc.returncode} (0 required); stderr tail={proc.stderr[-500:]!r}"
        )
    claude_session_id = (runner_evidence or {}).get("parent_session_id")

    # PR #2745 OWNER review (F4): canonical_task_id/canonical_activity_id are
    # now required and already validated against canonical_conn above --
    # always attribute the roll-up to exactly that caller-supplied pair (no
    # "reuse this run's own isolated seed ids" fallback -- see docstring).
    roll_up_task_id = canonical_task_id
    roll_up_activity_id = canonical_activity_id
    roll_up_runtime_smoke_execution_run(canonical_conn, task_id=roll_up_task_id, activity_id=roll_up_activity_id)

    after = snapshot_canonical_tables(canonical_conn)
    canonical_delta = canonical_delta_contract(
        before, after, expected_task_id=roll_up_task_id, expected_activity_id=roll_up_activity_id
    )
    if canonical_delta.status != "pass":
        violations.append("canonical_delta_contract failed (see canonical_delta for detail)")

    statusline_evidence = collect_statusline_evidence(env, claude_session_id=claude_session_id)
    if statusline_evidence["status"] == "executed_degenerate" and statusline_evidence.get("rendered") == "Unbound":
        # Issue #2747 (OWNER review, PR #2749 comment): the synthetic smoke
        # seed used by this orchestration is never pre-bound with
        # claude_session_id -- the out-of-band statusLine invocation below
        # therefore triggers a genuine, real SessionStart against
        # .claude/hooks/task_context/statusline.py, which creates a brand
        # new Binding for that session. That fresh Binding has not yet had
        # this run's Task attached to it, so the statusLine query
        # deterministically observes "Unbound" -- a structural artifact of
        # how this seed/probe is wired, NOT a signal about statusLine's own
        # health. Re-interpret ONLY this specific "executed_degenerate" +
        # rendered=="Unbound" combination as not_applicable -- never on its
        # own turning the aggregate `status` into "fail" -- while preserving
        # the original leaf evidence (status/rendered/returncode) under
        # `underlying_status`/as-is keys so a genuine regression stays
        # diagnosable and distinct from this known wiring limitation.
        # "Degraded" and "" renders are NOT re-interpreted here: per
        # .claude/hooks/task_context/statusline.py, "Degraded" represents a
        # genuine DB/query/transport failure surfaced via exit code 0, and
        # must remain a fail-closed violation below (falls through to the
        # `elif` branch). collect_statusline_evidence()'s own
        # executed/executed_degenerate/failed/skipped leaf semantics are
        # untouched by this re-interpretation (Out of Scope).
        statusline_evidence = {
            **statusline_evidence,
            "status": "not_applicable",
            "reason": "synthetic smoke seed lacks pre-bound claude_session_id, so the out-of-band "
            "statusLine probe observes a freshly-created, not-yet-Task-bound Binding (Unbound) "
            "rather than a genuine statusLine failure",
            "underlying_status": statusline_evidence["status"],
        }
    elif statusline_evidence["status"] != "executed":
        violations.append(f"statusline_evidence status={statusline_evidence['status']!r}, expected 'executed'")

    scenario_evidence: dict[str, Any] = {}
    for name, evidence in (
        ("wrong_primary_target", wrong_primary_target_evidence),
        ("clear_scenario", clear_scenario_evidence),
        ("clear_causal", clear_causal_evidence),
    ):
        if evidence is None:
            scenario_evidence[name] = {
                "status": "skipped",
                "reason": "no observed evidence supplied to this orchestration call",
            }
        else:
            scenario_evidence[name] = {"status": evidence.status, "violations": evidence.violations}
            if evidence.status != "pass":
                violations.append(f"{name} evidence status={evidence.status!r}")

    return RuntimeSmokeOrchestrationResult(
        status="pass" if not violations else "fail",
        seed=seed,
        canonical_delta=canonical_delta,
        runner_returncode=proc.returncode,
        runner_evidence=runner_evidence,
        statusline_evidence=statusline_evidence,
        scenario_evidence=scenario_evidence,
        violations=violations,
    )
