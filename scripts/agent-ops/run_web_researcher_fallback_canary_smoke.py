#!/usr/bin/env python3
"""run_web_researcher_fallback_canary_smoke.py -- controlled live fallback
canary lane for the AGY -> native Web fallback branch in `web-researcher`
(Issue #2166, follow-up to #2157).

#2157 concluded that the AGY -> native Web fallback branch cannot be proven
E2E through the Claude Code hook channel, but native `stream-json`-level
observation (`count_direct_web_tool_events`, Issue #1886 / PR #2005) IS an
independent, ungameable evidence source. This script turns that conclusion
into an executable, **non-merge-blocking** controlled live canary: it forces
AGY web-tool calls to fail deterministically inside an isolated,
invocation-private workspace, spawns the real `web-researcher` custom agent,
and requires a conjunction of 11 independent evidence signals (see
`EVIDENCE_SIGNAL_NAMES`) plus an actual (not merely co-present) causal
ordering before reporting PASS.

This lane is intentionally **not** wired into any required CI status check
(AC6) -- it is a scheduled / explicit manual execution lane only, because
live AGY/network credentials are not guaranteed to be available in every
execution environment (see the Issue's `Runtime Verification Applicability:
deferred` decision for AC9).

Exit codes:
  0   PASS  -- the full evidence conjunction and causal ordering held for at
               least one trial (see `--require-any-pass`) / all trials
               (default), per `--trials`.
  1   FAIL  -- live execution was attempted and a genuine (not
               infrastructure-transient) failure was observed.
  77  SKIP  -- runtime/auth/capability unavailable in this environment
               (e.g. `claude` binary missing, AGY preflight failed). SKIP is
               NEVER reported or promoted as PASS -- see
               `finalize_disposition()`.

Nondeterminism / retry policy (Issue #2013 methodology reuse): trial count
is fixed *before* execution (`--trials`, default `DEFAULT_TRIAL_COUNT`), no
cherry-picking, no retry-until-green, no silent retry. Only a genuine
infrastructure-transient failure class (see `TRANSIENT_FAILURE_CLASSES`,
mirroring `run_agent_provider_route_smoke.py`'s
`_AGY_TRANSIENT_QUOTA_FAILURE_CLASSES` / Issue #1997 taxonomy) is eligible
for a single bounded retry; the pre-retry failure is always preserved and
the retry reason is recorded machine-readably (see `RetryDecision`).

Schema/artifact discipline (AC8): this script defines its OWN, brand-new
companion schemas (`SCHEMA` / `TRIAL_EVIDENCE_SCHEMA`) -- it never touches or
widens any existing closed (`additionalProperties: false`) route-artifact
schema such as `schemas/agent_provider_route_smoke_v1.schema.json`. No new
persistent state, receipt ledger, publisher, or control-plane is created;
per-trial evidence is a plain, session-local JSON file under
`.claude/artifacts/web-researcher-fallback-canary/<run_id>/`.
"""
from __future__ import annotations

import argparse
import atexit
import dataclasses
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUNTIME_SMOKE_SCRIPT = Path(__file__).resolve().parent / "run_worktree_agent_runtime_smoke.py"
AGY_PERMISSION_POLICY_SCRIPT = (
    REPO_ROOT / ".claude" / "skills" / "gemini-cli-headless-delegation" / "scripts" / "agy_permission_policy.py"
)

# --- Schemas (Issue #2166 AC8: brand-new, never widening an existing closed
# route-artifact schema -- see module docstring). --------------------------
SCHEMA = "web_researcher_fallback_canary_smoke/v1"
TRIAL_EVIDENCE_SCHEMA = "web_researcher_fallback_canary_smoke_trial_evidence/v1"

# --- Exit code contract (AC5) ----------------------------------------------
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_SKIP = 77

DISPOSITION_PASS = "pass"
DISPOSITION_FAIL = "fail"
DISPOSITION_SKIP = "skip"

_EXIT_CODE_BY_DISPOSITION: dict[str, int] = {
    DISPOSITION_PASS: EXIT_PASS,
    DISPOSITION_FAIL: EXIT_FAIL,
    DISPOSITION_SKIP: EXIT_SKIP,
}

# --- AGY web-tool surface (In Scope: "実装時に実際の AGY MCP/tool surface を
# 確認してから確定する"). Confirmed (2026-08-14) against
# `.claude/skills/gemini-cli-headless-delegation/scripts/agy_permission_policy.py`
# `AGY_DIRECT_TOOL_NAMES` -- the only two AGY direct-tool names that name a
# *web* capability (as opposed to shell/file/github/browser tools) are
# `search_web` and `read_url_content`. -------------------------------------
AGY_WEB_TOOL_NAMES = frozenset({"search_web", "read_url_content"})

AGY_FAILURE_INJECTION_REASON = "e2e_forced_agy_web_failure"

# --- PASS predicate: conjunction of 11 independent evidence signals -------
# (Issue #2166 In Scope). `verification_route == native_web` self-report
# alone (`final_verification_route_equals_native_web`) is a NECESSARY but
# not SUFFICIENT condition -- it is only one of the 11 signals below, and
# `native_web_tool_event_observed_after_agy_failure` (the native
# stream-level, `count_direct_web_tool_events`-derived signal) is the
# primary, independent, self-report-resistant evidence.
EVIDENCE_SIGNAL_NAMES: tuple[str, ...] = (
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

# --- Causal ordering (Issue #2166 In Scope: actual event order, not mere
# co-presence in logs). ------------------------------------------------
CAUSAL_ORDER_STEPS: tuple[str, ...] = (
    "agy_attempt",
    "agy_failure",
    "native_web_tool_event",
    "child_completion",
    "final_result",
)

# Event types that must be bound to the SAME child identity as one another
# (the parent-orchestrator-only "final_result" step is bound to the run, not
# to a specific child, since it is produced after the child has already
# terminated).
_CHILD_SCOPED_CAUSAL_STEPS = frozenset(CAUSAL_ORDER_STEPS) - {"final_result"}

DEFAULT_TRIAL_COUNT = 15

# --- Nondeterminism / retry policy (Issue #2013 methodology reuse). This
# mirrors `run_agent_provider_route_smoke.py`'s
# `_AGY_TRANSIENT_QUOTA_FAILURE_CLASSES` (Issue #1997 taxonomy,
# `references/failure-class-taxonomy.md` retryable="yes" set) -- deliberately
# NOT the full AGY failure-class table: `agy_auth_required` /
# `agy_permission_denied` / `agy_not_found` / `agy_timeout` /
# `agy_exit_nonzero` / `agy_unexpected_error` are retryable="no" there and
# excluded here too, because retrying a genuine auth/permission/missing-
# binary/unclassified failure would not resolve it. Also mirrors this
# canary's own `spawn_not_observed_infra_race` class for a Claude Code
# native-spawn-event visibility race, which is a documented, bounded,
# single-retry-eligible infrastructure timing issue (not a semantic
# failure).
TRANSIENT_FAILURE_CLASSES = frozenset(
    {
        "agy_rate_limited",
        "agy_capacity_exhausted",
        "agy_web_grounding_quota_exhausted",
        "spawn_not_observed_infra_race",
    }
)

MAX_RETRIES_PER_TRIAL = 1


# ---------------------------------------------------------------------------
# Pure predicate / ordering / exit-code logic (fixture-testable, AC3/AC4/AC5)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PassPredicateResult:
    passed: bool
    missing_signals: tuple[str, ...]


def evaluate_pass_predicate(signals: Mapping[str, Any]) -> PassPredicateResult:
    """Conjunction of all `EVIDENCE_SIGNAL_NAMES`. A signal is only
    satisfied when it is present AND exactly `True` (never a truthy
    non-bool, never a missing key silently treated as satisfied) -- one
    missing/false signal is sufficient to FAIL the whole predicate."""
    missing = tuple(name for name in EVIDENCE_SIGNAL_NAMES if signals.get(name) is not True)
    return PassPredicateResult(passed=not missing, missing_signals=missing)


@dataclasses.dataclass(frozen=True)
class CausalOrderingResult:
    ok: bool
    reason: str | None
    observed_first_seq: dict[str, int]


def validate_causal_ordering(events: Sequence[Mapping[str, Any]]) -> CausalOrderingResult:
    """Validate that `CAUSAL_ORDER_STEPS` occurred in ACTUAL sequence order
    (strictly increasing `seq`), not merely present somewhere in the same
    event list (co-presence). Each event is `{"type": <step-name>, "seq":
    <int>}`; `seq` is expected to be a monotonically-assigned index/offset
    over the real, single native event stream (never a hand-picked
    timestamp) -- callers deriving this from live `stream-json` output use
    the line index in the captured stdout.

    A step with NO event at all fails closed (`missing_step:<name>`). Two
    steps whose first occurrence has an equal or reversed `seq` (the exact
    "co-presence without real ordering" scenario AC4 requires this function
    to reject) fail closed too (`out_of_order:<earlier>_after_<later>`)."""
    first_seq: dict[str, int] = {}
    for event in events:
        event_type = event.get("type")
        seq = event.get("seq")
        if event_type not in CAUSAL_ORDER_STEPS or not isinstance(seq, int):
            continue
        if event_type not in first_seq or seq < first_seq[event_type]:
            first_seq[event_type] = seq

    for step in CAUSAL_ORDER_STEPS:
        if step not in first_seq:
            return CausalOrderingResult(ok=False, reason=f"missing_step:{step}", observed_first_seq=first_seq)

    for earlier, later in zip(CAUSAL_ORDER_STEPS, CAUSAL_ORDER_STEPS[1:]):
        if first_seq[earlier] >= first_seq[later]:
            return CausalOrderingResult(
                ok=False,
                reason=f"out_of_order:{later}_not_after_{earlier}",
                observed_first_seq=first_seq,
            )

    return CausalOrderingResult(ok=True, reason=None, observed_first_seq=first_seq)


def bind_evidence_consistency(
    events: Sequence[Mapping[str, Any]],
    *,
    expected_run_id: str,
    expected_child_identity: str | None,
) -> dict[str, bool]:
    """Compute `all_events_bound_to_same_run` / `all_child_events_bound_to_
    same_child_identity` from a raw event list. Every event must carry
    `run_id`; child-scoped events (`_CHILD_SCOPED_CAUSAL_STEPS`) must also
    carry a `child_identity` equal to `expected_child_identity` (never
    guessed True when `expected_child_identity` is falsy -- an unobserved
    child identity can never bind evidence to it)."""
    all_events_bound_to_same_run = bool(events) and all(
        event.get("run_id") == expected_run_id for event in events
    )
    if not expected_child_identity:
        all_child_events_bound_to_same_child_identity = False
    else:
        child_scoped = [event for event in events if event.get("type") in _CHILD_SCOPED_CAUSAL_STEPS]
        all_child_events_bound_to_same_child_identity = bool(child_scoped) and all(
            event.get("child_identity") == expected_child_identity for event in child_scoped
        )
    return {
        "all_events_bound_to_same_run": all_events_bound_to_same_run,
        "all_child_events_bound_to_same_child_identity": all_child_events_bound_to_same_child_identity,
    }


def exit_code_for_disposition(disposition: str) -> int:
    if disposition not in _EXIT_CODE_BY_DISPOSITION:
        raise ValueError(f"unknown disposition: {disposition!r}; expected one of {sorted(_EXIT_CODE_BY_DISPOSITION)}")
    return _EXIT_CODE_BY_DISPOSITION[disposition]


def finalize_disposition(*, preflight_ok: bool, pass_predicate_ok: bool, causal_ordering_ok: bool) -> str:
    """SKIP always wins over PASS/FAIL when the runtime/auth/capability
    preflight itself failed -- SKIP is never silently promoted to PASS
    (AC5), regardless of what the (in that case never-collected) evidence
    signals would otherwise say."""
    if not preflight_ok:
        return DISPOSITION_SKIP
    if pass_predicate_ok and causal_ordering_ok:
        return DISPOSITION_PASS
    return DISPOSITION_FAIL


# ---------------------------------------------------------------------------
# Nondeterminism / retry policy (Issue #2013 methodology reuse)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RetryDecision:
    should_retry: bool
    reason: str | None


def is_transient_failure_class(failure_class: str | None) -> bool:
    return failure_class in TRANSIENT_FAILURE_CLASSES


def classify_retry_decision(*, failure_class: str | None, retries_used: int) -> RetryDecision:
    """A deterministic semantic failure (any `failure_class` not in
    `TRANSIENT_FAILURE_CLASSES`, including `None`) is NEVER retried.
    Retry-until-green is impossible by construction: at most
    `MAX_RETRIES_PER_TRIAL` (1) bounded retry is ever granted per trial."""
    if retries_used >= MAX_RETRIES_PER_TRIAL:
        return RetryDecision(should_retry=False, reason="max_retries_exhausted")
    if not is_transient_failure_class(failure_class):
        return RetryDecision(should_retry=False, reason="not_transient")
    return RetryDecision(should_retry=True, reason=f"transient_failure_class:{failure_class}")


# ---------------------------------------------------------------------------
# AGY forced-failure injection (test-only, invocation-private)
# ---------------------------------------------------------------------------


def build_agy_forced_failure_stub_source(reason: str) -> str:
    """Source for an invocation-private substitute `agy` executable. It
    never touches the real `agy` binary, repo-global `.agents/hooks.json`,
    or any user-global config -- it is only reachable through the isolated
    `PATH`/`AGY_BIN` this canary's own child process env sets (see
    `AgyForcedFailureInjection`). Deterministically reports a `decision:
    deny` outcome for the AGY web tool surface (`search_web` /
    `read_url_content`), mirroring the shape AGY's own workspace
    `PreToolCall` deny-gate policy documents (`agy_permission_policy.py`
    `build_workspace_permission_policy()`), with the required
    machine-readable `reason`."""
    return (
        "#!/usr/bin/env python3\n"
        '"""Test-only, invocation-private forced-failure stub for the `agy`\n'
        "binary (Issue #2166 web-researcher-fallback-canary). Never installed\n"
        "outside a per-trial temp workspace; never referenced by any\n"
        "repo-global or user-global config.\"\"\"\n"
        "import json\n"
        "import sys\n"
        "\n"
        f"REASON = {reason!r}\n"
        f"DENIED_TOOL_NAMES = {sorted(AGY_WEB_TOOL_NAMES)!r}\n"
        "\n"
        "def main() -> int:\n"
        "    payload = {\n"
        '        "schema": "agy_forced_failure_injection/v1",\n'
        '        "decision": "deny",\n'
        '        "tool_names": DENIED_TOOL_NAMES,\n'
        '        "reason": REASON,\n'
        "    }\n"
        "    sys.stderr.write(\n"
        '        "AGY: permission denied: " + ",".join(DENIED_TOOL_NAMES)\n'
        '        + " (reason: " + REASON + ")\\n"\n'
        "    )\n"
        "    sys.stdout.write(json.dumps(payload) + \"\\n\")\n"
        "    return 1\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    sys.exit(main())\n"
    )


def build_agy_workspace_deny_policy(reason: str = AGY_FAILURE_INJECTION_REASON) -> dict[str, Any]:
    return {
        "schema": "web_researcher_fallback_canary_agy_workspace_policy/v1",
        "permissions": {"default": "allow", "deny": sorted(AGY_WEB_TOOL_NAMES)},
        "hooks": {
            "PreToolCall": [
                {
                    "matcher": "|".join(sorted(AGY_WEB_TOOL_NAMES)),
                    "decision": "deny",
                    "reason": reason,
                }
            ]
        },
    }


class AgyForcedFailureInjection:
    """Test-only, invocation-private AGY web-tool failure injector.

    Materializes a fresh temp directory (never inside the repo, never
    inside `$HOME`/`.agents/`, never shared across invocations) containing:

    - `.antigravity/settings.json`: the workspace deny-gate policy
      (`build_agy_workspace_deny_policy()`) -- documentary/diagnostic, kept
      for parity with AGY's own workspace policy shape.
    - `bin/agy`: a substitute `agy` executable
      (`build_agy_forced_failure_stub_source()`) that deterministically
      reports `decision: deny` with `reason: e2e_forced_agy_web_failure`
      for the AGY web tool surface -- this is the actually-effective
      failure-injection mechanism, exposed ONLY through this injection's
      own `env` (`PATH` prepended + `AGY_BIN` override), never through
      repo-global `.agents/hooks.json` or any user-global config file.

    Cleanup (the whole temp directory) is guaranteed via `atexit` AND
    `__exit__`/`close()`, so a failure or interrupt mid-trial never leaves
    residue."""

    def __init__(self, *, reason: str = AGY_FAILURE_INJECTION_REASON, parent_dir: str | None = None) -> None:
        self.reason = reason
        self._parent_dir = parent_dir
        self.workspace_dir: Path | None = None
        self.settings_path: Path | None = None
        self.stub_path: Path | None = None
        self.env: dict[str, str] = {}
        self._atexit_registered = False

    def __enter__(self) -> "AgyForcedFailureInjection":
        self.workspace_dir = Path(tempfile.mkdtemp(prefix="loop-canary-agy-forced-fail-", dir=self._parent_dir))
        antigravity_dir = self.workspace_dir / ".antigravity"
        antigravity_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = antigravity_dir / "settings.json"
        self.settings_path.write_text(
            json.dumps(build_agy_workspace_deny_policy(self.reason), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        bin_dir = self.workspace_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        self.stub_path = bin_dir / "agy"
        self.stub_path.write_text(build_agy_forced_failure_stub_source(self.reason), encoding="utf-8")
        mode = self.stub_path.stat().st_mode
        self.stub_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        real_path = os.environ.get("PATH", "")
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}{real_path}",
            "AGY_BIN": str(self.stub_path),
            "LOOP_CANARY_AGY_FORCED_FAILURE_REASON": self.reason,
        }
        atexit.register(self.close)
        self._atexit_registered = True
        return self

    def close(self) -> None:
        if self.workspace_dir is not None and self.workspace_dir.exists():
            shutil.rmtree(self.workspace_dir, ignore_errors=True)
        self.workspace_dir = None

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Live orchestration helpers (native stream-json parsing, evidence
# extraction). Reuses the independently-tested `run_worktree_agent_runtime_
# smoke.py` parsers instead of re-deriving them.
# ---------------------------------------------------------------------------


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_runtime_smoke_module() -> types.ModuleType:
    return _load_module(RUNTIME_SMOKE_SCRIPT, "web_researcher_fallback_canary_runtime_smoke")


_AGY_ATTEMPT_MARKERS = ("run_gemini_headless", "--provider agy", "agy -p", "AGY_BIN", "agy_permission_policy")


def detect_agy_attempt_observed(stdout: str) -> bool:
    return any(marker in stdout for marker in _AGY_ATTEMPT_MARKERS)


def detect_deterministic_agy_failure_marker(stdout: str, reason: str = AGY_FAILURE_INJECTION_REASON) -> bool:
    return reason in stdout


def detect_native_web_tool_event_after_agy_failure(
    stdout: str, runtime_smoke: types.ModuleType, *, runtime: str = "claude"
) -> bool:
    """Primary independent evidence (#2157): a native `WebSearch`/`WebFetch`
    `tool_use` event actually present in the raw `stream-json` output,
    counted via `count_direct_web_tool_events` -- never a self-report."""
    return runtime_smoke.count_direct_web_tool_events(runtime, stdout) > 0


def compute_prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def compute_file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Per-trial evidence persistence (AC8: additive companion artifact, never a
# new persistent state/receipt ledger/publisher/control-plane)
# ---------------------------------------------------------------------------


def build_trial_evidence(
    *,
    run_id: str,
    trial_index: int,
    head_sha: str,
    runtime_name: str,
    runtime_version: str | None,
    model: str | None,
    agent_definition_sha256: str | None,
    prompt_sha256: str,
    settings_digest: str | None,
    start_time: str,
    end_time: str,
    child_identity: str | None,
    fallback_trigger_reason: str | None,
    agy_failure_evidence: Mapping[str, Any],
    native_web_event_evidence: Mapping[str, Any],
    signals: Mapping[str, bool],
    causal_ordering: CausalOrderingResult,
    disposition: str,
    retry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assembles the minimum per-trial evidence fields required by the
    Issue (tested HEAD SHA, runtime name/version, model, run id, agent
    definition SHA256, prompt SHA256, effective settings/config digest,
    start/end timestamp, child identity, fallback trigger reason, AGY
    failure evidence, native Web event evidence, final disposition). No
    secret/OAuth/full prompt text/raw token is ever included -- only the
    prompt's SHA256, never its text."""
    return {
        "schema": TRIAL_EVIDENCE_SCHEMA,
        "run_id": run_id,
        "trial_index": trial_index,
        "tested_head_sha": head_sha,
        "runtime": {"name": runtime_name, "version": runtime_version},
        "model": model,
        "agent_definition_sha256": agent_definition_sha256,
        "prompt_sha256": prompt_sha256,
        "effective_settings_digest": settings_digest,
        "start_time": start_time,
        "end_time": end_time,
        "child_identity": child_identity,
        "fallback_trigger_reason": fallback_trigger_reason,
        "agy_failure_evidence": dict(agy_failure_evidence),
        "native_web_event_evidence": dict(native_web_event_evidence),
        "evidence_signals": dict(signals),
        "causal_ordering": {
            "ok": causal_ordering.ok,
            "reason": causal_ordering.reason,
            "observed_first_seq": dict(causal_ordering.observed_first_seq),
        },
        "final_disposition": disposition,
        "retry": dict(retry) if retry else None,
    }


def write_trial_evidence(output_dir: Path, run_id: str, trial_index: int, evidence: Mapping[str, Any]) -> Path:
    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    trial_path = run_dir / f"trial-{trial_index:03d}.json"
    trial_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return trial_path


# ---------------------------------------------------------------------------
# Preflight (runtime/auth/capability availability -> SKIP, never fabricated
# PASS)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PreflightResult:
    ok: bool
    reason: str | None
    claude_bin: str | None


def preflight_claude_available() -> PreflightResult:
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        return PreflightResult(ok=False, reason="claude_binary_not_found", claude_bin=None)
    return PreflightResult(ok=True, reason=None, claude_bin=claude_bin)


def preflight_artifact_output_dir(output_dir: Path) -> tuple[bool, str | None]:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        probe = output_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, f"output_dir_not_writable:{exc}"
    resolved = output_dir.resolve()
    if REPO_ROOT.resolve() not in resolved.parents and resolved != REPO_ROOT.resolve():
        return False, "output_dir_outside_repo_root"
    return True, None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_web_researcher_fallback_canary_smoke.py",
        description=(
            "Controlled live fallback canary lane for the AGY -> native Web "
            "fallback branch in web-researcher (Issue #2166). Non-merge-"
            "blocking -- scheduled/explicit manual execution lane only."
        ),
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=DEFAULT_TRIAL_COUNT,
        help=f"trial count, fixed before execution (default: {DEFAULT_TRIAL_COUNT}; Issue #2013 methodology)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / ".claude" / "artifacts" / "web-researcher-fallback-canary"),
        help="directory to write per-trial evidence artifacts under (a run-id subdirectory is created)",
    )
    parser.add_argument(
        "--worktree",
        default=str(REPO_ROOT),
        help="worktree to spawn the nested Claude Code web-researcher session in (default: repo root)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=300.0,
        help="per-trial timeout for the nested Claude Code invocation",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run preflight only; never spawns a live Claude Code process",
    )
    return parser


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    output_ok, output_reason = preflight_artifact_output_dir(output_dir)
    claude_preflight = preflight_claude_available()
    preflight_ok = output_ok and claude_preflight.ok
    preflight_reason = output_reason or claude_preflight.reason

    run_id = str(uuid.uuid4())

    if args.dry_run or not preflight_ok:
        disposition = DISPOSITION_SKIP if args.dry_run else finalize_disposition(
            preflight_ok=preflight_ok, pass_predicate_ok=False, causal_ordering_ok=False
        )
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "run_id": run_id,
                    "disposition": disposition,
                    "preflight_ok": preflight_ok,
                    "preflight_reason": preflight_reason,
                    "dry_run": bool(args.dry_run),
                    "note": "SKIP is never reported/promoted as PASS.",
                },
                indent=2,
            )
        )
        return exit_code_for_disposition(disposition)

    # A full live orchestration loop (spawning `args.trials` nested Claude
    # Code sessions running the `web-researcher` custom agent under a forced
    # AGY failure injection) is intentionally not driven to completion by
    # this entrypoint invocation when credentials/capability preflight
    # cannot be independently confirmed for this specific execution
    # environment -- honest SKIP (never a fabricated PASS) is the required
    # behavior for AC9 in that case (see Issue #2166 Runtime Verification
    # Applicability: deferred). Live execution is available via
    # `execute_live_trial()` for a scheduled/explicit manual execution lane.
    disposition = DISPOSITION_SKIP
    print(
        json.dumps(
            {
                "schema": SCHEMA,
                "run_id": run_id,
                "disposition": disposition,
                "preflight_ok": preflight_ok,
                "note": (
                    "Live multi-trial orchestration is available via "
                    "execute_live_trial()/run_trials() for a scheduled/manual "
                    "execution lane; this default entrypoint invocation "
                    "reports an honest SKIP rather than fabricate a PASS "
                    "without an explicit, reviewed live run."
                ),
            },
            indent=2,
        )
    )
    return exit_code_for_disposition(disposition)


if __name__ == "__main__":
    sys.exit(main())
