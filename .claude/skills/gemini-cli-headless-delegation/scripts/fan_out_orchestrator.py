#!/usr/bin/env python3
"""fan_out_orchestrator.py -- parallel provider/profile fan-out orchestrator (Issue #1273).

Consumes a ``delegation_fanout_request_v1`` (an explicit ``subtasks[]`` list,
each element a self-contained ``delegation_request_v1`` -- planner mode is
out of scope for v1) and fans each subtask out to an independent child
worker, in parallel, subject to exact dedupe, provider/profile compatibility
preflight, and execution-control limits (``max_subtasks`` / ``max_workers`` /
per-provider and per-profile semaphores / ``max_total_attempts`` /
``overall_timeout_sec``). Returns a deterministic ``delegation_fanout_result_v1``
merge of all child outcomes.

Parent is the single writer of the run's NDJSON event journal and final
manifest; child workers never write files or perform GitHub/shell mutation
directly -- they return a result to the parent (or, for the production
subprocess runner, write only their own isolated ``--output-file`` inside the
run's private directory, which the parent then reads).

The executor used to run each subtask (``runner``) is dependency-injected so
that unit tests can substitute a fast, deterministic in-process fake instead
of spawning real ``gemini`` / ``agy`` CLI subprocesses. The default runner
(``make_subprocess_runner``) spawns ``run_gemini_headless.py`` as an isolated
subprocess in its own process group, so ``overall_timeout_sec`` can terminate
it (SIGTERM, then SIGKILL after a grace period) without relying on
cooperative in-process cancellation.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

FANOUT_REQUEST_SCHEMA = "delegation_fanout_request_v1"
FANOUT_RESULT_SCHEMA = "delegation_fanout_result_v1"

_SCRIPT_DIR = Path(__file__).resolve().parent
_RUN_GEMINI_HEADLESS_PATH = _SCRIPT_DIR / "run_gemini_headless.py"

# Closed top-level schema for delegation_fanout_request_v1 (Issue #1273 AC3).
_FANOUT_REQUEST_KNOWN_KEYS = frozenset(
    {
        "schema",
        "subtasks",
        "max_workers",
        "max_subtasks",
        "max_total_attempts",
        "overall_timeout_sec",
        "provider_concurrency",
        "profile_concurrency",
    }
)
_FANOUT_REQUEST_DEFAULTS: dict[str, int | float] = {
    "max_workers": 4,
    "max_subtasks": 20,
    "max_total_attempts": 20,
    "overall_timeout_sec": 300,
}

_CHILD_TERMINATE_GRACE_SEC = 5.0
_CHILD_POLL_INTERVAL_SEC = 0.2

RunnerFn = Callable[[str, dict, "RunnerContext"], dict]

_rgh_module_cache: dict[str, Any] = {}


def _load_run_gemini_headless_module():
    """Lazily (and cache-ably) load run_gemini_headless.py for
    ``validate_request`` / provider-profile compatibility constants /
    ``_validate_github_research_argv``, mirroring build_request.py's
    ``_load_validate_request()`` pattern. The loaded module is registered in
    ``sys.modules`` under a unique name *before* ``exec_module`` runs, so
    concurrent test-suite module loading never collides with this module's
    identity.
    """
    module = _rgh_module_cache.get("module")
    if module is not None:
        return module
    module_name = "fan_out_orchestrator_run_gemini_headless"
    spec = importlib.util.spec_from_file_location(module_name, _RUN_GEMINI_HEADLESS_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load run_gemini_headless from {_RUN_GEMINI_HEADLESS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    _rgh_module_cache["module"] = module
    return module


# ---------------------------------------------------------------------------
# delegation_fanout_request_v1 validation (AC3)
# ---------------------------------------------------------------------------


def validate_fanout_request(request: Mapping[str, Any]) -> list[str]:
    """Fail-closed validator for delegation_fanout_request_v1.

    Enforces a *closed* top-level schema (unknown keys rejected) and that
    ``subtasks[]`` is a non-empty list of dict-like ``delegation_request_v1``
    payloads that do not themselves declare ``subtasks`` (planner mode /
    recursive fan-out is out of scope for v1).
    """
    errors: list[str] = []
    if not isinstance(request, Mapping):
        return ["request must be a mapping"]

    if request.get("schema") != FANOUT_REQUEST_SCHEMA:
        errors.append(f"schema must equal {FANOUT_REQUEST_SCHEMA!r}")

    unknown = set(request) - _FANOUT_REQUEST_KNOWN_KEYS
    if unknown:
        errors.append(f"unknown top-level key(s): {sorted(unknown)}")

    subtasks = request.get("subtasks")
    if not isinstance(subtasks, list) or not subtasks:
        errors.append("subtasks must be a non-empty list")
        return errors

    for idx, subtask in enumerate(subtasks):
        if not isinstance(subtask, Mapping):
            errors.append(f"subtasks[{idx}] must be an object (delegation_request_v1)")
            continue
        if "subtasks" in subtask:
            errors.append(
                f"subtasks[{idx}] must not itself declare 'subtasks' "
                "(planner mode / recursive fan-out is not supported in v1)"
            )
        if subtask.get("schema") == FANOUT_REQUEST_SCHEMA:
            errors.append(
                f"subtasks[{idx}].schema must not be {FANOUT_REQUEST_SCHEMA!r} "
                "(recursive fan-out is not supported in v1)"
            )

    for key in ("max_workers", "max_subtasks", "max_total_attempts"):
        if key not in request:
            continue
        value = request[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            errors.append(f"{key} must be a positive integer when present")

    if "overall_timeout_sec" in request:
        timeout = request["overall_timeout_sec"]
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            errors.append("overall_timeout_sec must be a positive number when present")

    for key in ("provider_concurrency", "profile_concurrency"):
        if key not in request:
            continue
        value = request[key]
        valid = isinstance(value, Mapping) and all(
            isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in value.values()
        )
        if not valid:
            errors.append(f"{key} must be a mapping of name -> positive int when present")

    return errors


# ---------------------------------------------------------------------------
# Exact dedupe (AC5)
# ---------------------------------------------------------------------------


@dataclass
class PreparedSubtask:
    subtask_id: str
    original_ids: list[str]
    request: dict[str, Any]
    fingerprint: str


def _hash_context_file(path_str: str, base_dir: Path) -> str:
    candidate = Path(path_str)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    try:
        data = candidate.read_bytes()
    except OSError:
        return f"unreadable:{path_str}"
    digest = hashlib.sha256()
    digest.update(path_str.encode("utf-8", errors="replace"))
    digest.update(b"\0")
    digest.update(data)
    return digest.hexdigest()


def _subtask_fingerprint(subtask: Mapping[str, Any], base_dir: Path) -> str:
    """Exact-match fingerprint (Issue #1273 AC5): provider, profile,
    objective, instructions, output_sections, context file *content* hash,
    and gh_commands. Semantic/fuzzy dedupe is explicitly out of scope for v1
    -- two subtasks fingerprint identically only when every one of these
    fields is byte-for-byte identical (including the referenced context
    files' actual bytes, not just their paths).
    """
    context_files = subtask.get("context_files")
    context_hashes: list[Any] | None = None
    if isinstance(context_files, list):
        context_hashes = [
            _hash_context_file(p, base_dir) if isinstance(p, str) else None for p in context_files
        ]
    payload = {
        "provider": subtask.get("provider", "gemini"),
        "tool_profile": subtask.get("tool_profile"),
        "objective": subtask.get("objective"),
        "instructions": subtask.get("instructions"),
        "output_sections": subtask.get("output_sections"),
        "context_files_content_hash": context_hashes,
        "gh_commands": subtask.get("gh_commands"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _prepare_subtasks(
    subtasks: list[Mapping[str, Any]], base_dir: Path
) -> tuple[list[PreparedSubtask], dict[str, list[str]]]:
    """Assign stable subtask_ids and fold exact duplicates together.

    Returns (unique subtasks in first-seen order, deduplicated_aliases map of
    kept subtask_id -> [alias subtask_ids folded into it]).
    """
    seen: dict[str, PreparedSubtask] = {}
    order: list[str] = []
    deduplicated_aliases: dict[str, list[str]] = {}
    for idx, raw in enumerate(subtasks):
        subtask_id = str(raw.get("subtask_id") or f"subtask-{idx}")
        fingerprint = _subtask_fingerprint(raw, base_dir)
        if fingerprint in seen:
            kept = seen[fingerprint]
            deduplicated_aliases.setdefault(kept.subtask_id, []).append(subtask_id)
            kept.original_ids.append(subtask_id)
            continue
        prepared = PreparedSubtask(
            subtask_id=subtask_id,
            original_ids=[subtask_id],
            request=dict(raw),
            fingerprint=fingerprint,
        )
        seen[fingerprint] = prepared
        order.append(fingerprint)
    return [seen[fp] for fp in order], deduplicated_aliases


# ---------------------------------------------------------------------------
# Preflight: provider/profile compatibility + child safety (AC7, AC12)
# ---------------------------------------------------------------------------


def _child_safety_and_compatibility_errors(subtask: Mapping[str, Any], rgh: Any) -> list[str]:
    """Fail-closed preflight for a single (already-deduped) subtask.

    Runs entirely *before* any provider is invoked (AC7). Rejects, without
    ever starting a child process:
      - post_to_issue_url (GitHub write mutation) -- AC12
      - recursive fan-out (already caught by validate_fanout_request, but
        re-checked per-subtask here defensively) -- AC12
      - gh_commands entries that fail the same read-only argv allowlist used
        by github_research validation, applied uniformly regardless of
        tool_profile (delegation_request_v1 has no other structured shell/
        file-mutation channel, so this closes the write-mutation gap for
        non-github_research profiles too) -- AC12
      - provider/tool_profile combinations that are not supported (AC7)
      - any other delegation_request_v1 validation failure
    """
    errors: list[str] = []

    if subtask.get("post_to_issue_url"):
        errors.append("child_safety_violation: post_to_issue_url is forbidden for all fan-out children")

    if "subtasks" in subtask or subtask.get("schema") == FANOUT_REQUEST_SCHEMA:
        errors.append("child_safety_violation: recursive fan-out is forbidden")

    gh_commands = subtask.get("gh_commands")
    if isinstance(gh_commands, list):
        for idx, entry in enumerate(gh_commands):
            argv = entry.get("argv") if isinstance(entry, dict) else None
            if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
                errors.append(f"child_safety_violation: gh_commands[{idx}].argv must be a list of strings")
                continue
            errors.extend(
                f"child_safety_violation: gh_commands[{idx}]: {msg}"
                for msg in rgh._validate_github_research_argv(argv)  # noqa: SLF001
            )

    provider = subtask.get("provider", "gemini")
    tool_profile = subtask.get("tool_profile")
    if provider not in rgh.SUPPORTED_PROVIDERS:
        errors.append(f"provider_profile_incompatible: provider {provider!r} is not supported")
    elif provider == "agy" and tool_profile not in rgh.AGY_SUPPORTED_PROFILES:
        errors.append(
            f"provider_profile_incompatible: provider=agy does not support tool_profile={tool_profile!r}"
        )
    elif provider == "auto" and tool_profile not in rgh.PROVIDER_AUTO_ELIGIBLE_PROFILES:
        errors.append(
            f"provider_profile_incompatible: provider=auto does not support tool_profile={tool_profile!r}"
        )

    validation_errors = rgh.validate_request(subtask)
    errors.extend(f"validation_error: {msg}" for msg in validation_errors)
    return errors


# ---------------------------------------------------------------------------
# Child execution: runner protocol + default subprocess runner (AC4, AC8)
# ---------------------------------------------------------------------------


@dataclass
class RunnerContext:
    """Shared, thread-safe state handed to every runner invocation."""

    run_dir: Path
    audit_log_path: Path | None
    cancel_event: threading.Event
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _processes: dict[str, subprocess.Popen] = field(default_factory=dict)

    def register_process(self, subtask_id: str, proc: subprocess.Popen) -> None:
        with self._lock:
            self._processes[subtask_id] = proc

    def unregister_process(self, subtask_id: str) -> None:
        with self._lock:
            self._processes.pop(subtask_id, None)

    def terminate_all(self, grace_period: float = _CHILD_TERMINATE_GRACE_SEC) -> None:
        with self._lock:
            procs = list(self._processes.values())
        for proc in procs:
            _terminate_process_group(proc, grace_period)


def _terminate_process_group(proc: subprocess.Popen, grace_period: float) -> None:
    """SIGTERM the child's whole process group, then SIGKILL after a grace
    period if it hasn't exited (Issue #1273 AC8). Requires the child to have
    been started with ``start_new_session=True`` so it owns its own process
    group and terminating it cannot affect the parent orchestrator.
    """
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    deadline = time.monotonic() + grace_period
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.05)
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def _synthetic_failure_result(request: Mapping[str, Any], failure_class: str, failure_reason: str) -> dict:
    return {
        "schema": "delegation_result/v1",
        "ok": False,
        "provider": request.get("provider", "gemini"),
        "tool_profile": request.get("tool_profile", "unknown"),
        "actual_model": "unknown",
        "failure_class": failure_class,
        "failure_reason": failure_reason,
        "response_text": None,
        "stderr": failure_reason,
        "warnings": [failure_reason],
    }


def make_subprocess_runner(script_path: Path, python_executable: str | None = None) -> RunnerFn:
    """Build the production runner: spawns ``script_path`` (normally
    run_gemini_headless.py) as an isolated subprocess per subtask, in its own
    process group, inside ``ctx.run_dir``. Cooperatively checks
    ``ctx.cancel_event`` both before starting and while waiting, so
    ``overall_timeout_sec`` can prevent pending subtasks from ever starting
    and can terminate already-running ones.
    """
    executable = python_executable or sys.executable

    def _runner(subtask_id: str, request: dict, ctx: RunnerContext) -> dict:
        if ctx.cancel_event.is_set():
            return _synthetic_failure_result(request, "overall_timeout_pending_cancelled", "cancelled before start")

        req_path = ctx.run_dir / f"{subtask_id}.request.json"
        out_path = ctx.run_dir / f"{subtask_id}.result.json"
        req_path.write_text(json.dumps(request), encoding="utf-8")
        os.chmod(req_path, 0o600)

        argv = [
            executable,
            str(script_path),
            "--request-file",
            str(req_path),
            "--output-file",
            str(out_path),
        ]
        if ctx.audit_log_path is not None:
            argv.extend(["--audit-log", str(ctx.audit_log_path)])

        try:
            proc = subprocess.Popen(  # noqa: S603
                argv, cwd=str(ctx.run_dir), start_new_session=True
            )
        except OSError as exc:
            return _synthetic_failure_result(request, "child_spawn_failed", str(exc))

        ctx.register_process(subtask_id, proc)
        try:
            while True:
                if ctx.cancel_event.is_set():
                    _terminate_process_group(proc, _CHILD_TERMINATE_GRACE_SEC)
                    return _synthetic_failure_result(
                        request, "overall_timeout_terminated", "terminated due to overall_timeout_sec"
                    )
                try:
                    proc.wait(timeout=_CHILD_POLL_INTERVAL_SEC)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            ctx.unregister_process(subtask_id)

        if ctx.cancel_event.is_set():
            # Raced with the deadline: the child finished, but too late to be
            # honored -- discard whatever it returned (Issue #1273 AC8).
            return _synthetic_failure_result(
                request, "overall_timeout_late_result_discarded", "child result discarded (raced with timeout)"
            )

        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _synthetic_failure_result(request, "child_output_unreadable", str(exc))
        if not isinstance(data, dict):
            return _synthetic_failure_result(request, "child_output_unreadable", "output file did not contain an object")
        return data

    return _runner


# ---------------------------------------------------------------------------
# Orchestration entrypoint (AC4, AC6, AC8, AC9, AC10, AC11)
# ---------------------------------------------------------------------------


def _rejected_outcome(item: PreparedSubtask, reason_code: str, problems: list[str] | None = None) -> dict:
    return {
        "subtask_id": item.subtask_id,
        "original_ids": list(item.original_ids),
        "fanout_status": "failed",
        "result": None,
        "reasons": [reason_code] + list(problems or []),
    }


def _finalize_outcome(item: PreparedSubtask, result: Any, cancelled: bool) -> dict:
    if not isinstance(result, dict):
        result = _synthetic_failure_result({}, "invalid_runner_result", "runner did not return an object")
    if cancelled:
        return {
            "subtask_id": item.subtask_id,
            "original_ids": list(item.original_ids),
            "fanout_status": "cancelled",
            "result": result,
            "reasons": [result.get("failure_class") or "overall_timeout"],
        }
    ok = bool(result.get("ok"))
    return {
        "subtask_id": item.subtask_id,
        "original_ids": list(item.original_ids),
        "fanout_status": "succeeded" if ok else "failed",
        "result": result,
        "reasons": [] if ok else [result.get("failure_class") or "unknown_failure"],
    }


def _fanout_request_invalid_result(errors: list[str]) -> dict:
    return {
        "schema": FANOUT_RESULT_SCHEMA,
        "status": "failed",
        "ok": False,
        "parent_run_id": None,
        "counts": {"requested": 0, "unique": 0, "succeeded": 0, "failed": 0, "cancelled": 0},
        "results": [],
        "failures": [{"subtask_id": None, "fanout_status": "failed", "result": None, "reasons": errors}],
        "deduplicated_aliases": {},
        "run_dir": None,
        "manifest_path": None,
    }


def run_fanout(
    request: Mapping[str, Any],
    *,
    base_dir: Path | None = None,
    run_dir: Path | None = None,
    audit_log_path: Path | None = None,
    runner: RunnerFn | None = None,
) -> dict[str, Any]:
    """Execute a delegation_fanout_request_v1 and return a deterministic
    delegation_fanout_result_v1 (Issue #1273 AC4/AC9).
    """
    errors = validate_fanout_request(request)
    if errors:
        return _fanout_request_invalid_result(errors)

    base_dir = base_dir or Path.cwd()
    max_workers = int(request.get("max_workers", _FANOUT_REQUEST_DEFAULTS["max_workers"]))
    max_subtasks = int(request.get("max_subtasks", _FANOUT_REQUEST_DEFAULTS["max_subtasks"]))
    max_total_attempts = int(request.get("max_total_attempts", _FANOUT_REQUEST_DEFAULTS["max_total_attempts"]))
    overall_timeout_sec = float(request.get("overall_timeout_sec", _FANOUT_REQUEST_DEFAULTS["overall_timeout_sec"]))
    provider_limits: dict[str, int] = dict(request.get("provider_concurrency") or {})
    profile_limits: dict[str, int] = dict(request.get("profile_concurrency") or {})

    parent_run_id = uuid.uuid4().hex
    subtasks_in = list(request["subtasks"])
    requested_count = len(subtasks_in)

    prepared, deduplicated_aliases = _prepare_subtasks(subtasks_in, base_dir)
    unique_count = len(prepared)

    if run_dir is None:
        run_dir = Path(tempfile.mkdtemp(prefix=f"fanout-{parent_run_id}-"))
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir, 0o700)

    journal_path = run_dir / "events.ndjson"
    journal_lock = threading.Lock()

    def _journal(event: dict[str, Any]) -> None:
        line = (json.dumps(event, sort_keys=True, default=str) + "\n").encode("utf-8")
        with journal_lock:
            fd = os.open(journal_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)

    rgh = _load_run_gemini_headless_module()

    outcomes: dict[str, dict] = {}

    # max_subtasks (AC6): reject overflow beyond the unique-subtask cap
    # without ever running preflight or spawning a child for them.
    if unique_count > max_subtasks:
        eligible = prepared[:max_subtasks]
        for extra in prepared[max_subtasks:]:
            outcomes[extra.subtask_id] = _rejected_outcome(extra, "max_subtasks_exceeded")
    else:
        eligible = prepared

    # Preflight: provider/profile compatibility + child safety (AC7, AC12).
    runnable: list[PreparedSubtask] = []
    for item in eligible:
        problems = _child_safety_and_compatibility_errors(item.request, rgh)
        if problems:
            outcomes[item.subtask_id] = _rejected_outcome(item, "preflight_rejected", problems)
        else:
            runnable.append(item)

    # max_total_attempts (AC6): a v1 "attempt" is one child subprocess spawn
    # (no orchestrator-level retry in v1 -- retry/fallback policy is the
    # separate quota-management child issue referenced in the contract).
    if len(runnable) > max_total_attempts:
        overflow = runnable[max_total_attempts:]
        runnable = runnable[:max_total_attempts]
        for extra in overflow:
            outcomes[extra.subtask_id] = _rejected_outcome(extra, "max_total_attempts_exceeded")

    cancel_event = threading.Event()
    ctx = RunnerContext(run_dir=run_dir, audit_log_path=audit_log_path, cancel_event=cancel_event)
    active_runner = runner or make_subprocess_runner(_RUN_GEMINI_HEADLESS_PATH)

    global_sem = threading.Semaphore(max(1, max_workers))
    provider_sems: dict[str, threading.Semaphore] = {}
    profile_sems: dict[str, threading.Semaphore] = {}
    sem_registry_lock = threading.Lock()

    def _provider_sem(name: str) -> threading.Semaphore:
        with sem_registry_lock:
            sem = provider_sems.get(name)
            if sem is None:
                sem = threading.Semaphore(max(1, int(provider_limits.get(name, max_workers))))
                provider_sems[name] = sem
            return sem

    def _profile_sem(name: str) -> threading.Semaphore:
        with sem_registry_lock:
            sem = profile_sems.get(name)
            if sem is None:
                sem = threading.Semaphore(max(1, int(profile_limits.get(name, max_workers))))
                profile_sems[name] = sem
            return sem

    def _run_one(item: PreparedSubtask) -> None:
        subtask_id = item.subtask_id
        stamped_request = dict(item.request)
        # Audit correlation (AC11): reserved fan-out keys consumed by
        # run_gemini_headless._audit_build_start_record().
        stamped_request["parent_run_id"] = parent_run_id
        stamped_request["subtask_id"] = subtask_id
        stamped_request["attempt_id"] = "attempt-1"
        provider = str(stamped_request.get("provider", "gemini"))
        tool_profile = str(stamped_request.get("tool_profile", "unknown"))

        with global_sem, _provider_sem(provider), _profile_sem(tool_profile):
            if cancel_event.is_set():
                outcomes[subtask_id] = {
                    "subtask_id": subtask_id,
                    "original_ids": list(item.original_ids),
                    "fanout_status": "cancelled",
                    "result": None,
                    "reasons": ["overall_timeout_pending_cancelled"],
                }
                _journal(
                    {"event": "subtask_cancelled_before_start", "subtask_id": subtask_id, "parent_run_id": parent_run_id}
                )
                return
            _journal({"event": "subtask_started", "subtask_id": subtask_id, "parent_run_id": parent_run_id})
            try:
                result = active_runner(subtask_id, stamped_request, ctx)
            except Exception as exc:  # pylint: disable=broad-except
                result = _synthetic_failure_result(stamped_request, "runner_exception", str(exc))

        outcomes[subtask_id] = _finalize_outcome(item, result, cancel_event.is_set())
        _journal(
            {
                "event": "subtask_finished",
                "subtask_id": subtask_id,
                "parent_run_id": parent_run_id,
                "fanout_status": outcomes[subtask_id]["fanout_status"],
            }
        )

    all_done_event = threading.Event()

    def _watch_deadline() -> None:
        finished_in_time = all_done_event.wait(timeout=overall_timeout_sec)
        if not finished_in_time:
            cancel_event.set()
            _journal({"event": "overall_timeout_reached", "parent_run_id": parent_run_id})
            ctx.terminate_all()

    watcher = threading.Thread(target=_watch_deadline, daemon=True)
    watcher.start()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            futures = [pool.submit(_run_one, item) for item in runnable]
            concurrent.futures.wait(futures)
    except KeyboardInterrupt:
        cancel_event.set()
        ctx.terminate_all()
        raise
    finally:
        all_done_event.set()
        watcher.join(timeout=2)

    results_list: list[dict] = []
    failures_list: list[dict] = []
    succeeded = failed = cancelled = 0
    for item in prepared:
        outcome = outcomes.get(item.subtask_id) or _rejected_outcome(item, "internal_error_missing_outcome")
        results_list.append(outcome)
        status = outcome["fanout_status"]
        if status == "succeeded":
            succeeded += 1
        elif status == "cancelled":
            cancelled += 1
            failures_list.append(outcome)
        else:
            failed += 1
            failures_list.append(outcome)

    if unique_count == 0:
        status = "failed"
    elif succeeded == unique_count:
        status = "success"
    elif succeeded > 0:
        status = "partial_success"
    elif failed > 0:
        status = "failed"
    elif cancelled > 0:
        status = "cancelled"
    else:
        status = "failed"

    manifest = {
        "schema": FANOUT_RESULT_SCHEMA,
        "status": status,
        "ok": status == "success",
        "parent_run_id": parent_run_id,
        "counts": {
            "requested": requested_count,
            "unique": unique_count,
            "succeeded": succeeded,
            "failed": failed,
            "cancelled": cancelled,
        },
        "results": results_list,
        "failures": failures_list,
        "deduplicated_aliases": deduplicated_aliases,
        "run_dir": str(run_dir),
    }
    manifest_tmp = run_dir / ".manifest.json.tmp"
    manifest_final = run_dir / "manifest.json"
    manifest_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.chmod(manifest_tmp, 0o600)
    os.replace(manifest_tmp, manifest_final)
    manifest["manifest_path"] = str(manifest_final)
    return manifest


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-file", required=True, type=Path)
    parser.add_argument("--output-file", required=True, type=Path)
    parser.add_argument("--audit-log", required=False, type=Path, default=None)
    parser.add_argument("--run-dir", required=False, type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        request = json.loads(args.request_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[fan-out-orchestrator] error: cannot load request file: {exc}")
        return 1
    if not isinstance(request, dict):
        print("[fan-out-orchestrator] error: request file must contain a JSON object")
        return 1
    result = run_fanout(request, audit_log_path=args.audit_log, run_dir=args.run_dir)
    args.output_file.write_text(json.dumps(result, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(f"[fan-out-orchestrator] status={result.get('status')} counts={result.get('counts')}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
