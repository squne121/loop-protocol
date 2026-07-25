"""Behavioral tests for process lifecycle telemetry / overlap validator in
fan_out_orchestrator.py (Issue #1707).

Issue #1707 replaces the previous ``subtask_started``-journal-event-order-
only parallelism claim (review Blocker 2) with process-level telemetry:
``make_subprocess_runner()`` now records a ``process_start`` event
immediately after a successful ``subprocess.Popen()`` and a ``process_exit``
event right after the child is reaped, and a pure overlap predicate /
aggregation function / validator determine whether distinct AGY provider
processes actually overlapped in monotonic time.

Tests here exercise the *production* subprocess runner against real,
fixture-controlled child OS processes (never a mock ``runner``), plus pure
unit tests of the overlap predicate / aggregation / validator functions.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "fan_out_orchestrator.py"
    module_name = "fan_out_orchestrator_process_lifecycle_test"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def make_context_file(tmp_path: Path, name: str, content: str) -> str:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def make_subtask(
    tmp_path: Path,
    *,
    subtask_id: str | None = None,
    provider: str = "gemini",
    tool_profile: str = "no_tools",
    context_file_name: str | None = None,
) -> dict:
    # Dedupe fingerprinting excludes only subtask_id (Issue #1273 AC5), so
    # two subtasks that differ *only* in subtask_id would otherwise fold
    # together as exact duplicates and only one child would ever spawn --
    # defeating tests that need N genuinely distinct process spawns. Give
    # each subtask its own context file content by default so fingerprints
    # differ whenever subtask_id differs.
    if context_file_name is None:
        context_file_name = f"ctx-{subtask_id or 'default'}.md"
    ctx_path = make_context_file(tmp_path, context_file_name, f"content for {subtask_id or 'default'}")
    subtask: dict = {
        "schema": "delegation_request_v1",
        "provider": provider,
        "tool_profile": tool_profile,
        "objective": f"Investigate scripts/fan_out_orchestrator.py behavior for {context_file_name}",
        "instructions": ["Summarize findings", "List evidence"],
        "output_sections": ["Summary"],
        "context_files": [ctx_path],
    }
    if subtask_id is not None:
        subtask["subtask_id"] = subtask_id
    return subtask


def make_fake_provider_script(
    tmp_path: Path,
    name: str,
    *,
    sleep_sec: float = 0.0,
    exit_code: int = 0,
    ignore_sigterm: bool = False,
    pid_file: Path | None = None,
) -> Path:
    """A deterministic, sleep-then-exit fake executable standing in for a
    real ``gemini`` / ``agy`` provider CLI (Issue #1707 AC7): reads the same
    ``--request-file`` / ``--output-file`` / ``--audit-log`` contract that
    ``make_subprocess_runner()`` invokes ``run_gemini_headless.py`` with, so
    it is spawned through the real ``subprocess.Popen()`` path, not mocked.
    """
    script = tmp_path / name
    lines = [
        "import argparse",
        "import json",
        "import os",
        "import signal",
        "import sys",
        "import time",
        "from pathlib import Path",
        "",
    ]
    if ignore_sigterm:
        lines.append("signal.signal(signal.SIGTERM, signal.SIG_IGN)")
    lines += [
        "p = argparse.ArgumentParser()",
        "p.add_argument('--request-file', type=Path, required=True)",
        "p.add_argument('--output-file', type=Path, required=True)",
        "p.add_argument('--audit-log', type=Path, default=None)",
        "args = p.parse_args()",
    ]
    if pid_file is not None:
        lines.append(f"Path({str(pid_file)!r}).write_text(str(os.getpid()))")
    if sleep_sec:
        lines.append(f"time.sleep({sleep_sec})")
    lines += [
        "request = json.loads(args.request_file.read_text())",
        "result = {"
        "'schema': 'delegation_result/v1', 'ok': True, 'actual_model': 'fake', "
        "'provider': request.get('provider', 'gemini'), "
        "'tool_profile': request.get('tool_profile', 'unknown')}",
        "args.output_file.write_text(json.dumps(result))",
        f"sys.exit({exit_code})",
    ]
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return script


def read_journal_events(run_dir: Path) -> list[dict]:
    journal = run_dir / "events.ndjson"
    return [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]


def process_events(events: list[dict], kind: str | None = None) -> list[dict]:
    out = [e for e in events if e.get("schema") == "process_lifecycle_event_v1"]
    if kind is not None:
        out = [e for e in out if e.get("event") == kind]
    return out


# ---------------------------------------------------------------------------
# AC1: process-start event only recorded after a successful Popen()
# ---------------------------------------------------------------------------


def test_process_start_event_only_after_popen_success(tmp_path):
    module = load_module()

    # Success case: a real spawn must produce exactly one process_start event.
    ok_script = make_fake_provider_script(tmp_path, "ok.py")
    run_dir_ok = tmp_path / "run-ok"
    run_dir_ok.mkdir()
    ctx_ok = module.RunnerContext(
        run_dir=run_dir_ok,
        audit_log_path=None,
        cancel_event=threading.Event(),
        journal=lambda event: (run_dir_ok / "events.ndjson").open("a", encoding="utf-8").write(
            json.dumps(event) + "\n"
        ),
    )
    runner_ok = module.make_subprocess_runner(ok_script)
    job_ok = module.ChildJob(
        subtask_id="ok-1",
        artifact_stem="0000-ok",
        request={"provider": "agy", "tool_profile": "no_tools", "parent_run_id": "p1", "attempt_id": "attempt-1"},
    )
    result_ok = runner_ok(job_ok, ctx_ok)
    assert result_ok["ok"] is True

    events_ok = read_journal_events(run_dir_ok)
    starts_ok = process_events(events_ok, "process_start")
    assert len(starts_ok) == 1, "a successful Popen() must record exactly one process-start event"

    # Failure case: an unspawnable executable must never record a
    # process-start event (Issue #1707 AC1: spawn failure is not "started").
    run_dir_fail = tmp_path / "run-fail"
    run_dir_fail.mkdir()
    ctx_fail = module.RunnerContext(
        run_dir=run_dir_fail,
        audit_log_path=None,
        cancel_event=threading.Event(),
        journal=lambda event: (run_dir_fail / "events.ndjson").open("a", encoding="utf-8").write(
            json.dumps(event) + "\n"
        ),
    )
    runner_fail = module.make_subprocess_runner(
        ok_script, python_executable=str(tmp_path / "does-not-exist-binary-xyz")
    )
    job_fail = module.ChildJob(
        subtask_id="fail-1",
        artifact_stem="0000-fail",
        request={"provider": "agy", "tool_profile": "no_tools"},
    )
    result_fail = runner_fail(job_fail, ctx_fail)
    assert result_fail["ok"] is False
    assert result_fail["failure_class"] == "child_spawn_failed"

    assert not (run_dir_fail / "events.ndjson").exists(), (
        "spawn failure must never write a process-start event (no journal file expected)"
    )


# ---------------------------------------------------------------------------
# AC2: process-exit event recorded after reap
# ---------------------------------------------------------------------------


def test_process_exit_event_recorded_after_reap(tmp_path):
    module = load_module()
    script = make_fake_provider_script(tmp_path, "exit7.py", exit_code=7)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    events: list[dict] = []
    lock = threading.Lock()

    def journal(event):
        with lock:
            events.append(event)

    ctx = module.RunnerContext(run_dir=run_dir, audit_log_path=None, cancel_event=threading.Event(), journal=journal)
    runner = module.make_subprocess_runner(script)
    job = module.ChildJob(subtask_id="s1", artifact_stem="0000-s1", request={"provider": "agy"})
    runner(job, ctx)

    exits = [e for e in events if e.get("schema") == "process_lifecycle_event_v1" and e.get("event") == "process_exit"]
    assert len(exits) == 1
    assert exits[0]["returncode"] == 7
    assert exits[0]["termination_reason"] == "exited_nonzero"
    starts = [
        e for e in events if e.get("schema") == "process_lifecycle_event_v1" and e.get("event") == "process_start"
    ]
    assert len(starts) == 1
    assert exits[0]["exited_monotonic_ns"] >= starts[0]["started_monotonic_ns"]


# ---------------------------------------------------------------------------
# AC3: required fields on both event kinds
# ---------------------------------------------------------------------------


def test_process_lifecycle_event_required_fields(tmp_path):
    module = load_module()
    script = make_fake_provider_script(tmp_path, "fields.py")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    events: list[dict] = []
    ctx = module.RunnerContext(
        run_dir=run_dir, audit_log_path=None, cancel_event=threading.Event(), journal=events.append
    )
    runner = module.make_subprocess_runner(script)
    job = module.ChildJob(
        subtask_id="s-fields",
        artifact_stem="0000-fields",
        request={
            "provider": "agy",
            "tool_profile": "no_tools",
            "parent_run_id": "run-abc",
            "attempt_id": "attempt-1",
        },
    )
    runner(job, ctx)

    start = next(e for e in events if e["event"] == "process_start")
    exit_ = next(e for e in events if e["event"] == "process_exit")

    common_fields = [
        "schema",
        "process_role",
        "provider",
        "parent_run_id",
        "subtask_id",
        "attempt_id",
        "artifact_stem",
        "pid",
        "pgid",
        "executable",
    ]
    for field_name in common_fields:
        assert field_name in start, f"process_start missing {field_name}"
        assert field_name in exit_, f"process_exit missing {field_name}"

    assert start["schema"] == "process_lifecycle_event_v1"
    assert start["process_role"] == "delegation_wrapper"
    assert start["provider"] == "agy"
    assert start["parent_run_id"] == "run-abc"
    assert start["subtask_id"] == "s-fields"
    assert start["attempt_id"] == "attempt-1"
    assert start["artifact_stem"] == "0000-fields"
    assert isinstance(start["pid"], int)
    assert isinstance(start["started_monotonic_ns"], int)
    assert isinstance(start["started_utc"], str)
    assert "exited_monotonic_ns" not in start
    assert "returncode" not in start

    assert isinstance(exit_["exited_monotonic_ns"], int)
    assert isinstance(exit_["exited_utc"], str)
    assert isinstance(exit_["returncode"], int)
    assert isinstance(exit_["termination_reason"], str)


# ---------------------------------------------------------------------------
# AC4: overlap predicate (pure)
# ---------------------------------------------------------------------------


def test_overlap_predicate_monotonic_window():
    module = load_module()
    a = {"started_monotonic_ns": 100, "exited_monotonic_ns": 200}
    b = {"started_monotonic_ns": 150, "exited_monotonic_ns": 250}
    c = {"started_monotonic_ns": 200, "exited_monotonic_ns": 300}
    d = {"started_monotonic_ns": 300, "exited_monotonic_ns": 400}

    assert module.process_lifecycle_intervals_overlap(a, b) is True, "overlapping intervals must be detected"
    assert module.process_lifecycle_intervals_overlap(a, c) is False, "touching (non-strict) boundary is not overlap"
    assert module.process_lifecycle_intervals_overlap(a, d) is False, "disjoint intervals must not overlap"
    assert module.process_lifecycle_intervals_overlap(b, c) is True


# ---------------------------------------------------------------------------
# AC5: aggregation requires distinct pid AND distinct subtask_id
# ---------------------------------------------------------------------------


def test_actual_provider_process_overlap_requires_distinct_pid_and_subtask(module_events=None):
    module = load_module()

    def pair(pid, subtask_id, start, end):
        return {"pid": pid, "subtask_id": subtask_id, "started_monotonic_ns": start, "exited_monotonic_ns": end}

    # Distinct pid, distinct subtask, overlapping window -> True.
    overlapping_distinct = [pair(100, "t1", 0, 100), pair(200, "t2", 50, 150)]
    assert module.actual_provider_process_overlap(overlapping_distinct) is True

    # Same pid reused across two spawns (distinct subtask) -> must not count.
    same_pid = [pair(100, "t1", 0, 100), pair(100, "t2", 50, 150)]
    assert module.actual_provider_process_overlap(same_pid) is False

    # Same subtask_id, two events (e.g. future wrapper + provider_cli pair
    # for the same subtask) overlapping in time -> must not count on its own.
    same_subtask = [pair(100, "t1", 0, 100), pair(200, "t1", 50, 150)]
    assert module.actual_provider_process_overlap(same_subtask) is False

    # No overlap at all (sequential, distinct pid/subtask) -> False.
    sequential = [pair(100, "t1", 0, 100), pair(200, "t2", 100, 200)]
    assert module.actual_provider_process_overlap(sequential) is False

    # Empty / singleton input never overlaps.
    assert module.actual_provider_process_overlap([]) is False
    assert module.actual_provider_process_overlap([pair(100, "t1", 0, 100)]) is False


# ---------------------------------------------------------------------------
# AC6: validator FAILs when subtask_started ordering alone doesn't prove
# actual process overlap
# ---------------------------------------------------------------------------


def test_validator_fails_when_subtask_started_without_process_overlap():
    module = load_module()
    schema = module.PROCESS_LIFECYCLE_SCHEMA
    # Two subtask_started events (journal order suggests parallelism), but
    # the paired process lifecycle intervals do NOT actually overlap.
    events = [
        {"event": "subtask_started", "subtask_id": "t1", "artifact_stem": "0000-t1"},
        {"event": "subtask_started", "subtask_id": "t2", "artifact_stem": "0001-t2"},
        {
            "schema": schema,
            "event": "process_start",
            "artifact_stem": "0000-t1",
            "subtask_id": "t1",
            "pid": 111,
            "started_monotonic_ns": 0,
        },
        {
            "schema": schema,
            "event": "process_exit",
            "artifact_stem": "0000-t1",
            "subtask_id": "t1",
            "pid": 111,
            "exited_monotonic_ns": 100,
        },
        {
            "schema": schema,
            "event": "process_start",
            "artifact_stem": "0001-t2",
            "subtask_id": "t2",
            "pid": 222,
            "started_monotonic_ns": 100,
        },
        {
            "schema": schema,
            "event": "process_exit",
            "artifact_stem": "0001-t2",
            "subtask_id": "t2",
            "pid": 222,
            "exited_monotonic_ns": 200,
        },
    ]
    result = module.validate_fanout_parallelism(events)
    assert result["status"] == "fail"
    assert result["actual_provider_process_overlap"] is False
    assert result["subtask_started_count"] == 2

    # Sanity: a single subtask_started event is not_applicable, not fail.
    single = [{"event": "subtask_started", "subtask_id": "t1", "artifact_stem": "0000-t1"}]
    assert module.validate_fanout_parallelism(single)["status"] == "not_applicable"


# ---------------------------------------------------------------------------
# AC7: real subprocess integration with a fake executable
# ---------------------------------------------------------------------------


def test_real_subprocess_integration_with_fake_executable(tmp_path):
    module = load_module()
    pid_file = tmp_path / "child.pid"
    script = make_fake_provider_script(tmp_path, "real.py", sleep_sec=0.1, pid_file=pid_file)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    events: list[dict] = []
    ctx = module.RunnerContext(
        run_dir=run_dir, audit_log_path=None, cancel_event=threading.Event(), journal=events.append
    )
    runner = module.make_subprocess_runner(script)
    job = module.ChildJob(subtask_id="real-1", artifact_stem="0000-real", request={"provider": "agy"})
    result = runner(job, ctx)

    assert result["ok"] is True
    assert pid_file.exists(), "the fake executable must have actually run as a real OS process"
    child_pid = int(pid_file.read_text().strip())

    start = next(e for e in events if e["event"] == "process_start")
    assert start["pid"] == child_pid, "the recorded pid must match the real spawned child's own pid"


# ---------------------------------------------------------------------------
# AC8: eight independent scenarios
# ---------------------------------------------------------------------------


def _run_fanout_with_fake_provider(tmp_path, subtasks, *, script, max_workers=4, overall_timeout_sec=None):
    module = load_module()
    runner = module.make_subprocess_runner(script)
    request = {"schema": "delegation_fanout_request_v1", "subtasks": subtasks, "max_workers": max_workers}
    if overall_timeout_sec is not None:
        request["overall_timeout_sec"] = overall_timeout_sec
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)
    return module, result


def test_overlap_success_two_distinct_processes(tmp_path):
    script = make_fake_provider_script(tmp_path, "slow.py", sleep_sec=0.3)
    subtasks = [make_subtask(tmp_path, subtask_id="a"), make_subtask(tmp_path, subtask_id="b")]
    module, result = _run_fanout_with_fake_provider(tmp_path, subtasks, script=script, max_workers=2)

    assert result["counts"]["succeeded"] == 2, result
    events = read_journal_events(Path(result["run_dir"]))
    pairs = module.build_process_lifecycle_pairs(events)
    assert len(pairs) == 2
    assert module.actual_provider_process_overlap(pairs) is True
    verdict = module.validate_fanout_parallelism(events)
    assert verdict["status"] == "pass"


def test_sequential_no_overlap(tmp_path):
    script = make_fake_provider_script(tmp_path, "fast.py", sleep_sec=0.05)
    subtasks = [make_subtask(tmp_path, subtask_id="a"), make_subtask(tmp_path, subtask_id="b")]
    module, result = _run_fanout_with_fake_provider(tmp_path, subtasks, script=script, max_workers=1)

    assert result["counts"]["succeeded"] == 2, result
    events = read_journal_events(Path(result["run_dir"]))
    pairs = module.build_process_lifecycle_pairs(events)
    assert len(pairs) == 2
    assert module.actual_provider_process_overlap(pairs) is False


def test_one_spawn_failure_no_provider_overlap(tmp_path):
    module = load_module()
    ok_script = make_fake_provider_script(tmp_path, "ok.py", sleep_sec=0.1)
    bad_executable = str(tmp_path / "no-such-binary")

    ok_runner = module.make_subprocess_runner(ok_script)
    bad_runner = module.make_subprocess_runner(ok_script, python_executable=bad_executable)

    def dispatch_runner(job, ctx):
        if job.subtask_id == "bad":
            return bad_runner(job, ctx)
        return ok_runner(job, ctx)

    subtasks = [make_subtask(tmp_path, subtask_id="good"), make_subtask(tmp_path, subtask_id="bad")]
    request = {"schema": "delegation_fanout_request_v1", "subtasks": subtasks, "max_workers": 2}
    result = module.run_fanout(request, base_dir=tmp_path, runner=dispatch_runner)

    assert result["counts"]["succeeded"] == 1
    assert result["counts"]["failed"] == 1
    events = read_journal_events(Path(result["run_dir"]))
    starts = process_events(events, "process_start")
    assert len(starts) == 1, "only the successfully spawned subtask records a process-start event"
    pairs = module.build_process_lifecycle_pairs(events)
    assert module.actual_provider_process_overlap(pairs) is False


def test_all_spawn_failure_no_provider_overlap(tmp_path):
    module = load_module()
    ok_script = make_fake_provider_script(tmp_path, "ok.py")
    bad_executable = str(tmp_path / "no-such-binary")
    bad_runner = module.make_subprocess_runner(ok_script, python_executable=bad_executable)

    subtasks = [make_subtask(tmp_path, subtask_id="a"), make_subtask(tmp_path, subtask_id="b")]
    request = {"schema": "delegation_fanout_request_v1", "subtasks": subtasks, "max_workers": 2}
    result = module.run_fanout(request, base_dir=tmp_path, runner=bad_runner)

    assert result["counts"]["succeeded"] == 0
    assert result["counts"]["failed"] == 2
    events = read_journal_events(Path(result["run_dir"]))
    starts = process_events(events, "process_start")
    assert len(starts) == 0, "no spawn ever succeeded, so no process-start events must exist"
    pairs = module.build_process_lifecycle_pairs(events)
    assert pairs == []
    assert module.actual_provider_process_overlap(pairs) is False


def test_timeout_termination_recorded(tmp_path):
    script = make_fake_provider_script(tmp_path, "hang.py", sleep_sec=60)
    subtasks = [make_subtask(tmp_path, subtask_id="hang1")]
    module, result = _run_fanout_with_fake_provider(
        tmp_path, subtasks, script=script, max_workers=1, overall_timeout_sec=0.3
    )

    assert result["counts"]["cancelled"] >= 1 or result["counts"]["failed"] >= 1
    events = read_journal_events(Path(result["run_dir"]))
    exits = process_events(events, "process_exit")
    assert len(exits) == 1
    assert exits[0]["termination_reason"] == "sigterm"


def test_sigkill_escalation_recorded(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "_CHILD_TERMINATE_GRACE_SEC", 0.3)

    script = make_fake_provider_script(tmp_path, "stubborn.py", sleep_sec=60, ignore_sigterm=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    events: list[dict] = []
    lock = threading.Lock()

    def journal(event):
        with lock:
            events.append(event)

    cancel_event = threading.Event()
    ctx = module.RunnerContext(run_dir=run_dir, audit_log_path=None, cancel_event=cancel_event, journal=journal)
    runner = module.make_subprocess_runner(script)
    job = module.ChildJob(subtask_id="stubborn", artifact_stem="0000-stubborn", request={"provider": "agy"})

    result_holder: dict = {}

    def _invoke():
        result_holder["result"] = runner(job, ctx)

    thread = threading.Thread(target=_invoke)
    thread.start()
    time.sleep(0.3)
    cancel_event.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert result_holder["result"]["ok"] is False
    exits = [e for e in events if e.get("event") == "process_exit"]
    assert len(exits) == 1
    assert exits[0]["termination_reason"] == "sigkill"


def test_malformed_or_missing_exit_event_handled():
    module = load_module()
    schema = module.PROCESS_LIFECYCLE_SCHEMA
    events = [
        # A start event with no matching exit (process crashed / journal
        # truncated) -- must be dropped, not raise.
        {
            "schema": schema,
            "event": "process_start",
            "artifact_stem": "0000-orphan",
            "subtask_id": "orphan",
            "pid": 999,
            "started_monotonic_ns": 0,
        },
        # An exit event with no matching start (out-of-order / malformed
        # journal) -- must be dropped, not raise.
        {
            "schema": schema,
            "event": "process_exit",
            "artifact_stem": "0001-ghost",
            "subtask_id": "ghost",
            "pid": 998,
            "exited_monotonic_ns": 100,
        },
        # An exit event missing the required exited_monotonic_ns field.
        {
            "schema": schema,
            "event": "process_start",
            "artifact_stem": "0002-partial",
            "subtask_id": "partial",
            "pid": 997,
            "started_monotonic_ns": 0,
        },
        {
            "schema": schema,
            "event": "process_exit",
            "artifact_stem": "0002-partial",
            "subtask_id": "partial",
            "pid": 997,
            # exited_monotonic_ns intentionally omitted.
        },
        # A well-formed complete pair.
        {
            "schema": schema,
            "event": "process_start",
            "artifact_stem": "0003-ok",
            "subtask_id": "ok",
            "pid": 996,
            "started_monotonic_ns": 0,
        },
        {
            "schema": schema,
            "event": "process_exit",
            "artifact_stem": "0003-ok",
            "subtask_id": "ok",
            "pid": 996,
            "exited_monotonic_ns": 50,
        },
    ]
    pairs = module.build_process_lifecycle_pairs(events)
    assert len(pairs) == 1
    assert pairs[0]["artifact_stem"] == "0003-ok"

    verdict = module.validate_fanout_parallelism(events)
    assert verdict["process_lifecycle_pair_count"] == 1
    assert verdict["actual_provider_process_overlap"] is False


def test_wrapper_overlap_without_provider_overlap():
    """Journal-append ordering of subtask_started events alone suggests
    overlap (subtask 2's subtask_started appears before subtask 1's
    subtask_finished in the log), but the real process lifecycle intervals
    (monotonic timestamps) show the two child processes never actually ran
    concurrently. The validator must trust the lifecycle intervals, not the
    journal line ordering (Issue #1707 review Blocker 2 replacement)."""
    module = load_module()
    schema = module.PROCESS_LIFECYCLE_SCHEMA
    events = [
        {"event": "subtask_started", "subtask_id": "t1", "artifact_stem": "0000-t1"},
        # t2's subtask_started is journaled *before* t1's subtask_finished,
        # which a naive order-based check would misread as parallelism.
        {"event": "subtask_started", "subtask_id": "t2", "artifact_stem": "0001-t2"},
        {"event": "subtask_finished", "subtask_id": "t1", "artifact_stem": "0000-t1"},
        {"event": "subtask_finished", "subtask_id": "t2", "artifact_stem": "0001-t2"},
        {
            "schema": schema,
            "event": "process_start",
            "artifact_stem": "0000-t1",
            "subtask_id": "t1",
            "pid": 111,
            "started_monotonic_ns": 0,
        },
        {
            "schema": schema,
            "event": "process_exit",
            "artifact_stem": "0000-t1",
            "subtask_id": "t1",
            "pid": 111,
            "exited_monotonic_ns": 1_000_000,
        },
        {
            "schema": schema,
            "event": "process_start",
            "artifact_stem": "0001-t2",
            "subtask_id": "t2",
            "pid": 222,
            "started_monotonic_ns": 1_000_000,
        },
        {
            "schema": schema,
            "event": "process_exit",
            "artifact_stem": "0001-t2",
            "subtask_id": "t2",
            "pid": 222,
            "exited_monotonic_ns": 2_000_000,
        },
    ]
    verdict = module.validate_fanout_parallelism(events)
    assert verdict["status"] == "fail"
    assert verdict["actual_provider_process_overlap"] is False
    assert verdict["subtask_started_count"] == 2


# ---------------------------------------------------------------------------
# AC9: concurrent journal writes do not corrupt
# ---------------------------------------------------------------------------


def test_concurrent_journal_writes_do_not_corrupt(tmp_path):
    script = make_fake_provider_script(tmp_path, "concurrent.py", sleep_sec=0.05)
    subtasks = [make_subtask(tmp_path, subtask_id=f"c{i}") for i in range(8)]
    module, result = _run_fanout_with_fake_provider(tmp_path, subtasks, script=script, max_workers=8)

    assert result["counts"]["succeeded"] == 8, result
    events = read_journal_events(Path(result["run_dir"]))
    for event in events:
        assert isinstance(event, dict)

    starts = process_events(events, "process_start")
    exits = process_events(events, "process_exit")
    assert len(starts) == 8
    assert len(exits) == 8
    assert len({e["pid"] for e in starts}) == 8, "every concurrently-spawned child must have a distinct pid"

    pairs = module.build_process_lifecycle_pairs(events)
    assert len(pairs) == 8
    assert module.actual_provider_process_overlap(pairs) is True


# ---------------------------------------------------------------------------
# AC10: executable field redacted to basename (no absolute path leak)
# ---------------------------------------------------------------------------


def test_executable_field_is_redacted_to_basename(tmp_path):
    module = load_module()
    deep_dir = tmp_path / "very" / "deep" / "nested" / "path"
    deep_dir.mkdir(parents=True)
    script = make_fake_provider_script(deep_dir, "redact_me.py")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    events: list[dict] = []
    ctx = module.RunnerContext(
        run_dir=run_dir, audit_log_path=None, cancel_event=threading.Event(), journal=events.append
    )
    runner = module.make_subprocess_runner(script)
    job = module.ChildJob(subtask_id="redact", artifact_stem="0000-redact", request={"provider": "agy"})
    runner(job, ctx)

    start = next(e for e in events if e["event"] == "process_start")
    exit_ = next(e for e in events if e["event"] == "process_exit")
    assert start["executable"] == "redact_me.py"
    assert exit_["executable"] == "redact_me.py"
    assert str(tmp_path) not in start["executable"]
    assert "/" not in start["executable"]
    assert "\\" not in start["executable"]
