"""Concurrency regression tests for run_gemini_headless.run_delegation() (Issue #1273 AC2).

Issue #1273 AC1 replaced the module-global ``_AUDIT_REENTRANCY_DEPTH`` int
counter with a ``contextvars.ContextVar[int]`` because a plain module-global
int is shared *mutable* state across every thread in the process: concurrent
fan-out worker threads calling ``run_delegation()`` at the same time would
race on incrementing/decrementing the same counter, corrupting the
``is_top_level_call`` determination and causing a thread to skip emitting its
own ``delegation_audit_v1`` start/end pair (or emit a spurious extra pair).

This module proves the fix behaviorally: it starts N worker threads that all
call ``run_delegation()`` and are forced to overlap in time via a
``threading.Barrier`` (so the test cannot pass "by luck" due to threads
happening to run sequentially), then asserts that every thread's audit run_id
produced *exactly* one start record and one end record -- no run_id is
missing a pair, and no run_id has more than one pair.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path


def load_module():
    """Load run_gemini_headless.py under a unique module name.

    A unique name (rather than reusing "run_gemini_headless" as some sibling
    test files do) plus registering the module in ``sys.modules`` before
    ``exec_module`` avoids cross-test / cross-worker module identity
    collisions when the test suite runs under pytest-xdist.
    """
    path = Path(__file__).resolve().parent.parent / "scripts" / "run_gemini_headless.py"
    module_name = "run_gemini_headless_concurrency_test"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_concurrent_audit_pairs_are_not_dropped_or_duplicated(tmp_path, monkeypatch):
    """GIVEN N threads calling run_delegation() concurrently
    WHEN each thread's execution is forced to overlap via a barrier
    THEN each thread's run_id has exactly one start and one end audit record.
    """
    module = load_module()
    audit_log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("DELEGATION_AUDIT_LOG_PATH", str(audit_log))

    worker_count = 8
    barrier = threading.Barrier(worker_count)

    def fake_core(request, request_path=None, _routing=None):
        # Force every thread to be mid-flight inside run_delegation() at the
        # same wall-clock instant before any of them can return -- this is
        # what makes the test a genuine concurrency proof rather than a
        # sequential-by-luck pass.
        barrier.wait(timeout=10)
        return {
            "ok": True,
            "actual_model": "gemini-test-model",
            "tool_profile": str(request.get("tool_profile", "unknown")),
        }

    monkeypatch.setattr(module, "_run_delegation_core", fake_core)

    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        try:
            request = {
                "schema": "delegation_request_v1",
                "provider": "gemini",
                "tool_profile": "no_tools",
                "objective": f"concurrent-worker-{idx}",
            }
            result = module.run_delegation(request)
            assert result["ok"] is True
        except BaseException as exc:  # pylint: disable=broad-except
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(worker_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, f"worker thread(s) raised: {errors}"
    assert all(not t.is_alive() for t in threads), "worker thread(s) did not complete in time"

    assert audit_log.exists(), "audit log was never written"
    lines = [line for line in audit_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    records = [json.loads(line) for line in lines]

    by_run_id: dict[str, list[dict]] = {}
    for record in records:
        by_run_id.setdefault(record["run_id"], []).append(record)

    assert len(by_run_id) == worker_count, (
        f"expected {worker_count} distinct run_ids (one per thread's top-level "
        f"call), got {len(by_run_id)}: {sorted(by_run_id)}"
    )

    for run_id, run_records in by_run_id.items():
        record_types = sorted(r["record_type"] for r in run_records)
        assert record_types == ["end", "start"], (
            f"run_id={run_id!r} must have exactly one start and one end "
            f"record (context-local reentrancy depth must not leak across "
            f"threads), got record_types={record_types}"
        )

    assert len(records) == 2 * worker_count


def test_reentrant_provider_auto_still_emits_single_pair_per_thread(tmp_path, monkeypatch):
    """GIVEN nested (re-entrant) run_delegation() calls within a single thread
    WHEN multiple such threads run concurrently
    THEN each thread's outer call still emits exactly one audit pair and the
    inner re-entrant call emits none -- proving the ContextVar depth counter
    is correctly thread-local while still tracking same-thread re-entrancy.
    """
    module = load_module()
    audit_log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("DELEGATION_AUDIT_LOG_PATH", str(audit_log))

    worker_count = 4
    barrier = threading.Barrier(worker_count)

    def fake_core(request, request_path=None, _routing=None):
        if request.get("_nested_probe"):
            # Simulate a nested re-entrant call (e.g. provider="auto"
            # fallback) by calling run_delegation() again from inside the
            # "core" of the outer call, before the barrier releases.
            inner_request = dict(request)
            inner_request["_nested_probe"] = False
            module.run_delegation(inner_request)
        barrier.wait(timeout=10)
        return {
            "ok": True,
            "actual_model": "gemini-test-model",
            "tool_profile": str(request.get("tool_profile", "unknown")),
        }

    monkeypatch.setattr(module, "_run_delegation_core", fake_core)

    def worker(idx: int) -> None:
        request = {
            "schema": "delegation_request_v1",
            "provider": "gemini",
            "tool_profile": "no_tools",
            "objective": f"nested-worker-{idx}",
            "_nested_probe": True,
        }
        result = module.run_delegation(request)
        assert result["ok"] is True

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(worker_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert all(not t.is_alive() for t in threads)

    lines = [line for line in audit_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    records = [json.loads(line) for line in lines]
    by_run_id: dict[str, list[dict]] = {}
    for record in records:
        by_run_id.setdefault(record["run_id"], []).append(record)

    # Exactly one audit pair per thread (the inner nested call must not emit
    # its own pair, and no thread's outer pair must be lost/duplicated).
    assert len(by_run_id) == worker_count
    for run_id, run_records in by_run_id.items():
        record_types = sorted(r["record_type"] for r in run_records)
        assert record_types == ["end", "start"], (run_id, record_types)
