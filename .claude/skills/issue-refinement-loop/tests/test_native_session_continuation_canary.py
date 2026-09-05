"""Issue #2153 focused tests for run_native_session_continuation_canary.py.

``test_claude_native_native_session_continuation_canary`` (AC1) and
``test_claude_gpt_native_session_continuation_canary`` (AC2) each invoke the
canary runner as a subprocess and translate ITS exit code (0/1/77) into
pytest pass/fail/skip -- this test process never synthesizes its own exit 77
(AC7). ``test_argv_contract_*`` (AC8) and the smaller pure unit tests below
exercise the runner's argv-contract and fallback-classification functions
directly, without launching any runtime, so they always execute regardless
of runtime/auth/proxy availability.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "issue-refinement-loop"
    / "scripts"
    / "run_native_session_continuation_canary.py"
)
CLAUDE_GPT_LAUNCH_SH = REPO_ROOT / "scripts" / "claude-gpt" / "launch.sh"
EVIDENCE_ROOT = REPO_ROOT / "artifacts" / "native-session-continuation-canary"

# Real launches per lane; each is individually bounded by --timeout-seconds.
# The outer subprocess timeout must be a generous multiple of the actual
# number of timed operations the runner performs for the given adapter, so a
# genuine per-launch bound (not this outer one) is what fires first (Issue
# #2153 OWNER review P1 fix-delta). The native lane performs 3 timed
# launches (initial/resume/fresh); the claude-gpt lane performs 4 (an extra
# ``--check-only`` preflight probe before the same 3 launches) -- an outer
# timeout computed only for 3 launches on the claude-gpt lane could fire
# BEFORE the runner's own process-group cleanup/evidence-write completes.
_PER_LAUNCH_TIMEOUT_SECONDS = 180.0
_MAX_TURNS = 3


def _outer_runner_timeout_seconds(adapter: str) -> int:
    launch_count = 4 if adapter == "claude-gpt" else 3
    return int(_PER_LAUNCH_TIMEOUT_SECONDS * launch_count + 120)

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _load_canary_module():
    spec = importlib.util.spec_from_file_location(
        "issue_2153_native_session_continuation_canary", RUNNER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runner_accepts_current_worktree() -> bool:
    """Whether this pytest checkout is eligible for a spawned canary run.

    Mirrors ``scripts/agent-ops/tests/test_run_worktree_agent_runtime_smoke.py``
    and ``tests/test_codex_issue_creator_editor_runtime_smoke.py``'s own
    linked-worktree eligibility check: the canary's ``verify_worktree_identity``
    intentionally refuses a root checkout (only a linked worktree under
    ``.claude/worktrees/`` is accepted), so a pre-merge verifier checkout
    that is not itself a linked worktree must SKIP rather than fail.
    """
    module = _load_canary_module()
    try:
        module.verify_worktree_identity(str(REPO_ROOT), module._default_repo_root())
    except module.IdentityError:
        return False
    return True


def _assert_evidence_sanitized(evidence: dict) -> None:
    """Issue #2153 artifact_requirements: no raw (UUID-shaped) session id,
    no raw stdout event-stream capture, in the written evidence artifact."""
    serialized = json.dumps(evidence)
    assert not _UUID_RE.search(serialized), (
        "evidence must not contain a raw (UUID-shaped) session id"
    )
    assert "stdout_redacted_tail" not in serialized, (
        "evidence must not contain a raw captured stdout event stream"
    )
    for phase_key in ("initial_launch", "same_continuation_launch", "fresh_launch"):
        phase = evidence.get(phase_key)
        if not phase:
            continue
        session_hash = phase.get("observed_session_id_hash")
        if session_hash is not None:
            assert re.fullmatch(r"[0-9a-f]{16}", session_hash), (
                f"observed_session_id_hash must be a bounded hash, got: {session_hash!r}"
            )


def _run_canary(adapter: str, claude_bin: str | None) -> tuple[int, dict | None, str, str]:
    if not RUNNER.is_file() or not _runner_accepts_current_worktree():
        pytest.skip("linked worktree runtime surface is unavailable")

    output_dir = EVIDENCE_ROOT / f"{adapter}-{time.time_ns()}"
    command = [
        sys.executable,
        str(RUNNER),
        "--worktree", str(REPO_ROOT),
        "--claude-adapter", adapter,
        "--output-dir", str(output_dir),
        "--timeout-seconds", str(_PER_LAUNCH_TIMEOUT_SECONDS),
        "--max-turns", str(_MAX_TURNS),
        "--require-clean-postcondition",
    ]
    if claude_bin:
        command += ["--claude-bin", claude_bin]
    outer_timeout = _outer_runner_timeout_seconds(adapter)
    try:
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=outer_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # Issue #2153 OWNER review P1 fix-delta: distinguish an emergency
        # OUTER timeout (this pytest process's own subprocess.run bound
        # firing) from an ordinary runner-reported FAIL. If this fires, the
        # runner itself never got a chance to run its own process-group
        # cleanup / write its evidence.json receipt -- that is a
        # timeout-budget bug in this test harness, not a canary FAIL.
        pytest.fail(
            f"native session continuation canary emergency OUTER timeout fired "
            f"({outer_timeout}s) BEFORE the runner's own process-group cleanup/"
            f"evidence-write completed (adapter={adapter!r}); this is an outer "
            f"timeout-budget bug, not an ordinary canary FAIL. "
            f"partial stdout(tail)={(exc.stdout or '')[-1200:]!r}; "
            f"partial stderr(tail)={(exc.stderr or '')[-1200:]!r}"
        )
    evidence_path = output_dir / "evidence.json"
    evidence = (
        json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path.is_file() else None
    )
    return result.returncode, evidence, result.stdout, result.stderr


def _run_and_assert_pass_or_skip(adapter: str, claude_bin: str | None) -> None:
    returncode, evidence, stdout, stderr = _run_canary(adapter, claude_bin)

    assert evidence is not None, (
        f"canary exited without its evidence.json receipt: exit={returncode}; "
        f"stdout(tail)={stdout[-1200:]!r}; stderr(tail)={stderr[-1200:]!r}"
    )
    assert evidence.get("schema") == "NATIVE_SESSION_CONTINUATION_CANARY_RESULT_V1"
    _assert_evidence_sanitized(evidence)

    # AC7: this test translates the runner's OWN exit code into pass/fail/
    # skip. It never independently decides "unavailable" and synthesizes
    # exit 77 itself -- SKIP only ever follows the subprocess's own exit 77.
    if returncode == 77:
        assert evidence.get("verdict") == "SKIP"
        pytest.skip(f"native session continuation canary SKIP (exit 77): {evidence.get('skip_reason')}")

    assert returncode == 0, (
        f"native session continuation canary FAIL (exit {returncode}): "
        f"errors={evidence.get('errors')}; "
        f"provider_fallback={evidence.get('provider_fallback')}; "
        f"runtime_fallback={evidence.get('runtime_fallback')}"
    )
    assert evidence.get("verdict") == "PASS"
    assert evidence.get("provider_fallback") is False
    assert evidence.get("runtime_fallback") is False

    # AC8 (argv contract), self-verified by the runner and echoed into the
    # evidence artifact for each of the three launches.
    for phase_key in ("initial_launch", "same_continuation_launch", "fresh_launch"):
        phase = evidence[phase_key]
        assert phase["argv_contract_ok"] is True, phase.get("argv_contract_detail")

    # AC3: ID equality alone is not continuation proof -- the semantic
    # continuity marker probe must also have succeeded.
    assert evidence["same_continuation_launch"]["id_equality"] is True
    assert evidence["same_continuation_launch"]["semantic_continuity_marker_recalled"] is True

    # AC4: fresh launch must observe a genuinely distinct id.
    assert evidence["fresh_launch"]["id_inequality"] is True

    # In Scope: CLAUDE_CODE_SKIP_PROMPT_HISTORY must be explicitly unset
    # for the launch subprocess regardless of this test process's own env.
    assert evidence["env_contract"]["claude_code_skip_prompt_history_unset"] is True

    # AC6: cleanup / postcondition verdict (reused generic repo_fingerprint
    # primitives) must show no unexpected repository change.
    assert evidence["cleanup"]["postcondition_diffs"] == []


def test_claude_native_native_session_continuation_canary(monkeypatch):
    """AC1: claude-native lane initial launch observes a Claude Code
    session_id from native structured output; sanitized artifact only."""
    # In Scope: canary behavior must not depend on this env var, whether or
    # not it happens to be set in the calling process.
    monkeypatch.setenv("CLAUDE_CODE_SKIP_PROMPT_HISTORY", "1")
    _run_and_assert_pass_or_skip("native", None)


def test_claude_gpt_native_session_continuation_canary(monkeypatch):
    """AC2: claude-gpt lane runs as an independent Claude Code process /
    config boundary via scripts/claude-gpt/launch.sh, same observations."""
    if not CLAUDE_GPT_LAUNCH_SH.is_file():
        pytest.skip("scripts/claude-gpt/launch.sh is not present in this checkout")
    monkeypatch.setenv("CLAUDE_CODE_SKIP_PROMPT_HISTORY", "1")
    _run_and_assert_pass_or_skip("claude-gpt", str(CLAUDE_GPT_LAUNCH_SH))


# ---------------------------------------------------------------------------
# AC8: argv contract -- pure, always executed (no runtime required).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("adapter,bin_path", [
    ("native", "claude"),
    ("claude-gpt", "/abs/path/to/launch.sh"),
])
def test_argv_contract_initial_and_fresh_omit_continuation_flags(adapter, bin_path):
    module = _load_canary_module()
    for phase in ("initial", "fresh"):
        argv = module.build_launch_argv(bin_path, adapter, phase, max_turns=3)
        ok, detail = module.verify_argv_contract(argv, phase)
        assert ok, detail
        for forbidden in module.FORBIDDEN_INITIAL_FRESH_FLAGS:
            assert forbidden not in argv


@pytest.mark.parametrize("adapter,bin_path", [
    ("native", "claude"),
    ("claude-gpt", "/abs/path/to/launch.sh"),
])
def test_argv_contract_resume_includes_resume_exactly_once(adapter, bin_path):
    module = _load_canary_module()
    session_id = "fake-observed-session-id"
    argv = module.build_launch_argv(bin_path, adapter, "resume", max_turns=3, resume_session_id=session_id)
    ok, detail = module.verify_argv_contract(argv, "resume", resume_session_id=session_id)
    assert ok, detail
    assert argv.count("--resume") == 1
    idx = argv.index("--resume")
    assert argv[idx + 1] == session_id
    for forbidden in module.FORBIDDEN_RESUME_EXTRA_FLAGS:
        assert forbidden not in argv


def test_argv_contract_rejects_no_session_persistence_on_initial():
    module = _load_canary_module()
    bad_argv = module.build_launch_argv("claude", "native", "initial", max_turns=3) + [
        "--no-session-persistence"
    ]
    ok, detail = module.verify_argv_contract(bad_argv, "initial")
    assert ok is False
    assert "--no-session-persistence" in detail


def test_argv_contract_rejects_resume_id_mismatch():
    module = _load_canary_module()
    argv = module.build_launch_argv("claude", "native", "resume", max_turns=3, resume_session_id="id-a")
    ok, detail = module.verify_argv_contract(argv, "resume", resume_session_id="id-b")
    assert ok is False


def test_argv_contract_rejects_duplicate_resume_flag():
    module = _load_canary_module()
    argv = module.build_launch_argv("claude", "native", "resume", max_turns=3, resume_session_id="id-a")
    argv = argv + ["--resume", "id-a"]
    ok, detail = module.verify_argv_contract(argv, "resume", resume_session_id="id-a")
    assert ok is False


def test_build_launch_env_unsets_skip_prompt_history(monkeypatch):
    module = _load_canary_module()
    monkeypatch.setenv("CLAUDE_CODE_SKIP_PROMPT_HISTORY", "1")
    env = module.build_launch_env()
    assert "CLAUDE_CODE_SKIP_PROMPT_HISTORY" not in env


def test_build_launch_env_unset_is_idempotent_when_absent(monkeypatch):
    module = _load_canary_module()
    monkeypatch.delenv("CLAUDE_CODE_SKIP_PROMPT_HISTORY", raising=False)
    env = module.build_launch_env()
    assert "CLAUDE_CODE_SKIP_PROMPT_HISTORY" not in env


# ---------------------------------------------------------------------------
# AC5 / AC7: terminal-event and fallback classification -- pure.
# ---------------------------------------------------------------------------


def test_is_terminal_success_true_for_success_result_event():
    module = _load_canary_module()
    stdout = json.dumps({"type": "result", "is_error": False, "subtype": "success"}) + "\n"
    ok, reason = module.is_terminal_success(stdout)
    assert ok is True
    assert reason is None


def test_is_terminal_success_false_when_is_error_true():
    module = _load_canary_module()
    stdout = json.dumps({"type": "result", "is_error": True, "subtype": "error_during_execution"}) + "\n"
    ok, reason = module.is_terminal_success(stdout)
    assert ok is False
    assert reason is not None


def test_is_terminal_success_false_when_no_result_event():
    module = _load_canary_module()
    stdout = json.dumps({"type": "assistant", "message": {"content": "hello"}}) + "\n"
    ok, reason = module.is_terminal_success(stdout)
    assert ok is False


def test_detect_runtime_fallback_true_when_id_equal_but_marker_missing():
    module = _load_canary_module()
    assert module.detect_runtime_fallback(id_equal=True, marker_ok=False) is True


def test_detect_runtime_fallback_false_when_marker_recalled():
    module = _load_canary_module()
    assert module.detect_runtime_fallback(id_equal=True, marker_ok=True) is False


def test_detect_runtime_fallback_false_when_id_not_equal():
    module = _load_canary_module()
    # id inequality is itself an assertion failure handled elsewhere; the
    # fallback signal is specifically the id-equal-but-no-marker case.
    assert module.detect_runtime_fallback(id_equal=False, marker_ok=False) is False


def test_detect_provider_fallback_true_when_model_alias_not_ok():
    module = _load_canary_module()
    assert module.detect_provider_fallback({"model_alias_ok": False}) is True


def test_detect_provider_fallback_false_when_model_alias_ok():
    module = _load_canary_module()
    assert module.detect_provider_fallback({"model_alias_ok": True}) is False


def test_detect_provider_fallback_false_when_receipt_missing():
    module = _load_canary_module()
    assert module.detect_provider_fallback(None) is False


def test_marker_recalled_true_only_from_assistant_text():
    module = _load_canary_module()
    stdout = "\n".join([
        json.dumps({"type": "user", "message": {"content": "tok-shouldnotcount"}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "tok-abc123"}]}}),
    ])
    assert module.marker_recalled(stdout, "tok-abc123") is True
    assert module.marker_recalled(stdout, "tok-shouldnotcount") is False


def test_parse_claude_gpt_receipt_handles_embedded_multiline_json():
    """Regression for a live finding: the launch.sh --check-only success
    receipt embeds a pretty-printed (multi-line) nested ``preflight``
    object, which the reused single-line regex extractor cannot match."""
    module = _load_canary_module()
    text = (
        'launcher=/x/launch.sh git=abc dirty=false proxy=v0.1.0\n'
        '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"ok","mode":"check_only",'
        '"model_alias_ok":true,"preflight":{\n'
        '  "schema": "CLAUDE_GPT_PREFLIGHT_RESULT_V1",\n'
        '  "binary_available": true\n'
        '}}\n'
    )
    receipt = module._parse_claude_gpt_receipt(text)
    assert receipt is not None
    assert receipt["status"] == "ok"
    assert receipt["model_alias_ok"] is True
    assert receipt["preflight"]["binary_available"] is True


def test_detect_claude_gpt_launch_failure_receipt_detects_blocked_status():
    module = _load_canary_module()
    stdout = json.dumps(
        {"schema": "CLAUDE_GPT_LAUNCH_RESULT_V1", "status": "blocked", "reason": "claude_binary_not_found"}
    )
    receipt = module.detect_claude_gpt_launch_failure_receipt(stdout, "")
    assert receipt is not None
    assert receipt["status"] == "blocked"


def test_detect_claude_gpt_launch_failure_receipt_none_for_ok_status():
    module = _load_canary_module()
    stdout = json.dumps({"schema": "CLAUDE_GPT_LAUNCH_RESULT_V1", "status": "ok"})
    assert module.detect_claude_gpt_launch_failure_receipt(stdout, "") is None


# ---------------------------------------------------------------------------
# Issue #2153 OWNER review P0 fix-delta: judge_phase() consolidated
# timed_out -> capability_skip -> non-zero exit -> terminal-success ordering.
# A terminal-success-looking event present in (partial) stdout must never be
# promoted to OK when the process actually timed out or exited non-zero.
# ---------------------------------------------------------------------------

_TERMINAL_SUCCESS_STDOUT = json.dumps({"type": "result", "is_error": False, "subtype": "success"}) + "\n"


def test_judge_phase_terminal_success_event_but_nonzero_exit_is_fail():
    """Regression: a terminal-success (type=result) event present in stdout
    must not be promoted to OK when the process exited non-zero."""
    module = _load_canary_module()
    verdict, reason = module.judge_phase(1, False, _TERMINAL_SUCCESS_STDOUT, "")
    assert verdict == "fail"
    assert reason is not None


def test_judge_phase_terminal_success_event_but_timed_out_is_fail():
    """Regression: the shared ``_run()`` helper kills the process group on
    timeout but still returns whatever partial stdout was captured, which
    can misleadingly already contain a terminal-success event; a timeout
    must always FAIL regardless."""
    module = _load_canary_module()
    verdict, reason = module.judge_phase(0, True, _TERMINAL_SUCCESS_STDOUT, "")
    assert verdict == "fail"
    assert reason is not None


def test_judge_phase_turn_limit_reached_is_fail():
    """Regression: reaching the --max-turns bound (flag accepted; a runtime
    failure, not a capability gap) must FAIL, not be promoted to OK/SKIP."""
    module = _load_canary_module()
    verdict, reason = module.judge_phase(1, False, "", "Claude reached max turns limit\n")
    assert verdict == "fail"
    assert reason is not None


def test_judge_phase_ok_when_clean_terminal_success():
    module = _load_canary_module()
    verdict, reason = module.judge_phase(0, False, _TERMINAL_SUCCESS_STDOUT, "")
    assert verdict == "ok"
    assert reason is None


def test_judge_phase_skip_on_capability_skip_classification():
    """A narrowly-matched parser-level unknown/unrecognized-option
    rejection of one of this runner's own fixed argv flags (and no valid
    JSON event observed) is a genuine capability SKIP, not FAIL."""
    module = _load_canary_module()
    verdict, reason = module.judge_phase(
        1, False, "", "error: unknown option '--max-turns'\n"
    )
    assert verdict == "skip"
    assert reason is not None


# ---------------------------------------------------------------------------
# Issue #2050: judge_phase_with_bounded_retry() -- resume-phase-only bounded
# retry so a single transient wait-timeout observation does not immediately
# freeze the phase verdict at "fail". judge_phase() itself (used directly by
# the initial/fresh phases) is unchanged; these tests exercise the NEW
# wrapper in isolation via a pure observation-producing callable, with no
# real Claude Code launch required.
# ---------------------------------------------------------------------------


def test_bounded_retry_recovers_from_transient_timeout():
    """AC1: the first observation being timed_out does not immediately
    return fail -- a bounded retry (default max_retries=1) is attempted."""
    module = _load_canary_module()
    observations = iter(
        [
            (0, True, "", ""),
            (0, False, _TERMINAL_SUCCESS_STDOUT, ""),
        ]
    )
    verdict, reason, attempts = module.judge_phase_with_bounded_retry(lambda: next(observations))
    assert verdict == "ok"
    assert len(attempts) == 2
    assert attempts[0]["timed_out"] is True
    assert attempts[0]["verdict"] == "fail"
    assert attempts[1]["timed_out"] is False
    assert attempts[1]["verdict"] == "ok"


def test_bounded_retry_final_verdict_reflects_successful_retry_not_stale_timeout():
    """AC2: once the bounded retry observation succeeds, the FINAL
    (verdict, reason) returned by judge_phase_with_bounded_retry() reflects
    that success -- the stale first timeout's own (verdict, reason) must
    never leak into the returned final result."""
    module = _load_canary_module()
    observations = iter(
        [
            (0, True, "", ""),
            (0, False, _TERMINAL_SUCCESS_STDOUT, ""),
        ]
    )
    verdict, reason, attempts = module.judge_phase_with_bounded_retry(lambda: next(observations))
    assert verdict == "ok"
    assert reason is None
    stale_reason = attempts[0]["reason"]
    assert stale_reason is not None and "timed out" in stale_reason
    # The final (verdict, reason) must not be the stale timed-out attempt's.
    assert (verdict, reason) != (attempts[0]["verdict"], attempts[0]["reason"])


def test_bounded_retry_exhausted_still_times_out_is_fail():
    """AC3: if the bounded retry is ALSO timed_out, the final verdict is
    fail -- and observe_fn is called no more than max_retries + 1 times
    (bounded, never unlimited)."""
    module = _load_canary_module()
    calls = {"n": 0}

    def observe() -> tuple[int, bool, str, str]:
        calls["n"] += 1
        if calls["n"] > 2:
            raise AssertionError("observe_fn must not be called more than max_retries + 1 times")
        return (0, True, "", "")

    verdict, reason, attempts = module.judge_phase_with_bounded_retry(observe)
    assert verdict == "fail"
    assert reason is not None
    assert calls["n"] == 2
    assert len(attempts) == 2
    assert all(a["timed_out"] for a in attempts)


def test_bounded_retry_single_observation_matches_bare_judge_phase_for_non_timeout():
    """A non-timed-out first observation is judged exactly like a bare
    judge_phase() call -- no retry is attempted (only one observe_fn call)."""
    module = _load_canary_module()
    calls = {"n": 0}

    def observe() -> tuple[int, bool, str, str]:
        calls["n"] += 1
        return (0, False, _TERMINAL_SUCCESS_STDOUT, "")

    verdict, reason, attempts = module.judge_phase_with_bounded_retry(observe)
    bare_verdict, bare_reason = module.judge_phase(0, False, _TERMINAL_SUCCESS_STDOUT, "")
    assert (verdict, reason) == (bare_verdict, bare_reason)
    assert calls["n"] == 1
    assert len(attempts) == 1


# ---------------------------------------------------------------------------
# Issue #2050: regression coverage that a plain `rg "def judge_phase"`
# substring check is insufficient to prove the real resume call site inside
# main() is actually wired to judge_phase_with_bounded_retry() (it would
# also match `def judge_phase_with_bounded_retry`). This test runs the ACTUAL
# production main() end-to-end with only I/O-boundary primitives (subprocess
# launch, worktree identity, output-dir preparation, signal handlers)
# monkeypatched -- proving via call-site spying that judge_phase_with_bounded_
# retry is invoked for the resume phase and NOT for the initial/fresh phases,
# which keep calling judge_phase() directly.
# ---------------------------------------------------------------------------


def test_resume_call_site_uses_bounded_retry_wrapper_not_initial_or_fresh(monkeypatch, tmp_path):
    module = _load_canary_module()

    original_judge_phase = module.judge_phase
    judge_phase_calls: list[tuple] = []

    def spy_judge_phase(exit_code, timed_out, stdout, stderr):
        judge_phase_calls.append((exit_code, timed_out, stdout, stderr))
        return original_judge_phase(exit_code, timed_out, stdout, stderr)

    original_bounded_retry = module.judge_phase_with_bounded_retry
    bounded_retry_calls: list = []

    def spy_bounded_retry(observe_fn, **kwargs):
        bounded_retry_calls.append(observe_fn)
        return original_bounded_retry(observe_fn, **kwargs)

    monkeypatch.setattr(module, "judge_phase", spy_judge_phase)
    monkeypatch.setattr(module, "judge_phase_with_bounded_retry", spy_bounded_retry)

    # I/O-boundary mocks -- subprocess launch / worktree identity / output-dir
    # preparation / classification are not this test's concern, only the
    # CALL-SITE WIRING of judge_phase vs. judge_phase_with_bounded_retry is.
    monkeypatch.setattr(module, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(module, "_default_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(module, "verify_worktree_identity", lambda worktree_arg, repo_root: worktree_arg)
    monkeypatch.setattr(module, "prepare_output_dir", lambda output_dir: None)
    monkeypatch.setattr(module, "preflight_claude_available", lambda claude_bin: ("fake-claude-bin", None))
    monkeypatch.setattr(module, "extract_claude_resolved_executable_sha256", lambda resolved_bin: "fakehash")
    monkeypatch.setattr(
        module,
        "classify_claude_structured_outcome",
        lambda exit_code, stdout, stderr, timed_out: ("ok", None),
    )
    monkeypatch.setattr(
        module,
        "is_terminal_success",
        lambda stdout: (True, None) if "TERMINAL_OK" in stdout else (False, "not terminal"),
    )
    monkeypatch.setattr(module, "marker_recalled", lambda stdout, marker: True)

    def fake_extract_parent_session_id(stdout: str) -> str | None:
        if "SESSION_A" in stdout:
            return "session-a"
        if "SESSION_C" in stdout:
            return "session-c"
        return None

    monkeypatch.setattr(module, "extract_claude_parent_session_id", fake_extract_parent_session_id)

    resume_attempts = {"n": 0}

    def fake_run(argv, *, cwd=None, timeout=None, input_text=None, env=None):
        # _run()'s real return order is (returncode, stdout, stderr, timed_out).
        if "--resume" in argv:
            resume_attempts["n"] += 1
            if resume_attempts["n"] == 1:
                # Transient wait-timeout observation on the FIRST resume attempt.
                return 0, "", "", True
            return 0, "SESSION_A TERMINAL_OK", "", False
        if "--tools" in argv:
            return 0, "SESSION_A TERMINAL_OK", "", False
        return 0, "SESSION_C TERMINAL_OK", "", False

    monkeypatch.setattr(module, "_run", fake_run)

    output_dir = tmp_path / "evidence"
    exit_code = module.main(
        [
            "--worktree", str(tmp_path),
            "--claude-adapter", "native",
            "--output-dir", str(output_dir),
            "--timeout-seconds", "5",
        ]
    )

    evidence_path = output_dir / "evidence.json"
    assert exit_code == 0, evidence_path.read_text() if evidence_path.is_file() else "no evidence written"

    # judge_phase_with_bounded_retry is invoked EXACTLY ONCE (resume phase only).
    assert len(bounded_retry_calls) == 1

    # judge_phase() itself is invoked 4 times total: once directly for the
    # initial phase, once directly for the fresh phase, and twice more
    # (internally, via the wrapper) for the resume phase's two observations
    # (AC4: judge_phase() semantics/call sites for initial/fresh unchanged).
    assert len(judge_phase_calls) == 4
    timed_out_flags = [call[1] for call in judge_phase_calls]
    assert timed_out_flags.count(True) == 1
    assert timed_out_flags.count(False) == 3
    stdouts = [call[2] for call in judge_phase_calls]
    assert any("SESSION_A" in s for s in stdouts)
    assert any("SESSION_C" in s for s in stdouts)

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["verdict"] == "PASS"
    # Bounded-retry evidence: the stale first (timed-out) resume attempt is
    # preserved as evidence, but the authoritative same_continuation_launch
    # record reflects the successful retry, not the stale timeout.
    assert evidence["same_continuation_launch"]["timed_out"] is False
    stale_attempts = evidence["same_continuation_launch"]["bounded_retry_stale_attempts"]
    assert len(stale_attempts) == 1
    assert stale_attempts[0]["timed_out"] is True


# ---------------------------------------------------------------------------
# Issue #2153 OWNER review P1 fix-delta: classify_launcher_receipt() SKIP
# allowlist (exit 3/4/7 only) vs. everything-else FAIL (caller/launcher
# integration bugs must never be silently absorbed into SKIP).
# ---------------------------------------------------------------------------


def _receipt(status: str, reason: str) -> dict:
    return {"schema": "CLAUDE_GPT_LAUNCH_RESULT_V1", "status": status, "reason": reason}


@pytest.mark.parametrize(
    "rc,receipt,expected_verdict",
    [
        (3, _receipt("blocked", "claude_binary_not_found"), "skip"),
        (4, _receipt("blocked", "chatgpt_auth_unavailable"), "skip"),
        (7, _receipt("failed", "proxy_not_ready_or_bind_not_confirmed"), "skip"),
        (7, _receipt("failed", "model_alias_not_resolved"), "skip"),
        (2, _receipt("blocked", "missing_value"), "fail"),
        (2, _receipt("blocked", "unknown_launcher_option"), "fail"),
        (2, _receipt("blocked", "unexpected_positional_argument_before_double_dash"), "fail"),
        (2, _receipt("blocked", "policy_weakening_flag_rejected"), "fail"),
        (5, _receipt("blocked", "canonical_path_under_repo_or_worktree"), "fail"),
        (6, _receipt("blocked", "invalid_generated_settings"), "fail"),
        (9, _receipt("blocked", "spark_launch_nonce_unsafe_chars"), "fail"),
        (1, {"status": "unknown-schema-junk"}, "fail"),
        (1, None, "fail"),
    ],
)
def test_classify_launcher_receipt(rc, receipt, expected_verdict):
    module = _load_canary_module()
    verdict, reason = module.classify_launcher_receipt(rc, receipt)
    assert verdict == expected_verdict
    assert isinstance(reason, str) and reason


# ---------------------------------------------------------------------------
# Issue #2153 OWNER review P1 fix-delta: apply_postcondition_check() must
# actually EXECUTE the after-fingerprint/diff check on every finishing
# return path, and must truthfully report ``postcondition_checked: False``
# when ``before_fingerprint`` was never captured (a failure occurred before
# preflight completed).
# ---------------------------------------------------------------------------


def test_apply_postcondition_check_not_executed_when_before_fingerprint_missing():
    module = _load_canary_module()
    evidence = {"cleanup": {}, "errors": []}
    verdict, code = module.apply_postcondition_check(
        evidence,
        before_fingerprint=None,
        worktree_real="/does/not/matter",
        require_clean_postcondition=True,
        verdict="PASS",
        code=0,
    )
    assert (verdict, code) == ("PASS", 0)
    assert evidence["cleanup"]["postcondition_checked"] is False
    assert "postcondition_diffs" not in evidence["cleanup"]


def test_apply_postcondition_check_not_executed_when_not_required(tmp_path):
    module = _load_canary_module()
    evidence = {"cleanup": {}, "errors": []}
    verdict, code = module.apply_postcondition_check(
        evidence,
        before_fingerprint={"anything": True},
        worktree_real=str(tmp_path),
        require_clean_postcondition=False,
        verdict="PASS",
        code=0,
    )
    assert (verdict, code) == ("PASS", 0)
    assert evidence["cleanup"]["postcondition_checked"] is False


def test_apply_postcondition_check_downgrades_pass_to_fail_on_diff(monkeypatch):
    module = _load_canary_module()
    monkeypatch.setattr(module, "repo_fingerprint", lambda worktree, ignore: {"after": True})
    monkeypatch.setattr(module, "diff_fingerprints", lambda before, after: ["untracked file appeared"])
    evidence = {"cleanup": {}, "errors": []}
    verdict, code = module.apply_postcondition_check(
        evidence,
        before_fingerprint={"before": True},
        worktree_real="/x",
        require_clean_postcondition=True,
        verdict="PASS",
        code=0,
    )
    assert (verdict, code) == ("FAIL", 1)
    assert evidence["cleanup"]["postcondition_checked"] is True
    assert evidence["cleanup"]["postcondition_diffs"] == ["untracked file appeared"]
    assert any("postcondition violated" in e for e in evidence["errors"])


def test_apply_postcondition_check_downgrades_skip_to_fail_on_diff(monkeypatch):
    """A real repository-state mutation must never be hidden behind a SKIP
    verdict either."""
    module = _load_canary_module()
    monkeypatch.setattr(module, "repo_fingerprint", lambda worktree, ignore: {"after": True})
    monkeypatch.setattr(module, "diff_fingerprints", lambda before, after: ["untracked file appeared"])
    evidence = {"cleanup": {}, "errors": []}
    verdict, code = module.apply_postcondition_check(
        evidence,
        before_fingerprint={"before": True},
        worktree_real="/x",
        require_clean_postcondition=True,
        verdict="SKIP",
        code=77,
    )
    assert (verdict, code) == ("FAIL", 1)


def test_apply_postcondition_check_keeps_pass_when_no_diff(monkeypatch):
    module = _load_canary_module()
    monkeypatch.setattr(module, "repo_fingerprint", lambda worktree, ignore: {"after": True})
    monkeypatch.setattr(module, "diff_fingerprints", lambda before, after: [])
    evidence = {"cleanup": {}, "errors": []}
    verdict, code = module.apply_postcondition_check(
        evidence,
        before_fingerprint={"before": True},
        worktree_real="/x",
        require_clean_postcondition=True,
        verdict="PASS",
        code=0,
    )
    assert (verdict, code) == ("PASS", 0)
    assert evidence["cleanup"]["postcondition_checked"] is True
    assert evidence["cleanup"]["postcondition_diffs"] == []


# ---------------------------------------------------------------------------
# Issue #2153 OWNER review P2 (low-risk, applied): the marker-only
# semantic-continuity probe launches (initial/resume) disable built-in tools
# via ``--tools ""`` to reduce flakiness/latency/accidental worktree
# mutation. The fresh launch (AC4 distinct-id check, not part of the marker
# probe) is intentionally left unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("adapter,bin_path", [
    ("native", "claude"),
    ("claude-gpt", "/abs/path/to/launch.sh"),
])
def test_argv_marker_probe_launches_disable_builtin_tools(adapter, bin_path):
    module = _load_canary_module()
    for phase in ("initial", "resume"):
        argv = module.build_launch_argv(
            bin_path, adapter, phase, max_turns=3, resume_session_id="fake-id"
        )
        assert "--tools" in argv, f"{phase} launch argv must disable built-in tools: {argv}"
        idx = argv.index("--tools")
        assert argv[idx + 1] == "", f"{phase} launch --tools value must be empty string: {argv}"

    fresh_argv = module.build_launch_argv(bin_path, adapter, "fresh", max_turns=3)
    assert "--tools" not in fresh_argv, (
        f"fresh launch is not part of the marker probe and must be left unchanged: {fresh_argv}"
    )
