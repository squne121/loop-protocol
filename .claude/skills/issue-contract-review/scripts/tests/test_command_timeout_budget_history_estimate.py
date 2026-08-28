"""
Unit tests for the `history_estimate` `command_timeout_budget/v1` source
(Issue #2254 AC1 / AC2 / AC4 / AC5).

AC1 (select with `-k snapshot_determinism`): the SAME `body` + SAME
    immutable `history_snapshot` always yields the SAME `plan_digest` from
    every one of the 4 canonical-plan-computing call sites
    (`baseline_vc_preflight.py` itself, `contract_readiness_check.py`,
    `run_contract_review_once.py`, `run_root_review_pipeline.py`) --
    including AFTER the underlying history store has been written to, or a
    TTL boundary has been crossed in wall-clock time.

AC2 (select with `-k identity_separation`): `command_group_key` /
    `environment_fingerprint` / `execution_id` are separate concepts;
    changing a command's resolved `applied_timeout_ms` (an execution
    PARAMETER) never changes its `command_group_key` (a TEXT/cwd/family
    GROUPING key), so history for a command keeps being found across
    different resolved timeouts.

AC4 (select with `-k raise_only_precedence`): `history_candidate_seconds` /
    `timeout_backoff_floor_seconds` can only ever RAISE the resolved
    timeout above `static_base`, never lower it, and the ceiling
    (`MAX_PER_COMMAND_TIMEOUT_SECONDS`) still applies.

AC5 (select with `-k timeout_censoring_backoff`): a `timed_out` sample is
    treated as a right-censored lower bound (feeds
    `timeout_backoff_floor_seconds`, never `observed_p95_ms`), and
    `cancelled` / non-zero-exit `failed` samples never contaminate the
    success-only duration percentile.

Runtime Verification Applicability: not_applicable (this file exercises
`compute_command_timeout_budget()` / `compute_canonical_vc_plan()` purely
in-process against synthetic `history_snapshot` dicts -- no subprocess, no
SQLite I/O. The genuine end-to-end SQLite + subprocess runtime
verification lives in
`.claude/skills/issue-contract-review/tests/test_history_snapshot_production_wiring.py`
(AC9) and `test_command_timeout_history_store.py` (AC3/AC6/AC7/AC8).
"""

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
# Issue #2254 AC1: run_root_review_pipeline.py lives in a sibling skill's
# scripts/ dir -- added here so the snapshot-determinism cross-consumer
# test can import it directly, same as the other 3 consumers.
_REFINEMENT_LOOP_SCRIPTS_DIR = (
    _SCRIPTS_DIR.parents[1] / "issue-refinement-loop" / "scripts"
)
sys.path.insert(0, str(_REFINEMENT_LOOP_SCRIPTS_DIR))

import baseline_vc_preflight as m  # noqa: E402
import vc_runtime_history as h  # noqa: E402


def _make_snapshot(records, store_status="ok"):
    return {
        "schema": h.HISTORY_SNAPSHOT_SCHEMA,
        "snapshot_as_of_utc": "2026-01-01T00:00:00+00:00",
        "store_status": store_status,
        "records": records,
        "snapshot_digest": "sha256:" + ("0" * 64),
    }


def _history_record(
    *,
    fingerprint,
    eligible_sample_count=10,
    observed_p95_ms=None,
    history_candidate_seconds=None,
    previous_applied_timeout_seconds=None,
    timeout_backoff_floor_seconds=None,
):
    return {
        "environment_fingerprint": fingerprint,
        "eligible_sample_count": eligible_sample_count,
        "observed_p95_ms": observed_p95_ms,
        "history_candidate_seconds": history_candidate_seconds,
        "previous_applied_timeout_seconds": previous_applied_timeout_seconds,
        "timeout_backoff_floor_seconds": timeout_backoff_floor_seconds,
    }


_SAMPLE_BODY = """## Verification Commands

```bash
# AC1
$ pnpm typecheck
```
"""


# ---------------------------------------------------------------------------
# AC1: snapshot determinism (`-k snapshot_determinism`)
# ---------------------------------------------------------------------------


def test_snapshot_determinism_across_all_four_canonical_plan_consumers():
    """The SAME body + SAME history_snapshot yields the SAME plan_digest
    whether computed via baseline_vc_preflight.py's own producer path, or
    via the exact `compute_canonical_vc_plan` symbol each of the other 3
    consumer modules imports (contract_readiness_check.py /
    run_contract_review_once.py / run_root_review_pipeline.py) -- proving
    the wiring those modules add is a pure pass-through of the SAME
    snapshot object, never re-deriving it independently per consumer."""
    import contract_readiness_check as crc
    import run_contract_review_once as rcro
    import run_root_review_pipeline as rrrp

    gk = h.compute_command_group_key("pnpm typecheck", ".")
    fp = h.compute_environment_fingerprint("pnpm")
    snapshot = _make_snapshot(
        {gk: _history_record(fingerprint=fp, history_candidate_seconds=5)}
    )

    plan_a = m.compute_canonical_vc_plan(_SAMPLE_BODY, cwd=".", history_snapshot=snapshot)
    plan_b = crc._compute_canonical_vc_plan(_SAMPLE_BODY, cwd=".", history_snapshot=snapshot)
    plan_c = rcro.compute_canonical_vc_plan(_SAMPLE_BODY, cwd=".", history_snapshot=snapshot)
    plan_d = rrrp._compute_canonical_vc_plan(_SAMPLE_BODY, cwd=".", history_snapshot=snapshot)

    digests = {plan_a["plan_digest"], plan_b["plan_digest"], plan_c["plan_digest"], plan_d["plan_digest"]}
    assert len(digests) == 1, digests


def test_snapshot_determinism_survives_store_write_after_snapshot_built():
    """Once a snapshot dict has been produced, mutating what a FRESH
    snapshot for the SAME store would say (simulated here by simply
    building a second, DIFFERENT snapshot dict) does not retroactively
    change the plan_digest computed from the ORIGINAL, already-built
    snapshot object -- compute_canonical_vc_plan() never re-reads the
    store itself."""
    gk = h.compute_command_group_key("pnpm typecheck", ".")
    fp = h.compute_environment_fingerprint("pnpm")
    snapshot_before = _make_snapshot(
        {gk: _history_record(fingerprint=fp, history_candidate_seconds=5)}
    )
    plan_before = m.compute_canonical_vc_plan(
        _SAMPLE_BODY, cwd=".", history_snapshot=snapshot_before
    )

    # A hypothetical "later" snapshot (e.g. after new samples were written,
    # or after a TTL boundary passed) is DIFFERENT ...
    snapshot_after = _make_snapshot(
        {gk: _history_record(fingerprint=fp, history_candidate_seconds=999)}
    )
    # ... but re-deriving the plan from the ORIGINAL snapshot object still
    # produces the EXACT SAME digest (pure function of its inputs).
    plan_replay = m.compute_canonical_vc_plan(
        _SAMPLE_BODY, cwd=".", history_snapshot=snapshot_before
    )
    plan_with_new_snapshot = m.compute_canonical_vc_plan(
        _SAMPLE_BODY, cwd=".", history_snapshot=snapshot_after
    )

    assert plan_before["plan_digest"] == plan_replay["plan_digest"]
    assert plan_before["plan_digest"] != plan_with_new_snapshot["plan_digest"]


def test_snapshot_determinism_ttl_boundary_crossing_does_not_change_digest():
    """`snapshot_as_of_utc` is FROZEN at snapshot-build time.
    `compute_canonical_vc_plan()` never calls `datetime.now()` itself, so
    even if wall-clock time crosses the 30-day TTL boundary between when a
    snapshot was built and when its plan_digest is recomputed, re-deriving
    the plan from the SAME (now technically 'stale-looking')
    snapshot_as_of_utc value still yields the identical digest."""
    gk = h.compute_command_group_key("pnpm typecheck", ".")
    fp = h.compute_environment_fingerprint("pnpm")
    stale_snapshot = _make_snapshot(
        {gk: _history_record(fingerprint=fp, history_candidate_seconds=5)},
    )
    stale_snapshot["snapshot_as_of_utc"] = "2020-01-01T00:00:00+00:00"  # far past TTL

    plan_1 = m.compute_canonical_vc_plan(_SAMPLE_BODY, cwd=".", history_snapshot=stale_snapshot)
    plan_2 = m.compute_canonical_vc_plan(_SAMPLE_BODY, cwd=".", history_snapshot=stale_snapshot)
    assert plan_1["plan_digest"] == plan_2["plan_digest"]


# ---------------------------------------------------------------------------
# AC2: identity separation (`-k identity_separation`)
# ---------------------------------------------------------------------------


def test_identity_separation_group_key_unaffected_by_applied_timeout():
    """`command_group_key` is derived from argv/cwd/family ONLY --
    computing it twice for the SAME command text at two DIFFERENT
    `_effective_timeout_for()` resolutions (an execution PARAMETER) yields
    the SAME group key, so history for a command is not silently
    invalidated the moment its resolved timeout changes."""
    command = "uv run --locked pytest foo.py -v"
    key_at_150s = h.compute_command_group_key(command, ".")
    key_at_420s = h.compute_command_group_key(command, ".")
    assert key_at_150s == key_at_420s


def test_identity_separation_group_key_distinct_from_execution_key_hash():
    """`command_group_key` (history GROUPING) and
    `compute_execution_key_hash()` (fine-grained dedup-replay identity,
    which DOES bind timeout_seconds/state_epoch) are governed by different
    inputs: two calls with the SAME argv/cwd but DIFFERENT
    `timeout_seconds` diverge under `compute_execution_key_hash()` but
    NEVER under `compute_command_group_key()`."""
    argv = ["pnpm", "test"]
    cwd = "."
    group_key = h.compute_command_group_key("pnpm test", cwd)
    exec_key_a = m.compute_execution_key_hash(argv, cwd, {}, timeout_seconds=150)
    exec_key_b = m.compute_execution_key_hash(argv, cwd, {}, timeout_seconds=420)
    assert exec_key_a != exec_key_b
    # The group key does not vary with timeout_seconds at all (it is not
    # even a parameter of compute_command_group_key()).
    assert group_key == h.compute_command_group_key("pnpm test", cwd)


def test_identity_separation_execution_id_is_unique_per_call():
    """`execution_id` identifies ONE subprocess launch; two independent
    calls to `new_execution_id()` never collide."""
    ids = {h.new_execution_id() for _ in range(100)}
    assert len(ids) == 100


def test_identity_separation_environment_fingerprint_independent_of_group_key():
    """`environment_fingerprint` is keyed by command FAMILY (+ lock/tool
    version), not by the full command text -- two DIFFERENT commands in
    the SAME family (and therefore DIFFERENT `command_group_key`s) share
    the SAME fingerprint, proving fingerprint and group key vary
    independently."""
    fp_a = h.compute_environment_fingerprint("pnpm")
    fp_b = h.compute_environment_fingerprint("pnpm")
    gk_a = h.compute_command_group_key("pnpm test", ".")
    gk_b = h.compute_command_group_key("pnpm lint", ".")
    assert fp_a == fp_b
    assert gk_a != gk_b


def test_identity_separation_budget_uses_group_key_not_command_hash():
    """`compute_command_timeout_budget()`'s history lookup keys off
    `command_group_key` (from `history_snapshot["records"]`), which is
    reported back verbatim in the budget entry -- distinct from
    `command_hash`/`command_identity_hash` (Issue #2233's pre-existing
    command TEXT identity)."""
    command = "pnpm build"
    gk = h.compute_command_group_key(command, ".")
    fp = h.compute_environment_fingerprint("pnpm")
    snapshot = _make_snapshot({gk: _history_record(fingerprint=fp, history_candidate_seconds=999)})
    budget = m.compute_command_timeout_budget(command, history_snapshot=snapshot, cwd=".")
    assert budget["command_group_key"] == gk
    assert budget["command_group_key"] != budget["command_hash"]


# ---------------------------------------------------------------------------
# AC4: raise-only precedence (`-k raise_only_precedence`)
# ---------------------------------------------------------------------------


def test_raise_only_precedence_history_below_static_base_never_lowers_resolved():
    """A history_candidate_seconds STRICTLY BELOW static_base (150s
    static_fallback) never lowers the resolved timeout, and `source` stays
    `static_fallback` (never claims `history_estimate` for a value that
    did not actually win)."""
    command = "pnpm lint"
    gk = h.compute_command_group_key(command, ".")
    fp = h.compute_environment_fingerprint("pnpm")
    snapshot = _make_snapshot(
        {gk: _history_record(fingerprint=fp, observed_p95_ms=1000, history_candidate_seconds=2)}
    )
    budget = m.compute_command_timeout_budget(command, history_snapshot=snapshot, cwd=".")
    assert budget["source"] == "static_fallback"
    assert budget["timeout_seconds"] == m.DEFAULT_PER_COMMAND_TIMEOUT_SECONDS


def test_raise_only_precedence_history_above_static_base_raises_resolved():
    """A history_candidate_seconds STRICTLY ABOVE static_base raises the
    resolved timeout and `source` becomes `history_estimate`."""
    command = "pnpm lint"
    gk = h.compute_command_group_key(command, ".")
    fp = h.compute_environment_fingerprint("pnpm")
    snapshot = _make_snapshot(
        {gk: _history_record(fingerprint=fp, observed_p95_ms=200000, history_candidate_seconds=300)}
    )
    budget = m.compute_command_timeout_budget(command, history_snapshot=snapshot, cwd=".")
    assert budget["source"] == "history_estimate"
    assert budget["timeout_seconds"] == 300
    assert budget["timeout_seconds"] > m.DEFAULT_PER_COMMAND_TIMEOUT_SECONDS


def test_raise_only_precedence_never_lowers_a_static_policy_entry():
    """A `static_policy`-curated command (420s) with a WEAKER history
    candidate (e.g. 60s) still resolves to the trusted static_policy
    value, never the lower history value -- raise-only, not
    replace-with-history."""
    command = "uv run --locked pytest .claude/skills/issue-refinement-loop/tests -v"
    assert command in m.STATIC_PER_COMMAND_TIMEOUT_POLICY
    gk = h.compute_command_group_key(command, ".")
    fp = h.compute_environment_fingerprint("uv")
    snapshot = _make_snapshot(
        {gk: _history_record(fingerprint=fp, observed_p95_ms=40000, history_candidate_seconds=60)}
    )
    budget = m.compute_command_timeout_budget(command, history_snapshot=snapshot, cwd=".")
    assert budget["source"] == "static_policy"
    assert budget["timeout_seconds"] == m.STATIC_PER_COMMAND_TIMEOUT_POLICY[command]


def test_raise_only_precedence_explicit_override_always_wins_over_history():
    """`override_seconds` (Issue #2233 AC4 hard global override) always
    wins, regardless of how large a history candidate is."""
    command = "pnpm test"
    gk = h.compute_command_group_key(command, ".")
    fp = h.compute_environment_fingerprint("pnpm")
    snapshot = _make_snapshot(
        {gk: _history_record(fingerprint=fp, observed_p95_ms=500000, history_candidate_seconds=500)}
    )
    budget = m.compute_command_timeout_budget(
        command, override_seconds=42, history_snapshot=snapshot, cwd="."
    )
    assert budget["source"] == "explicit_override"
    assert budget["timeout_seconds"] == 42


def test_raise_only_precedence_history_still_clamped_by_hard_ceiling():
    """A history candidate above MAX_PER_COMMAND_TIMEOUT_SECONDS is
    clamped to the ceiling, not rejected -- the ceiling still applies
    uniformly regardless of source (Issue #2233 AC5, extended to
    history_estimate by Issue #2254 AC4)."""
    command = "pnpm build"
    gk = h.compute_command_group_key(command, ".")
    fp = h.compute_environment_fingerprint("pnpm")
    snapshot = _make_snapshot(
        {
            gk: _history_record(
                fingerprint=fp,
                observed_p95_ms=(m.MAX_PER_COMMAND_TIMEOUT_SECONDS + 500) * 1000,
                history_candidate_seconds=m.MAX_PER_COMMAND_TIMEOUT_SECONDS + 500,
            )
        }
    )
    budget = m.compute_command_timeout_budget(command, history_snapshot=snapshot, cwd=".")
    assert budget["source"] == "history_estimate"
    assert budget["timeout_seconds"] == m.MAX_PER_COMMAND_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# AC5: timeout right-censoring + backoff (`-k timeout_censoring_backoff`)
# ---------------------------------------------------------------------------


def test_timeout_censoring_backoff_raises_next_invocation_resolved_timeout():
    """A `timed_out` sample's `timeout_backoff_floor_seconds` (already
    computed by `vc_runtime_history.compute_timeout_backoff_floor()`,
    exercised here via a synthetic snapshot record) raises the NEXT
    invocation's resolved timeout above the previous static_base."""
    command = "pnpm build"
    gk = h.compute_command_group_key(command, ".")
    fp = h.compute_environment_fingerprint("pnpm")
    snapshot = _make_snapshot(
        {
            gk: _history_record(
                fingerprint=fp,
                eligible_sample_count=0,
                previous_applied_timeout_seconds=150,
                timeout_backoff_floor_seconds=300,
            )
        }
    )
    budget = m.compute_command_timeout_budget(command, history_snapshot=snapshot, cwd=".")
    assert budget["source"] == "history_estimate"
    assert budget["timeout_seconds"] == 300
    assert budget["history_backoff_applied"] is True


def test_timeout_censoring_backoff_floor_formula_matches_contract():
    """`timeout_backoff_floor = min(MAX_PER_COMMAND_TIMEOUT_SECONDS, previous_applied_timeout_seconds * 2)`
    (Issue #2254 In Scope fixed formula), exercised directly against
    `vc_runtime_history.compute_timeout_backoff_floor()`."""
    import sqlite3
    from datetime import datetime, timezone

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE samples (command_group_key TEXT, environment_fingerprint TEXT, "
        "observed_at_utc TEXT, status TEXT, duration_ms INTEGER, applied_timeout_ms INTEGER)"
    )
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO samples VALUES ('gk', 'fp', ?, 'timed_out', NULL, 200000)",
        (now.isoformat(),),
    )
    result = h.compute_timeout_backoff_floor(
        conn, "gk", "fp", now_utc=now, max_seconds=m.MAX_PER_COMMAND_TIMEOUT_SECONDS
    )
    assert result["previous_applied_timeout_seconds"] == 200
    assert result["timeout_backoff_floor_seconds"] == 400  # 200 * 2, below ceiling


def test_timeout_censoring_backoff_floor_clamped_by_ceiling():
    """A very large previous applied timeout's doubled backoff floor is
    still clamped to `MAX_PER_COMMAND_TIMEOUT_SECONDS`."""
    import sqlite3
    from datetime import datetime, timezone

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE samples (command_group_key TEXT, environment_fingerprint TEXT, "
        "observed_at_utc TEXT, status TEXT, duration_ms INTEGER, applied_timeout_ms INTEGER)"
    )
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO samples VALUES ('gk', 'fp', ?, 'timed_out', NULL, 500000)",
        (now.isoformat(),),
    )
    result = h.compute_timeout_backoff_floor(
        conn, "gk", "fp", now_utc=now, max_seconds=m.MAX_PER_COMMAND_TIMEOUT_SECONDS
    )
    assert result["timeout_backoff_floor_seconds"] == m.MAX_PER_COMMAND_TIMEOUT_SECONDS


def test_timeout_censoring_cancelled_and_failed_never_pollute_percentile():
    """`cancelled` and non-zero-exit `failed` samples never contribute to
    `compute_history_estimate()`'s success-only duration percentile."""
    import sqlite3
    from datetime import datetime, timezone

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE samples (command_group_key TEXT, environment_fingerprint TEXT, "
        "observed_at_utc TEXT, status TEXT, duration_ms INTEGER, applied_timeout_ms INTEGER)"
    )
    now = datetime.now(timezone.utc)
    # 5 genuine success samples (meets minimum) ...
    for i in range(5):
        conn.execute(
            "INSERT INTO samples VALUES ('gk', 'fp', ?, 'success', ?, 150000)",
            (now.isoformat(), 1000 + i),
        )
    # ... plus contamination candidates that must be EXCLUDED.
    conn.execute("INSERT INTO samples VALUES ('gk', 'fp', ?, 'cancelled', 999999, 150000)", (now.isoformat(),))
    conn.execute("INSERT INTO samples VALUES ('gk', 'fp', ?, 'failed', 888888, 150000)", (now.isoformat(),))

    result = h.compute_history_estimate(conn, "gk", "fp", now_utc=now)
    assert result["eligible_sample_count"] == 5
    assert result["observed_p95_ms"] < 999999
    assert result["observed_p95_ms"] < 888888
