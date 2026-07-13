#!/usr/bin/env python3
"""
Unit / CLI tests for Issue #1488: extend baseline_vc_preflight.py current-head
evidence-mode certified PASS to non-regression-gate VC command types (rg,
test -f / test -s etc).

Truth table under test (see Issue #1488 body):

| evidence mode | 実行結果                              | 期待分類                     |
|----------------|---------------------------------------|-------------------------------|
| baseline       | 非 regression VC exit 0               | unexpected_pass / blocked     |
| baseline       | rg no match / test -f false           | expected_fail / go            |
| current-head   | 静的 policy を通過したコマンドが exit 0 | expected_pass / go            |
| current-head   | exit nonzero                          | 既存の error/failure 分類を維持 |
| current-head   | unsafe・broad search・不正構文         | 実行せず blocked              |
| current-head   | HEAD drift・dirty worktree・body hash不整合 | envelope 全体を blocked  |
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT_PATH = Path(__file__).parent.parent / "baseline_vc_preflight.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))

from baseline_vc_preflight import classify_result  # noqa: E402


# ---------------------------------------------------------------------------
# AC1: baseline evidence-mode semantics are unchanged for non-regression VCs.
# ---------------------------------------------------------------------------


def test_ac1_baseline_mode_rg_exit0_stays_unexpected_pass_blocked():
    """GIVEN baseline evidence-mode WHEN a non-regression rg VC exits 0
    THEN classification stays unexpected_pass / blocked (default evidence_mode)."""
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=0,
        stdout="match found\n",
        stderr="",
        command="rg -q hello tracked.txt",
    )
    assert classification == "unexpected_pass"
    assert category == "unexpected_pass"
    assert decision == "blocked"
    assert scope_class == "baseline_fail_expected"


def test_ac1_baseline_mode_explicit_evidence_mode_rg_exit0_stays_unexpected_pass_blocked():
    """GIVEN evidence_mode explicitly set to baseline WHEN rg exits 0
    THEN classification stays unexpected_pass / blocked (no promotion)."""
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=0,
        stdout="match found\n",
        stderr="",
        command="rg -q hello tracked.txt",
        evidence_mode="baseline",
    )
    assert classification == "unexpected_pass"
    assert decision == "blocked"


def test_ac1_baseline_mode_test_f_exit0_stays_unexpected_pass_blocked():
    """GIVEN baseline evidence-mode WHEN test -f exits 0 (file exists)
    THEN classification stays unexpected_pass / blocked."""
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=0,
        stdout="",
        stderr="",
        command="test -f tracked.txt",
        evidence_mode="baseline",
    )
    assert classification == "unexpected_pass"
    assert decision == "blocked"


# ---------------------------------------------------------------------------
# AC2: current-head evidence-mode certifies rg exit 0 as expected_pass / go.
# ---------------------------------------------------------------------------


def test_ac2_current_head_mode_rg_exit0_becomes_expected_pass_go():
    """GIVEN current-head evidence-mode WHEN a static-policy-passing rg VC
    exits 0 THEN classification becomes expected_pass / go (certified PASS)."""
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=0,
        stdout="match found\n",
        stderr="",
        command="rg -q hello tracked.txt",
        evidence_mode="current-head",
        static_policy_passed=True,
    )
    assert classification == "expected_pass"
    assert category == "expected_pass_resolved_on_current_head"
    assert decision == "go"
    assert fix_hint is None
    assert scope_class == "baseline_fail_expected"


def test_ac2_current_head_mode_rg_exit1_no_match_stays_expected_fail_go():
    """GIVEN current-head evidence-mode WHEN rg finds no match (exit 1)
    THEN classification stays expected_fail / go (unchanged failure path)."""
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=1,
        stdout="",
        stderr="",
        command="rg -q hello tracked.txt",
        evidence_mode="current-head",
        static_policy_passed=True,
    )
    assert classification == "expected_fail"
    assert category == "expected_baseline_fail"
    assert decision == "go"


# ---------------------------------------------------------------------------
# AC3: current-head evidence-mode certifies test -f / test -s exit 0.
# ---------------------------------------------------------------------------


def test_ac3_current_head_mode_test_f_exit0_becomes_expected_pass_go():
    """GIVEN current-head evidence-mode WHEN test -f exits 0 (file now exists)
    THEN classification becomes expected_pass / go."""
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=0,
        stdout="",
        stderr="",
        command="test -f tracked.txt",
        evidence_mode="current-head",
        static_policy_passed=True,
    )
    assert classification == "expected_pass"
    assert category == "expected_pass_resolved_on_current_head"
    assert decision == "go"


def test_ac3_current_head_mode_test_s_exit0_becomes_expected_pass_go():
    """GIVEN current-head evidence-mode WHEN test -s exits 0 (file non-empty)
    THEN classification becomes expected_pass / go."""
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=0,
        stdout="",
        stderr="",
        command="test -s tracked.txt",
        evidence_mode="current-head",
        static_policy_passed=True,
    )
    assert classification == "expected_pass"
    assert category == "expected_pass_resolved_on_current_head"
    assert decision == "go"


def test_ac3_current_head_mode_test_f_exit1_stays_expected_fail_go():
    """GIVEN current-head evidence-mode WHEN test -f exits 1 (file missing)
    THEN classification stays expected_fail / go (unchanged failure path)."""
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=1,
        stdout="",
        stderr="",
        command="test -f missing.txt",
        evidence_mode="current-head",
        static_policy_passed=True,
    )
    assert classification == "expected_fail"
    assert category == "file_not_found_expected"
    assert decision == "go"


# ---------------------------------------------------------------------------
# AC4: unsafe / broad / invalid / exit-nonzero / missing-command / timeout
# are never promoted to PASS in current-head mode.
# ---------------------------------------------------------------------------


def test_ac4_current_head_mode_exit_nonzero_regression_gate_stays_blocked():
    """GIVEN current-head evidence-mode WHEN a regression-gate command exits
    non-zero THEN classification stays blocked (existing failure semantics)."""
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=1,
        stdout="",
        stderr="1 failed",
        command="pnpm lint",
        evidence_mode="current-head",
        static_policy_passed=True,
    )
    assert classification == "blocked"
    assert decision == "blocked"
    assert scope_class == "regression_gate"


def test_ac4_current_head_mode_missing_command_exit127_stays_blocked():
    """GIVEN current-head evidence-mode WHEN command is missing (exit 127)
    THEN classification stays blocked / env_missing_dep (not promoted)."""
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=127,
        stdout="",
        stderr="command not found",
        command="totally-missing-binary --flag",
        evidence_mode="current-head",
        static_policy_passed=True,
    )
    assert classification == "blocked"
    assert category == "env_missing_dep"
    assert decision == "blocked"


def test_ac4_current_head_mode_timeout_stays_blocked():
    """GIVEN current-head evidence-mode WHEN command times out
    THEN classification stays blocked / timeout (not promoted)."""
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=-1,
        stdout="",
        stderr="Command exceeded timeout after 90s",
        command="rg -q hello tracked.txt",
        evidence_mode="current-head",
        static_policy_passed=True,
    )
    assert classification == "blocked"
    assert category == "timeout"
    assert decision == "blocked"


def test_ac4_current_head_mode_static_policy_failed_never_promoted():
    """GIVEN current-head evidence-mode WHEN static_policy_passed is False
    (a command that should never have been auto-promoted, defensively
    guarded) THEN exit 0 does NOT become expected_pass / go."""
    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=0,
        stdout="match found\n",
        stderr="",
        command="rg -q hello tracked.txt",
        evidence_mode="current-head",
        static_policy_passed=False,
    )
    assert classification == "unexpected_pass"
    assert decision == "blocked"


def test_ac4_cli_unsafe_command_stays_blocked_in_current_head_mode():
    """GIVEN current-head evidence-mode CLI invocation WHEN the VC is an
    unsafe/broad rg (rejected by classify_static_command before execution)
    THEN the item stays blocked and is never executed / promoted."""
    with tempfile.TemporaryDirectory() as outer_dir:
        outer = Path(outer_dir)
        repo = outer / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "tracked.txt").write_text("hello world\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
        head = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()

        body = (
            "## Verification Commands\n\n"
            "```bash\n"
            "# AC1\n"
            "$ rg -q hello .\n"
            "```\n"
        )
        # body_file lives OUTSIDE the repo so writing it does not dirty the
        # worktree under test (dirty-worktree behavior is exercised
        # separately below).
        body_file = outer / "issue-body.md"
        body_file.write_text(body, encoding="utf-8")

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--body-file",
                str(body_file),
                "--cwd",
                str(repo),
                "--evidence-mode",
                "current-head",
                "--reviewed-head-sha",
                head,
                "--format",
                "json",
                "--issue",
                "1",
                "--repo",
                "test/test",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    payload = json.loads(completed.stdout)
    assert payload["status"] == "blocked"
    assert payload["results"]
    item = payload["results"][0]
    assert item["classification"] == "blocked"
    assert item["decision"] == "blocked"
    # A broad, non-path-scoped rg search must never be promoted to a
    # certified current PASS, regardless of evidence_mode.
    assert item["classification"] != "expected_pass"


# ---------------------------------------------------------------------------
# AC6 cross-check (regression only; full AC6 coverage lives in
# test_baseline_vc_preflight.py per Verification Commands): a dirty worktree
# still blocks the whole envelope in current-head mode even when the VC
# itself would otherwise certify as a current PASS.
# ---------------------------------------------------------------------------


def test_ac6_dirty_worktree_blocks_envelope_even_with_passing_non_regression_vc():
    """GIVEN a dirty worktree WHEN current-head evidence-mode runs a
    non-regression VC that would exit 0 THEN the whole envelope is blocked
    (uncertified), not a certified current PASS."""
    with tempfile.TemporaryDirectory() as outer_dir:
        outer = Path(outer_dir)
        repo = outer / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "tracked.txt").write_text("hello world\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
        head = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        (repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")

        body = (
            "## Verification Commands\n\n"
            "```bash\n"
            "# AC1\n"
            "$ rg -q hello tracked.txt\n"
            "```\n"
        )
        # body_file lives OUTSIDE the repo so the only dirtiness source is
        # the intentional untracked.txt inside the repo.
        body_file = outer / "issue-body.md"
        body_file.write_text(body, encoding="utf-8")

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--body-file",
                str(body_file),
                "--cwd",
                str(repo),
                "--evidence-mode",
                "current-head",
                "--reviewed-head-sha",
                head,
                "--format",
                "json",
                "--issue",
                "1",
                "--repo",
                "test/test",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    payload = json.loads(completed.stdout)
    assert completed.returncode != 0
    assert payload["status"] == "blocked"
    assert payload["stop_condition_triggered"] is True
