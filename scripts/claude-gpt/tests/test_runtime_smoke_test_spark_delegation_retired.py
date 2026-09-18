"""scripts/claude-gpt/tests/test_runtime_smoke_test_spark_delegation_retired.py

Issue #2651 AC9: `scripts/claude-gpt/runtime_smoke_test.sh --spark-delegation`
must never fall through the catch-all argument-parsing loop and silently
succeed as an ordinary smoke run -- it must be an explicit, deterministic
non-zero exit, reached BEFORE any SUT/proxy identity resolution, evidence
directory setup, or preflight logic that the (now-removed) live Spark E2E
harness used to depend on.

This is a real subprocess invocation of the actual script (never a
reimplementation of its argv-parsing logic), matching this Issue's Runtime
Verification Applicability (`decision: immediate`, `applicable_acs`
includes AC9).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_RUNTIME_SMOKE_SH = _REPO_ROOT / "scripts" / "claude-gpt" / "runtime_smoke_test.sh"

_SH_BIN = shutil.which("sh") or "/bin/sh"

_RETIRED_MARKER = "--spark-delegation is retired"


def _run_smoke(*extra_args: str, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_SH_BIN, str(_RUNTIME_SMOKE_SH), *extra_args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(_REPO_ROOT),
    )


def test_spark_delegation_flag_exits_nonzero_deterministically():
    """GIVEN the real `runtime_smoke_test.sh --spark-delegation` invocation
    WHEN it runs
    THEN it exits non-zero (never 0 -- the retired flag must never report
    success) and never the SKIP exit code 77 (this is a deterministic
    rejection, not an environment-unavailable SKIP)."""
    result = _run_smoke("--spark-delegation")
    assert result.returncode != 0, result.stdout + result.stderr
    assert result.returncode != 77, (
        "retirement must be a deterministic rejection, not an environment-unavailable SKIP: "
        + result.stdout
        + result.stderr
    )


def test_spark_delegation_flag_reports_explicit_retired_reason():
    """GIVEN the real `runtime_smoke_test.sh --spark-delegation` invocation
    WHEN it runs
    THEN stderr contains an explicit, human-readable retired-route message
    naming Issue #2651 -- not a generic/ambiguous failure."""
    result = _run_smoke("--spark-delegation")
    combined = result.stdout + result.stderr
    assert _RETIRED_MARKER in combined, combined
    assert "#2651" in combined, combined


def test_spark_delegation_flag_never_produces_a_pass_or_skip_evidence_schema():
    """GIVEN the real `runtime_smoke_test.sh --spark-delegation` invocation
    WHEN it runs
    THEN no `SPARK_DELEGATION_EVIDENCE_V2` / `CLAUDE_GPT_SMOKE_RESULT_V1`
    schema output is ever produced -- the retired branch is reached before
    any evidence-schema logic, so there is no dormant success/skip shape
    that could be reached instead."""
    result = _run_smoke("--spark-delegation")
    combined = result.stdout + result.stderr
    assert "SPARK_DELEGATION_EVIDENCE_V2" not in combined
    assert "CLAUDE_GPT_SMOKE_RESULT_V1" not in combined
    assert '"status":"pass"' not in combined
    assert '"status": "pass"' not in combined


def test_spark_delegation_flag_does_not_fall_through_to_ordinary_smoke_via_catch_all():
    """GIVEN `--spark-delegation` combined with a second unrelated unknown
    flag (which WOULD legitimately fall through the main loop's catch-all
    `*) shift ;;` branch on its own)
    WHEN the script runs
    THEN `--spark-delegation` is still rejected deterministically -- the
    argv pre-scan that catches it runs unconditionally over the entire
    `"$@"`, regardless of position or other unrecognized flags, so it can
    never be shadowed by the catch-all silently consuming it first."""
    result = _run_smoke("--some-unrelated-unknown-flag", "--spark-delegation")
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert _RETIRED_MARKER in combined, combined


def test_ordinary_invocation_without_the_flag_does_not_hit_the_retired_branch():
    """Negative control: the argv pre-scan case/esac only matches the exact
    literal `--spark-delegation` token (proves it is not a false-positive
    substring match that would reject unrelated flags too). This is a
    static source check, not a real subprocess invocation, deliberately --
    an ordinary (non-`--spark-delegation`) real invocation would proceed
    into the full script's live proxy/ChatGPT-auth environment-availability
    logic, which is unbounded/live-dependent and not this AC's subject."""
    text = _RUNTIME_SMOKE_SH.read_text(encoding="utf-8")
    pre_scan_start = text.index("for _arg in \"$@\"; do")
    pre_scan_end = text.index("\ndone\n", pre_scan_start)
    pre_scan_block = text[pre_scan_start:pre_scan_end]
    assert 'case "$_arg" in' in pre_scan_block
    assert "--spark-delegation)" in pre_scan_block
    # The case pattern is the exact literal token only -- no glob wildcard
    # that could accidentally also match an unrelated flag.
    assert "--spark-delegation*" not in pre_scan_block
    assert "*--spark-delegation*" not in pre_scan_block
