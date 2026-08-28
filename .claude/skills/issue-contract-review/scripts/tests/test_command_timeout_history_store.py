"""
Unit tests for `vc_runtime_history.py`'s SQLite store (Issue #2254 AC3 /
AC6 / AC7 / AC8).

AC3 (select with `-k nearest_rank`): the `nearest_rank_v1` percentile
    contract (eligible = exit-0 success only, TTL 30 days, newest 50
    samples, minimum 5 eligible samples,
    `P95 = sorted_samples[ceil(0.95 * n) - 1]`,
    `candidate_seconds = ceil(observed_p95_ms * 1.5 / 1000)`).

AC6 (select with `-k one_launch_one_sample`): the SAME `execution_id`
    (representing ONE real subprocess launch, even if referenced by
    multiple AC/occurrences) is stored as exactly ONE sample row --
    repeated `record_sample()` calls for the SAME `execution_id` never
    create a second row.

AC7 (select with `-k atomic_concurrent_write`): concurrent writers (2
    threads, each its own SQLite connection) never lose a sample, and a
    genuinely lock-contended write degrades to a non-blocking failure
    (bounded by `busy_timeout_seconds`) rather than hanging.

AC8 (select with `-k non_blocking_degradation`): every failure mode
    (missing store, locked store, corrupt store, unknown schema version,
    invalid row, environment_fingerprint mismatch, stale/TTL-expired
    samples) degrades `build_history_snapshot()` to an empty/`store_status
    != "ok"` snapshot rather than raising -- callers fall back to
    `static_policy`/`static_fallback` deterministically.

Runtime Verification Applicability: not_applicable (this file uses
`tmp_path`-isolated SQLite files and in-process threads only; it never
launches a real VC subprocess -- see
`.claude/skills/issue-contract-review/tests/test_history_snapshot_production_wiring.py`
for the AC9 real-subprocess runtime verification).
"""

import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import vc_runtime_history as h  # noqa: E402


def _now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# AC3: nearest_rank_v1 (`-k nearest_rank`)
# ---------------------------------------------------------------------------


def test_nearest_rank_below_minimum_sample_count_returns_no_candidate():
    """Fewer than HISTORY_MIN_SUCCESS_SAMPLES (5) eligible success samples
    -> no candidate at all (not a degenerate 0-second candidate)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE samples (command_group_key TEXT, environment_fingerprint TEXT, "
        "observed_at_utc TEXT, status TEXT, duration_ms INTEGER, applied_timeout_ms INTEGER)"
    )
    now = _now()
    for i in range(4):  # one short of the minimum
        conn.execute(
            "INSERT INTO samples VALUES ('gk', 'fp', ?, 'success', ?, 150000)",
            (now.isoformat(), 1000 + i),
        )
    result = h.compute_history_estimate(conn, "gk", "fp", now_utc=now)
    assert result["eligible_sample_count"] == 4
    assert result["observed_p95_ms"] is None
    assert result["history_candidate_seconds"] is None


def test_nearest_rank_exact_minimum_sample_count_produces_candidate():
    """Exactly HISTORY_MIN_SUCCESS_SAMPLES (5) DOES produce a candidate
    (boundary is inclusive)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE samples (command_group_key TEXT, environment_fingerprint TEXT, "
        "observed_at_utc TEXT, status TEXT, duration_ms INTEGER, applied_timeout_ms INTEGER)"
    )
    now = _now()
    for i in range(5):
        conn.execute(
            "INSERT INTO samples VALUES ('gk', 'fp', ?, 'success', ?, 150000)",
            (now.isoformat(), 1000 + i * 10),
        )
    result = h.compute_history_estimate(conn, "gk", "fp", now_utc=now)
    assert result["eligible_sample_count"] == 5
    assert result["observed_p95_ms"] is not None
    assert result["history_candidate_seconds"] is not None


def test_nearest_rank_formula_matches_fixed_contract():
    """`P95 = sorted_samples[ceil(0.95 * n) - 1]`;
    `candidate_seconds = ceil(observed_p95_ms * 1.5 / 1000)` -- verified
    against a hand-computed fixture (n=10, durations 1000..10000ms)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE samples (command_group_key TEXT, environment_fingerprint TEXT, "
        "observed_at_utc TEXT, status TEXT, duration_ms INTEGER, applied_timeout_ms INTEGER)"
    )
    now = _now()
    durations = [1000 * i for i in range(1, 11)]  # 1000..10000
    for d in durations:
        conn.execute(
            "INSERT INTO samples VALUES ('gk', 'fp', ?, 'success', ?, 150000)",
            (now.isoformat(), d),
        )
    result = h.compute_history_estimate(conn, "gk", "fp", now_utc=now)
    n = 10
    import math

    expected_p95 = sorted(durations)[math.ceil(0.95 * n) - 1]  # index 9 -> 10000
    expected_candidate = math.ceil(expected_p95 * 1.5 / 1000)
    assert result["observed_p95_ms"] == expected_p95
    assert result["history_candidate_seconds"] == expected_candidate


def test_nearest_rank_ttl_expired_samples_excluded():
    """Samples older than HISTORY_TTL_DAYS (30) are excluded from the
    eligible window even if there are enough of them in total."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE samples (command_group_key TEXT, environment_fingerprint TEXT, "
        "observed_at_utc TEXT, status TEXT, duration_ms INTEGER, applied_timeout_ms INTEGER)"
    )
    now = _now()
    stale = now - timedelta(days=h.HISTORY_TTL_DAYS + 1)
    for i in range(6):
        conn.execute(
            "INSERT INTO samples VALUES ('gk', 'fp', ?, 'success', ?, 150000)",
            (stale.isoformat(), 1000 + i),
        )
    result = h.compute_history_estimate(conn, "gk", "fp", now_utc=now)
    assert result["eligible_sample_count"] == 0
    assert result["history_candidate_seconds"] is None


def test_nearest_rank_window_caps_at_50_newest_samples():
    """Only the newest HISTORY_WINDOW_MAX_SAMPLES (50) samples are
    considered even if more exist within the TTL window."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE samples (command_group_key TEXT, environment_fingerprint TEXT, "
        "observed_at_utc TEXT, status TEXT, duration_ms INTEGER, applied_timeout_ms INTEGER)"
    )
    now = _now()
    # 60 samples: the OLDEST 10 have a huge duration that must NOT affect
    # the result once windowed down to the newest 50.
    for i in range(60):
        ts = now - timedelta(minutes=(60 - i))
        duration = 999_000 if i < 10 else 1000
        conn.execute(
            "INSERT INTO samples VALUES ('gk', 'fp', ?, 'success', ?, 150000)",
            (ts.isoformat(), duration),
        )
    result = h.compute_history_estimate(conn, "gk", "fp", now_utc=now)
    assert result["eligible_sample_count"] == h.HISTORY_WINDOW_MAX_SAMPLES
    assert result["observed_p95_ms"] == 1000  # the huge outliers were windowed out


def test_nearest_rank_environment_fingerprint_mismatch_excluded():
    """Samples recorded under a DIFFERENT environment_fingerprint never
    contribute to this fingerprint's percentile."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE samples (command_group_key TEXT, environment_fingerprint TEXT, "
        "observed_at_utc TEXT, status TEXT, duration_ms INTEGER, applied_timeout_ms INTEGER)"
    )
    now = _now()
    for i in range(5):
        conn.execute(
            "INSERT INTO samples VALUES ('gk', 'fp-OTHER', ?, 'success', ?, 150000)",
            (now.isoformat(), 1000 + i),
        )
    result = h.compute_history_estimate(conn, "gk", "fp", now_utc=now)
    assert result["eligible_sample_count"] == 0


# ---------------------------------------------------------------------------
# AC6: one launch, one sample (`-k one_launch_one_sample`)
# ---------------------------------------------------------------------------


def test_one_launch_one_sample_duplicate_execution_id_never_double_stores(tmp_path):
    """Repeated `record_sample()` calls for the SAME execution_id (as
    would happen if multiple AC occurrences reference one dedup-replayed
    launch) never create a second row."""
    store = tmp_path / "store.sqlite3"
    execution_id = h.new_execution_id()
    for _ in range(3):
        result = h.record_sample(
            store,
            execution_id=execution_id,
            command_group_key="gk",
            environment_fingerprint="fp",
            status="success",
            command_hash="abc123",
            duration_ms=1000,
            applied_timeout_ms=150000,
        )
        assert result["store_status"] == "ok"
    conn = sqlite3.connect(str(store))
    count = conn.execute("SELECT COUNT(*) FROM samples WHERE sample_id = ?", (execution_id,)).fetchone()[0]
    assert count == 1


def test_one_launch_one_sample_first_call_recorded_subsequent_calls_ignored():
    """`record_sample()`'s own return value distinguishes the FIRST
    (genuinely recorded) call from a later duplicate (ignored)."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        store = Path(d) / "store.sqlite3"
        execution_id = h.new_execution_id()
        first = h.record_sample(
            store, execution_id=execution_id, command_group_key="gk",
            environment_fingerprint="fp", status="success", command_hash="abc",
            duration_ms=500, applied_timeout_ms=150000,
        )
        second = h.record_sample(
            store, execution_id=execution_id, command_group_key="gk",
            environment_fingerprint="fp", status="success", command_hash="abc",
            duration_ms=999999, applied_timeout_ms=150000,  # different payload, same id
        )
        assert first["recorded"] is True
        assert second["recorded"] is False
        assert second["reason"] == "duplicate_sample_id_ignored"


def test_one_launch_one_sample_distinct_execution_ids_each_stored():
    """Distinct execution_ids (distinct real launches) each get their own
    row -- this is NOT a blanket dedup of the whole table."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        store = Path(d) / "store.sqlite3"
        for _ in range(5):
            result = h.record_sample(
                store, execution_id=h.new_execution_id(), command_group_key="gk",
                environment_fingerprint="fp", status="success", command_hash="abc",
                duration_ms=1000, applied_timeout_ms=150000,
            )
            assert result["recorded"] is True
        conn = sqlite3.connect(str(store))
        count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        assert count == 5


# ---------------------------------------------------------------------------
# AC7: atomic local persistence (`-k atomic_concurrent_write`)
# ---------------------------------------------------------------------------


def test_atomic_concurrent_write_two_threads_both_persist(tmp_path):
    """Two concurrent writers (each its own connection/thread), each
    inserting a DISTINCT execution_id, both succeed and both rows are
    present -- SQLite's busy_timeout serializes the two short
    transactions rather than losing either."""
    store = tmp_path / "store.sqlite3"
    results = {}

    def _writer(idx):
        results[idx] = h.record_sample(
            store,
            execution_id=h.new_execution_id(),
            command_group_key="gk",
            environment_fingerprint="fp",
            status="success",
            command_hash="abc",
            duration_ms=1000 + idx,
            applied_timeout_ms=150000,
        )

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert results[0]["store_status"] == "ok"
    assert results[1]["store_status"] == "ok"
    assert results[0]["recorded"] is True
    assert results[1]["recorded"] is True
    conn = sqlite3.connect(str(store))
    count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    assert count == 2
    # Schema still valid (integrity_check passes) after concurrent writes.
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_atomic_concurrent_write_lock_contention_degrades_non_blocking(tmp_path):
    """A genuinely lock-contended write (another connection holding an
    EXCLUSIVE lock) degrades to a non-blocking `locked` failure within the
    bounded `busy_timeout_seconds` -- it does NOT hang indefinitely."""
    store = tmp_path / "store.sqlite3"
    # Pre-create the file/schema via one ordinary write first.
    h.record_sample(
        store, execution_id=h.new_execution_id(), command_group_key="gk",
        environment_fingerprint="fp", status="success", command_hash="abc",
        duration_ms=1000, applied_timeout_ms=150000,
    )

    blocker_conn = sqlite3.connect(str(store), isolation_level=None)
    blocker_conn.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        result = h.record_sample(
            store,
            execution_id=h.new_execution_id(),
            command_group_key="gk",
            environment_fingerprint="fp",
            status="success",
            command_hash="abc",
            duration_ms=2000,
            applied_timeout_ms=150000,
            busy_timeout_seconds=0.25,  # bottom of the 0.25-1s contract range
        )
        elapsed = time.monotonic() - start
    finally:
        blocker_conn.execute("COMMIT")
        blocker_conn.close()

    assert result["recorded"] is False
    assert result["store_status"] == "locked"
    # Non-blocking: bounded by busy_timeout_seconds, with generous slack
    # for scheduler jitter -- must NOT hang indefinitely.
    assert elapsed < 5.0


# ---------------------------------------------------------------------------
# AC8: non-blocking degradation (`-k non_blocking_degradation`)
# ---------------------------------------------------------------------------


def test_non_blocking_degradation_missing_store(tmp_path):
    store = tmp_path / "does-not-exist.sqlite3"
    snapshot = h.build_history_snapshot(store, [("gk", "fp")], max_seconds=600)
    assert snapshot["store_status"] == "missing"
    assert snapshot["records"] == {}


def test_non_blocking_degradation_corrupt_store(tmp_path):
    store = tmp_path / "corrupt.sqlite3"
    store.write_bytes(b"not a sqlite database at all")
    snapshot = h.build_history_snapshot(store, [("gk", "fp")], max_seconds=600)
    assert snapshot["store_status"] == "corrupt"
    assert snapshot["records"] == {}


def test_non_blocking_degradation_unknown_schema_version(tmp_path):
    store = tmp_path / "future-schema.sqlite3"
    conn = sqlite3.connect(str(store))
    conn.executescript(h._SCHEMA_SQL)
    conn.execute("DELETE FROM schema_meta")
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES ('version', ?)",
        (str(h.HISTORY_STORE_SCHEMA_VERSION + 999),),
    )
    conn.commit()
    conn.close()
    snapshot = h.build_history_snapshot(store, [("gk", "fp")], max_seconds=600)
    assert snapshot["store_status"] == "unknown_schema"
    assert snapshot["records"] == {}


def test_non_blocking_degradation_locked_store_snapshot_build(tmp_path):
    """`build_history_snapshot()` itself (not just `record_sample()`)
    degrades non-blocking when the store is exclusively locked."""
    store = tmp_path / "store.sqlite3"
    h.record_sample(
        store, execution_id=h.new_execution_id(), command_group_key="gk",
        environment_fingerprint="fp", status="success", command_hash="abc",
        duration_ms=1000, applied_timeout_ms=150000,
    )
    blocker_conn = sqlite3.connect(str(store), isolation_level=None)
    blocker_conn.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        snapshot = h.build_history_snapshot(
            store, [("gk", "fp")], max_seconds=600, busy_timeout_seconds=0.25
        )
        elapsed = time.monotonic() - start
    finally:
        blocker_conn.execute("COMMIT")
        blocker_conn.close()
    assert snapshot["store_status"] == "locked"
    assert snapshot["records"] == {}
    assert elapsed < 5.0


def test_non_blocking_degradation_invalid_row_rejected_from_estimate():
    """A row with an out-of-range (negative) duration_ms is rejected by
    `_row_is_valid()` and never contributes to the percentile, even if
    SQLite itself (no CHECK constraint) would happily store it."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE samples (command_group_key TEXT, environment_fingerprint TEXT, "
        "observed_at_utc TEXT, status TEXT, duration_ms INTEGER, applied_timeout_ms INTEGER)"
    )
    now = _now()
    for i in range(5):
        conn.execute(
            "INSERT INTO samples VALUES ('gk', 'fp', ?, 'success', ?, 150000)",
            (now.isoformat(), 1000 + i),
        )
    # A corrupt/negative row directly inserted (bypassing record_sample()'s
    # own validation) must still be rejected at READ time.
    conn.execute(
        "INSERT INTO samples VALUES ('gk', 'fp', ?, 'success', -1, 150000)", (now.isoformat(),)
    )
    result = h.compute_history_estimate(conn, "gk", "fp", now_utc=now)
    assert result["eligible_sample_count"] == 5  # the negative row excluded


def test_non_blocking_degradation_record_sample_rejects_invalid_status():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        store = Path(d) / "store.sqlite3"
        result = h.record_sample(
            store, execution_id=h.new_execution_id(), command_group_key="gk",
            environment_fingerprint="fp", status="not_a_real_status",
            command_hash="abc", duration_ms=1000, applied_timeout_ms=150000,
        )
        assert result["recorded"] is False
        assert result["store_status"] == "invalid_row"


def test_non_blocking_degradation_never_raises_for_any_failure_mode(tmp_path):
    """Belt-and-braces: none of the above failure modes ever raises an
    exception out of build_history_snapshot() / record_sample()."""
    missing = tmp_path / "missing.sqlite3"
    corrupt = tmp_path / "corrupt2.sqlite3"
    corrupt.write_bytes(b"garbage")
    for store in (missing, corrupt):
        h.build_history_snapshot(store, [("gk", "fp")], max_seconds=600)
        h.record_sample(
            store, execution_id=h.new_execution_id(), command_group_key="gk",
            environment_fingerprint="fp", status="success", command_hash="abc",
            duration_ms=1000, applied_timeout_ms=150000,
        )
