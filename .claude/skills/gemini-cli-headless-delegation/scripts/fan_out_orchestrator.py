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

Two distinct identifiers exist per subtask (iteration 3 / review Blocker 1):
``subtask_id`` is the caller-facing *logical* identifier (validated to a safe
charset, but never used to build a filesystem path or dict key that could
alias another subtask's), and ``artifact_stem`` is an orchestrator-generated,
filesystem-safe stem (``{index:04d}-{fingerprint[:16]}``) used for request/
result file names and the in-process child-process registry key. This
separation makes path traversal via a malicious ``subtask_id`` (e.g.
``"../../outside"``) structurally impossible and removes duplicate-``subtask_id``
collisions in the process registry (which previously risked leaking an
un-terminated child past ``overall_timeout_sec``).

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
import datetime
import hashlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

FANOUT_REQUEST_SCHEMA = "delegation_fanout_request_v1"
FANOUT_RESULT_SCHEMA = "delegation_fanout_result_v1"

# ---------------------------------------------------------------------------
# Process lifecycle telemetry (Issue #1707 -- review Blocker 2 replacement
# for the previous subtask_started-event-order-only parallelism claim).
# ---------------------------------------------------------------------------

# journal event schema for process-start / process-exit records emitted by
# make_subprocess_runner(). Distinct from FANOUT_RESULT_SCHEMA / the existing
# subtask_started / subtask_finished audit-correlation events -- this is a
# purely additive journal event, the public request/result schemas are
# unchanged.
PROCESS_LIFECYCLE_SCHEMA = "process_lifecycle_event_v1"

# process_role distinguishes the run_gemini_headless.py wrapper subprocess
# that make_subprocess_runner() spawns (delegation_wrapper) from the actual
# provider CLI process it may invoke internally (provider_cli, e.g. the
# `agy` binary). Issue #1707 implements delegation_wrapper telemetry only:
# run_gemini_headless.py itself spawns the `agy` CLI via subprocess.run()
# internally (see _run_agy()), and that spawn point is outside this Issue's
# Allowed Paths (scripts/run_gemini_headless.py), so provider_cli events are
# not emitted by this module yet -- see the Issue #1707 "Remaining Parent
# Gaps" / Stop Conditions for the deferred follow-up. The constants are
# still exposed so overlap/validator logic never hard-codes a single role.
PROCESS_ROLE_DELEGATION_WRAPPER = "delegation_wrapper"
PROCESS_ROLE_PROVIDER_CLI = "provider_cli"

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

# Iteration 3 / review Major 4: bound the number of *raw* (pre-dedup)
# subtasks that ever enter the (potentially expensive: file-hashing,
# validate_request) preparation pipeline, independent of and before
# max_subtasks/max_total_attempts (which apply to the post-dedup count).
# Generous multiplier so legitimate duplicate-heavy submissions that would
# collapse well below max_subtasks are not penalized.
_RAW_SUBTASK_HARD_MULTIPLIER = 4

# Iteration 3 / review Major 4: per-file and total context-file byte caps
# applied while computing dedupe fingerprints, so a submission cannot force
# the orchestrator to read unbounded amounts of disk data before any
# execution-control limit has had a chance to reject it. Once a file (or the
# running total) exceeds these caps, its dedupe fingerprint degrades from a
# content hash to a (path, size) hash -- still deterministic, but no longer
# content-based for that file.
_MAX_CONTEXT_FILE_BYTES = 10 * 1024 * 1024
_MAX_TOTAL_CONTEXT_BYTES = 50 * 1024 * 1024

# subtask_id charset (Issue #1273 iteration 3 Blocker 1): first character
# alphanumeric, remainder alphanumeric/underscore/dot/hyphen. This excludes
# '/', '\', and any control character by construction, and excludes a
# leading '.' so a bare "." or ".." (or any "../..." prefix) can never match.
_SUBTASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SUBTASK_ID_MAX_LEN = 128


@dataclass(frozen=True)
class ChildJob:
    """Everything a runner needs to execute one subtask.

    ``subtask_id`` is the caller-facing logical id (safe to log / echo back
    in results, but must never be used to build a filesystem path or a
    process-registry key -- use ``artifact_stem`` for that).
    """

    subtask_id: str
    artifact_stem: str
    request: dict[str, Any]


RunnerFn = Callable[[ChildJob, "RunnerContext"], dict]

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
# subtask_id validation (Issue #1273 iteration 3 Blocker 1)
# ---------------------------------------------------------------------------


def _validate_subtask_id(value: Any) -> str | None:
    """Return an error message, or None if ``value`` is a safe subtask_id."""
    if not isinstance(value, str):
        return "subtask_id must be a string"
    if not value:
        return "subtask_id must not be empty"
    if len(value) > _SUBTASK_ID_MAX_LEN:
        return f"subtask_id must be at most {_SUBTASK_ID_MAX_LEN} characters"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return "subtask_id must not contain control characters"
    if not _SUBTASK_ID_RE.match(value):
        return (
            "subtask_id must match ^[A-Za-z0-9][A-Za-z0-9_.-]*$ "
            "(no path separators, no leading '.')"
        )
    return None


def _resolve_subtask_id(raw: Mapping[str, Any], index: int) -> str:
    value = raw.get("subtask_id")
    if isinstance(value, str) and value:
        return value
    return f"subtask-{index}"


# ---------------------------------------------------------------------------
# delegation_fanout_request_v1 validation (AC3)
# ---------------------------------------------------------------------------


def validate_fanout_request(request: Mapping[str, Any]) -> list[str]:
    """Fail-closed validator for delegation_fanout_request_v1.

    Enforces a *closed* top-level schema (unknown keys rejected) and that
    ``subtasks[]`` is a non-empty list of dict-like ``delegation_request_v1``
    payloads that do not themselves declare ``subtasks`` (planner mode /
    recursive fan-out is out of scope for v1). Also validates ``subtask_id``
    charset/length when present and rejects duplicate (explicit or
    default-resolved) ``subtask_id`` values across the whole request
    (Issue #1273 iteration 3 Blocker 1).
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

    resolved_ids: list[str] = []
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
        if "subtask_id" in subtask:
            id_error = _validate_subtask_id(subtask.get("subtask_id"))
            if id_error:
                errors.append(f"subtasks[{idx}].subtask_id: {id_error}")
        resolved_ids.append(_resolve_subtask_id(subtask, idx))

    duplicate_counts = Counter(resolved_ids)
    duplicate_ids = sorted(sid for sid, count in duplicate_counts.items() if count > 1)
    if duplicate_ids:
        errors.append(
            f"duplicate subtask_id(s) after resolution (explicit or default 'subtask-<index>'): {duplicate_ids}"
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
# Exact dedupe (AC5) -- runs only over already-validated raw subtasks
# (Issue #1273 iteration 3 Blocker 2: see _prepare_and_validate_subtasks)
# ---------------------------------------------------------------------------


@dataclass
class PreparedSubtask:
    subtask_id: str
    original_ids: list[str]
    request: dict[str, Any]
    fingerprint: str
    artifact_stem: str


def _hash_context_file(path_str: str, base_dir: Path, budget: dict[str, int]) -> str:
    candidate = Path(path_str)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    try:
        size = candidate.stat().st_size
    except OSError:
        return f"unreadable:{path_str}"

    if size > _MAX_CONTEXT_FILE_BYTES or size > budget["remaining"]:
        # Iteration 3 / review Major 4: degrade to a (path, size) hash rather
        # than reading unbounded bytes. Still deterministic for dedupe
        # purposes, but no longer content-based for this file.
        digest = hashlib.sha256()
        digest.update(b"oversized-or-budget-exceeded\0")
        digest.update(path_str.encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        return digest.hexdigest()

    try:
        data = candidate.read_bytes()
    except OSError:
        return f"unreadable:{path_str}"
    budget["remaining"] -= size
    digest = hashlib.sha256()
    digest.update(path_str.encode("utf-8", errors="replace"))
    digest.update(b"\0")
    digest.update(data)
    return digest.hexdigest()


# Purely correlational fields excluded from the dedupe fingerprint. Nothing
# else is excluded (Issue #1273 iteration 3 Blocker 2): the fingerprint is
# computed over the *entire validated* subtask contract (schema,
# post_to_issue_url, model, role, timeout_sec, gh_commands, etc. are all
# included), not a hand-picked subset, so a dangerous subtask can never
# fingerprint-collide with (and be silently dropped as an alias of) a safe
# one just because some field wasn't in the hashed subset.
_FINGERPRINT_EXCLUDED_KEYS = frozenset({"subtask_id"})


def _subtask_fingerprint(subtask: Mapping[str, Any], base_dir: Path, budget: dict[str, int]) -> str:
    """Exact-match fingerprint (Issue #1273 AC5, revised iteration 3 Blocker 2).

    Canonical JSON (RFC 8785-style: sorted keys, compact separators,
    ensure_ascii) of the full validated subtask contract, with
    ``context_files`` path strings replaced by their content hashes (so two
    subtasks referencing the same file via different paths but identical
    bytes still dedupe) and only ``subtask_id`` excluded as pure correlation
    metadata. Semantic/fuzzy dedupe is explicitly out of scope for v1.
    """
    context_files = subtask.get("context_files")
    context_hashes: list[Any] | None = None
    if isinstance(context_files, list):
        context_hashes = [
            _hash_context_file(p, base_dir, budget) if isinstance(p, str) else None for p in context_files
        ]

    payload = {k: v for k, v in subtask.items() if k not in _FINGERPRINT_EXCLUDED_KEYS}
    if context_hashes is not None:
        payload["context_files"] = context_hashes

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _prepare_subtasks(
    validated: list[tuple[str, dict[str, Any]]], base_dir: Path
) -> tuple[list[PreparedSubtask], dict[str, list[str]]]:
    """Fold exact duplicates together over an already safety-validated list.

    ``validated`` is a list of ``(subtask_id, raw)`` pairs where every
    ``raw`` has already passed ``_child_safety_and_compatibility_errors``
    (Issue #1273 iteration 3 Blocker 2 -- dedup never runs on unvalidated
    input, so an unsafe subtask can never enter this function at all).

    Returns (unique subtasks in first-seen order, deduplicated_aliases map of
    kept subtask_id -> [alias subtask_ids folded into it]).
    """
    budget: dict[str, int] = {"remaining": _MAX_TOTAL_CONTEXT_BYTES}
    seen: dict[str, PreparedSubtask] = {}
    order: list[str] = []
    deduplicated_aliases: dict[str, list[str]] = {}
    for idx, (subtask_id, raw) in enumerate(validated):
        fingerprint = _subtask_fingerprint(raw, base_dir, budget)
        if fingerprint in seen:
            kept = seen[fingerprint]
            deduplicated_aliases.setdefault(kept.subtask_id, []).append(subtask_id)
            kept.original_ids.append(subtask_id)
            continue
        # artifact_stem (Blocker 1): filesystem-safe, generated -- never
        # derived from caller-controlled subtask_id text.
        artifact_stem = f"{idx:04d}-{fingerprint[:16]}"
        prepared = PreparedSubtask(
            subtask_id=subtask_id,
            original_ids=[subtask_id],
            request=raw,
            fingerprint=fingerprint,
            artifact_stem=artifact_stem,
        )
        seen[fingerprint] = prepared
        order.append(fingerprint)
    return [seen[fp] for fp in order], deduplicated_aliases


# ---------------------------------------------------------------------------
# Preflight: provider/profile compatibility + child safety (AC7, AC12)
# ---------------------------------------------------------------------------


def _child_safety_and_compatibility_errors(subtask: Mapping[str, Any], rgh: Any) -> list[str]:
    """Fail-closed preflight for a single raw subtask.

    Runs entirely *before* any provider is invoked, and -- as of iteration 3
    Blocker 2 -- *before dedupe*, over every raw leaf individually, so a
    dangerous subtask can never be folded away as a duplicate alias of a
    safe one and skip this check. Rejects, without ever starting a child
    process:
      - post_to_issue_url (GitHub write mutation) -- AC12
      - recursive fan-out (already caught by validate_fanout_request, but
        re-checked per-subtask here defensively) -- AC12
      - gh_commands entries that fail the same read-only argv allowlist used
        by github_research validation, applied uniformly regardless of
        tool_profile (delegation_request_v1 has no other structured shell/
        file-mutation channel, so this closes the write-mutation gap for
        non-github_research profiles too) -- AC12
      - provider="auto" (iteration 3 Blocker 3 -- see rationale inline below)
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
    elif provider == "auto":
        # Iteration 3 Blocker 3 (deliberate v1 design decision, documented
        # per reviewer request rather than implementing full per-attempt
        # budget accounting): provider="auto" internally re-enters
        # run_delegation() once per candidate provider in
        # PROVIDER_AUTO_RUNTIME_ORDER (gemini, then agy fallback) *inside a
        # single child subprocess*. From the orchestrator's point of view
        # that subprocess still consumes exactly one max_total_attempts slot
        # and exactly one "auto" provider-semaphore slot -- but it may
        # actually invoke both the gemini and agy CLIs underneath, which are
        # not separately budgeted. Building real per-attempt budget handoff
        # would require the orchestrator to intercept run_delegation()'s
        # internal provider_auto_dispatch() loop (a non-trivial coupling
        # into run_gemini_headless.py's retry machinery), which is out of
        # proportion for a fix_delta iteration. Banning provider="auto" for
        # fan-out children removes the specific double-counting/semaphore-
        # bypass risk; callers that want provider fallback can submit
        # separate gemini and agy subtasks explicitly instead.
        errors.append(
            "provider_profile_incompatible: provider=auto is forbidden for fan-out children in v1 "
            "-- its internal gemini-then-agy fallback attempts are not accounted for by "
            "max_total_attempts / per-provider semaphores; submit explicit gemini/agy subtasks instead"
        )
    elif provider == "agy" and tool_profile not in rgh.AGY_SUPPORTED_PROFILES:
        errors.append(
            f"provider_profile_incompatible: provider=agy does not support tool_profile={tool_profile!r}"
        )

    validation_errors = rgh.validate_request(subtask)
    errors.extend(f"validation_error: {msg}" for msg in validation_errors)
    return errors


def _prepare_and_validate_subtasks(
    subtasks: list[Mapping[str, Any]],
    base_dir: Path,
    rgh: Any,
) -> tuple[list[PreparedSubtask], dict[str, list[str]], dict[str, dict]]:
    """Validate + safety-check every raw leaf, THEN dedupe (Issue #1273
    iteration 3 Blocker 2). Returns (unique validated subtasks,
    deduplicated_aliases, rejected outcomes keyed by subtask_id).
    """
    validated_raw: list[tuple[str, dict[str, Any]]] = []
    rejected: dict[str, dict] = {}
    for idx, raw in enumerate(subtasks):
        subtask_id = _resolve_subtask_id(raw, idx)
        stamped_raw = dict(raw)
        stamped_raw.setdefault("subtask_id", subtask_id)
        problems = _child_safety_and_compatibility_errors(stamped_raw, rgh)
        if problems:
            rejected[subtask_id] = {
                "subtask_id": subtask_id,
                "original_ids": [subtask_id],
                "fanout_status": "failed",
                "result": None,
                "reasons": ["preflight_rejected"] + problems,
            }
            continue
        validated_raw.append((subtask_id, stamped_raw))

    prepared, deduplicated_aliases = _prepare_subtasks(validated_raw, base_dir)
    return prepared, deduplicated_aliases, rejected


# ---------------------------------------------------------------------------
# Child execution: runner protocol + default subprocess runner (AC4, AC8)
# ---------------------------------------------------------------------------


@dataclass
class RunnerContext:
    """Shared, thread-safe state handed to every runner invocation."""

    run_dir: Path
    audit_log_path: Path | None
    cancel_event: threading.Event
    # Issue #1707: optional sink for process lifecycle telemetry events
    # (schema PROCESS_LIFECYCLE_SCHEMA). Defaults to None so existing callers
    # that construct a RunnerContext directly (tests, other integrations)
    # keep working unchanged -- when None, make_subprocess_runner() simply
    # does not emit lifecycle events. run_fanout() always wires this to its
    # single-writer ``_journal`` closure.
    journal: Callable[[dict[str, Any]], None] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _processes: dict[str, subprocess.Popen] = field(default_factory=dict)

    def register_process(self, artifact_stem: str, proc: subprocess.Popen) -> None:
        with self._lock:
            self._processes[artifact_stem] = proc

    def unregister_process(self, artifact_stem: str) -> None:
        with self._lock:
            self._processes.pop(artifact_stem, None)

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


def _write_ndjson_record(fd: int, line: bytes, journal_path: Path) -> None:
    """Write one NDJSON record and verify the full record was written.

    Iteration 3 Major 5: a previous version ignored os.write()'s return
    value (the actual number of bytes written), which for a *regular* local
    file is virtually always the full length but is not guaranteed by
    POSIX -- a short write would silently corrupt the journal for every
    subsequent reader. Extracted as a standalone, unit-testable function
    (rather than left inline in the run_fanout() closure) so the partial-
    write path can be exercised directly with a monkeypatched os.write.
    """
    written = os.write(fd, line)
    if written != len(line):
        raise OSError(f"partial NDJSON journal write: wrote {written} of {len(line)} bytes to {journal_path}")


def _utc_now_iso() -> str:
    """Timezone-aware UTC ISO-8601 timestamp for process lifecycle events.

    Wall-clock only (human-readable correlation); overlap detection itself
    uses the monotonic clock fields (``*_monotonic_ns``), never this value.
    """
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _public_safe_executable_identity(path: str | Path) -> str:
    """Redact an executable path to a public-safe identity (Issue #1707 AC10).

    Process lifecycle events are written to the run's NDJSON journal, which
    may end up in artifacts/logs read outside the local machine's trust
    boundary -- so the ``executable`` field must never leak the local
    filesystem's absolute path layout (home directory names, worktree
    paths, etc). basename is deterministic, sufficient to identify *what*
    ran, and carries no local-path information.
    """
    return os.path.basename(str(path)) or "unknown"


def _classify_termination_reason(returncode: int | None, *, timed_out: bool) -> str:
    """Classify how a reaped child process ended (Issue #1707 AC3/AC6).

    Negative POSIX returncodes are classified by signal number regardless
    of which code path observed the timeout locally: ``run_fanout()``'s
    deadline watcher thread (``ctx.terminate_all()``) and this runner's own
    cancel_event poll loop both call ``_terminate_process_group()`` against
    the *same* registered process, so either one may win the race to
    SIGTERM/SIGKILL and reap it -- the resulting returncode is what matters,
    not which thread observed it first. ``timed_out`` (this runner's own
    local observation of ``ctx.cancel_event``) is only used as a fallback
    label for the rare case of a non-negative returncode reaped after a
    timeout was observed.
    """
    if returncode is None:
        return "unknown_not_reaped"
    if returncode == 0:
        return "exited_normally"
    if returncode < 0:
        try:
            sig = signal.Signals(-returncode)
        except ValueError:
            return f"signal_{-returncode}"
        if sig == signal.SIGTERM:
            return "sigterm"
        if sig == signal.SIGKILL:
            return "sigkill"
        return f"signal_{sig.name.lower()}"
    if timed_out:
        return "timeout_terminated"
    return "exited_nonzero"


def _emit_process_lifecycle_event(ctx: RunnerContext, event_name: str, fields: Mapping[str, Any]) -> None:
    """Write one process lifecycle event through ``ctx.journal`` (Issue #1707
    AC1/AC2). No-op when ``ctx.journal`` is unset (e.g. tests that construct
    a bare RunnerContext without wiring a journal sink).
    """
    if ctx.journal is None:
        return
    record: dict[str, Any] = {"schema": PROCESS_LIFECYCLE_SCHEMA, "event": event_name}
    record.update(fields)
    ctx.journal(record)


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
    and can terminate already-running ones. Uses ``job.artifact_stem`` (never
    ``job.subtask_id``) for all file names and the process-registry key.
    """
    executable = python_executable or sys.executable

    def _runner(job: ChildJob, ctx: RunnerContext) -> dict:
        if ctx.cancel_event.is_set():
            return _synthetic_failure_result(
                job.request, "overall_timeout_pending_cancelled", "cancelled before start"
            )

        req_path = ctx.run_dir / f"{job.artifact_stem}.request.json"
        out_path = ctx.run_dir / f"{job.artifact_stem}.result.json"
        req_path.write_text(json.dumps(job.request), encoding="utf-8")
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

        # Issue #1707: correlation fields shared by both the process-start
        # and process-exit lifecycle events for this spawn. Pulled from
        # job.request (stamped by run_fanout()._run_one()) with defensive
        # .get() fallbacks so a bare RunnerContext/ChildJob built directly by
        # a test (without going through run_fanout()) never raises here.
        process_role = PROCESS_ROLE_DELEGATION_WRAPPER
        provider = str(job.request.get("provider") or "unknown")
        parent_run_id = job.request.get("parent_run_id")
        subtask_id = job.subtask_id
        attempt_id = job.request.get("attempt_id")
        artifact_stem = job.artifact_stem
        executable_identity = _public_safe_executable_identity(script_path)

        try:
            proc = subprocess.Popen(  # noqa: S603
                argv, cwd=str(ctx.run_dir), start_new_session=True
            )
        except OSError as exc:
            # Issue #1707 AC1: spawn failure must never be recorded as
            # started -- no process-start event is emitted on this path.
            return _synthetic_failure_result(job.request, "child_spawn_failed", str(exc))

        started_monotonic_ns = time.monotonic_ns()
        started_utc = _utc_now_iso()
        pid = proc.pid
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, OSError):
            pgid = None

        _emit_process_lifecycle_event(
            ctx,
            "process_start",
            {
                "process_role": process_role,
                "provider": provider,
                "parent_run_id": parent_run_id,
                "subtask_id": subtask_id,
                "attempt_id": attempt_id,
                "artifact_stem": artifact_stem,
                "pid": pid,
                "pgid": pgid,
                "executable": executable_identity,
                "started_monotonic_ns": started_monotonic_ns,
                "started_utc": started_utc,
            },
        )

        ctx.register_process(job.artifact_stem, proc)
        timed_out = False
        try:
            while True:
                if ctx.cancel_event.is_set():
                    timed_out = True
                    _terminate_process_group(proc, _CHILD_TERMINATE_GRACE_SEC)
                    # _terminate_process_group() only signals -- reap here so
                    # proc.returncode / the exit event reflect the real
                    # outcome (SIGTERM success vs SIGKILL escalation).
                    try:
                        proc.wait(timeout=_CHILD_TERMINATE_GRACE_SEC + 2.0)
                    except subprocess.TimeoutExpired:
                        pass
                    break
                try:
                    proc.wait(timeout=_CHILD_POLL_INTERVAL_SEC)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            ctx.unregister_process(job.artifact_stem)

        exited_monotonic_ns = time.monotonic_ns()
        exited_utc = _utc_now_iso()
        returncode = proc.returncode
        termination_reason = _classify_termination_reason(returncode, timed_out=timed_out)

        # Issue #1707 AC2: process-exit event recorded once, right after
        # reap, regardless of which branch above detected the exit.
        _emit_process_lifecycle_event(
            ctx,
            "process_exit",
            {
                "process_role": process_role,
                "provider": provider,
                "parent_run_id": parent_run_id,
                "subtask_id": subtask_id,
                "attempt_id": attempt_id,
                "artifact_stem": artifact_stem,
                "pid": pid,
                "pgid": pgid,
                "executable": executable_identity,
                "exited_monotonic_ns": exited_monotonic_ns,
                "exited_utc": exited_utc,
                "returncode": returncode,
                "termination_reason": termination_reason,
            },
        )

        if timed_out:
            return _synthetic_failure_result(
                job.request, "overall_timeout_terminated", "terminated due to overall_timeout_sec"
            )

        if ctx.cancel_event.is_set():
            # Raced with the deadline: the child finished, but too late to be
            # honored -- discard whatever it returned (Issue #1273 AC8).
            return _synthetic_failure_result(
                job.request, "overall_timeout_late_result_discarded", "child result discarded (raced with timeout)"
            )

        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _synthetic_failure_result(job.request, "child_output_unreadable", str(exc))
        if not isinstance(data, dict):
            return _synthetic_failure_result(
                job.request, "child_output_unreadable", "output file did not contain an object"
            )
        return data

    return _runner


# ---------------------------------------------------------------------------
# Process lifecycle event pairing + overlap validator (Issue #1707 AC4-AC6)
# ---------------------------------------------------------------------------


def build_process_lifecycle_pairs(events: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pair ``process_start`` / ``process_exit`` journal events by
    ``artifact_stem`` into complete lifecycle intervals.

    Only ``PROCESS_LIFECYCLE_SCHEMA`` events are considered (the existing
    ``subtask_started`` / ``subtask_finished`` audit-correlation events, and
    anything malformed, are ignored). ``artifact_stem`` is the pairing key
    because it is the orchestrator-generated, per-spawn-unique registry key
    (never reused within a run -- unlike ``pid``, which the OS can recycle).

    A start event with no matching exit event (crash, truncated journal,
    process still running when the journal was read) -- or an exit event
    with no matching start (malformed/out-of-order journal) -- is dropped
    rather than raising (Issue #1707 AC8g: the validator must handle
    malformed/missing exit events safely, not crash).
    """
    starts: dict[str, dict[str, Any]] = {}
    pairs: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("schema") != PROCESS_LIFECYCLE_SCHEMA:
            continue
        stem = event.get("artifact_stem")
        if not isinstance(stem, str) or not stem:
            continue
        kind = event.get("event")
        if kind == "process_start":
            if isinstance(event.get("started_monotonic_ns"), int):
                starts[stem] = dict(event)
            continue
        if kind == "process_exit":
            start = starts.pop(stem, None)
            if start is None:
                continue
            if not isinstance(event.get("exited_monotonic_ns"), int):
                continue
            merged = dict(start)
            merged.update({k: v for k, v in event.items() if k not in ("event", "schema")})
            pairs.append(merged)
    return pairs


def process_lifecycle_intervals_overlap(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Pure overlap predicate for two process lifecycle intervals (Issue
    #1707 AC4): the intervals ``[started_monotonic_ns, exited_monotonic_ns)``
    overlap iff the later of the two starts is strictly before the earlier
    of the two ends.
    """
    return max(a["started_monotonic_ns"], b["started_monotonic_ns"]) < min(
        a["exited_monotonic_ns"], b["exited_monotonic_ns"]
    )


def actual_provider_process_overlap(pairs: list[Mapping[str, Any]]) -> bool:
    """Return True iff at least one pair of *distinct-pid, distinct-subtask*
    process lifecycle intervals actually overlapped in wall/monotonic time
    (Issue #1707 AC5).

    Same-``subtask_id`` intervals (e.g. a wrapper process and a future
    provider_cli process for the same subtask) and same-``pid`` intervals
    (PID reuse across two spawns) are never treated as evidence of parallel
    execution on their own -- only overlap between two genuinely distinct
    process identities/subtasks counts.
    """
    n = len(pairs)
    for i in range(n):
        a = pairs[i]
        a_pid = a.get("pid")
        a_subtask = a.get("subtask_id")
        for j in range(i + 1, n):
            b = pairs[j]
            if a_pid is not None and a_pid == b.get("pid"):
                continue
            if a_subtask is not None and a_subtask == b.get("subtask_id"):
                continue
            if process_lifecycle_intervals_overlap(a, b):
                return True
    return False


def validate_fanout_parallelism(events: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Replace the previous ``subtask_started``-event-order-only parallelism
    claim (Issue #1707 review Blocker 2) with a validator grounded in actual
    process lifecycle telemetry.

    Journal-append ordering of ``subtask_started`` events across threads
    does not guarantee wall-clock overlap (thread scheduling can interleave
    log lines for subtasks that never actually ran concurrently), so a FAIL
    is returned whenever multiple ``subtask_started`` events are present but
    ``actual_provider_process_overlap()`` cannot confirm real process
    overlap from the paired lifecycle events (Issue #1707 AC6).
    """
    subtask_started_count = sum(
        1 for event in events if isinstance(event, Mapping) and event.get("event") == "subtask_started"
    )
    pairs = build_process_lifecycle_pairs(events)
    overlap = actual_provider_process_overlap(pairs)

    if subtask_started_count <= 1:
        status = "not_applicable"
    elif overlap:
        status = "pass"
    else:
        status = "fail"

    return {
        "status": status,
        "actual_provider_process_overlap": overlap,
        "subtask_started_count": subtask_started_count,
        "process_lifecycle_pair_count": len(pairs),
    }


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


def _fanout_raw_overflow_result(parent_run_id: str, requested_count: int, raw_cap: int, run_dir: Path) -> dict:
    """Issue #1273 iteration 3 Major 4: fail-closed *before* any per-subtask
    validation/hashing work when the raw (pre-dedup) subtask count exceeds a
    hard defensive multiplier of max_subtasks/max_total_attempts.
    """
    reason = f"raw_subtasks_exceeded_hard_cap: requested={requested_count} raw_cap={raw_cap}"
    return {
        "schema": FANOUT_RESULT_SCHEMA,
        "status": "failed",
        "ok": False,
        "parent_run_id": parent_run_id,
        "counts": {
            "requested": requested_count,
            "unique": 0,
            "succeeded": 0,
            "failed": requested_count,
            "cancelled": 0,
        },
        "results": [],
        "failures": [{"subtask_id": None, "fanout_status": "failed", "result": None, "reasons": [reason]}],
        "deduplicated_aliases": {},
        "run_dir": str(run_dir),
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
                _write_ndjson_record(fd, line, journal_path)
            finally:
                os.close(fd)

    cancel_event = threading.Event()
    ctx = RunnerContext(
        run_dir=run_dir, audit_log_path=audit_log_path, cancel_event=cancel_event, journal=_journal
    )
    all_done_event = threading.Event()

    def _watch_deadline() -> None:
        finished_in_time = all_done_event.wait(timeout=overall_timeout_sec)
        if not finished_in_time:
            cancel_event.set()
            _journal({"event": "overall_timeout_reached", "parent_run_id": parent_run_id})
            ctx.terminate_all()

    # Iteration 3 Major 4: the overall_timeout_sec deadline starts counting
    # from here -- run_fanout() entry, after only cheap top-level request
    # validation -- not after the (now safety-validation-heavy) dedup/
    # preflight pipeline below.
    watcher = threading.Thread(target=_watch_deadline, daemon=True)
    watcher.start()

    try:
        raw_cap = max(max_subtasks, max_total_attempts) * _RAW_SUBTASK_HARD_MULTIPLIER
        if requested_count > raw_cap:
            return _fanout_raw_overflow_result(parent_run_id, requested_count, raw_cap, run_dir)

        rgh = _load_run_gemini_headless_module()

        outcomes: dict[str, dict] = {}

        # Blocker 2: validate + safety-check every raw leaf BEFORE dedupe.
        prepared, deduplicated_aliases, rejected = _prepare_and_validate_subtasks(subtasks_in, base_dir, rgh)
        outcomes.update(rejected)

        # Build the ordered list of *logical* entities that appear in
        # results[] -- one entry per unique subtask_id in original raw-input
        # order, EXCLUDING alias ids that were folded into a kept prepared
        # entry by dedupe (those live only in that entry's original_ids /
        # deduplicated_aliases, not as their own results[] row). subtask_id
        # duplicates across raw input are already fail-closed rejected by
        # validate_fanout_request(), so every resolved id here is distinct.
        alias_ids = {alias for aliases in deduplicated_aliases.values() for alias in aliases}
        ordered_logical_ids: list[str] = []
        for idx, raw in enumerate(subtasks_in):
            sid = _resolve_subtask_id(raw, idx)
            if sid in alias_ids:
                continue
            ordered_logical_ids.append(sid)
        unique_count = len(ordered_logical_ids)

        # max_subtasks (AC6): reject overflow beyond the unique-subtask cap
        # without ever spawning a child for them.
        if unique_count > max_subtasks:
            eligible = prepared[:max_subtasks]
            for extra in prepared[max_subtasks:]:
                outcomes[extra.subtask_id] = _rejected_outcome(extra, "max_subtasks_exceeded")
        else:
            eligible = prepared

        # max_total_attempts (AC6): a v1 "attempt" is one child subprocess
        # spawn (no orchestrator-level retry in v1; provider="auto" is
        # banned above at preflight, so a spawn cannot silently fan out into
        # multiple uncounted provider attempts -- see Blocker 3 rationale).
        runnable = eligible
        if len(runnable) > max_total_attempts:
            overflow = runnable[max_total_attempts:]
            runnable = runnable[:max_total_attempts]
            for extra in overflow:
                outcomes[extra.subtask_id] = _rejected_outcome(extra, "max_total_attempts_exceeded")

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
            job = ChildJob(subtask_id=subtask_id, artifact_stem=item.artifact_stem, request=stamped_request)

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
                        {
                            "event": "subtask_cancelled_before_start",
                            "subtask_id": subtask_id,
                            "artifact_stem": item.artifact_stem,
                            "parent_run_id": parent_run_id,
                        }
                    )
                    return
                _journal(
                    {
                        "event": "subtask_started",
                        "subtask_id": subtask_id,
                        "artifact_stem": item.artifact_stem,
                        "parent_run_id": parent_run_id,
                    }
                )
                try:
                    result = active_runner(job, ctx)
                except Exception as exc:  # pylint: disable=broad-except
                    result = _synthetic_failure_result(stamped_request, "runner_exception", str(exc))

            outcomes[subtask_id] = _finalize_outcome(item, result, cancel_event.is_set())
            _journal(
                {
                    "event": "subtask_finished",
                    "subtask_id": subtask_id,
                    "artifact_stem": item.artifact_stem,
                    "parent_run_id": parent_run_id,
                    "fanout_status": outcomes[subtask_id]["fanout_status"],
                }
            )

        # Iteration 3 Blocker 4: manage the executor explicitly (no `with`
        # block) so a KeyboardInterrupt raised out of concurrent.futures.wait()
        # reaches our handler *before* any implicit shutdown(wait=True) would
        # block on hung futures. `ThreadPoolExecutor.__exit__` calls
        # shutdown(wait=True) unconditionally, which -- inside a `with`
        # block -- runs BEFORE an enclosing `except KeyboardInterrupt` gets
        # control, defeating fast cancellation entirely.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_workers))
        futures = [pool.submit(_run_one, item) for item in runnable]
        try:
            concurrent.futures.wait(futures)
        except KeyboardInterrupt:
            cancel_event.set()
            ctx.terminate_all()
            for fut in futures:
                fut.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown(wait=True)
    finally:
        all_done_event.set()
        watcher.join(timeout=2)

    results_list: list[dict] = []
    failures_list: list[dict] = []
    succeeded = failed = cancelled = 0
    for subtask_id in ordered_logical_ids:
        outcome = outcomes.get(subtask_id) or {
            "subtask_id": subtask_id,
            "original_ids": [subtask_id],
            "fanout_status": "failed",
            "result": None,
            "reasons": ["internal_error_missing_outcome"],
        }
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

    # Iteration 3 Major 1: resolve manifest_path BEFORE serialization, so the
    # dict written to disk is byte-identical (post round-trip) to the dict
    # returned to the caller -- no "append after write" divergence.
    manifest_final = run_dir / "manifest.json"
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
        "manifest_path": str(manifest_final),
    }
    manifest_tmp = run_dir / ".manifest.json.tmp"
    manifest_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.chmod(manifest_tmp, 0o600)
    os.replace(manifest_tmp, manifest_final)
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
