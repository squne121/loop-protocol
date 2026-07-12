"""Behavioral tests for fan_out_orchestrator.py (Issue #1273 AC5-AC12, AC16).

These tests exercise ``run_fanout()`` end-to-end with dependency-injected
fake runners (never spawning a real ``gemini``/``agy`` CLI subprocess), plus
one lower-level test that exercises the *production* subprocess runner
against a real (harmless, fixture) child process to prove process-group
termination actually works.
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
    return subtask


def ok_runner_factory(calls: list):
    """A fake runner recording every invocation and always succeeding."""

    def _runner(subtask_id, request, ctx):
        calls.append((subtask_id, dict(request)))
        return {
            "schema": "delegation_result/v1",
            "ok": True,
            "provider": request.get("provider"),
            "tool_profile": request.get("tool_profile"),
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

    def runner(subtask_id, request, ctx):
        with lock:
            state["current"] += 1
            state["max_seen"] = max(state["max_seen"], state["current"])
        time.sleep(0.15)
        with lock:
            state["current"] -= 1
        return {"ok": True, "actual_model": "m", "tool_profile": request.get("tool_profile")}

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

    def runner(subtask_id, request, ctx):
        with lock:
            state["current"] += 1
            state["max_seen"] = max(state["max_seen"], state["current"])
        time.sleep(0.15)
        with lock:
            state["current"] -= 1
        return {"ok": True, "actual_model": "m", "tool_profile": request.get("tool_profile")}

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

    def runner(subtask_id, request, ctx):
        with invoked_lock:
            invoked.append(subtask_id)
        if subtask_id == "slow":
            # Cooperatively "hang" until the orchestrator signals cancellation
            # (the production subprocess runner polls this same event and
            # terminates the real child process group -- see the separate
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

    result_holder: dict = {}

    def _invoke():
        result_holder["result"] = runner("hang", {"provider": "gemini", "tool_profile": "no_tools"}, ctx)

    thread = threading.Thread(target=_invoke)
    thread.start()
    time.sleep(0.3)  # let the child process actually start
    cancel_event.set()
    thread.join(timeout=10)

    assert not thread.is_alive(), "runner must return once cancel_event is set (process group terminated)"
    assert result_holder["result"]["ok"] is False
    assert "timeout" in result_holder["result"]["failure_class"]


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

    def runner(subtask_id, request, ctx):
        if subtask_id == "bad-1":
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

    def runner(subtask_id, request, ctx):
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

    def runner(subtask_id, request, ctx):
        time.sleep(0.05)
        with seen_lock:
            seen.append(dict(request))
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
