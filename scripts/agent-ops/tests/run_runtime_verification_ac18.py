#!/usr/bin/env python3
"""run_runtime_verification_ac18.py — AC18 runtime-verification VC wrapper (Issue #2161).

Wraps the existing, already-independently-tested claude-native / claude-gpt
focused regression pytest suite for the two Allowed Paths runners this
Issue modified (native Codex CLI lane removal):

  - ``scripts/agent-ops/run_worktree_agent_runtime_smoke.py`` --
    ``scripts/agent-ops/tests/test_run_worktree_agent_runtime_smoke*.py``
  - ``scripts/agent-ops/run_agent_provider_route_smoke.py`` (``claude_code``
    route cases) -- ``scripts/agent-ops/tests/test_agent_provider_route_smoke.py``

Both target file sets are exclusively claude-native/claude-gpt focused
already: the Codex-only test cases that historically lived alongside them
were deleted in this same Issue (native Codex CLI retirement), so this
wrapper runs the FULL remaining suite in both files -- no separate
`-k`/`-m` filter is needed to "exclude Codex-only cases" because there are
none left to exclude.

This script does NOT invoke or require the real Codex CLI, and does NOT
build a new live-smoke harness -- it only orchestrates the existing pytest
suite (real ``claude`` subprocess spawns happen inside those tests exactly
as they already did before this Issue) and classifies the aggregate
PASS / FAIL / SKIP outcome per docs/dev/runtime-verification-policy.md:

  - exit 0  : the targeted subset ran and at least one test genuinely
              PASSED (a mix of pass + legitimate individual pytest SKIPs is
              still exit 0 -- that is the harness's own existing SKIP
              design, not a fabricated result).
  - exit 1  : any targeted test genuinely FAILED (or pytest could not even
              collect the targets -- fail-closed, never silently exit 0).
  - exit 77 : the ENTIRE targeted subset was skipped by pytest's own
              internal SKIP semantics (e.g. no ``claude`` binary available
              in this environment, so every test in the subset hit its own
              ``pytest.skip(...)``/SKIP-77-equivalent early-return). Prints
              ``SKIP: <reason>`` to stdout.

A log evidence file is written to
``artifacts/runtime-verification-AC18-<timestamp>.log`` with the required
fields per docs/dev/runtime-verification-policy.md section 4 (AC /
Timestamp / Environment / Input / Output / Verdict).
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TESTS_DIR = REPO_ROOT / "scripts" / "agent-ops" / "tests"
ARTIFACTS_DIR = REPO_ROOT / "artifacts"

# Bounded overall timeout for the wrapped pytest subprocess -- this suite
# spawns real subprocesses (claude/herdr preflight probes, fake-exe
# harnesses) and has been observed to take up to ~4 minutes locally; a
# generous but bounded ceiling keeps this VC from hanging indefinitely in a
# genuinely broken environment (fail-closed: a timeout is FAIL, never a
# fabricated PASS or SKIP).
_SUBPROCESS_TIMEOUT_SECONDS = 900.0

_MAX_OUTPUT_LINES = 500


def _target_files() -> list[Path]:
    smoke_files = sorted(TESTS_DIR.glob("test_run_worktree_agent_runtime_smoke*.py"))
    route_file = TESTS_DIR / "test_agent_provider_route_smoke.py"
    targets = list(smoke_files)
    if route_file.is_file():
        targets.append(route_file)
    return targets


def _environment_summary() -> str:
    claude_bin = shutil.which("claude")
    node_bin = shutil.which("node")
    herdr_bin = shutil.which("herdr")
    return (
        f"OS={platform.platform()}; Python={platform.python_version()}; "
        f"claude_on_PATH={'yes' if claude_bin else 'no'}; "
        f"node_on_PATH={'yes' if node_bin else 'no'}; "
        f"herdr_on_PATH={'yes' if herdr_bin else 'no'}"
    )


def _truncate(text: str, max_lines: int = _MAX_OUTPUT_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    head = lines[:max_lines]
    return "\n".join(head) + f"\n... [truncated, {len(lines) - max_lines} more lines]"


def _parse_junit_counts(junit_path: Path) -> dict[str, int] | None:
    try:
        tree = ET.parse(junit_path)  # noqa: S314
    except (ET.ParseError, OSError):
        return None
    root = tree.getroot()
    # pytest's junit-xml root is <testsuites><testsuite .../></testsuites>
    # (or a bare <testsuite> for a single suite); sum across all <testsuite>.
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.attrib.get(key, 0) or 0)
    return totals


def _skip_reasons(junit_path: Path, limit: int = 5) -> list[str]:
    try:
        tree = ET.parse(junit_path)  # noqa: S314
    except (ET.ParseError, OSError):
        return []
    reasons: list[str] = []
    for testcase in tree.getroot().iter("testcase"):
        skipped = testcase.find("skipped")
        if skipped is not None:
            message = skipped.attrib.get("message") or (skipped.text or "").strip()
            name = testcase.attrib.get("name", "<unknown>")
            reasons.append(f"{name}: {message[:200]}")
            if len(reasons) >= limit:
                break
    return reasons


def _write_evidence_log(
    *,
    now: datetime,
    input_argv: list[str],
    stdout: str,
    stderr: str,
    verdict: str,
    exit_code: int,
    reason: str | None,
) -> Path:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    log_path = ARTIFACTS_DIR / f"runtime-verification-AC18-{timestamp}.log"
    combined_output = stdout
    if stderr.strip():
        combined_output += "\n--- stderr ---\n" + stderr
    body = "\n".join(
        [
            "=== Runtime Verification Log ===",
            "AC: AC18 -- claude-native/claude-gpt runtime smoke regression subset "
            "(Issue #2161 native Codex CLI runtime lane retirement)",
            f"Timestamp: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            f"Environment: {_environment_summary()}",
            "",
            "--- Input ---",
            " ".join(input_argv),
            "",
            "--- Output ---",
            _truncate(combined_output),
            "",
            "--- Verdict ---",
            f"Result: {verdict}",
            f"Exit Code: {exit_code}",
            f"Reason: {reason or '-'}",
            "",
        ]
    )
    log_path.write_text(body, encoding="utf-8")
    return log_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    targets = _target_files()
    if not targets:
        print("SKIP: no target test files found under scripts/agent-ops/tests/", file=sys.stdout)
        _write_evidence_log(
            now=now,
            input_argv=["<no targets>"],
            stdout="",
            stderr="",
            verdict="SKIP",
            exit_code=77,
            reason="no target test files found",
        )
        return 77

    with tempfile.TemporaryDirectory(prefix="ac18-junit-") as tmp_dir:
        junit_path = Path(tmp_dir) / "junit.xml"
        relative_targets = [str(p.relative_to(REPO_ROOT)) for p in targets]
        argv_cmd = [
            "uv",
            "run",
            "--locked",
            "pytest",
            *relative_targets,
            "-q",
            "--tb=short",
            f"--junitxml={junit_path}",
        ]
        # --Input-- evidence records the relative-path argv (never absolute
        # local worktree paths, per docs/dev/runtime-verification-policy.md
        # section 5's redact guidance); the actual subprocess is still
        # invoked with cwd=REPO_ROOT so the relative paths resolve.
        try:
            result = subprocess.run(  # noqa: S603
                argv_cmd,
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
                check=False,
            )
            stdout, stderr = result.stdout, result.stderr
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            reason = f"pytest subprocess exceeded {_SUBPROCESS_TIMEOUT_SECONDS:.0f}s bounded timeout"
            print(f"FAIL: {reason}", file=sys.stderr)
            _write_evidence_log(
                now=now, input_argv=argv_cmd, stdout=stdout, stderr=stderr,
                verdict="FAIL", exit_code=1, reason=reason,
            )
            return 1

        counts = _parse_junit_counts(junit_path)
        if counts is None:
            reason = "pytest produced no parseable junit-xml report (operational failure)"
            print(f"FAIL: {reason}", file=sys.stderr)
            _write_evidence_log(
                now=now, input_argv=argv_cmd, stdout=stdout, stderr=stderr,
                verdict="FAIL", exit_code=1, reason=reason,
            )
            return 1

        tests = counts["tests"]
        failures = counts["failures"]
        errors = counts["errors"]
        skipped = counts["skipped"]
        passed = tests - failures - errors - skipped

        if failures > 0 or errors > 0:
            reason = f"{failures} failure(s), {errors} error(s) out of {tests} collected test(s)"
            print(f"FAIL: {reason}", file=sys.stderr)
            _write_evidence_log(
                now=now, input_argv=argv_cmd, stdout=stdout, stderr=stderr,
                verdict="FAIL", exit_code=1, reason=reason,
            )
            return 1

        if tests == 0:
            reason = "no tests were collected from the target files (operational failure)"
            print(f"FAIL: {reason}", file=sys.stderr)
            _write_evidence_log(
                now=now, input_argv=argv_cmd, stdout=stdout, stderr=stderr,
                verdict="FAIL", exit_code=1, reason=reason,
            )
            return 1

        if passed == 0:
            # Every collected test hit pytest's own internal SKIP (e.g. no
            # `claude` binary / no test-double authorization in this
            # environment) -- the whole targeted subset SKIPs (never
            # promoted to a fabricated PASS).
            skip_reasons = _skip_reasons(junit_path)
            reason = (
                f"all {tests} collected test(s) were skipped by the suite's own "
                "internal SKIP semantics (no genuine execution occurred): "
                + ("; ".join(skip_reasons) if skip_reasons else "no skip reason captured")
            )
            print(f"SKIP: {reason}")
            _write_evidence_log(
                now=now, input_argv=argv_cmd, stdout=stdout, stderr=stderr,
                verdict="SKIP", exit_code=77, reason=reason,
            )
            return 77

        reason = f"{passed} passed, {skipped} skipped (legitimate internal SKIP), 0 failed out of {tests}"
        print(f"PASS: {reason}")
        _write_evidence_log(
            now=now, input_argv=argv_cmd, stdout=stdout, stderr=stderr,
            verdict="PASS", exit_code=0, reason=reason,
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
