#!/usr/bin/env python3
"""
Unit / CLI tests for Issue #1488: extend baseline_vc_preflight.py current-head
evidence-mode certified PASS to non-regression-gate VC command types (rg,
test -f / test -s etc).

PR #1497 review (REQUEST_CHANGES) added two hard requirements that this file
also covers:

  - Blocker 1: current PASS must be bound to a target path that is both
    repo-relative and contained in Allowed Paths (rejecting repo-external
    state such as `/tmp/vc-sentinel`), verified to exist as a regular file
    at certification time. `grep`/`egrep`/`fgrep` are excluded from
    promotion entirely (closed allowlist: only `test -f|-d|-s` and `rg`).
  - Blocker 2: `rg -q` / `grep -q` can exit 0 on a "quiet partial success"
    (one operand matched while another produced a missing-path / permission
    error). Certification requires stderr to be empty, verified against a
    REAL subprocess invocation (not hand-typed stdout/stderr), so partial
    I/O errors are not silently absorbed.

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
import os
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT_PATH = Path(__file__).parent.parent / "baseline_vc_preflight.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))

from baseline_vc_preflight import (  # noqa: E402
    certify_current_pass_command,
    classify_result,
)


def _run(argv: list[str], cwd: Path) -> tuple[int, str, str]:
    """Run a REAL subprocess (not hand-typed stdout/stderr) and return
    (exit_code, stdout, stderr)."""
    completed = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True)
    return completed.returncode, completed.stdout, completed.stderr


# ---------------------------------------------------------------------------
# AC1: baseline evidence-mode semantics are unchanged for non-regression VCs.
# ---------------------------------------------------------------------------


def test_ac1_baseline_mode_rg_exit0_stays_unexpected_pass_blocked(tmp_path):
    """GIVEN baseline evidence-mode WHEN a non-regression rg VC exits 0 on a
    REAL matching file THEN classification stays unexpected_pass / blocked
    (default evidence_mode; certify_current_pass_command is never consulted)."""
    (tmp_path / "tracked.txt").write_text("hello world\n", encoding="utf-8")
    exit_code, stdout, stderr = _run(["rg", "-q", "hello", "tracked.txt"], tmp_path)
    assert exit_code == 0

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        command="rg -q hello tracked.txt",
    )
    assert classification == "unexpected_pass"
    assert category == "unexpected_pass"
    assert decision == "blocked"
    assert scope_class == "baseline_fail_expected"


def test_ac1_baseline_mode_explicit_evidence_mode_rg_exit0_stays_unexpected_pass_blocked(
    tmp_path,
):
    """GIVEN evidence_mode explicitly set to baseline WHEN rg exits 0 on a
    REAL matching file THEN classification stays unexpected_pass / blocked
    (no promotion), even with cwd / allowed_paths / static_policy_passed set."""
    (tmp_path / "tracked.txt").write_text("hello world\n", encoding="utf-8")
    exit_code, stdout, stderr = _run(["rg", "-q", "hello", "tracked.txt"], tmp_path)
    assert exit_code == 0

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        command="rg -q hello tracked.txt",
        cwd=str(tmp_path),
        allowed_paths=["tracked.txt"],
        static_policy_passed=True,
        evidence_mode="baseline",
    )
    assert classification == "unexpected_pass"
    assert decision == "blocked"


def test_ac1_baseline_mode_test_f_exit0_stays_unexpected_pass_blocked(tmp_path):
    """GIVEN baseline evidence-mode WHEN test -f exits 0 (file exists)
    THEN classification stays unexpected_pass / blocked."""
    (tmp_path / "tracked.txt").write_text("hello\n", encoding="utf-8")
    exit_code, stdout, stderr = _run(["test", "-f", "tracked.txt"], tmp_path)
    assert exit_code == 0

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        command="test -f tracked.txt",
        cwd=str(tmp_path),
        allowed_paths=["tracked.txt"],
        static_policy_passed=True,
        evidence_mode="baseline",
    )
    assert classification == "unexpected_pass"
    assert decision == "blocked"


# ---------------------------------------------------------------------------
# AC2: current-head evidence-mode certifies rg exit 0 as expected_pass / go,
# bound to a real, Allowed-Paths-contained, existing regular file.
# ---------------------------------------------------------------------------


def test_ac2_current_head_mode_rg_exit0_becomes_expected_pass_go(tmp_path):
    """GIVEN current-head evidence-mode WHEN a static-policy-passing rg VC
    exits 0 against a REAL matching file inside Allowed Paths THEN
    classification becomes expected_pass / go (certified PASS)."""
    (tmp_path / "tracked.txt").write_text("hello world\n", encoding="utf-8")
    exit_code, stdout, stderr = _run(["rg", "-q", "hello", "tracked.txt"], tmp_path)
    assert exit_code == 0
    assert stderr == ""

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        command="rg -q hello tracked.txt",
        cwd=str(tmp_path),
        allowed_paths=["tracked.txt"],
        evidence_mode="current-head",
        static_policy_passed=True,
    )
    assert classification == "expected_pass"
    assert category == "expected_pass_resolved_on_current_head"
    assert decision == "go"
    assert fix_hint is None
    assert scope_class == "baseline_fail_expected"


def test_ac2_current_head_mode_rg_exit1_no_match_stays_expected_fail_go(tmp_path):
    """GIVEN current-head evidence-mode WHEN rg finds no match (exit 1)
    THEN classification stays expected_fail / go (unchanged failure path)."""
    (tmp_path / "tracked.txt").write_text("nothing here\n", encoding="utf-8")
    exit_code, stdout, stderr = _run(["rg", "-q", "hello", "tracked.txt"], tmp_path)
    assert exit_code == 1

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        command="rg -q hello tracked.txt",
        cwd=str(tmp_path),
        allowed_paths=["tracked.txt"],
        evidence_mode="current-head",
        static_policy_passed=True,
    )
    assert classification == "expected_fail"
    assert category == "expected_baseline_fail"
    assert decision == "go"


# ---------------------------------------------------------------------------
# AC3: current-head evidence-mode certifies test -f / test -s exit 0.
# ---------------------------------------------------------------------------


def test_ac3_current_head_mode_test_f_exit0_becomes_expected_pass_go(tmp_path):
    """GIVEN current-head evidence-mode WHEN test -f exits 0 (file now
    exists inside Allowed Paths) THEN classification becomes expected_pass /
    go."""
    (tmp_path / "tracked.txt").write_text("done\n", encoding="utf-8")
    exit_code, stdout, stderr = _run(["test", "-f", "tracked.txt"], tmp_path)
    assert exit_code == 0

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        command="test -f tracked.txt",
        cwd=str(tmp_path),
        allowed_paths=["tracked.txt"],
        evidence_mode="current-head",
        static_policy_passed=True,
    )
    assert classification == "expected_pass"
    assert category == "expected_pass_resolved_on_current_head"
    assert decision == "go"


def test_ac3_current_head_mode_test_s_exit0_becomes_expected_pass_go(tmp_path):
    """GIVEN current-head evidence-mode WHEN test -s exits 0 (file non-empty)
    THEN classification becomes expected_pass / go."""
    (tmp_path / "tracked.txt").write_text("nonempty\n", encoding="utf-8")
    exit_code, stdout, stderr = _run(["test", "-s", "tracked.txt"], tmp_path)
    assert exit_code == 0

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        command="test -s tracked.txt",
        cwd=str(tmp_path),
        allowed_paths=["tracked.txt"],
        evidence_mode="current-head",
        static_policy_passed=True,
    )
    assert classification == "expected_pass"
    assert category == "expected_pass_resolved_on_current_head"
    assert decision == "go"


def test_ac3_current_head_mode_test_f_exit1_stays_expected_fail_go(tmp_path):
    """GIVEN current-head evidence-mode WHEN test -f exits 1 (file missing)
    THEN classification stays expected_fail / go (unchanged failure path)."""
    exit_code, stdout, stderr = _run(["test", "-f", "missing.txt"], tmp_path)
    assert exit_code == 1

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        command="test -f missing.txt",
        cwd=str(tmp_path),
        allowed_paths=["missing.txt"],
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


def test_ac4_current_head_mode_static_policy_failed_never_promoted(tmp_path):
    """GIVEN current-head evidence-mode WHEN static_policy_passed is False
    (a command that should never have been auto-promoted, defensively
    guarded) THEN exit 0 does NOT become expected_pass / go, even against a
    real matching Allowed-Paths file."""
    (tmp_path / "tracked.txt").write_text("hello world\n", encoding="utf-8")
    exit_code, stdout, stderr = _run(["rg", "-q", "hello", "tracked.txt"], tmp_path)
    assert exit_code == 0

    classification, category, decision, fix_hint, scope_class = classify_result(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        command="rg -q hello tracked.txt",
        cwd=str(tmp_path),
        allowed_paths=["tracked.txt"],
        evidence_mode="current-head",
        static_policy_passed=False,
    )
    assert classification == "unexpected_pass"
    assert decision == "blocked"


def test_ac4_static_policy_passed_defaults_to_false_fail_closed():
    """PR #1497 review Major 1: static_policy_passed must default to False
    (fail-closed), not True, so a call site that forgets to run static
    policy checks can never accidentally certify a current PASS."""
    import inspect

    sig = inspect.signature(classify_result)
    assert sig.parameters["static_policy_passed"].default is False


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
# PR #1497 review Blocker 1: target-scope certification (closed allowlist,
# repo-relative + Allowed Paths containment, real filesystem existence).
# ---------------------------------------------------------------------------


def test_blocker1_test_f_on_repo_external_tmp_path_is_never_certified(tmp_path):
    """Reproduction of the reviewer's counter-example: `test -f
    /tmp/vc-sentinel` exits 0 because of repo-EXTERNAL state, unrelated to
    the reviewed commit or Allowed Paths. It must never be certified, even
    though the file genuinely exists on disk."""
    sentinel = tmp_path / "vc-sentinel"
    sentinel.write_text("x\n", encoding="utf-8")
    exit_code, stdout, stderr = _run(["test", "-f", str(sentinel)], tmp_path)
    assert exit_code == 0

    certified, reason = certify_current_pass_command(
        command=f"test -f {sentinel}",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        cwd=str(tmp_path),
        allowed_paths=["src/feature.ts"],
    )
    assert certified is False
    assert reason == "test_path_not_repo_relative"


def test_blocker1_test_f_outside_allowed_paths_is_never_certified(tmp_path):
    """A repo-relative path that exists but is NOT listed in Allowed Paths
    must never be certified as a current PASS."""
    (tmp_path / "unrelated.txt").write_text("x\n", encoding="utf-8")
    exit_code, stdout, stderr = _run(["test", "-f", "unrelated.txt"], tmp_path)
    assert exit_code == 0

    certified, reason = certify_current_pass_command(
        command="test -f unrelated.txt",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        cwd=str(tmp_path),
        allowed_paths=["src/feature.ts"],
    )
    assert certified is False
    assert reason == "test_path_outside_allowed_paths"


def test_blocker1_grep_family_never_certified_even_with_clean_exit0(tmp_path):
    """grep / egrep / fgrep have no path-operand parser implemented and must
    NEVER be certified as a current PASS, even with exit 0 and empty
    stderr, until a dedicated parser exists (closed allowlist)."""
    (tmp_path / "tracked.txt").write_text("hello world\n", encoding="utf-8")
    for binary in ("grep", "egrep", "fgrep"):
        exit_code, stdout, stderr = _run([binary, "-q", "hello", "tracked.txt"], tmp_path)
        assert exit_code == 0
        assert stderr == ""

        certified, reason = certify_current_pass_command(
            command=f"{binary} -q hello tracked.txt",
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            cwd=str(tmp_path),
            allowed_paths=["tracked.txt"],
        )
        assert certified is False, f"{binary} must not be certified"
        assert reason == "command_not_in_current_pass_allowlist"

        classification, category, decision, _fix_hint, _scope_class = classify_result(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            command=f"{binary} -q hello tracked.txt",
            cwd=str(tmp_path),
            allowed_paths=["tracked.txt"],
            evidence_mode="current-head",
            static_policy_passed=True,
        )
        assert classification == "unexpected_pass"
        assert decision == "blocked"


# ---------------------------------------------------------------------------
# PR #1497 review Blocker 2: quiet partial success (rg -q / grep -q exits 0
# despite a missing-path / permission error on another operand). All cases
# below use REAL subprocess invocations, not hand-typed stdout/stderr.
# ---------------------------------------------------------------------------


def test_blocker2_rg_quiet_missing_path_plus_matching_path_never_certified(tmp_path):
    """rg -q <missing> <match> exits 0 (a match was found) while ALSO
    emitting a missing-path error on stderr for the other operand. This
    reproduces the reviewer's exact counter-example and must never be
    certified."""
    (tmp_path / "changed.txt").write_text("needle\n", encoding="utf-8")
    missing = tmp_path / "deleted.txt"

    exit_code, stdout, stderr = _run(
        ["rg", "-q", "needle", str(missing), "changed.txt"], tmp_path
    )
    assert exit_code == 0
    assert stderr != ""
    assert "No such file or directory" in stderr

    certified, reason = certify_current_pass_command(
        command=f"rg -q needle {missing} changed.txt",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        cwd=str(tmp_path),
        allowed_paths=["deleted.txt", "changed.txt"],
    )
    assert certified is False
    assert reason == "nonempty_stderr"


def test_blocker2_rg_quiet_unreadable_path_plus_matching_path_never_certified(tmp_path):
    """rg -q <unreadable> <match> exits 0 (a match was found) while ALSO
    emitting a permission-denied error on stderr for the other operand."""
    unreadable = tmp_path / "unreadable.txt"
    unreadable.write_text("secret\n", encoding="utf-8")
    os.chmod(unreadable, 0o000)
    (tmp_path / "match.txt").write_text("needle\n", encoding="utf-8")
    try:
        exit_code, stdout, stderr = _run(
            ["rg", "-q", "needle", "unreadable.txt", "match.txt"], tmp_path
        )
    finally:
        os.chmod(unreadable, 0o644)

    if exit_code != 0 or stderr == "":
        # Running as root (or another environment where 0o000 does not
        # block reads) cannot reproduce the permission-denied condition;
        # skip rather than assert a false negative.
        import pytest

        pytest.skip("current process can read a 0o000 file (likely running as root)")

    certified, reason = certify_current_pass_command(
        command="rg -q needle unreadable.txt match.txt",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        cwd=str(tmp_path),
        allowed_paths=["unreadable.txt", "match.txt"],
    )
    assert certified is False
    assert reason == "nonempty_stderr"


def test_blocker2_grep_quiet_missing_path_plus_matching_path_never_certified(tmp_path):
    """grep -q <missing> <match> reproduces the same quiet-partial-success
    shape as rg, and (being outside the closed allowlist entirely) must
    never be certified regardless of stderr content."""
    (tmp_path / "match.txt").write_text("needle\n", encoding="utf-8")
    missing = tmp_path / "missing.txt"

    exit_code, stdout, stderr = _run(
        ["grep", "-q", "needle", str(missing), "match.txt"], tmp_path
    )
    assert exit_code == 0

    certified, reason = certify_current_pass_command(
        command=f"grep -q needle {missing} match.txt",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        cwd=str(tmp_path),
        allowed_paths=["missing.txt", "match.txt"],
    )
    assert certified is False
    # Both non-empty stderr (Blocker 2) and the closed allowlist
    # (Blocker 1: grep is excluded entirely) independently reject grep;
    # whichever check runs first is an implementation detail, not part of
    # the observable contract.
    assert reason in {"nonempty_stderr", "command_not_in_current_pass_allowlist"}


def test_blocker2_quiet_search_over_directory_with_unreadable_entry_never_certified(
    tmp_path,
):
    """A quiet rg search over a DIRECTORY operand (as opposed to a single
    file) must never be certified, even if the directory itself is
    Allowed-Paths-contained and rg happens to exit 0. This models a
    directory containing an entry that is not independently readable."""
    target_dir = tmp_path / "scope"
    target_dir.mkdir()
    (target_dir / "readable.txt").write_text("needle\n", encoding="utf-8")
    unreadable = target_dir / "unreadable.txt"
    unreadable.write_text("secret\n", encoding="utf-8")
    os.chmod(unreadable, 0o000)
    try:
        exit_code, stdout, stderr = _run(["rg", "-q", "needle", "scope"], tmp_path)
    finally:
        os.chmod(unreadable, 0o644)
    assert exit_code == 0

    certified, reason = certify_current_pass_command(
        command="rg -q needle scope",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        cwd=str(tmp_path),
        allowed_paths=["scope"],
    )
    assert certified is False
    # A directory operand is never a regular file, so it is rejected on
    # that basis (and, if it also emitted stderr, on nonempty_stderr too;
    # either fail-closed reason is acceptable here).
    assert reason in {"rg_path_not_a_regular_file_or_missing", "nonempty_stderr"}


def test_blocker2_grep_error_message_suppression_never_certified(tmp_path):
    """grep -s (--no-messages) suppresses the error message text on stderr
    even when an operand is missing, which is exactly the kind of "error
    suppression" the reviewer flagged. Being outside the closed allowlist,
    grep must still never be certified regardless of what stderr contains."""
    (tmp_path / "match.txt").write_text("needle\n", encoding="utf-8")
    missing = tmp_path / "missing.txt"

    exit_code, stdout, stderr = _run(
        ["grep", "-q", "-s", "needle", str(missing), "match.txt"], tmp_path
    )
    assert exit_code == 0

    certified, reason = certify_current_pass_command(
        command=f"grep -q -s needle {missing} match.txt",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        cwd=str(tmp_path),
        allowed_paths=["missing.txt", "match.txt"],
    )
    assert certified is False
    assert reason == "command_not_in_current_pass_allowlist"


def test_certify_current_pass_command_requires_nonempty_allowed_paths(tmp_path):
    """Fail-closed: without any Allowed Paths, containment can never be
    proven, so certification must always be refused."""
    (tmp_path / "tracked.txt").write_text("hello\n", encoding="utf-8")
    exit_code, stdout, stderr = _run(["rg", "-q", "hello", "tracked.txt"], tmp_path)
    assert exit_code == 0

    certified, reason = certify_current_pass_command(
        command="rg -q hello tracked.txt",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        cwd=str(tmp_path),
        allowed_paths=[],
    )
    assert certified is False
    assert reason == "no_allowed_paths"


# ---------------------------------------------------------------------------
# Major 2: the new category must be high-confidence and schema-consistent.
# ---------------------------------------------------------------------------


def test_major2_expected_pass_resolved_on_current_head_is_high_confidence():
    """PR #1497 review Major 2: the new category must not silently fall
    back to confidence: low (which would contradict a certified PASS)."""
    from baseline_vc_preflight import compute_confidence

    assert compute_confidence("expected_pass_resolved_on_current_head") == "high"


def test_major2_schema_consistency_accepts_well_formed_current_pass_item():
    from baseline_vc_preflight import check_c13_vc_preflight_decision_consistency

    ok, failures = check_c13_vc_preflight_decision_consistency(
        [
            {
                "classification": "expected_pass",
                "category": "expected_pass_resolved_on_current_head",
                "decision": "go",
                "scope_class": "baseline_fail_expected",
                "confidence": "high",
            }
        ]
    )
    assert ok is True
    assert failures == []


def test_major2_schema_consistency_rejects_low_confidence_current_pass():
    from baseline_vc_preflight import check_c13_vc_preflight_decision_consistency

    ok, failures = check_c13_vc_preflight_decision_consistency(
        [
            {
                "classification": "expected_pass",
                "category": "expected_pass_resolved_on_current_head",
                "decision": "go",
                "scope_class": "baseline_fail_expected",
                "confidence": "low",
            }
        ]
    )
    assert ok is False
    assert any("confidence high" in failure for failure in failures)


def test_major2_schema_consistency_rejects_mismatched_classification():
    from baseline_vc_preflight import check_c13_vc_preflight_decision_consistency

    ok, failures = check_c13_vc_preflight_decision_consistency(
        [
            {
                "classification": "unexpected_pass",
                "category": "expected_pass_resolved_on_current_head",
                "decision": "go",
                "scope_class": "baseline_fail_expected",
                "confidence": "high",
            }
        ]
    )
    assert ok is False
    assert any("classification expected_pass" in failure for failure in failures)


def test_cli_current_pass_item_includes_certified_target_paths_and_high_confidence():
    """End-to-end CLI check that a certified current PASS item exposes
    certified_target_paths (Blocker 1: "producer 結果に検証済み target path
    を含める") and confidence: high (Major 2)."""
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
            "## Allowed Paths\n"
            "- tracked.txt\n\n"
            "## Verification Commands\n\n"
            "```bash\n"
            "# AC1\n"
            "$ rg -q hello tracked.txt\n"
            "```\n"
        )
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
    assert payload["status"] == "pass"
    item = payload["results"][0]
    assert item["classification"] == "expected_pass"
    assert item["category"] == "expected_pass_resolved_on_current_head"
    assert item["confidence"] == "high"
    assert item["certified_target_paths"] == ["tracked.txt"]


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
