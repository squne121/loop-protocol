#!/usr/bin/env python3
"""One-off WSL2 latency measurement: synchronous=NORMAL vs synchronous=FULL
(AC10b). This is deliberately NOT a pytest CI-gated test -- it is a manual
measurement tool whose output is transcribed into
docs/dev/task-context.md as documented evidence. Running it does not
affect CI pass/fail.

Measures two things per synchronous mode, matching AC10(b):
    (a) normal single-row commit latency (many small commits in a row)
    (b) tail latency across a WAL checkpoint boundary (commit immediately
        after forcing `PRAGMA wal_checkpoint(TRUNCATE)`)

Usage:
    uv run python3 scripts/task-context/measure_synchronous_latency.py
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import task_context_db as db  # noqa: E402

N_COMMITS = 200
N_CHECKPOINT_TRIALS = 30


def measure(synchronous: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        db_file = os.path.join(tmp, "measure.sqlite3")
        conn = db.connect(db_file, synchronous=synchronous)
        conn.execute(
            "CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT)"
        )

        # (a) normal commit latency
        normal_latencies_ms = []
        for i in range(N_COMMITS):
            started = time.perf_counter()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO probe (value) VALUES (?)", (f"row-{i}",))
            conn.execute("COMMIT")
            normal_latencies_ms.append((time.perf_counter() - started) * 1000.0)

        # (b) tail latency across a WAL checkpoint boundary
        checkpoint_latencies_ms = []
        for i in range(N_CHECKPOINT_TRIALS):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            started = time.perf_counter()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO probe (value) VALUES (?)", (f"ckpt-row-{i}",))
            conn.execute("COMMIT")
            checkpoint_latencies_ms.append((time.perf_counter() - started) * 1000.0)

        conn.close()

    def summarize(samples: list[float]) -> dict:
        sorted_samples = sorted(samples)
        return {
            "n": len(samples),
            "mean_ms": round(statistics.mean(samples), 3),
            "median_ms": round(statistics.median(samples), 3),
            "p95_ms": round(sorted_samples[int(len(sorted_samples) * 0.95) - 1], 3),
            "max_ms": round(max(samples), 3),
        }

    return {
        "synchronous": synchronous,
        "normal_commit": summarize(normal_latencies_ms),
        "checkpoint_boundary_commit": summarize(checkpoint_latencies_ms),
    }


def main() -> int:
    results = [measure("NORMAL"), measure("FULL")]
    for result in results:
        print(f"-- synchronous={result['synchronous']} --")
        print(f"  normal commit:            {result['normal_commit']}")
        print(f"  checkpoint-boundary commit: {result['checkpoint_boundary_commit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
