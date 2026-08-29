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

# Three real launches per lane; each is individually bounded by
# --timeout-seconds. The outer subprocess timeout is a generous multiple so
# a genuine per-launch bound (not this outer one) is what fires first.
_PER_LAUNCH_TIMEOUT_SECONDS = 180.0
_MAX_TURNS = 3
_RUNNER_PROCESS_TIMEOUT_SECONDS = int(_PER_LAUNCH_TIMEOUT_SECONDS * 3 + 120)

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
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_RUNNER_PROCESS_TIMEOUT_SECONDS,
        check=False,
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
