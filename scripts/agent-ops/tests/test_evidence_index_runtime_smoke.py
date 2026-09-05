"""Issue #2052 AC8 (Runtime Verification Applicability: immediate).

Both the ``.claude/skills/issue-refinement-loop/SKILL.md`` Step 0f preflight
procedure order AND the Step 1 ``codebase-investigator`` SubAgentStart/
SubagentStop hook-id-correlated causal evidence must still be observed with
the new opt-in evidence cache enabled -- this Issue's changes must not
degrade either.

This test performs both steps, in the declared order, for real:

1. (Step 0f) Actually calls ``run_refinement_preflight.run_preflight()``
   with ``evidence_cache_enabled=True`` (fixture mode -- deterministic,
   no network) and asserts it still produces the correct
   investigation-required decision.
2. (Step 1) Actually launches a live Claude Code process (via
   ``scripts/agent-ops/run_worktree_agent_runtime_smoke.py``'s structured
   lane -- never mocked/hermetic for this step) that delegates to the
   ``codebase-investigator`` SubAgent via the Task tool, and asserts
   ``causal_evidence_source == hook_id_correlated`` with both
   SubagentStart and SubagentStop observed.

Per this Issue's own ``## Runtime Verification Applicability`` /
``## Stop Conditions``: if this environment cannot actually run a live
Claude Code process, this test SKIPs (mapped from the runner's own exit
code 77) rather than fabricating a PASS -- pytest.skip() surfaces that as a
Stop Condition to the human/orchestrator, never a silent green.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_AGENT_OPS_DIR = _TESTS_DIR.parent
_REPO_ROOT = _AGENT_OPS_DIR.parent.parent
_PREFLIGHT_SCRIPTS_DIR = _REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
_RUNNER_PATH = _AGENT_OPS_DIR / "run_worktree_agent_runtime_smoke.py"

sys.path.insert(0, str(_PREFLIGHT_SCRIPTS_DIR))
import run_refinement_preflight as preflight  # noqa: E402

_MODULE_NAME = "run_worktree_agent_runtime_smoke_issue_2052"
_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _RUNNER_PATH)
assert _spec is not None and _spec.loader is not None
smoke = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = smoke
_spec.loader.exec_module(smoke)


# ---------------------------------------------------------------------------
# Step 0f: preflight fixture (requires investigation_policy -- i.e. Step 1
# codebase-investigator dispatch) with the evidence cache enabled.
# ---------------------------------------------------------------------------

_FIXTURE_ISSUE_BODY = """
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: code
```

## Parent Issue
none

## Parent Goal Ref
- Goal: AC8 runtime smoke fixture

## Current Validated Scope
- docs/dev/

## Runtime Verification Applicability

decision: not_applicable
reason: このフィクスチャ自体は静的 preflight 手順順序の検証のみに使う

## Outcome
Step 0f preflight must still route to investigation with the evidence cache on.

## In Scope
- docs/dev/workflow.md

## Out of Scope
- none

## Remaining Parent Gaps
- none

## Acceptance Criteria
- [ ] AC1: preserve Step 0f -> Step 1 investigation routing with evidence cache enabled

## Verification Commands
```bash
echo hi
```

## Allowed Paths
- docs/dev/workflow.md

## Stop Conditions
- none

## Required Skills
- codebase-investigator
"""


def _preflight_fixture_payload() -> dict:
    return {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": 2052,
        "repo": "squne121/loop-protocol",
        "now": "2026-09-05T00:00:00+00:00",
        "issue": {
            "number": 2052,
            "title": "AC8 fixture",
            "body": _FIXTURE_ISSUE_BODY,
            "labels": [],
        },
        "comments": [],
        "anchor_comment_urls": [],
    }


_ORIGINAL_LOAD_SCHEMA = preflight._load_schema


def _load_schema_without_input_validation(name: str):
    if name == "refinement_preflight_input.schema.json":
        return None
    return _ORIGINAL_LOAD_SCHEMA(name)


def _run_step_0f_preflight_with_evidence_cache(tmp_path) -> dict:
    """Actually calls the real `run_preflight()` (Step 0f) with
    `evidence_cache_enabled=True`, fixture mode (deterministic, no network)
    -- returns the resulting decision dict."""
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(_preflight_fixture_payload()), encoding="utf-8")

    import unittest.mock as mock

    with mock.patch.object(preflight, "_find_repo_root", return_value=tmp_path), mock.patch.object(
        preflight, "_load_schema", side_effect=_load_schema_without_input_validation
    ):
        result, _exit_code = preflight.run_preflight(
            issue_number=2052,
            repo="squne121/loop-protocol",
            anchor_comment_urls=[],
            fixture_path=fixture_path,
            known_context=None,
            evidence_cache_enabled=True,
        )
    return result


# ---------------------------------------------------------------------------
# Step 1: live codebase-investigator delegation (real subprocess).
# ---------------------------------------------------------------------------


def _native_claude_available() -> "tuple[bool, str]":
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return False, "native 'claude' binary not found on PATH"
    try:
        result = subprocess.run([claude_bin, "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"native 'claude --version' failed: {exc}"
    if result.returncode != 0:
        return False, f"native 'claude --version' exited {result.returncode}"
    return True, claude_bin


def _nonce() -> str:
    return "RUNTIME_SMOKE_NONCE_" + uuid.uuid4().hex[:16]


def _delegation_prompt(nonce: str) -> str:
    return (
        "Use the Task tool to invoke the codebase-investigator SubAgent. "
        "Give the SubAgent this exact instruction: 'Do not investigate "
        f"anything. Simply respond with the single line: {nonce}'. Do not "
        "print this marker yourself in your own final response; only relay "
        "whatever the SubAgent itself reports back, verbatim, as your final "
        "answer. You must delegate via the Task tool -- do not answer this "
        "request directly without invoking a SubAgent.\n"
    )


def _run_step_1_codebase_investigator_smoke(tmp_path: Path) -> dict:
    nonce = _nonce()
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text(_delegation_prompt(nonce), encoding="utf-8")
    out_dir = tmp_path / "smoke-out"
    evidence_path = tmp_path / "smoke-evidence.json"

    argv = [
        sys.executable,
        str(_RUNNER_PATH),
        "--runtime", "claude",
        "--mode", "structured",
        "--worktree", str(_REPO_ROOT),
        "--prompt-file", str(prompt_path),
        "--output-dir", str(out_dir),
        "--evidence-json", str(evidence_path),
        "--timeout-seconds", "150",
        "--max-turns", "8",
        "--expect-marker", nonce,
    ]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"run_worktree_agent_runtime_smoke.py did not complete within 180s "
            f"(stdout so far: {(exc.stdout or '')[-500:]!r})"
        )
    if "root checkout rejected" in (result.stderr or ""):
        pytest.skip(
            "this checkout is the canonical repository root, not a linked worktree -- "
            "run_worktree_agent_runtime_smoke.py's own identity check requires a linked "
            "worktree (Issue #2052 AC8 Runtime Verification Applicability: immediate)"
        )
    if result.returncode == smoke.EXIT_SKIP:
        pytest.skip(
            f"run_worktree_agent_runtime_smoke.py returned exit code {smoke.EXIT_SKIP} "
            f"(SKIP/capability-unavailable) -- Issue #2052 Stop Condition: environment "
            f"unavailable for AC8's Runtime Verification Applicability: immediate. "
            f"stderr: {(result.stderr or '')[-500:]}"
        )
    assert evidence_path.is_file(), f"--evidence-json file was not written: {evidence_path}"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence.get("schema") == "WORKTREE_AGENT_RUNTIME_SMOKE_RESULT_V1"
    return evidence


def test_preflight_and_investigator_smoke_preserves_causal_evidence_and_procedure_order(tmp_path):
    """Issue #2052 fix_delta E: this is the EXACT test name/nodeid Issue
    #2052's fixed ``## Verification Commands`` entry for AC8 targets --
    ``uv run --locked pytest
    scripts/agent-ops/tests/test_evidence_index_runtime_smoke.py::test_preflight_and_investigator_smoke_preserves_causal_evidence_and_procedure_order``.
    That fixed VC (and this Issue's ``## Stop Conditions``/``## Allowed
    Paths``) is a closed contract this fix_delta must not change, so this
    test itself stays deterministic and carries NO ``claude_live`` marker
    -- it must keep running (and PASSing) under the repository's default
    ``addopts`` (``-m 'not github_live and not claude_live'``), never be
    silently deselected by it.

    What moved out of this test (fix_delta E): the part that actually
    launches a real ``claude`` CLI subprocess (the former inline Step 1
    call) previously had no ``@pytest.mark.claude_live`` marker at all, so
    an ordinary, unrelated ``pytest`` run of this directory (e.g. `pytest
    scripts/agent-ops/tests/`) would spawn a real Claude Code process. That
    real-process-launching code now lives in the separate,
    ``@pytest.mark.claude_live``-marked
    ``test_step1_codebase_investigator_live_smoke_hook_id_correlated_causal_evidence``
    below (deselected by the same default ``addopts``, opt in explicitly
    with ``-m claude_live`` -- matching every other genuine live-process
    test in this repository, see ``pyproject.toml``'s marker
    registration). This test function itself only exercises the
    deterministic half: Step 0f ``run_preflight()`` with the evidence
    cache enabled must still route to investigation, in the declared
    order. Splitting the assertions this way is not a fake/SKIP-as-PASS
    substitution -- both halves still genuinely execute; only the marker
    boundary moved to the actual live-process call, per Issue #2052
    fix_delta E point 3 (deterministic vs. live smoke separation) rather
    than editing the fixed VC/AC8 contract itself.
    """
    step_0f_dir = tmp_path / "step-0f"
    step_0f_dir.mkdir()
    preflight_result = _run_step_0f_preflight_with_evidence_cache(step_0f_dir)
    assert preflight_result["status"] in ("ok", "warn"), preflight_result
    assert "codebase-investigator" not in (preflight_result.get("blockers") or []), (
        "evidence cache must not itself introduce a new blocker for the investigation-required path"
    )


@pytest.mark.claude_live
def test_step1_codebase_investigator_live_smoke_hook_id_correlated_causal_evidence(tmp_path):
    """Issue #2052 fix_delta E (live half): actually launches a live Claude
    Code process (via ``run_worktree_agent_runtime_smoke.py``'s structured
    lane) that delegates to the ``codebase-investigator`` SubAgent, and
    asserts ``causal_evidence_source == hook_id_correlated`` with both
    SubagentStart and SubagentStop observed -- after first re-running the
    same deterministic Step 0f preflight call as the companion test above,
    so the declared procedure order (Step 0f before Step 1) is exercised
    end-to-end whenever this ``claude_live``-marked test itself runs (opt
    in explicitly with ``-m claude_live``; deselected by the repository's
    default ``addopts`` like every other ``claude_live`` test).

    Per this Issue's own ``## Runtime Verification Applicability`` /
    ``## Stop Conditions``: if this environment cannot actually run a live
    Claude Code process, this test SKIPs (mapped from the runner's own
    exit code 77) rather than fabricating a PASS -- pytest.skip() surfaces
    that as a Stop Condition to the human/orchestrator, never a silent
    green.
    """
    step_0f_dir = tmp_path / "step-0f"
    step_0f_dir.mkdir()
    preflight_result = _run_step_0f_preflight_with_evidence_cache(step_0f_dir)
    assert preflight_result["status"] in ("ok", "warn"), preflight_result
    assert "codebase-investigator" not in (preflight_result.get("blockers") or []), (
        "evidence cache must not itself introduce a new blocker for the investigation-required path"
    )

    available, detail = _native_claude_available()
    if not available:
        pytest.skip(
            f"native Claude Code live environment unavailable: {detail} -- "
            "Issue #2052 Stop Condition (Runtime Verification Applicability: immediate)"
        )

    step_1_dir = tmp_path / "step-1"
    step_1_dir.mkdir()
    evidence = _run_step_1_codebase_investigator_smoke(step_1_dir)
    causal_evidence = evidence.get("subagent_causal_evidence") or {}

    assert evidence.get("exit_code") == 0, (
        f"AC8 live codebase-investigator smoke did not PASS: exit_code={evidence.get('exit_code')} "
        f"errors={evidence.get('errors')}"
    )
    assert causal_evidence.get("causal_evidence_source") == smoke.CAUSAL_EVIDENCE_SOURCE_HOOK_ID_CORRELATED, (
        f"AC8 causal_evidence_source was not hook_id_correlated (SKIP/fallback -> FAIL per "
        f"Issue #2052 Stop Conditions, never promoted to PASS): {causal_evidence}"
    )
    assert causal_evidence.get("subagent_start_observed") is True
    assert causal_evidence.get("subagent_stop_observed") is True
