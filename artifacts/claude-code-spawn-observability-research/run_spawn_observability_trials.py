#!/usr/bin/env python3
"""Issue #2013 research harness: Claude Code custom SubAgent spawn observability.

This harness is *research-only*. It never mutates
``scripts/agent-ops/run_worktree_agent_runtime_smoke.py`` or
``scripts/agent-ops/run_agent_provider_route_smoke.py``; it imports their
public helpers read-only so that every classification recorded here is
computed by the *same* production code path under investigation.

Two lanes, both driven by a *fixed* trial plan that is frozen (and digested)
before the first live invocation:

- ``control``: a minimal, offline custom subagent declared inline via
  ``claude --agents``. No external provider, no MCP, no GitHub retrieval.
  Session-local ``--settings`` registers ``SubagentStart``/``SubagentStop``
  no-op logger hooks so both hook lifecycle events are observable, without
  touching the repo-tracked ``.claude/settings.json`` / ``.claude/hooks/**``.
- ``production``: the real ``codebase-investigator`` / ``web-researcher``
  custom agents on current ``main``, driven by the real
  ``build_route_prompt()`` route prompt, with the real gemini sentinel stub
  on ``PATH`` -- exactly as ``_run_route_once()`` does.

Every trial's raw stdout/stderr is persisted under ``raw/`` so that every
derived flag in ``reproduction-log.jsonl`` is independently recomputable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ARTIFACT_DIR = Path(__file__).resolve().parent
REPO_ROOT = ARTIFACT_DIR.parent.parent
AGENT_OPS = REPO_ROOT / "scripts" / "agent-ops"
sys.path.insert(0, str(AGENT_OPS))

import run_agent_provider_route_smoke as route_smoke  # noqa: E402
import run_worktree_agent_runtime_smoke as runtime_smoke  # noqa: E402

RAW_DIR = ARTIFACT_DIR / "raw"
LEDGER_PATH = ARTIFACT_DIR / "reproduction-log.jsonl"
CONTROL_AGENTS_PATH = ARTIFACT_DIR / "control_lane_agents.json"
CONTROL_SETTINGS_PATH = ARTIFACT_DIR / "session_local_settings.json"
TRIAL_PLAN_PATH = ARTIFACT_DIR / "trial-plan.json"

CONTROL_AGENT_NAME = "spawn-probe"
CONTROL_MARKER = "CONTROL_PROBE_DONE"
PRODUCTION_MARKER = "ROUTE_SMOKE_DONE"

# The 12 lifecycle checkpoints recorded per trial (AC2). Frozen: the contract
# test pins this exact set so no future edit can silently collapse a
# checkpoint into another.
LIFECYCLE_CHECKPOINTS = (
    "process_started",
    "system_init_observed",
    "agent_tool_use_observed",
    "subagent_start_hook_observed",
    "subagent_stop_hook_observed",
    "tool_result_observed",
    "tool_result_agent_id_observed",
    "tool_result_agent_type_observed",
    "agent_type_matches_requested",
    "terminal_event_observed",
    "expected_marker_observed",
    "delegation_request_validated",
)

# Extended diagnostic_cause taxonomy (Issue #2013 In Scope). ``None`` is used
# for a passing trial; every failing trial gets exactly one of these.
DIAGNOSTIC_CAUSES = (
    "spawn_not_attempted",
    "subagent_start_not_observed",
    "subagent_completion_timeout",
    "tool_result_identity_not_observed",
    "agent_type_mismatch",
    "runtime_api_retry_timeout",
    "runtime_nonzero",
    "terminal_event_missing",
    "marker_not_observed",
    "request_validation_failed",
    "delegation_wrapper_failed",
    "downstream_route_failed",
)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iter_events(stdout: str):
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict):
            yield payload


# ---------------------------------------------------------------------------
# Raw-evidence probes (all computed from the persisted stdout, never guessed)
# ---------------------------------------------------------------------------

_AGENT_TOOL_NAMES = {"Agent", "Task"}


def observe_agent_tool_use(stdout: str) -> tuple[bool, str | None]:
    """``(dispatch_observed, requested_subagent_type)`` from the assistant
    ``tool_use`` block that dispatches the Agent/Task tool."""
    for payload in _iter_events(stdout):
        if payload.get("type") != "assistant":
            continue
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") not in _AGENT_TOOL_NAMES:
                continue
            tool_input = block.get("input")
            subagent_type = None
            if isinstance(tool_input, dict):
                raw = tool_input.get("subagent_type")
                if isinstance(raw, str) and raw:
                    subagent_type = raw
            return True, subagent_type
    return False, None


def observe_hook_event(stdout: str, hook_event: str) -> bool:
    """True iff a ``system`` stream-json event reports the named hook
    lifecycle event (``--include-hook-events`` surfaces these as
    ``system/hook_started`` and ``system/hook_response`` records carrying a
    ``hook_event`` field)."""
    for payload in _iter_events(stdout):
        if payload.get("type") != "system":
            continue
        if payload.get("hook_event") == hook_event:
            return True
    return False


_HOOK_PAYLOAD_PREFIX = "SPAWN_OBS_HOOK_PAYLOAD "


def observe_hook_identity(stdout: str, hook_event: str) -> dict:
    """Independent identity evidence from the *hook* channel.

    Two sub-channels, both emitted by the runtime itself (never a self-report
    and never an assumed value):

    - ``hook_name``: the runtime labels a per-agent hook invocation as
      ``"<HookEvent>:<agent_type>"`` (observed on ``SubagentStart``).
    - the official hook stdin payload (``agent_id`` / ``agent_type`` /
      ``agent_transcript_path`` / ``stop_reason``), which the session-local
      no-op logger hook echoes back with a non-JSON prefix so it surfaces in
      the ``hook_response.stdout`` field of the stream-json event.
    """
    result: dict = {
        "hook_name_agent_type": None,
        "payload_agent_id": None,
        "payload_agent_type": None,
        "payload_stop_reason": None,
        "payload_observed": False,
    }
    for payload in _iter_events(stdout):
        if payload.get("type") != "system" or payload.get("hook_event") != hook_event:
            continue
        hook_name = payload.get("hook_name")
        if isinstance(hook_name, str) and hook_name.startswith(f"{hook_event}:"):
            suffix = hook_name.split(":", 1)[1].strip()
            if suffix and result["hook_name_agent_type"] is None:
                result["hook_name_agent_type"] = suffix
        stdout_text = payload.get("stdout")
        if not isinstance(stdout_text, str):
            continue
        for chunk in stdout_text.splitlines():
            chunk = chunk.strip()
            if not chunk.startswith(_HOOK_PAYLOAD_PREFIX):
                continue
            try:
                parsed = json.loads(chunk[len(_HOOK_PAYLOAD_PREFIX):])
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(parsed, dict):
                continue
            result["payload_observed"] = True
            for src, dst in (
                ("agent_id", "payload_agent_id"),
                ("agent_type", "payload_agent_type"),
                ("stop_reason", "payload_stop_reason"),
            ):
                value = parsed.get(src)
                if isinstance(value, str) and value and result[dst] is None:
                    result[dst] = value
    return result


def observe_tool_result(stdout: str) -> bool:
    """True iff a ``type: "user"`` event carrying a ``tool_use_result`` object
    (the Agent/Task tool result envelope) is present."""
    for payload in _iter_events(stdout):
        if payload.get("type") != "user":
            continue
        if isinstance(payload.get("tool_use_result"), dict):
            return True
    return False


def count_api_retry_events(stdout: str) -> int:
    """Count of ``system``/``api_retry`` stream-json events."""
    count = 0
    for payload in _iter_events(stdout):
        if payload.get("type") == "system" and payload.get("subtype") == "api_retry":
            count += 1
    return count


# ---------------------------------------------------------------------------
# failure_class: faithful replication of the production evaluation order
# ---------------------------------------------------------------------------


def compute_harness_exit_equivalent(
    *,
    rc: int | None,
    stdout: str,
    stderr: str,
    timed_out: bool,
    expected_marker: str,
) -> tuple[int, list[str]]:
    """Replicate ``run_worktree_agent_runtime_smoke.py``'s structured-lane
    exit-code decision (``EXIT_OK`` / ``EXIT_FAIL`` / ``EXIT_SKIP``) using its
    own imported classifiers, so the ``harness_exit`` fed into the
    ``_run_route_once()`` failure ladder below is the real one."""
    reasons: list[str] = []
    decision, reason = runtime_smoke.classify_claude_structured_outcome(rc, stdout, stderr, timed_out)
    event_count = runtime_smoke.parse_native_event_count(stdout)
    if decision == "capability_skip":
        return runtime_smoke.EXIT_SKIP, [reason or "capability_skip"]
    exit_code = runtime_smoke.EXIT_OK
    if timed_out:
        reasons.append("structured lane timed out")
        exit_code = runtime_smoke.EXIT_FAIL
    elif rc is None:
        reasons.append("structured lane failed to start")
        exit_code = runtime_smoke.EXIT_FAIL
    elif decision == "turn_limit_reached":
        reasons.append(reason or "turn_limit_reached")
        exit_code = runtime_smoke.EXIT_FAIL
    elif rc != 0:
        reasons.append(f"structured lane exited non-zero: {rc}")
        exit_code = runtime_smoke.EXIT_FAIL
    elif event_count > 0 and not runtime_smoke.has_terminal_event("claude", stdout):
        reasons.append("no terminal/result event observed in structured output")
        exit_code = runtime_smoke.EXIT_FAIL
    if expected_marker and expected_marker not in (stdout + "\n" + stderr):
        reasons.append(f"expected markers not observed: ['{expected_marker}']")
        exit_code = runtime_smoke.EXIT_FAIL
    return exit_code, reasons


def compute_failure_class(
    *,
    lane: str,
    profile: str | None,
    gemini_hits: int,
    fallback_hits: int,
    harness_exit: int,
    native_spawn_event_observed: bool,
    request_validation: str,
    selected_provider: str | None,
    route_evidence_sha256: str | None,
    wrapper_ok: bool,
) -> tuple[str, str | None]:
    """``(status, failure_class)`` using the exact ordering of
    ``run_agent_provider_route_smoke._run_route_once()`` (see
    ``code-analysis.md``). Steps (7)-(9) are provider-route specific and are
    skipped for the control lane, which performs no AGY delegation at all."""
    if gemini_hits > 0:
        return "fail", "gemini_invoked"
    if fallback_hits > 0:
        return "fail", "direct_fallback_invoked"
    if harness_exit == 77:
        return "skip", "agy_unavailable"
    if harness_exit != 0:
        return "fail", "validation_failed"
    if not native_spawn_event_observed:
        return "fail", "spawn_not_observed"
    if request_validation != "pass":
        return "fail", "validation_failed"
    if lane == "production":
        if selected_provider != "agy":
            return "fail", "provider_mismatch"
        if profile == "github_research" and route_evidence_sha256 is None:
            return "fail", "route_evidence_schema_mismatch"
        if not wrapper_ok:
            return "fail", "validation_failed"
    return "pass", None


def compute_diagnostic_cause(
    *,
    status: str,
    lifecycle: dict[str, bool],
    rc: int | None,
    timed_out: bool,
    api_retry_count: int,
    downstream: dict,
) -> str | None:
    """Lossless diagnostic classification. Deliberately independent of
    ``failure_class``: a ``validation_failed`` produced by the harness-exit
    branch (step 4) is evaluated *before* spawn evidence in production, so the
    outer failure class alone can never tell whether a spawn happened. This
    ladder reads the raw lifecycle checkpoints instead."""
    if status == "pass":
        return None
    if timed_out and api_retry_count > 0:
        return "runtime_api_retry_timeout"
    if timed_out:
        return "subagent_completion_timeout"
    if not lifecycle["agent_tool_use_observed"]:
        if rc is None or rc != 0:
            return "runtime_nonzero"
        return "spawn_not_attempted"
    if not lifecycle["tool_result_observed"] and not lifecycle["subagent_start_hook_observed"]:
        return "subagent_start_not_observed"
    if not lifecycle["tool_result_observed"]:
        return "subagent_completion_timeout"
    if not (
        lifecycle["tool_result_agent_id_observed"] and lifecycle["tool_result_agent_type_observed"]
    ):
        return "tool_result_identity_not_observed"
    if not lifecycle["agent_type_matches_requested"]:
        return "agent_type_mismatch"
    if rc is None or rc != 0:
        return "runtime_nonzero"
    if not lifecycle["terminal_event_observed"]:
        return "terminal_event_missing"
    if not lifecycle["delegation_request_validated"]:
        return "request_validation_failed"
    if not lifecycle["expected_marker_observed"]:
        return "marker_not_observed"
    if downstream.get("wrapper_ok") is False:
        return "delegation_wrapper_failed"
    return "downstream_route_failed"


# ---------------------------------------------------------------------------
# Trial plan (frozen before the first live invocation)
# ---------------------------------------------------------------------------


def build_trial_plan() -> dict:
    """The fixed 30-trial plan. Nothing here may be re-tuned after the first
    live trial: the plan digest is stamped into every record."""
    trials: list[dict] = []
    for index in range(1, 16):
        trials.append(
            {
                "trial_id": f"control-{index:02d}",
                "lane": "control",
                "runtime": "claude_code",
                "agent": CONTROL_AGENT_NAME,
                "profile": None,
                "timeout_seconds": 300,
                "max_turns": 6,
                "expected_marker": CONTROL_MARKER,
            }
        )
    # Fixed, pre-declared production route rotation (round-robin over the three
    # claude_code routes in REQUIRED_ROUTES declaration order): 5 trials each.
    claude_routes = [r for r in route_smoke.REQUIRED_ROUTES if r["runtime"] == "claude_code"]
    for index in range(1, 16):
        route = claude_routes[(index - 1) % len(claude_routes)]
        trials.append(
            {
                "trial_id": f"production-{index:02d}",
                "lane": "production",
                "runtime": "claude_code",
                "agent": route["agent"],
                "profile": route["profile"],
                "timeout_seconds": 600,
                "max_turns": 30,
                "expected_marker": PRODUCTION_MARKER,
            }
        )
    return {
        "schema": "spawn_observability_trial_plan/v1",
        "issue": 2013,
        "control_trial_count": 15,
        "production_trial_count": 15,
        "lifecycle_checkpoints": list(LIFECYCLE_CHECKPOINTS),
        "diagnostic_causes": list(DIAGNOSTIC_CAUSES),
        "trials": trials,
    }


def plan_digest(plan: dict) -> str:
    return _sha256_text(json.dumps(plan, sort_keys=True, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Trial execution
# ---------------------------------------------------------------------------


def build_control_prompt() -> str:
    return (
        f"SPAWN_OBSERVABILITY_CONTROL probe (Issue #2013). Use the Agent tool exactly once "
        f'with subagent_type "{CONTROL_AGENT_NAME}" and the prompt '
        f'"Reply with SPAWN_PROBE_CHILD_DONE". Do not use any other tool, do not access the '
        f"network, and do not read or write any file. After the Agent tool returns, reply with "
        f"exactly one line: {CONTROL_MARKER}"
    )


def _write_gemini_sentinel(bin_dir: Path, marker_path: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "gemini"
    stub.write_text(route_smoke._GEMINI_SENTINEL_TEMPLATE, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def run_trial(spec: dict, *, plan_sha256: str, tested_head_sha: str, claude_version: str,
              worktree: Path) -> dict:
    lane = spec["lane"]
    trial_id = spec["trial_id"]
    expected_marker = spec["expected_marker"]
    requested_agent_type = spec["agent"]

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"spawn-obs-{trial_id}-"))
    evidence_dir = tmp_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    sentinel_bin_dir = tmp_dir / "sentinel-bin"
    marker_path = tmp_dir / "gemini-sentinel-hits.jsonl"

    child_env = dict(os.environ)
    argv = [
        "claude",
        "-p",
        "--output-format", "stream-json",
        "--include-hook-events",
        "--no-session-persistence",
        "--max-turns", str(spec["max_turns"]),
        "--verbose",
        "--settings", str(CONTROL_SETTINGS_PATH),
    ]

    if lane == "control":
        prompt = build_control_prompt()
        agents_json = CONTROL_AGENTS_PATH.read_text(encoding="utf-8")
        argv += ["--agents", agents_json]
        cwd = tmp_dir / "cwd"
        cwd.mkdir(parents=True, exist_ok=True)
        agent_definition_sha256 = _sha256_file(CONTROL_AGENTS_PATH)
        route = None
    else:
        route = route_smoke._find_route("claude_code", spec["agent"], spec["profile"])
        assert route is not None, spec
        prompt = route_smoke.build_route_prompt(route, evidence_dir)
        _write_gemini_sentinel(sentinel_bin_dir, marker_path)
        child_env["PATH"] = f"{sentinel_bin_dir}:{child_env.get('PATH', '')}"
        child_env["AGENT_PROVIDER_ROUTE_SMOKE_GEMINI_SENTINEL_MARKER"] = str(marker_path)
        cwd = worktree
        agent_definition_sha256 = _sha256_file(
            route_smoke._agent_definition_path(spec["agent"], "claude_code", REPO_ROOT)
        )

    settings_digest_material = CONTROL_SETTINGS_PATH.read_bytes()
    repo_settings = REPO_ROOT / ".claude" / "settings.json"
    if lane == "production" and repo_settings.is_file():
        settings_digest_material += repo_settings.read_bytes()
    effective_settings_digest = hashlib.sha256(settings_digest_material).hexdigest()

    start_time = _now()
    start_monotonic = time.monotonic()
    timed_out = False
    rc: int | None = None
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            argv, cwd=str(cwd), input=prompt, capture_output=True, text=True,
            timeout=float(spec["timeout_seconds"]), env=child_env, check=False,
        )
        rc, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    except OSError as exc:
        stderr = f"OSError: {exc}"
    end_time = _now()
    duration_seconds = round(time.monotonic() - start_monotonic, 3)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_stdout_path = RAW_DIR / f"{trial_id}.stdout.jsonl"
    raw_stderr_path = RAW_DIR / f"{trial_id}.stderr.txt"
    raw_stdout_path.write_text(stdout, encoding="utf-8")
    raw_stderr_path.write_text(stderr, encoding="utf-8")

    # --- lifecycle checkpoints, computed from raw evidence only -------------
    dispatch_observed, dispatched_subagent_type = observe_agent_tool_use(stdout)
    parent_session_id = runtime_smoke.extract_claude_parent_session_id(stdout)
    child_session_id = runtime_smoke.extract_claude_child_session_id(
        parent_session_id, str(cwd), stdout
    )
    child_session_id_from_stream = runtime_smoke._extract_claude_child_session_id_from_stream(stdout)
    child_agent_type_observed = runtime_smoke.extract_claude_child_agent_type(stdout)
    api_retry_count = count_api_retry_events(stdout)
    hook_identity = {
        "subagent_start": observe_hook_identity(stdout, "SubagentStart"),
        "subagent_stop": observe_hook_identity(stdout, "SubagentStop"),
    }
    hook_agent_type = (
        hook_identity["subagent_start"]["payload_agent_type"]
        or hook_identity["subagent_start"]["hook_name_agent_type"]
        or hook_identity["subagent_stop"]["payload_agent_type"]
        or hook_identity["subagent_stop"]["hook_name_agent_type"]
    )
    hook_agent_id = (
        hook_identity["subagent_start"]["payload_agent_id"]
        or hook_identity["subagent_stop"]["payload_agent_id"]
    )

    if lane == "production":
        request_validation = route_smoke._validate_delegation_request_evidence(evidence_dir, route)
        selected_provider, provider_attempts, wrapper_ok = (
            route_smoke._validate_delegation_result_evidence(evidence_dir)
        )
        gemini_hits = route_smoke._count_gemini_sentinel_hits(marker_path)
        fallback_hits = runtime_smoke.count_direct_web_tool_events("claude", stdout)
        route_evidence_sha256 = None
        if spec["profile"] == "github_research":
            route_evidence_sha256 = route_smoke._validate_github_research_route_evidence(evidence_dir)
        delegation_request_validated = request_validation == "pass"
    else:
        # Control lane performs no AGY delegation. Its request-validation
        # checkpoint is the independently observable fact that the dispatched
        # Agent tool_use actually requested the declared custom agent -- never
        # a self-report, never an assumed value.
        request_validation = "pass" if dispatched_subagent_type == requested_agent_type else "fail"
        selected_provider, provider_attempts, wrapper_ok = None, [], True
        gemini_hits = 0
        fallback_hits = runtime_smoke.count_direct_web_tool_events("claude", stdout)
        route_evidence_sha256 = None
        delegation_request_validated = request_validation == "pass"

    lifecycle = {
        "process_started": rc is not None or timed_out,
        "system_init_observed": any(
            p.get("type") == "system" and p.get("subtype") == "init" for p in _iter_events(stdout)
        ),
        "agent_tool_use_observed": dispatch_observed,
        "subagent_start_hook_observed": observe_hook_event(stdout, "SubagentStart"),
        "subagent_stop_hook_observed": observe_hook_event(stdout, "SubagentStop"),
        "tool_result_observed": observe_tool_result(stdout),
        "tool_result_agent_id_observed": child_session_id_from_stream is not None,
        "tool_result_agent_type_observed": child_agent_type_observed is not None,
        "agent_type_matches_requested": (
            child_agent_type_observed is not None
            and child_agent_type_observed == requested_agent_type
        ),
        "terminal_event_observed": runtime_smoke.has_terminal_event("claude", stdout),
        "expected_marker_observed": expected_marker in (stdout + "\n" + stderr),
        "delegation_request_validated": delegation_request_validated,
    }

    native_spawn_event_observed = bool(
        parent_session_id
        and child_session_id
        and parent_session_id != child_session_id
        and lifecycle["agent_type_matches_requested"]
    )

    harness_exit, harness_reasons = compute_harness_exit_equivalent(
        rc=rc, stdout=stdout, stderr=stderr, timed_out=timed_out, expected_marker=expected_marker,
    )
    status, failure_class = compute_failure_class(
        lane=lane,
        profile=spec["profile"],
        gemini_hits=gemini_hits,
        fallback_hits=fallback_hits,
        harness_exit=harness_exit,
        native_spawn_event_observed=native_spawn_event_observed,
        request_validation=request_validation,
        selected_provider=selected_provider,
        route_evidence_sha256=route_evidence_sha256,
        wrapper_ok=wrapper_ok,
    )
    downstream = {
        "selected_provider": selected_provider,
        "provider_attempts": provider_attempts,
        "wrapper_ok": wrapper_ok if lane == "production" else None,
        "request_validation": request_validation,
        "route_evidence_sha256": route_evidence_sha256,
        "gemini_sentinel_hits": gemini_hits,
        "direct_web_tool_event_count": fallback_hits,
        "delegation_result_present": (evidence_dir / "delegation_result.json").is_file(),
        "delegation_request_present": (evidence_dir / "delegation_request.json").is_file(),
    }
    diagnostic_cause = compute_diagnostic_cause(
        status=status, lifecycle=lifecycle, rc=rc, timed_out=timed_out,
        api_retry_count=api_retry_count, downstream=downstream,
    )

    record = {
        "schema": "spawn_observability_trial/v1",
        "trial_id": trial_id,
        "lane": lane,
        "route": (
            f"claude_code:{spec['agent']}:{spec['profile']}"
            if lane == "production"
            else f"control:{CONTROL_AGENT_NAME}"
        ),
        "claude_code_version": claude_version,
        "tested_head_sha": tested_head_sha,
        "historical_baseline_sha": "28394e226533cd59cdfc0f55602ac65e389a6600",
        "trial_plan_sha256": plan_sha256,
        "prompt_sha256": _sha256_text(prompt),
        "agent_definition_sha256": agent_definition_sha256,
        "effective_settings_digest": effective_settings_digest,
        "requested_agent_type": requested_agent_type,
        "observed_agent_type": child_agent_type_observed,
        "dispatched_subagent_type": dispatched_subagent_type,
        "timeout_seconds": spec["timeout_seconds"],
        "max_turns": spec["max_turns"],
        "start_time": start_time,
        "end_time": end_time,
        "duration_seconds": duration_seconds,
        "process_exit_code": rc,
        "timed_out": timed_out,
        "native_event_count": runtime_smoke.parse_native_event_count(stdout),
        "api_retry_count": api_retry_count,
        "parent_session_id_observed": bool(parent_session_id),
        "child_session_id_observed": bool(child_session_id),
        "native_spawn_event_observed": native_spawn_event_observed,
        "tool_result_agent_id": child_session_id_from_stream,
        "hook_identity": hook_identity,
        "hook_agent_type_observed": hook_agent_type,
        "hook_agent_id_observed": hook_agent_id,
        "hook_agent_type_matches_requested": hook_agent_type == requested_agent_type,
        "cross_channel_identity_agreement": {
            "tool_result_channel_has_agent_type": child_agent_type_observed is not None,
            "hook_channel_has_agent_type": hook_agent_type is not None,
            "agent_id_channels_agree": (
                child_session_id_from_stream is not None
                and hook_agent_id is not None
                and child_session_id_from_stream == hook_agent_id
            ),
        },
        "harness_exit_equivalent": harness_exit,
        "harness_failure_reasons": harness_reasons,
        "lifecycle": lifecycle,
        "failure_class": failure_class,
        "status": status,
        "diagnostic_cause": diagnostic_cause,
        "downstream": downstream,
        "trial_valid": True,
        "excluded_reason": None,
        "raw_stdout_path": str(raw_stdout_path.relative_to(ARTIFACT_DIR)),
        "raw_stderr_path": str(raw_stderr_path.relative_to(ARTIFACT_DIR)),
    }
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="run a single trial_id (smoke check)")
    parser.add_argument("--lane", choices=["control", "production"])
    parser.add_argument("--worktree", default=str(REPO_ROOT))
    parser.add_argument("--freeze-plan", action="store_true",
                        help="write trial-plan.json and exit without running trials")
    args = parser.parse_args(argv)

    plan = build_trial_plan()
    digest = plan_digest(plan)
    plan_out = dict(plan)
    plan_out["trial_plan_sha256"] = digest
    plan_out["frozen_at"] = plan_out.get("frozen_at") or _now()
    if args.freeze_plan:
        if TRIAL_PLAN_PATH.is_file():
            existing = json.loads(TRIAL_PLAN_PATH.read_text(encoding="utf-8"))
            if existing.get("trial_plan_sha256") != digest:
                print("ERROR: trial plan already frozen with a different digest", file=sys.stderr)
                return 2
            print(f"plan already frozen: {digest}")
            return 0
        TRIAL_PLAN_PATH.write_text(
            json.dumps(plan_out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"frozen: {digest}")
        return 0

    if not TRIAL_PLAN_PATH.is_file():
        print("ERROR: run --freeze-plan first", file=sys.stderr)
        return 2
    frozen = json.loads(TRIAL_PLAN_PATH.read_text(encoding="utf-8"))
    if frozen.get("trial_plan_sha256") != digest:
        print("ERROR: live plan digest does not match frozen trial-plan.json", file=sys.stderr)
        return 2

    claude_version = route_smoke._runtime_version("claude")
    tested_head_sha = route_smoke._git_head_sha(REPO_ROOT) or "unknown"

    already: set[str] = set()
    if LEDGER_PATH.is_file():
        for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            already.add(json.loads(line)["trial_id"])

    for spec in frozen["trials"]:
        if args.only and spec["trial_id"] != args.only:
            continue
        if args.lane and spec["lane"] != args.lane:
            continue
        if spec["trial_id"] in already:
            print(f"skip (already recorded): {spec['trial_id']}")
            continue
        print(f"running {spec['trial_id']} ...", flush=True)
        record = run_trial(
            spec, plan_sha256=digest, tested_head_sha=tested_head_sha,
            claude_version=claude_version, worktree=Path(args.worktree),
        )
        with LEDGER_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        print(
            f"  status={record['status']} failure_class={record['failure_class']} "
            f"diagnostic_cause={record['diagnostic_cause']} "
            f"spawn={record['native_spawn_event_observed']} "
            f"start_hook={record['lifecycle']['subagent_start_hook_observed']} "
            f"stop_hook={record['lifecycle']['subagent_stop_hook_observed']} "
            f"({record['duration_seconds']}s)",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
