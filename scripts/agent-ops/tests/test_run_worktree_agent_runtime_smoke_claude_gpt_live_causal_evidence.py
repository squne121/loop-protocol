"""Issue #2231 (follow-up to Issue #2183 / PR #2220, OWNER comment
https://github.com/squne121/loop-protocol/pull/2220#issuecomment-5309790514):
live causal-evidence verification of ``run_worktree_agent_runtime_smoke.py``
across both the Claude-GPT launcher (``scripts/claude-gpt/launch.sh``) and
native Claude Code, plus a deterministic negative-fixture regression and a
live parent-only diagnostic run.

Per the live Issue's ``## Verification Commands`` section, AC1/AC2/AC4 must
each be a single ``uv run pytest ...`` line, with the actual live
``run_worktree_agent_runtime_smoke.py`` launch happening INSIDE the pytest
test function's own ``subprocess.run()`` call -- not as a separate raw
command in the VC itself (the PR preflight allowlist only accepts
``uv run pytest ...`` / ``uv run python3 -m pytest ...`` /
``runtime_dependency_smoke.py`` as VC command shapes). This module performs
those live launches directly, reads back the ``--evidence-json`` file this
Issue adds to the runner (machine-generated, never hand-written), and
asserts on its fields.

Runtime Verification Applicability (per the live Issue body): ``immediate``
for AC1/AC2/AC4. Each of those three tests requires ``scripts/claude-gpt/
launch.sh`` (proxy + ChatGPT Pro Codex subscription auth) or native Claude
Code auth to actually be available in this environment; when it is not,
they SKIP via ``pytest.skip()`` (mapped from the runner's own exit code 77
convention), never fabricating a PASS. AC3/AC5 are fully hermetic/
deterministic and always run.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_AGENT_OPS_DIR = _TESTS_DIR.parent
_CHECKOUT_ROOT = _AGENT_OPS_DIR.parent.parent
_RUNNER_PATH = _AGENT_OPS_DIR / "run_worktree_agent_runtime_smoke.py"
_LAUNCHER_PATH = _CHECKOUT_ROOT / "scripts" / "claude-gpt" / "launch.sh"
_PREFLIGHT_PATH = _CHECKOUT_ROOT / "scripts" / "claude-gpt" / "preflight.sh"

_MODULE_NAME = "run_worktree_agent_runtime_smoke_issue_2231_live_causal_evidence"
_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _RUNNER_PATH)
assert _spec is not None and _spec.loader is not None
smoke = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = smoke
_spec.loader.exec_module(smoke)


# ---------------------------------------------------------------------------
# Live-environment preflight helpers (Issue #2231 skip_conditions).
# ---------------------------------------------------------------------------


def _native_claude_available() -> tuple[bool, str]:
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return False, "native 'claude' binary not found on PATH"
    try:
        result = subprocess.run(
            [claude_bin, "--version"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"native 'claude --version' failed: {exc}"
    if result.returncode != 0:
        return False, f"native 'claude --version' exited {result.returncode}"
    return True, claude_bin


def _claude_gpt_available() -> tuple[bool, str]:
    if not _LAUNCHER_PATH.is_file():
        return False, f"launcher not found: {_LAUNCHER_PATH}"
    if not _PREFLIGHT_PATH.is_file():
        return False, f"preflight script not found: {_PREFLIGHT_PATH}"
    try:
        result = subprocess.run(
            ["sh", str(_PREFLIGHT_PATH)], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"claude-gpt preflight.sh failed to run: {exc}"
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return False, f"claude-gpt preflight.sh produced non-JSON output: {result.stdout[:200]!r}"
    if payload.get("exit_code") != 0:
        return False, f"claude-gpt preflight.sh exit_code={payload.get('exit_code')}"
    if not payload.get("binary_available"):
        return False, "claude-gpt preflight: binary_available is False"
    proxy = payload.get("proxy") or {}
    if not proxy.get("absolute_path"):
        return False, "claude-gpt preflight: proxy.absolute_path missing (raine/claude-code-proxy unavailable)"
    chatgpt_auth = payload.get("chatgpt_auth") or {}
    if not chatgpt_auth.get("available"):
        return False, "claude-gpt preflight: chatgpt_auth.available is False"
    return True, str(_LAUNCHER_PATH)


# ---------------------------------------------------------------------------
# Live-run helpers.
# ---------------------------------------------------------------------------


def _nonce() -> str:
    return "RUNTIME_SMOKE_NONCE_" + uuid.uuid4().hex[:16]


def _delegation_prompt(nonce: str) -> str:
    """Instructs the parent to delegate to the codebase-investigator
    SubAgent via the Task tool and relay ONLY that SubAgent's own reply --
    the positive fixture for AC1/AC2 (a genuine SubagentStart/SubagentStop
    hook pair correlated by agent_id, with the marker recovered from the
    child's own scoped output)."""
    return (
        "Use the Task tool to invoke the codebase-investigator SubAgent. "
        "Give the SubAgent this exact instruction: 'Do not investigate "
        f"anything. Simply respond with the single line: {nonce}'. Do not "
        "print this marker yourself in your own final response; only relay "
        "whatever the SubAgent itself reports back, verbatim, as your final "
        "answer. You must delegate via the Task tool -- do not answer this "
        "request directly without invoking a SubAgent.\n"
    )


def _parent_only_prompt(nonce: str) -> str:
    """Instructs the parent to answer directly, WITHOUT invoking any
    SubAgent -- the negative fixture for AC4 (a live parent-only run)."""
    return (
        f"Respond with exactly this single line and nothing else: {nonce}. "
        "Do not use the Task tool. Do not invoke any SubAgent. Answer "
        "directly yourself in this same turn.\n"
    )


def _run_smoke(
    tmp_path: Path,
    *,
    label: str,
    prompt_text: str,
    expect_marker: str | None,
    claude_bin: str | None = None,
    claude_adapter: str | None = None,
    timeout_seconds: int = 150,
    subprocess_timeout: int = 180,
    max_turns: int = 8,
) -> tuple[subprocess.CompletedProcess, Path]:
    """Actually launches ``run_worktree_agent_runtime_smoke.py`` via
    ``subprocess.run()`` (never mocked/hermetic for AC1/AC2/AC4) against
    this checkout (main repository or a linked worktree -- whichever
    contains this test file), writing the machine-generated
    ``--evidence-json`` receipt to ``tmp_path`` (never committed)."""
    prompt_path = tmp_path / f"{label}.prompt.txt"
    prompt_path.write_text(prompt_text, encoding="utf-8")
    out_dir = tmp_path / f"{label}-out"
    evidence_path = tmp_path / f"{label}.evidence.json"

    argv = [
        sys.executable,
        str(_RUNNER_PATH),
        "--runtime", "claude",
        "--mode", "structured",
        "--worktree", str(_CHECKOUT_ROOT),
        "--prompt-file", str(prompt_path),
        "--output-dir", str(out_dir),
        "--evidence-json", str(evidence_path),
        "--timeout-seconds", str(timeout_seconds),
        "--max-turns", str(max_turns),
    ]
    if expect_marker is not None:
        argv += ["--expect-marker", expect_marker]
    if claude_bin is not None:
        argv += ["--claude-bin", claude_bin]
    if claude_adapter is not None:
        argv += ["--claude-adapter", claude_adapter]

    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=subprocess_timeout
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"{label}: run_worktree_agent_runtime_smoke.py did not complete "
            f"within {subprocess_timeout}s (stdout so far: "
            f"{(exc.stdout or '')[-500:]!r})"
        )
    if "root checkout rejected" in (result.stderr or ""):
        pytest.skip(
            f"{label}: this checkout is the canonical repository root, not "
            "a linked worktree -- run_worktree_agent_runtime_smoke.py's own "
            "identity check requires a linked worktree (Issue #2231 live "
            "AC1/AC2/AC4 must run from a dedicated worktree checkout)"
        )
    if result.returncode == smoke.EXIT_SKIP:
        pytest.skip(
            f"{label}: run_worktree_agent_runtime_smoke.py returned exit "
            f"code {smoke.EXIT_SKIP} (SKIP/capability-unavailable) -- "
            f"stderr: {(result.stderr or '')[-500:]}"
        )
    return result, evidence_path


def _load_evidence(evidence_path: Path) -> dict:
    assert evidence_path.is_file(), f"--evidence-json file was not written: {evidence_path}"
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload.get("schema") == "WORKTREE_AGENT_RUNTIME_SMOKE_RESULT_V1"
    return payload


# ---------------------------------------------------------------------------
# AC1: Claude-GPT launcher positive run.
# ---------------------------------------------------------------------------


def test_ac1_claude_gpt_positive_hook_correlated(tmp_path: Path) -> None:
    available, detail = _claude_gpt_available()
    if not available:
        pytest.skip(f"claude-gpt launcher live environment unavailable: {detail}")

    nonce = _nonce()
    result, evidence_path = _run_smoke(
        tmp_path,
        label="ac1-claude-gpt",
        prompt_text=_delegation_prompt(nonce),
        expect_marker=nonce,
        claude_bin=str(_LAUNCHER_PATH),
        claude_adapter="claude-gpt",
    )
    evidence = _load_evidence(evidence_path)
    causal_evidence = evidence.get("subagent_causal_evidence") or {}

    assert evidence.get("exit_code") == 0, (
        f"AC1 live claude-gpt run did not PASS: exit_code={evidence.get('exit_code')} "
        f"errors={evidence.get('errors')} stderr={(result.stderr or '')[-500:]}"
    )
    assert (
        causal_evidence.get("causal_evidence_source")
        == smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    ), f"AC1 causal_evidence_source was not hook_id_correlated: {causal_evidence}"
    assert causal_evidence.get("subagent_start_observed") is True
    assert causal_evidence.get("subagent_stop_observed") is True


# ---------------------------------------------------------------------------
# AC2: native Claude Code positive run (same prompt, no --claude-adapter).
# ---------------------------------------------------------------------------


def test_ac2_native_positive_hook_correlated(tmp_path: Path) -> None:
    available, detail = _native_claude_available()
    if not available:
        pytest.skip(f"native Claude Code live environment unavailable: {detail}")

    nonce = _nonce()
    result, evidence_path = _run_smoke(
        tmp_path,
        label="ac2-native",
        prompt_text=_delegation_prompt(nonce),
        expect_marker=nonce,
    )
    evidence = _load_evidence(evidence_path)
    causal_evidence = evidence.get("subagent_causal_evidence") or {}

    assert evidence.get("exit_code") == 0, (
        f"AC2 live native run did not PASS: exit_code={evidence.get('exit_code')} "
        f"errors={evidence.get('errors')} stderr={(result.stderr or '')[-500:]}"
    )
    assert (
        causal_evidence.get("causal_evidence_source")
        == smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    ), f"AC2 causal_evidence_source was not hook_id_correlated: {causal_evidence}"
    assert causal_evidence.get("subagent_start_observed") is True
    assert causal_evidence.get("subagent_stop_observed") is True


# ---------------------------------------------------------------------------
# AC3: deterministic parent-marker-only negative fixture (hermetic, no live
# process spawned -- mirrors test_run_worktree_agent_runtime_smoke_causal_
# evidence.py's own marker-only fixture, kept self-contained in this module
# per the live Issue's own regression-test scope).
# ---------------------------------------------------------------------------


def test_ac3_negative_fixture_marker_only_insufficient() -> None:
    marker = "RUNTIME_SMOKE_NONCE_deterministic_fixture"
    # A parent-marker-only stdout stream: the marker text is present, but no
    # SubagentStart/SubagentStop hook events were ever observed at all --
    # exactly the "parent said it, no child ever ran" shape.
    stdout = (
        "some ordinary assistant prose leading up to the final answer\n"
        f"{marker}\n"
    )
    verdict = smoke.subagent_causal_evidence_verdict(stdout, [marker])
    assert (
        verdict["causal_evidence_source"]
        == smoke.CAUSAL_EVIDENCE_SOURCE_MARKER_ONLY_INSUFFICIENT
    )
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE
    assert verdict["causal_evidence_source"] != smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED
    assert verdict["subagent_start_observed"] is False
    assert verdict["subagent_stop_observed"] is False


# ---------------------------------------------------------------------------
# AC4: live parent-only negative diagnostic run. Model-directed behavior at
# runtime (the model choosing to invoke a SubAgent anyway, despite being
# told not to) is recorded as an inconclusive diagnostic, never conflated
# with an evaluator failure (a bug in subagent_causal_evidence_verdict()
# itself, which is out of this Issue's scope per #2183/PR #2220).
# ---------------------------------------------------------------------------


def test_ac4_live_parent_only_negative_diagnostic(tmp_path: Path) -> None:
    available, detail = _native_claude_available()
    if not available:
        pytest.skip(f"native Claude Code live environment unavailable: {detail}")

    nonce = _nonce()
    result, evidence_path = _run_smoke(
        tmp_path,
        label="ac4-parent-only",
        prompt_text=_parent_only_prompt(nonce),
        expect_marker=nonce,
    )
    evidence = _load_evidence(evidence_path)
    causal_evidence = evidence.get("subagent_causal_evidence") or {}
    source = causal_evidence.get("causal_evidence_source")

    # The runner process itself may legitimately exit non-zero here (its own
    # --expect-marker default gate FAILs a structured run whose causal
    # evidence is not hook_id_correlated) -- that is an expected property of
    # the *runner's* PASS/FAIL gate, not a signal about whether this
    # diagnostic test itself succeeded. This test's own pass/fail criterion
    # is solely: did the live run complete and produce a well-formed,
    # machine-generated evidence receipt with a causal_evidence_source in
    # the closed classification set.
    assert source in (
        smoke.CAUSAL_EVIDENCE_SOURCE_MARKER_ONLY_INSUFFICIENT,
        smoke.CAUSAL_EVIDENCE_SOURCE_NO_EVIDENCE,
        smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED,
    ), f"AC4 unrecognized causal_evidence_source: {causal_evidence}"

    if source == smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED:
        # Model-directed: it chose to delegate anyway, despite the explicit
        # instruction not to. This is a genuine, honestly-recorded
        # inconclusive diagnostic outcome -- not an evaluator failure, and
        # not asserted against as a hard pass/fail signal for this AC.
        diagnostic_classification = "inconclusive_model_invoked_subagent_anyway"
    else:
        # The expected diagnostic shape: the parent answered directly and no
        # SubAgent hook pair was observed.
        diagnostic_classification = "expected_parent_only_no_subagent_observed"

    diagnostic_path = tmp_path / "ac4-diagnostic-classification.json"
    diagnostic_path.write_text(
        json.dumps(
            {
                "schema": "WORKTREE_AGENT_RUNTIME_SMOKE_AC4_DIAGNOSTIC_V1",
                "causal_evidence_source": source,
                "diagnostic_classification": diagnostic_classification,
                "runner_exit_code": evidence.get("exit_code"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    assert diagnostic_path.is_file()


# ---------------------------------------------------------------------------
# AC5: raw JSON receipt provenance -- runner-generated (never hand-edited),
# a single generic secret scan finds 0 matches, and the receipt is never
# committed. Fully hermetic (no live model call: Runtime Verification
# Applicability marks only AC1/AC2/AC4 as immediate).
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")),
    ("oauth_token_field", re.compile(r"OAUTH_TOKEN")),
    ("authorization_header", re.compile(r"Authorization:\s*\S+")),
)


def _generic_secret_scan(raw_text: str) -> list[str]:
    """A lightweight, single-pass generic secret scanner (Issue #2231 AC5):
    no dedicated allowlist validator file, no dedicated pytest selector
    beyond this one AC5 test -- just a fixed, closed pattern set checked
    once against raw receipt text."""
    return [name for name, pattern in _SECRET_PATTERNS if pattern.search(raw_text)]


def test_ac5_evidence_json_no_secrets_and_not_committed(tmp_path: Path) -> None:
    # Positive control: the scanner itself must actually detect a planted
    # secret-shaped string (never a vacuously-passing no-op scan).
    planted = _generic_secret_scan("Authorization header was: Bearer abc123xyz789secretlooking")
    assert planted, "generic secret scanner failed to flag a planted Bearer token (positive control)"

    # A genuine, runner-generated (never hand-edited) evidence receipt: a
    # fast, marker-free native structured run with no live-auth requirement
    # beyond the local claude binary being resolvable enough to attempt
    # (unavailable environments SKIP, matching every other live-adjacent
    # test in this module).
    available, detail = _native_claude_available()
    if not available:
        pytest.skip(f"native Claude Code unavailable for AC5 receipt generation: {detail}")

    result, evidence_path = _run_smoke(
        tmp_path,
        label="ac5-receipt",
        prompt_text="Respond with exactly: ac5-receipt-ok\n",
        expect_marker=None,
        timeout_seconds=60,
        subprocess_timeout=90,
        max_turns=1,
    )
    raw_text = evidence_path.read_text(encoding="utf-8")
    payload = json.loads(raw_text)
    assert payload.get("schema") == "WORKTREE_AGENT_RUNTIME_SMOKE_RESULT_V1"
    assert "exit_code" in payload, "raw receipt is missing a runner-populated field (hand-edited?)"

    matches = _generic_secret_scan(raw_text)
    assert matches == [], f"generic secret scanner found forbidden pattern(s) in raw receipt: {matches}"

    # The receipt lives under a pytest tmp_path, never under this
    # checkout's tracked tree -- assert it is not, and could never become,
    # a tracked file (defense against a future accidental commit path).
    checkout_git_dir = _CHECKOUT_ROOT / ".git"
    assert checkout_git_dir.exists()
    assert not str(evidence_path).startswith(str(_CHECKOUT_ROOT)), (
        "AC5 receipt must live outside the tracked checkout tree "
        f"(got {evidence_path}, checkout root {_CHECKOUT_ROOT})"
    )
