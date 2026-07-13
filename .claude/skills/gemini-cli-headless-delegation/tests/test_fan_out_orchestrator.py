"""Behavioral tests for fan_out_orchestrator.py (Issue #1273 AC5-AC12, AC16,
and iteration 3 fix_delta: subtask_id/artifact_stem separation, validate-
before-dedupe, provider=auto ban, KeyboardInterrupt handling, manifest/
journal integrity).

These tests exercise ``run_fanout()`` end-to-end with dependency-injected
fake runners (never spawning a real ``gemini``/``agy`` CLI subprocess), plus
several lower-level tests that exercise the *production* subprocess runner
against real (harmless, fixture) child processes to prove process-group
termination, KeyboardInterrupt handling, and multi-process audit-log
concurrency actually work.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "fan_out_orchestrator.py"
    module_name = "fan_out_orchestrator_behavioral_test"
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
    objective: str | None = None,
    provider: str = "gemini",
    tool_profile: str = "no_tools",
    context_file_name: str = "ctx.md",
    context_content: str = "default content",
    gh_commands=None,
    post_to_issue_url: str | None = None,
    model: str | None = None,
    role: str | None = None,
    timeout_sec: int | None = None,
) -> dict:
    ctx_path = make_context_file(tmp_path, context_file_name, context_content)
    subtask: dict = {
        "schema": "delegation_request_v1",
        "provider": provider,
        "tool_profile": tool_profile,
        "objective": objective or f"Investigate scripts/fan_out_orchestrator.py behavior for {context_file_name}",
        "instructions": ["Summarize findings", "List evidence"],
        "output_sections": ["Summary"],
        "context_files": [ctx_path],
    }
    if subtask_id is not None:
        subtask["subtask_id"] = subtask_id
    if gh_commands is not None:
        subtask["gh_commands"] = gh_commands
    if post_to_issue_url is not None:
        subtask["post_to_issue_url"] = post_to_issue_url
    if model is not None:
        subtask["model"] = model
    if role is not None:
        subtask["role"] = role
    if timeout_sec is not None:
        subtask["timeout_sec"] = timeout_sec
    return subtask


def ok_runner_factory(calls: list):
    """A fake runner recording every invocation and always succeeding."""

    def _runner(job, ctx):
        calls.append((job.subtask_id, job.artifact_stem, dict(job.request)))
        return {
            "schema": "delegation_result/v1",
            "ok": True,
            "provider": job.request.get("provider"),
            "tool_profile": job.request.get("tool_profile"),
            "actual_model": "test-model",
        }

    return _runner


# ---------------------------------------------------------------------------
# AC5: dedupe_before_invocation
# ---------------------------------------------------------------------------


def test_dedupe_before_invocation(tmp_path):
    module = load_module()
    ctx_path = make_context_file(tmp_path, "shared.md", "identical content")
    subtask_a = {
        "schema": "delegation_request_v1",
        "provider": "gemini",
        "tool_profile": "no_tools",
        "objective": "Investigate scripts/fan_out_orchestrator.py dedupe behavior",
        "instructions": ["Summarize findings", "List evidence"],
        "output_sections": ["Summary"],
        "context_files": [ctx_path],
        "subtask_id": "alpha",
    }
    subtask_b = dict(subtask_a)
    subtask_b["subtask_id"] = "beta"  # differs only in the caller-supplied id

    calls: list = []
    runner = ok_runner_factory(calls)

    request = {
        "schema": "delegation_fanout_request_v1",
        "subtasks": [subtask_a, subtask_b],
    }
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)

    assert result["ok"] is True
    assert result["status"] == "success"
    assert result["counts"]["requested"] == 2
    assert result["counts"]["unique"] == 1
    assert result["counts"]["succeeded"] == 1
    assert len(calls) == 1, f"provider must be invoked exactly once for exact duplicates, got {len(calls)}"
    assert result["deduplicated_aliases"] == {"alpha": ["beta"]}


def test_dedupe_does_not_collapse_different_objectives(tmp_path):
    module = load_module()
    subtask_a = make_subtask(tmp_path, subtask_id="a", objective="Investigate scripts/foo.py behavior A")
    subtask_b = make_subtask(tmp_path, subtask_id="b", objective="Investigate scripts/foo.py behavior B")
    calls: list = []
    runner = ok_runner_factory(calls)
    request = {"schema": "delegation_fanout_request_v1", "subtasks": [subtask_a, subtask_b]}
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)
    assert result["counts"]["unique"] == 2
    assert len(calls) == 2
    assert result["deduplicated_aliases"] == {}


# ---------------------------------------------------------------------------
# AC6: max_workers_not_exceeded
# ---------------------------------------------------------------------------


def test_max_workers_not_exceeded(tmp_path):
    module = load_module()
    subtasks = [
        make_subtask(tmp_path, subtask_id=f"w{i}", objective=f"Investigate scripts/w{i}.py distinct task {i}")
        for i in range(6)
    ]
    lock = threading.Lock()
    state = {"current": 0, "max_seen": 0}

    def runner(job, ctx):
        with lock:
            state["current"] += 1
            state["max_seen"] = max(state["max_seen"], state["current"])
        time.sleep(0.15)
        with lock:
            state["current"] -= 1
        return {"ok": True, "actual_model": "m", "tool_profile": job.request.get("tool_profile")}

    request = {
        "schema": "delegation_fanout_request_v1",
        "subtasks": subtasks,
        "max_workers": 2,
    }
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)

    assert state["max_seen"] <= 2, f"max_workers=2 must never be exceeded, observed {state['max_seen']}"
    assert state["max_seen"] >= 2, "6 subtasks with 150ms runtime and max_workers=2 should overlap"
    assert result["counts"]["succeeded"] == 6


def test_profile_concurrency_limit_is_respected(tmp_path):
    module = load_module()
    subtasks = [
        make_subtask(tmp_path, subtask_id=f"p{i}", objective=f"Investigate scripts/p{i}.py distinct task {i}")
        for i in range(4)
    ]
    lock = threading.Lock()
    state = {"current": 0, "max_seen": 0}

    def runner(job, ctx):
        with lock:
            state["current"] += 1
            state["max_seen"] = max(state["max_seen"], state["current"])
        time.sleep(0.15)
        with lock:
            state["current"] -= 1
        return {"ok": True, "actual_model": "m", "tool_profile": job.request.get("tool_profile")}

    request = {
        "schema": "delegation_fanout_request_v1",
        "subtasks": subtasks,
        "max_workers": 4,
        "profile_concurrency": {"no_tools": 1},
    }
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)
    assert state["max_seen"] <= 1, "profile_concurrency=1 must serialize same-profile subtasks"
    assert result["counts"]["succeeded"] == 4


# ---------------------------------------------------------------------------
# AC7: provider_profile_compatibility_preflight
# ---------------------------------------------------------------------------


def test_provider_profile_compatibility_preflight(tmp_path):
    module = load_module()
    # agy does not support github_research (AGY_SUPPORTED_PROFILES excludes it).
    incompatible = make_subtask(
        tmp_path, subtask_id="incompatible", provider="agy", tool_profile="github_research"
    )
    compatible = make_subtask(tmp_path, subtask_id="compatible", provider="gemini", tool_profile="no_tools")

    calls: list = []
    runner = ok_runner_factory(calls)
    request = {"schema": "delegation_fanout_request_v1", "subtasks": [incompatible, compatible]}
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)

    assert result["status"] == "partial_success"
    called_ids = [c[0] for c in calls]
    assert "incompatible" not in called_ids, "provider must never be invoked for an incompatible combination"
    assert "compatible" in called_ids

    incompatible_outcome = next(r for r in result["results"] if r["subtask_id"] == "incompatible")
    assert incompatible_outcome["fanout_status"] == "failed"
    assert any("provider_profile_incompatible" in reason for reason in incompatible_outcome["reasons"])


def test_provider_auto_rejected_at_preflight(tmp_path):
    """Issue #1273 iteration 3 Blocker 3: provider="auto" is forbidden for
    fan-out children (its internal gemini-then-agy fallback attempts are not
    accounted for by max_total_attempts / per-provider semaphores)."""
    module = load_module()
    auto_subtask = make_subtask(tmp_path, subtask_id="auto-1", provider="auto", tool_profile="no_tools")
    safe_subtask = make_subtask(tmp_path, subtask_id="safe-1")

    calls: list = []
    runner = ok_runner_factory(calls)
    request = {"schema": "delegation_fanout_request_v1", "subtasks": [auto_subtask, safe_subtask]}
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)

    called_ids = [c[0] for c in calls]
    assert "auto-1" not in called_ids
    assert "safe-1" in called_ids

    auto_outcome = next(r for r in result["results"] if r["subtask_id"] == "auto-1")
    assert auto_outcome["fanout_status"] == "failed"
    assert any("provider=auto is forbidden" in reason for reason in auto_outcome["reasons"])


# ---------------------------------------------------------------------------
# AC8: overall_timeout_cancels_pending_and_terminates_running
# ---------------------------------------------------------------------------


def test_overall_timeout_cancels_pending_and_terminates_running(tmp_path):
    module = load_module()
    subtasks = [
        make_subtask(tmp_path, subtask_id="slow", objective="Investigate scripts/slow.py hang scenario 1"),
        make_subtask(tmp_path, subtask_id="pending-1", objective="Investigate scripts/pending1.py scenario 2"),
        make_subtask(tmp_path, subtask_id="pending-2", objective="Investigate scripts/pending2.py scenario 3"),
    ]
    invoked: list = []
    invoked_lock = threading.Lock()

    def runner(job, ctx):
        with invoked_lock:
            invoked.append(job.subtask_id)
        if job.subtask_id == "slow":
            # Cooperatively "hang" until the orchestrator signals cancellation
            # (the production subprocess runner polls this same event and
            # terminates the real child process group -- see
            # test_subprocess_runner_terminates_process_group_on_timeout).
            deadline = time.monotonic() + 5
            while not ctx.cancel_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.02)
            return {"ok": False, "failure_class": "overall_timeout_terminated", "actual_model": "m"}
        return {"ok": True, "actual_model": "m"}

    request = {
        "schema": "delegation_fanout_request_v1",
        "subtasks": subtasks,
        "max_workers": 1,
        "overall_timeout_sec": 0.3,
    }
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)

    assert result["status"] in ("cancelled", "failed")
    assert result["counts"]["cancelled"] >= 1

    with invoked_lock:
        invoked_snapshot = list(invoked)
    assert "pending-1" not in invoked_snapshot, "pending subtasks must never start the runner after timeout"
    assert "pending-2" not in invoked_snapshot, "pending subtasks must never start the runner after timeout"

    outcomes_by_id = {r["subtask_id"]: r for r in result["results"]}
    assert outcomes_by_id["pending-1"]["fanout_status"] == "cancelled"
    assert outcomes_by_id["pending-2"]["fanout_status"] == "cancelled"


def test_subprocess_runner_terminates_process_group_on_timeout(tmp_path):
    """Lower-level realism check: the production subprocess runner really
    terminates a hung child's whole process group (SIGTERM, then SIGKILL
    after the grace period) rather than merely giving up on it in-process.
    """
    module = load_module()
    fixture_script = tmp_path / "hang_forever.py"
    fixture_script.write_text(
        "import time\n"
        "import signal\n"
        "signal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    cancel_event = threading.Event()
    ctx = module.RunnerContext(run_dir=run_dir, audit_log_path=None, cancel_event=cancel_event)
    runner = module.make_subprocess_runner(fixture_script)
    job = module.ChildJob(
        subtask_id="hang", artifact_stem="0000-hang", request={"provider": "gemini", "tool_profile": "no_tools"}
    )

    result_holder: dict = {}

    def _invoke():
        result_holder["result"] = runner(job, ctx)

    thread = threading.Thread(target=_invoke)
    thread.start()
    time.sleep(0.3)  # let the child process actually start
    cancel_event.set()
    thread.join(timeout=10)

    assert not thread.is_alive(), "runner must return once cancel_event is set (process group terminated)"
    assert result_holder["result"]["ok"] is False
    assert "timeout" in result_holder["result"]["failure_class"]


def test_keyboard_interrupt_terminates_running_child_process_group(tmp_path):
    """Issue #1273 iteration 3 Blocker 4: a real SIGINT delivered to the
    orchestrator process (not just an in-process fake) must terminate the
    running child's process group -- not hang waiting for
    ThreadPoolExecutor's implicit shutdown(wait=True) to finish first.

    Spawns a "harness" process (its own process group) that calls
    run_fanout() with the production subprocess runner pointed at a
    fixture child that ignores SIGTERM and only dies to SIGKILL. The test
    sends SIGINT to the harness's process group (simulating a real Ctrl+C)
    and verifies both that the harness exits promptly and that the
    grandchild (hang fixture) process is actually dead afterward.
    """
    orchestrator_path = Path(__file__).resolve().parent.parent / "scripts" / "fan_out_orchestrator.py"

    hang_script = tmp_path / "hang_forever_writes_pid.py"
    pid_file = tmp_path / "hang.pid"
    hang_script.write_text(
        "import sys\n"
        "import time\n"
        "import signal\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(pid_file)!r}).write_text(str(__import__('os').getpid()))\n"
        "import json\n"
        "req = json.loads(Path(sys.argv[sys.argv.index('--request-file') + 1]).read_text())\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    ctx_file = make_context_file(tmp_path, "harness-ctx.md", "harness context")
    harness_script = tmp_path / "harness.py"
    harness_script.write_text(
        "import importlib.util\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"spec = importlib.util.spec_from_file_location('harness_orch', {str(orchestrator_path)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['harness_orch'] = module\n"
        "spec.loader.exec_module(module)\n"
        "request = {\n"
        "    'schema': 'delegation_fanout_request_v1',\n"
        "    'subtasks': [{\n"
        "        'schema': 'delegation_request_v1',\n"
        "        'provider': 'gemini',\n"
        "        'tool_profile': 'no_tools',\n"
        "        'objective': 'Investigate scripts/harness.py hang test scenario',\n"
        "        'instructions': ['a', 'b'],\n"
        "        'output_sections': ['Summary'],\n"
        f"        'context_files': [{str(ctx_file)!r}],\n"
        "    }],\n"
        "    'overall_timeout_sec': 120,\n"
        "    'max_workers': 1,\n"
        "}\n"
        f"runner = module.make_subprocess_runner(Path({str(hang_script)!r}))\n"
        f"base_dir = Path({str(tmp_path)!r})\n"
        "try:\n"
        "    module.run_fanout(request, base_dir=base_dir, run_dir=base_dir / 'run', runner=runner)\n"
        "except KeyboardInterrupt:\n"
        "    sys.exit(42)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )

    harness_proc = subprocess.Popen(
        [sys.executable, str(harness_script)], start_new_session=True
    )
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.exists(), "grandchild (hang fixture) never started"
        grandchild_pid = int(pid_file.read_text().strip())

        # Confirm the grandchild is actually alive before we interrupt.
        os.kill(grandchild_pid, 0)

        harness_pgid = os.getpgid(harness_proc.pid)
        os.killpg(harness_pgid, signal.SIGINT)

        try:
            exit_code = harness_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            harness_proc.kill()
            harness_proc.wait(timeout=5)
            raise AssertionError("harness did not exit promptly after SIGINT (KeyboardInterrupt handling hung)")

        assert exit_code == 42, f"harness must catch KeyboardInterrupt and exit(42), got {exit_code}"

        deadline = time.monotonic() + 10
        grandchild_dead = False
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                grandchild_dead = True
                break
            time.sleep(0.1)
        assert grandchild_dead, "grandchild (hang fixture, ignores SIGTERM) must be SIGKILLed after KeyboardInterrupt"
    finally:
        if harness_proc.poll() is None:
            harness_proc.kill()
            harness_proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# AC9: partial_success_result_contract
# ---------------------------------------------------------------------------


def test_partial_success_result_contract(tmp_path):
    module = load_module()
    subtasks = [
        make_subtask(tmp_path, subtask_id="ok-1", objective="Investigate scripts/ok1.py contract case 1"),
        make_subtask(tmp_path, subtask_id="bad-1", objective="Investigate scripts/bad1.py contract case 2"),
        make_subtask(tmp_path, subtask_id="ok-2", objective="Investigate scripts/ok2.py contract case 3"),
    ]

    def runner(job, ctx):
        if job.subtask_id == "bad-1":
            return {"ok": False, "failure_class": "simulated_failure", "actual_model": "m"}
        return {"ok": True, "actual_model": "m"}

    request = {"schema": "delegation_fanout_request_v1", "subtasks": subtasks}
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)

    assert result["status"] == "partial_success"
    assert result["ok"] is False
    assert result["counts"] == {"requested": 3, "unique": 3, "succeeded": 2, "failed": 1, "cancelled": 0}
    assert len(result["results"]) == 3
    assert len(result["failures"]) == 1
    assert result["failures"][0]["subtask_id"] == "bad-1"
    # results[] is fixed in input (subtask_id) order.
    assert [r["subtask_id"] for r in result["results"]] == ["ok-1", "bad-1", "ok-2"]


# ---------------------------------------------------------------------------
# AC10: ndjson_integrity_under_concurrency
# ---------------------------------------------------------------------------


def test_ndjson_integrity_under_concurrency(tmp_path):
    module = load_module()
    subtasks = [
        make_subtask(tmp_path, subtask_id=f"n{i}", objective=f"Investigate scripts/n{i}.py ndjson case {i}")
        for i in range(10)
    ]

    def runner(job, ctx):
        time.sleep(0.02)
        return {"ok": True, "actual_model": "m"}

    request = {"schema": "delegation_fanout_request_v1", "subtasks": subtasks, "max_workers": 5}
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)

    run_dir = Path(result["run_dir"])
    journal = run_dir / "events.ndjson"
    assert journal.exists()

    lines = [line for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, "journal must contain at least one event"
    for line in lines:
        parsed = json.loads(line)  # raises if any record is corrupted/interleaved
        assert isinstance(parsed, dict)
        assert "event" in parsed

    # Run directory and journal must be permission-restricted (0700 / 0600).
    assert (run_dir.stat().st_mode & 0o777) == 0o700
    assert (journal.stat().st_mode & 0o777) == 0o600

    manifest_path = Path(result["manifest_path"])
    assert manifest_path.exists()
    manifest_from_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_from_disk["schema"] == "delegation_fanout_result_v1"


def test_manifest_on_disk_matches_returned_result(tmp_path):
    """Issue #1273 iteration 3 Major 1: the manifest written to disk must be
    byte-identical (post JSON round-trip) to the dict run_fanout() returns --
    manifest_path must not be appended in-memory only after the file write."""
    module = load_module()
    subtasks = [make_subtask(tmp_path, subtask_id="only-one")]

    def runner(job, ctx):
        return {"ok": True, "actual_model": "m"}

    request = {"schema": "delegation_fanout_request_v1", "subtasks": subtasks}
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)

    manifest_path = Path(result["manifest_path"])
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert on_disk == result


# ---------------------------------------------------------------------------
# AC11: audit_correlation_under_concurrency
# ---------------------------------------------------------------------------


def test_audit_correlation_under_concurrency(tmp_path):
    module = load_module()
    subtasks = [
        make_subtask(tmp_path, subtask_id=f"c{i}", objective=f"Investigate scripts/c{i}.py audit case {i}")
        for i in range(6)
    ]
    seen: list[dict] = []
    seen_lock = threading.Lock()

    def runner(job, ctx):
        time.sleep(0.05)
        with seen_lock:
            seen.append(dict(job.request))
        return {"ok": True, "actual_model": "m"}

    request = {"schema": "delegation_fanout_request_v1", "subtasks": subtasks, "max_workers": 4}
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)

    parent_run_id = result["parent_run_id"]
    assert parent_run_id

    by_subtask_id = {entry["subtask_id"]: entry for entry in seen}
    assert len(by_subtask_id) == 6, "every subtask must be invoked exactly once with distinct correlation ids"
    for i in range(6):
        entry = by_subtask_id[f"c{i}"]
        assert entry["parent_run_id"] == parent_run_id
        assert entry["subtask_id"] == f"c{i}"
        assert entry["attempt_id"] == "attempt-1"


def test_real_multi_process_audit_concurrency(tmp_path):
    """Issue #1273 iteration 3 Major 2: exercise the *production* subprocess
    runner with several real, independent child OS processes concurrently
    appending delegation_audit_v1 start/end records to the same
    ``--audit-log`` file, via run_gemini_headless.py's actual
    ``run_delegation()`` / ``_audit_write_record()`` code path (only the
    expensive/network ``_run_delegation_core`` is stubbed, inside each
    child process). Proves the real multi-process O_APPEND write is safe,
    not just same-process multi-thread writes (see
    test_run_gemini_headless_concurrency.py for the latter).
    """
    module = load_module()
    rgh_path = Path(__file__).resolve().parent.parent / "scripts" / "run_gemini_headless.py"

    stub_script = tmp_path / "audit_stub_runner.py"
    stub_script.write_text(
        "import argparse\n"
        "import importlib.util\n"
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "def load_rgh():\n"
        f"    spec = importlib.util.spec_from_file_location('audit_stub_rgh', {str(rgh_path)!r})\n"
        "    module = importlib.util.module_from_spec(spec)\n"
        "    sys.modules['audit_stub_rgh'] = module\n"
        "    spec.loader.exec_module(module)\n"
        "    return module\n"
        "\n"
        "def main():\n"
        "    p = argparse.ArgumentParser()\n"
        "    p.add_argument('--request-file', type=Path, required=True)\n"
        "    p.add_argument('--output-file', type=Path, required=True)\n"
        "    p.add_argument('--audit-log', type=Path, default=None)\n"
        "    args = p.parse_args()\n"
        "    rgh = load_rgh()\n"
        "    rgh.set_audit_log_path_override(args.audit_log)\n"
        "    request = json.loads(args.request_file.read_text())\n"
        "\n"
        "    def fake_core(req, request_path=None, _routing=None):\n"
        "        tp = req.get('tool_profile', 'unknown')\n"
        "        return {'ok': True, 'actual_model': 'stub-model', 'tool_profile': tp}\n"
        "\n"
        "    rgh._run_delegation_core = fake_core\n"
        "    result = rgh.run_delegation(request)\n"
        "    args.output_file.write_text(json.dumps(result))\n"
        "    return 0 if result.get('ok') else 1\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )

    subtasks = [
        make_subtask(tmp_path, subtask_id=f"real{i}", objective=f"Investigate scripts/real{i}.py audit case {i}")
        for i in range(4)
    ]
    audit_log = tmp_path / "audit.jsonl"
    runner = module.make_subprocess_runner(stub_script)
    request = {"schema": "delegation_fanout_request_v1", "subtasks": subtasks, "max_workers": 4}
    result = module.run_fanout(request, base_dir=tmp_path, audit_log_path=audit_log, runner=runner)

    assert result["counts"]["succeeded"] == 4, result

    assert audit_log.exists()
    lines = [line for line in audit_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    records = [json.loads(line) for line in lines]  # raises if any line is corrupted/interleaved

    parent_run_id = result["parent_run_id"]
    by_run_id: dict[str, list[dict]] = {}
    for record in records:
        assert record["parent_run_id"] == parent_run_id
        by_run_id.setdefault(record["run_id"], []).append(record)

    assert len(by_run_id) == 4, "each of the 4 real child processes must have its own delegation_audit_v1 run_id"
    subtask_ids_seen = set()
    for run_id, run_records in by_run_id.items():
        record_types = sorted(r["record_type"] for r in run_records)
        assert record_types == ["end", "start"], (run_id, record_types)
        subtask_ids_seen.add(run_records[0]["subtask_id"])
    assert subtask_ids_seen == {"real0", "real1", "real2", "real3"}


# ---------------------------------------------------------------------------
# AC12: github_mutation_fail_closed
# ---------------------------------------------------------------------------


def test_github_mutation_fail_closed(tmp_path):
    module = load_module()
    post_to_issue = make_subtask(
        tmp_path,
        subtask_id="post-to-issue",
        post_to_issue_url="https://github.com/squne121/loop-protocol/issues/1273",
    )
    write_command = make_subtask(
        tmp_path,
        subtask_id="write-command",
        tool_profile="github_research",
        gh_commands=[{"argv": ["issue", "edit", "1", "--body", "malicious"]}],
    )
    safe = make_subtask(tmp_path, subtask_id="safe", objective="Investigate scripts/safe.py fail-closed case")

    calls: list = []
    runner = ok_runner_factory(calls)
    request = {
        "schema": "delegation_fanout_request_v1",
        "subtasks": [post_to_issue, write_command, safe],
    }
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)

    called_ids = [c[0] for c in calls]
    assert "post-to-issue" not in called_ids
    assert "write-command" not in called_ids
    assert "safe" in called_ids

    outcomes_by_id = {r["subtask_id"]: r for r in result["results"]}
    assert outcomes_by_id["post-to-issue"]["fanout_status"] == "failed"
    assert any("child_safety_violation" in reason for reason in outcomes_by_id["post-to-issue"]["reasons"])
    assert outcomes_by_id["write-command"]["fanout_status"] == "failed"
    assert any("child_safety_violation" in reason for reason in outcomes_by_id["write-command"]["reasons"])


def test_recursive_fanout_rejected_before_run(tmp_path):
    module = load_module()
    inner = make_subtask(tmp_path, subtask_id="inner")
    recursive = dict(make_subtask(tmp_path, subtask_id="outer"))
    recursive["subtasks"] = [inner]

    request = {"schema": "delegation_fanout_request_v1", "subtasks": [recursive]}
    errors = module.validate_fanout_request(request)
    assert errors, "a subtask that itself declares subtasks[] must fail validation"
    assert any("recursive" in e or "subtasks" in e for e in errors)


def test_unsafe_subtask_not_hidden_by_dedupe_when_unsafe_field_added_first(tmp_path):
    """Issue #1273 iteration 3 Blocker 2: an unsafe subtask (post_to_issue_url)
    must be rejected by AC12 preflight regardless of whether it happens to be
    submitted *before* an otherwise-identical safe subtask. Safety validation
    now runs on every raw leaf before dedupe, so the unsafe leaf can never be
    folded away as a duplicate alias and skip AC12."""
    module = load_module()
    ctx_path = make_context_file(tmp_path, "shared.md", "identical content for ordering test")
    base = {
        "schema": "delegation_request_v1",
        "provider": "gemini",
        "tool_profile": "no_tools",
        "objective": "Investigate scripts/ordering.py dedupe-vs-safety case",
        "instructions": ["Summarize findings", "List evidence"],
        "output_sections": ["Summary"],
        "context_files": [ctx_path],
    }
    unsafe = dict(base)
    unsafe["subtask_id"] = "unsafe-first"
    unsafe["post_to_issue_url"] = "https://github.com/squne121/loop-protocol/issues/1273"
    safe = dict(base)
    safe["subtask_id"] = "safe-second"

    calls: list = []
    runner = ok_runner_factory(calls)
    request = {"schema": "delegation_fanout_request_v1", "subtasks": [unsafe, safe]}
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)

    called_ids = [c[0] for c in calls]
    assert "unsafe-first" not in called_ids, "unsafe subtask must never be invoked, regardless of input order"
    outcomes_by_id = {r["subtask_id"]: r for r in result["results"]}
    assert outcomes_by_id["unsafe-first"]["fanout_status"] == "failed"
    assert any("child_safety_violation" in r for r in outcomes_by_id["unsafe-first"]["reasons"])
    # The safe subtask (identical otherwise) is unaffected.
    assert "safe-second" in called_ids


def test_unsafe_subtask_not_hidden_by_dedupe_when_unsafe_field_added_second(tmp_path):
    """Same as above with the safe subtask submitted first -- proves the
    fingerprint (not just the "first one wins" ordering accident) now
    includes post_to_issue_url, so the two are never even fingerprint-equal.
    """
    module = load_module()
    ctx_path = make_context_file(tmp_path, "shared2.md", "identical content for reverse ordering test")
    base = {
        "schema": "delegation_request_v1",
        "provider": "gemini",
        "tool_profile": "no_tools",
        "objective": "Investigate scripts/ordering2.py dedupe-vs-safety case",
        "instructions": ["Summarize findings", "List evidence"],
        "output_sections": ["Summary"],
        "context_files": [ctx_path],
    }
    safe = dict(base)
    safe["subtask_id"] = "safe-first"
    unsafe = dict(base)
    unsafe["subtask_id"] = "unsafe-second"
    unsafe["post_to_issue_url"] = "https://github.com/squne121/loop-protocol/issues/1273"

    calls: list = []
    runner = ok_runner_factory(calls)
    request = {"schema": "delegation_fanout_request_v1", "subtasks": [safe, unsafe]}
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)

    called_ids = [c[0] for c in calls]
    assert "unsafe-second" not in called_ids
    assert "safe-first" in called_ids
    outcomes_by_id = {r["subtask_id"]: r for r in result["results"]}
    assert outcomes_by_id["unsafe-second"]["fanout_status"] == "failed"
    # Because the fingerprint now includes post_to_issue_url, the two
    # subtasks are NOT deduped into one -- both appear as distinct unique
    # entries (one succeeded, one rejected), not a single aliased pair.
    assert result["counts"]["unique"] == 2
    assert result["deduplicated_aliases"] == {}


def test_dedupe_respects_model_role_timeout_sec_differences(tmp_path):
    """Issue #1273 iteration 3 Blocker 2: model/role/timeout_sec differences
    must NOT be silently dropped from the dedupe fingerprint."""
    module = load_module()
    ctx_path = make_context_file(tmp_path, "shared3.md", "identical content for field-coverage test")
    base = {
        "schema": "delegation_request_v1",
        "provider": "gemini",
        "tool_profile": "no_tools",
        "objective": "Investigate scripts/field-coverage.py dedupe fingerprint case",
        "instructions": ["Summarize findings", "List evidence"],
        "output_sections": ["Summary"],
        "context_files": [ctx_path],
    }
    variant_a = dict(base)
    variant_a["subtask_id"] = "model-a"
    variant_a["model"] = "gemini-model-a"
    variant_b = dict(base)
    variant_b["subtask_id"] = "model-b"
    variant_b["model"] = "gemini-model-b"

    calls: list = []
    runner = ok_runner_factory(calls)
    request = {"schema": "delegation_fanout_request_v1", "subtasks": [variant_a, variant_b]}
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)

    assert result["counts"]["unique"] == 2, "differing 'model' must not be treated as a duplicate"
    assert len(calls) == 2
    assert result["deduplicated_aliases"] == {}


# ---------------------------------------------------------------------------
# Issue #1273 iteration 3 Blocker 1: subtask_id validation / artifact_stem
# ---------------------------------------------------------------------------


def test_subtask_id_path_traversal_rejected(tmp_path):
    module = load_module()
    subtask = make_subtask(tmp_path, subtask_id="../../outside")
    request = {"schema": "delegation_fanout_request_v1", "subtasks": [subtask]}
    errors = module.validate_fanout_request(request)
    assert errors
    assert any("subtask_id" in e for e in errors)


def test_subtask_id_absolute_path_rejected(tmp_path):
    module = load_module()
    subtask = make_subtask(tmp_path, subtask_id="/etc/passwd")
    request = {"schema": "delegation_fanout_request_v1", "subtasks": [subtask]}
    errors = module.validate_fanout_request(request)
    assert errors
    assert any("subtask_id" in e for e in errors)


def test_subtask_id_control_character_rejected(tmp_path):
    module = load_module()
    subtask = make_subtask(tmp_path, subtask_id="abc\x00def")
    request = {"schema": "delegation_fanout_request_v1", "subtasks": [subtask]}
    errors = module.validate_fanout_request(request)
    assert errors
    assert any("subtask_id" in e for e in errors)


def test_subtask_id_unicode_control_character_rejected(tmp_path):
    module = load_module()
    # U+0007 BEL is a C0 control character (ord < 0x20).
    subtask = make_subtask(tmp_path, subtask_id="abc\x07def")
    request = {"schema": "delegation_fanout_request_v1", "subtasks": [subtask]}
    errors = module.validate_fanout_request(request)
    assert errors
    assert any("subtask_id" in e for e in errors)


def test_duplicate_explicit_subtask_id_rejected(tmp_path):
    module = load_module()
    subtask_a = make_subtask(tmp_path, subtask_id="dup", context_file_name="a.md")
    subtask_b = make_subtask(tmp_path, subtask_id="dup", context_file_name="b.md")
    request = {"schema": "delegation_fanout_request_v1", "subtasks": [subtask_a, subtask_b]}
    errors = module.validate_fanout_request(request)
    assert errors
    assert any("duplicate subtask_id" in e for e in errors)


def test_explicit_subtask_id_colliding_with_default_rejected(tmp_path):
    """An explicit subtask_id equal to the auto-generated default id
    ('subtask-0') for a *different* subtask index must also be rejected."""
    module = load_module()
    subtask_no_id = make_subtask(tmp_path, context_file_name="c.md")  # resolves to "subtask-0"
    subtask_collides = make_subtask(tmp_path, subtask_id="subtask-0", context_file_name="d.md")
    request = {"schema": "delegation_fanout_request_v1", "subtasks": [subtask_no_id, subtask_collides]}
    errors = module.validate_fanout_request(request)
    assert errors
    assert any("duplicate subtask_id" in e for e in errors)


def test_artifact_stem_is_safe_and_independent_of_subtask_id(tmp_path):
    """Behavioral proof that runners never see a filesystem-unsafe artifact
    identifier even though subtask_id itself (once validated as safe) is
    fully caller-controlled text."""
    module = load_module()
    subtask = make_subtask(tmp_path, subtask_id="weird.but-valid_ID.2")

    seen: dict = {}

    def runner(job, ctx):
        seen["subtask_id"] = job.subtask_id
        seen["artifact_stem"] = job.artifact_stem
        return {"ok": True, "actual_model": "m"}

    request = {"schema": "delegation_fanout_request_v1", "subtasks": [subtask]}
    module.run_fanout(request, base_dir=tmp_path, runner=runner)

    assert seen["subtask_id"] == "weird.but-valid_ID.2"
    assert seen["artifact_stem"] != seen["subtask_id"]
    assert "/" not in seen["artifact_stem"]
    assert "\\" not in seen["artifact_stem"]
    # Deterministic {index:04d}-{fingerprint[:16]} shape.
    stem_index, _, stem_hash = seen["artifact_stem"].partition("-")
    assert stem_index == "0000"
    assert len(stem_hash) == 16
    assert all(ch in "0123456789abcdef" for ch in stem_hash)


# ---------------------------------------------------------------------------
# AC3: delegation_fanout_request_v1 closed schema
# ---------------------------------------------------------------------------


def test_closed_schema_rejects_unknown_top_level_key(tmp_path):
    module = load_module()
    subtask = make_subtask(tmp_path)
    request = {
        "schema": "delegation_fanout_request_v1",
        "subtasks": [subtask],
        "planner_mode": True,  # unknown key: planner mode is explicitly out of scope for v1
    }
    errors = module.validate_fanout_request(request)
    assert any("unknown top-level key" in e for e in errors)


def test_closed_schema_rejects_missing_subtasks():
    module = load_module()
    errors = module.validate_fanout_request({"schema": "delegation_fanout_request_v1"})
    assert any("subtasks must be a non-empty list" in e for e in errors)


# ---------------------------------------------------------------------------
# AC6: max_subtasks / max_total_attempts hard caps
# ---------------------------------------------------------------------------


def test_max_subtasks_rejects_overflow_without_invoking(tmp_path):
    module = load_module()
    subtasks = [
        make_subtask(tmp_path, subtask_id=f"m{i}", objective=f"Investigate scripts/m{i}.py overflow case {i}")
        for i in range(5)
    ]
    calls: list = []
    runner = ok_runner_factory(calls)
    request = {"schema": "delegation_fanout_request_v1", "subtasks": subtasks, "max_subtasks": 3}
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)
    assert len(calls) == 3
    assert result["counts"]["failed"] == 2
    overflow_outcomes = [r for r in result["results"] if r["subtask_id"] in ("m3", "m4")]
    assert all("max_subtasks_exceeded" in r["reasons"] for r in overflow_outcomes)


def test_max_total_attempts_caps_total_child_invocations(tmp_path):
    module = load_module()
    subtasks = [
        make_subtask(tmp_path, subtask_id=f"a{i}", objective=f"Investigate scripts/a{i}.py attempts case {i}")
        for i in range(5)
    ]
    calls: list = []
    runner = ok_runner_factory(calls)
    request = {
        "schema": "delegation_fanout_request_v1",
        "subtasks": subtasks,
        "max_total_attempts": 2,
    }
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)
    assert len(calls) <= 2, "total child invocations must never exceed max_total_attempts"
    assert result["counts"]["failed"] >= 3, "overflow subtasks beyond max_total_attempts must be rejected"


def test_raw_subtask_count_hard_cap_rejected_without_hashing(tmp_path):
    """Issue #1273 iteration 3 Major 4: a raw subtask count far beyond
    max_subtasks/max_total_attempts must be rejected immediately, before any
    per-subtask validation or context-file hashing (and therefore without
    ever invoking the runner)."""
    module = load_module()
    # raw_cap = max(max_subtasks, max_total_attempts) * 4; use defaults
    # (max_subtasks=20 -> raw_cap=80) and submit well beyond it.
    subtasks = [{"schema": "delegation_request_v1", "objective": f"raw overflow {i}"} for i in range(200)]
    calls: list = []
    runner = ok_runner_factory(calls)
    request = {"schema": "delegation_fanout_request_v1", "subtasks": subtasks}
    result = module.run_fanout(request, base_dir=tmp_path, runner=runner)

    assert not calls, "runner must never be invoked when the raw hard cap is exceeded"
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["counts"]["requested"] == 200
    assert any("raw_subtasks_exceeded_hard_cap" in f["reasons"][0] for f in result["failures"])


def test_write_ndjson_record_raises_on_partial_write(tmp_path, monkeypatch):
    """Issue #1273 iteration 3 Major 5: os.write()'s return value must be
    checked -- a short write must raise rather than being silently ignored.
    """
    module = load_module()

    def fake_os_write(fd, data):
        return max(0, len(data) - 1)  # simulate a short write

    monkeypatch.setattr(module.os, "write", fake_os_write)
    journal_path = tmp_path / "events.ndjson"
    fd = os.open(journal_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        try:
            module._write_ndjson_record(fd, b'{"event": "x"}\n', journal_path)
            raised = False
        except OSError:
            raised = True
    finally:
        os.close(fd)
    assert raised, "a short write must raise OSError, not be silently ignored"


# ---------------------------------------------------------------------------
# AC16: fake-runner injection is exercised by every test above; this test
# additionally proves run_fanout() rejects a structurally invalid request
# without ever touching the filesystem/runner (fail-closed on bad input).
# ---------------------------------------------------------------------------


def test_invalid_request_returns_failed_without_runner(tmp_path):
    module = load_module()
    calls: list = []
    runner = ok_runner_factory(calls)
    result = module.run_fanout({"schema": "delegation_fanout_request_v1"}, base_dir=tmp_path, runner=runner)
    assert result["status"] == "failed"
    assert result["ok"] is False
    assert not calls
