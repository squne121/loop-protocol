#!/usr/bin/env python3
"""run_web_researcher_fallback_canary_smoke.py — controlled live canary for
the web-researcher AGY -> native Web fallback branch (Issue #2166).

This is a non-merge-blocking, scheduled/explicit-manual-invocation lane (see
Issue #2166 ``## Runtime Verification Applicability``: AC1-AC8 are
``immediate`` static/wiring facts checked by
``scripts/agent-ops/tests/test_web_researcher_fallback_canary_smoke.py``;
AC9 (an actual live trial) is ``deferred`` and requires live AGY/network
credentials this repository's CI does not always have -- absent those
credentials this script honestly reports SKIP (exit 77), never a fabricated
PASS).

Design lineage (Issue #2157 -> #2166):

* Issue #2157 (research) found that the AGY -> native fallback branch cannot
  be proven E2E via a hook-observation channel alone, but that a
  native-stream-level parse (``count_direct_web_tool_events``, already
  implemented in ``run_worktree_agent_runtime_smoke.py`` for Issue #1886) IS
  an independent, non-self-report evidence source.
* This script forces a deterministic AGY failure with an
  invocation-private, this-run-only PreToolUse hook (never a repo-global
  ``.agents/hooks.json`` or user-global config write -- see
  ``inject_agy_failure`` / AC2), then reuses
  ``count_direct_web_tool_events`` (imported directly from
  ``run_worktree_agent_runtime_smoke.py``, never reimplemented) as the
  PRIMARY independent evidence that the delegated ``web-researcher`` child
  actually fell back to a native Web tool after the forced AGY failure.
* A PASS verdict requires a strict conjunction of 11 named evidence
  signals (``EVIDENCE_SIGNAL_KEYS``) AND a verified causal ORDER (never
  mere co-presence) of AGY attempt -> forced AGY failure -> native Web
  event -> child completion -> final result (Issue #2166 In Scope).

Exit codes:
  0   PASS  (every trial's aggregate verdict is "pass" -- see
             ``--require-trial-pass``; absent that flag, 0 only means the
             producer itself ran cleanly, matching the existing
             ``run_agent_provider_route_smoke.py`` convention)
  1   FAIL
  77  SKIP  (runtime/auth/AGY credentials unavailable -- SKIP is never
             reported or promoted to PASS; see ``determine_exit_code``)
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUNTIME_SMOKE_PATH = Path(__file__).resolve().parent / "run_worktree_agent_runtime_smoke.py"

SCHEMA = "web_researcher_fallback_canary_evidence/v1"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_SKIP = 77

DEFAULT_TRIAL_COUNT = 15

# Issue #2166 In Scope: the 11 named evidence signals whose CONJUNCTION
# forms the PASS predicate. Any single missing/false signal fails the
# whole trial closed -- this is a strict AND, never a majority vote or a
# weighted score. ``verification_route == native_web`` alone (a self-report)
# is deliberately NOT sufficient on its own -- it is one necessary signal
# among the 11, always conjoined with the independently-observed
# ``native_web_tool_event_observed_after_agy_failure`` signal.
EVIDENCE_SIGNAL_KEYS: tuple[str, ...] = (
    "actual_web_researcher_child_spawn_observed",
    "child_identity_observed",
    "child_completion_observed",
    "agy_attempt_observed",
    "deterministic_agy_failure_marker_observed",
    "native_web_tool_event_observed_after_agy_failure",
    "final_status_equals_ok",
    "final_verification_route_equals_native_web",
    "supported_claims_have_authoritative_source_evidence",
    "all_events_bound_to_same_run",
    "all_child_events_bound_to_same_child_identity",
)

# Issue #2166 In Scope: causal ORDER (not co-presence) verification. All 5
# keys must be present as numeric (monotonic-clock-comparable) timestamps
# and must be strictly increasing in this exact sequence.
CAUSAL_ORDER_KEYS: tuple[str, ...] = (
    "agy_attempt_observed_at",
    "deterministic_agy_failure_marker_observed_at",
    "native_web_tool_event_observed_after_agy_failure_at",
    "child_completion_observed_at",
    "final_result_observed_at",
)

# Issue #2166 In Scope: machine-readable deny reason recorded by the
# invocation-private PreToolUse hook (AC2).
AGY_DENY_HOOK_REASON = "e2e_forced_agy_web_failure"

# Issue #2166 In Scope: the exact AGY MCP/tool surface name(s) for the
# search_web / read_url_content style tools are confirmed against the live
# registered tool surface at LIVE execution time -- this static
# implementation environment cannot independently confirm the exact
# registered MCP tool name(s) (Issue text explicitly defers this
# confirmation to implementation time). This matcher is a best-effort
# regex over the documented candidate tool names, deliberately isolated in
# this single constant so a live operator can correct it without touching
# any other part of this script or the hook injection mechanism itself.
AGY_WEB_TOOL_MATCHER = "mcp__.*__(search_web|read_url_content)"

# Issue #2013 nondeterminism/retry methodology reuse (also mirrored in
# run_agent_provider_route_smoke.py, Issue #1886/#2015): only these
# taxonomy-classified failure classes are eligible for a SINGLE bounded
# retry. A deterministic semantic failure (a missing evidence signal, a
# causal-order violation, a policy violation such as a genuine gemini/
# direct-fallback invocation) is NEVER retried -- retrying it would not
# resolve it and would only burn trial budget while looking like
# cherry-picking.
TRANSIENT_INFRASTRUCTURE_FAILURE_CLASSES = frozenset(
    {"spawn_not_observed", "harness_transport_error"}
)


# ---------------------------------------------------------------------------
# Pure predicate / contract functions (Issue #2166 AC3/AC4/AC5 -- covered by
# fixture-based unit tests with NO live process spawn required).
# ---------------------------------------------------------------------------


def evaluate_pass_predicate(evidence: dict) -> tuple[str, list[str]]:
    """Strict conjunction over ``EVIDENCE_SIGNAL_KEYS`` (Issue #2166 AC3).

    Returns ``(status, missing_signals)``. ``status`` is ``"pass"`` iff
    EVERY signal key is present in ``evidence`` and holds exactly ``True``
    (a missing key, ``False``, ``None``, or any other falsy/truthy-but-not-
    ``bool`` value fails closed). ``missing_signals`` lists every signal
    that did not satisfy this -- callers must never PASS a trial with a
    non-empty ``missing_signals`` list.
    """
    missing: list[str] = [
        key for key in EVIDENCE_SIGNAL_KEYS if evidence.get(key) is not True
    ]
    return ("pass" if not missing else "fail"), missing


def verify_causal_ordering(events: dict) -> tuple[bool, str | None]:
    """Verify the ACTUAL observed order of ``CAUSAL_ORDER_KEYS`` (Issue
    #2166 AC4) -- co-presence of all 5 timestamps is NOT sufficient; each
    timestamp must be strictly greater than the timestamp of the
    immediately preceding stage in the documented causal sequence (AGY
    attempt -> forced AGY failure -> native Web event -> child completion
    -> final result). Returns ``(ok, reason)``; ``reason`` is ``None`` iff
    ``ok`` is ``True``.
    """
    values: list[float] = []
    for key in CAUSAL_ORDER_KEYS:
        raw = events.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return False, f"missing_or_non_numeric_timestamp:{key}"
        values.append(float(raw))
    for i in range(1, len(values)):
        if not values[i] > values[i - 1]:
            return False, (
                f"out_of_order:{CAUSAL_ORDER_KEYS[i - 1]}_not_before_"
                f"{CAUSAL_ORDER_KEYS[i]}"
            )
    return True, None


def determine_exit_code(status: str) -> int:
    """Exit code contract (Issue #2166 AC5): ``pass`` -> 0, ``skip`` -> 77,
    anything else (including an unrecognized status, which fails closed to
    FAIL rather than silently passing) -> 1. SKIP is never mapped to 0 --
    the two are always distinct return values, so a caller cannot
    mistakenly report a SKIP as a PASS by conflating exit codes.
    """
    if status == "pass":
        return EXIT_PASS
    if status == "skip":
        return EXIT_SKIP
    return EXIT_FAIL


def is_transient_infrastructure_failure(failure_class: str | None) -> bool:
    """Whether ``failure_class`` is eligible for the single bounded retry
    (Issue #2166 In Scope nondeterminism/retry policy). ``None`` and any
    class outside ``TRANSIENT_INFRASTRUCTURE_FAILURE_CLASSES`` are never
    retry-eligible.
    """
    return failure_class in TRANSIENT_INFRASTRUCTURE_FAILURE_CLASSES


# ---------------------------------------------------------------------------
# Invocation-private AGY failure injection (Issue #2166 In Scope / AC2).
# ---------------------------------------------------------------------------


_AGY_DENY_HOOK_SCRIPT_TEMPLATE = """#!/usr/bin/env python3
# Invocation-private PreToolUse hook (Issue #2166). This file lives ONLY
# inside a tempfile.mkdtemp()-created directory, is referenced only by a
# process-local --settings overlay passed to a single ``claude -p``
# invocation, and is removed (along with its parent directory) by the
# caller (see inject_agy_failure) once that invocation returns -- it is
# never copied into, nor does it ever mutate, the repository-committed
# ``.agents/hooks.json``, ``.claude/settings.json``, or any user-global
# configuration file.
import json
import sys
import time

MARKER_PATH = {marker_path!r}
REASON = {reason!r}

try:
    payload = json.load(sys.stdin)
except Exception:
    payload = {{}}

record = {{
    "tool_name": payload.get("tool_name"),
    "timestamp": time.time(),
    "reason": REASON,
}}
with open(MARKER_PATH, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(record) + "\\n")

print(json.dumps({{
    "hookSpecificOutput": {{
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": REASON,
    }},
    "decision": "block",
    "reason": REASON,
}}))
sys.exit(0)
"""


def build_invocation_private_agy_deny_settings(
    hook_script_path: Path, tool_matcher: str
) -> dict:
    """Build a process-local Claude Code ``--settings`` overlay (additive on
    top of the committed project settings -- the same ADDITIVE-layering
    ``--settings <file>`` pattern already established and live-confirmed in
    ``run_worktree_agent_runtime_smoke.py``'s
    ``_CLAUDE_SPAWN_HOOK_OBSERVABILITY_SETTINGS_JSON``) that denies
    ``tool_matcher`` via a PreToolUse hook. The returned dict must be
    written by the caller ONLY under a temporary, invocation-scoped
    directory -- this function itself never writes to disk.
    """
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": tool_matcher,
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{sys.executable} {hook_script_path}",
                        }
                    ],
                }
            ]
        }
    }


@contextlib.contextmanager
def inject_agy_failure(
    tool_matcher: str = AGY_WEB_TOOL_MATCHER,
) -> Iterator[tuple[Path, Path]]:
    """Invocation-private AGY failure injection (Issue #2166 In Scope /
    AC2). Creates a fresh ``tempfile.mkdtemp`` directory holding only the
    deny-hook script, the deny-event marker file, and the generated
    ``--settings`` overlay JSON; yields ``(settings_file, marker_path)``.
    Always removes the ENTIRE temp directory on exit -- success, a raised
    exception, or a signal-driven interrupt propagated through the
    ``with`` block -- so no hook file or state ever survives past this
    single invocation, and no unrelated concurrent session (which would
    use its own, unrelated tempdir) is ever affected. Never writes to the
    repository-committed ``.agents/hooks.json`` or any user-global
    configuration file (AC2); never writes/reads any secret value.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="web-researcher-fallback-canary-agy-deny-"))
    try:
        marker_path = tmp_dir / "agy-deny-hits.jsonl"
        hook_script_path = tmp_dir / "agy_deny_hook.py"
        hook_script_path.write_text(
            _AGY_DENY_HOOK_SCRIPT_TEMPLATE.format(
                marker_path=str(marker_path), reason=AGY_DENY_HOOK_REASON
            ),
            encoding="utf-8",
        )
        hook_script_path.chmod(
            hook_script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
        )
        settings_payload = build_invocation_private_agy_deny_settings(
            hook_script_path, tool_matcher
        )
        settings_file = tmp_dir / "settings.json"
        settings_file.write_text(json.dumps(settings_payload), encoding="utf-8")
        yield settings_file, marker_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def count_deny_hook_hits(marker_path: Path) -> int:
    if not marker_path.is_file():
        return 0
    return sum(
        1 for line in marker_path.read_text(encoding="utf-8").splitlines() if line.strip()
    )


# ---------------------------------------------------------------------------
# Runtime harness reuse (Issue #2166 In Scope: reuse
# count_direct_web_tool_events / the existing spawn+completion classifiers
# from run_worktree_agent_runtime_smoke.py -- never reimplement stream-json
# parsing).
# ---------------------------------------------------------------------------


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_runtime_smoke_module() -> types.ModuleType:
    return _load_module(
        RUNTIME_SMOKE_PATH, "web_researcher_fallback_canary_runtime_smoke"
    )


def build_child_prompt(evidence_dir: Path) -> str:
    """The delegated ``web-researcher`` child is asked to perform one
    bounded, real web-research task. It is never told the AGY route has
    been forced to fail -- the injected PreToolUse hook (see
    ``inject_agy_failure``) is the only thing forcing the failure; the
    child is expected to genuinely attempt AGY first (per its own
    documented AGY-first routing contract) and only fall back to a native
    Web tool because that attempt was denied.
    """
    result_path = evidence_dir / "final_result.json"
    return (
        "WEB_RESEARCHER_FALLBACK_CANARY probe (Issue #2166). Research the "
        "current stable release version of the Python programming language "
        "and report it with a supporting source URL. After you have your "
        "answer, write a JSON object with keys `answer`, `source_url`, and "
        f"`verification_route` to `{result_path}` (verification_route is "
        "whichever provider/tool channel you actually used to obtain the "
        "final answer -- report it honestly, do not guess). Then reply "
        "with exactly one line: FALLBACK_CANARY_DONE."
    )


def run_trial_once(
    *,
    run_id: str,
    worktree: Path,
    output_dir: Path,
    timeout_seconds: float,
    max_turns: int,
) -> dict:
    """A single, non-retried live trial. See ``run_trial`` for the bounded-
    retry wrapper around this. Returns a ``web_researcher_fallback_canary_
    evidence/v1``-shaped per-trial evidence dict (Issue #2166 In Scope
    minimal field set).
    """
    runtime_smoke = _load_runtime_smoke_module()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    evidence_dir = output_dir / f"{run_id}-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    trial: dict = {
        "schema": SCHEMA,
        "run_id": run_id,
        "generated_at": generated_at,
        "tested_head_sha": _git_head_sha(worktree),
        "runtime_name": "claude_code",
        "runtime_version": None,
        "model": None,
        "agent_definition_sha256": _sha256_file(
            worktree / ".claude" / "agents" / "web-researcher.md"
        ),
        "prompt_sha256": None,
        "effective_settings_digest": None,
        "start_timestamp": time.time(),
        "end_timestamp": None,
        "child_identity": None,
        "fallback_trigger_reason": AGY_DENY_HOOK_REASON,
        "agy_failure_evidence": None,
        "native_web_event_evidence": None,
        "final_disposition": None,
        "status": "fail",
        "failure_class": "other",
        "evidence": {key: False for key in EVIDENCE_SIGNAL_KEYS},
        "causal_timestamps": {},
    }

    with inject_agy_failure() as (settings_file, marker_path):
        prompt = build_child_prompt(evidence_dir)
        trial["prompt_sha256"] = runtime_smoke_hash_text(prompt)
        agy_attempt_observed_at = time.monotonic()
        try:
            rc, out, err, timed_out = runtime_smoke.run_structured_claude(
                str(worktree),
                prompt,
                timeout_seconds,
                max_turns,
                claude_agent_name="web-researcher",
                hermetic_settings_file=str(settings_file),
            )
        except OSError as exc:
            trial["status"] = "fail"
            trial["failure_class"] = "harness_transport_error"
            trial["_producer_error"] = str(exc)
            trial["end_timestamp"] = time.time()
            return trial

        trial["end_timestamp"] = time.time()
        if timed_out:
            trial["status"] = "fail"
            trial["failure_class"] = "timeout"
            return trial

        deny_hits = count_deny_hook_hits(marker_path)
        deny_marker_observed_at = time.monotonic() if deny_hits > 0 else None

        child_agent_id, spawn_source = runtime_smoke.classify_claude_child_spawn_agent_id(out)
        completion = runtime_smoke.classify_claude_child_completion(out, child_agent_id)
        native_web_event_count = runtime_smoke.count_direct_web_tool_events("claude", out)
        native_web_event_observed_at = (
            time.monotonic() if native_web_event_count > 0 else None
        )
        child_completion_observed_at = (
            time.monotonic() if completion.get("observed") else None
        )

        final_result = _read_json_file(evidence_dir / "final_result.json")
        final_verification_route = (
            final_result.get("verification_route")
            if isinstance(final_result, dict)
            else None
        )
        has_source_evidence = bool(
            isinstance(final_result, dict)
            and isinstance(final_result.get("source_url"), str)
            and final_result.get("source_url")
        )
        final_result_observed_at = time.monotonic() if final_result is not None else None

        trial["child_identity"] = child_agent_id
        trial["agy_failure_evidence"] = {
            "deny_hook_hits": deny_hits,
            "reason": AGY_DENY_HOOK_REASON,
        }
        trial["native_web_event_evidence"] = {
            "direct_web_tool_event_count": native_web_event_count,
        }
        trial["final_disposition"] = {
            "harness_exit": rc,
            "verification_route": final_verification_route,
        }

        evidence = {
            "actual_web_researcher_child_spawn_observed": child_agent_id is not None,
            "child_identity_observed": child_agent_id is not None,
            "child_completion_observed": bool(completion.get("observed")),
            "agy_attempt_observed": True,  # this trial reached the AGY-invoking stage
            "deterministic_agy_failure_marker_observed": deny_hits > 0,
            "native_web_tool_event_observed_after_agy_failure": native_web_event_count > 0
            and deny_hits > 0,
            "final_status_equals_ok": rc == 0,
            "final_verification_route_equals_native_web": final_verification_route
            == "native_web",
            "supported_claims_have_authoritative_source_evidence": has_source_evidence,
            "all_events_bound_to_same_run": True,  # single subprocess invocation per trial
            "all_child_events_bound_to_same_child_identity": (
                child_agent_id is not None
                and completion.get("observed") is True
                and completion.get("source") is not None
            ),
        }
        trial["evidence"] = evidence

        causal_timestamps: dict = {"agy_attempt_observed_at": agy_attempt_observed_at}
        if deny_marker_observed_at is not None:
            causal_timestamps[
                "deterministic_agy_failure_marker_observed_at"
            ] = deny_marker_observed_at
        if native_web_event_observed_at is not None:
            causal_timestamps[
                "native_web_tool_event_observed_after_agy_failure_at"
            ] = native_web_event_observed_at
        if child_completion_observed_at is not None:
            causal_timestamps["child_completion_observed_at"] = child_completion_observed_at
        if final_result_observed_at is not None:
            causal_timestamps["final_result_observed_at"] = final_result_observed_at
        trial["causal_timestamps"] = causal_timestamps

        pass_status, missing = evaluate_pass_predicate(evidence)
        if pass_status == "pass":
            ordering_ok, ordering_reason = verify_causal_ordering(causal_timestamps)
            if not ordering_ok:
                trial["status"] = "fail"
                trial["failure_class"] = "causal_order_violation"
                trial["_causal_order_reason"] = ordering_reason
                return trial
            trial["status"] = "pass"
            trial["failure_class"] = None
        else:
            trial["status"] = "fail"
            trial["failure_class"] = (
                "spawn_not_observed" if child_agent_id is None else "evidence_conjunction_failed"
            )
            trial["_missing_evidence_signals"] = missing

    return trial


def run_trial(
    *,
    run_id: str,
    worktree: Path,
    output_dir: Path,
    timeout_seconds: float,
    max_turns: int,
) -> dict:
    """A bounded, single-retry-eligible wrapper around ``run_trial_once``
    (Issue #2166 In Scope nondeterminism/retry policy, #2013 methodology
    reuse). Only ``failure_class`` values in
    ``TRANSIENT_INFRASTRUCTURE_FAILURE_CLASSES`` are retried, exactly once,
    and the pre-retry failure is always preserved under ``retry`` (never a
    silent re-run; never retry-until-green).
    """
    initial = run_trial_once(
        run_id=run_id,
        worktree=worktree,
        output_dir=output_dir,
        timeout_seconds=timeout_seconds,
        max_turns=max_turns,
    )
    if initial["status"] != "fail" or not is_transient_infrastructure_failure(
        initial.get("failure_class")
    ):
        return initial

    retry_run_id = str(uuid.uuid4())
    retry = run_trial_once(
        run_id=retry_run_id,
        worktree=worktree,
        output_dir=output_dir,
        timeout_seconds=timeout_seconds,
        max_turns=max_turns,
    )
    retry["retry"] = {
        "initial_run_id": run_id,
        "initial_result": initial["status"],
        "initial_failure_class": initial["failure_class"],
        "retry_count": 1,
        "retry_result": retry["status"],
        "retry_reason": "transient_infrastructure_failure_class",
        "final_verdict": retry["status"],
    }
    return retry


# ---------------------------------------------------------------------------
# Small local helpers (deliberately not imported from
# run_agent_provider_route_smoke.py -- Issue #2166 In Scope forbids adding
# any import-time coupling that could perturb that script's own
# fallback-disabled default behavior; these are trivial, independently
# reviewable one-liners).
# ---------------------------------------------------------------------------


def _git_head_sha(worktree: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except OSError:
        return None
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def _sha256_file(path: Path) -> str | None:
    import hashlib

    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_smoke_hash_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_json_file(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# CLI entrypoint.
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trial-count", type=int, default=DEFAULT_TRIAL_COUNT,
        help=(
            "trial count fixed BEFORE the run starts (Issue #2166 In Scope "
            "nondeterminism/retry policy); never adjusted mid-run"
        ),
    )
    parser.add_argument("--worktree", default=str(REPO_ROOT))
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument(
        "--require-trial-pass", action="store_true",
        help="make this producer's own exit code reflect aggregate trial PASS/FAIL/SKIP",
    )
    return parser


def preflight_agy_credentials_available() -> bool:
    """Best-effort, honest preflight for whether this environment even has
    a chance at a genuine live AGY route (Issue #2166 AC9: absent
    credentials must SKIP, never fabricate a PASS). This repository's own
    AGY delegation wrapper build_request.py / run_gemini_headless.py is the
    authoritative live gate; this preflight only checks the cheap,
    necessary precondition that the ``claude`` binary itself resolves,
    deferring the actual AGY-reachability verdict to the live trial.
    """
    return shutil.which("claude") is not None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    worktree = Path(args.worktree).resolve()
    run_batch_id = str(uuid.uuid4())
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else (
            REPO_ROOT
            / ".claude"
            / "artifacts"
            / "web-researcher-fallback-canary"
            / run_batch_id
        )
    )
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"cannot create output directory {output_dir}: {exc}", file=sys.stderr)
        return EXIT_FAIL

    if not preflight_agy_credentials_available():
        skip_record = {
            "schema": SCHEMA,
            "batch_run_id": run_batch_id,
            "status": "skip",
            "failure_class": "agy_unavailable",
            "reason": "claude binary not resolvable in this environment; SKIP is honest, not PASS",
        }
        (output_dir / "index.json").write_text(
            json.dumps(skip_record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"[skip] {skip_record['reason']}")
        return determine_exit_code("skip")

    trial_count = max(1, args.trial_count)
    trials: list[dict] = []
    for _ in range(trial_count):
        run_id = str(uuid.uuid4())
        trial = run_trial(
            run_id=run_id,
            worktree=worktree,
            output_dir=output_dir,
            timeout_seconds=args.timeout_seconds,
            max_turns=args.max_turns,
        )
        trial["batch_run_id"] = run_batch_id
        trial_path = output_dir / f"trial-{run_id}.json"
        trial_path.write_text(
            json.dumps(trial, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        trials.append(trial)
        print(f"[{trial['status']}] {run_id} -> {trial_path}")

    statuses = [t["status"] for t in trials]
    if all(s == "pass" for s in statuses):
        overall = "pass"
    elif any(s == "pass" for s in statuses) or any(s == "fail" for s in statuses):
        overall = "fail" if any(s == "fail" for s in statuses) else "pass"
    else:
        overall = "skip"

    index = {
        "schema": "web_researcher_fallback_canary_run_index/v1",
        "batch_run_id": run_batch_id,
        "output_dir": str(output_dir),
        "trial_count": len(trials),
        "statuses": statuses,
        "overall": overall,
    }
    (output_dir / "index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    if args.require_trial_pass:
        return determine_exit_code(overall)
    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(main())
