#!/usr/bin/env python3
"""run_runtime_verification_ac11.py -- AC11 runtime-verification VC wrapper (Issue #2007).

Wraps ``test_root_temporary_residue_advisory_runtime_smoke.py`` (real
``.claude/hooks/root_temporary_residue_advisory.sh`` PreToolUse hook process
spawns) in a non-pytest-collected exit-code translation wrapper, following
the same pattern as
``scripts/agent-ops/tests/run_runtime_verification_ac18.py`` (Issue #2161):

  - exit 0  : the targeted suite ran and at least one test genuinely
              PASSED (a mix of pass + legitimate individual pytest SKIPs is
              still exit 0).
  - exit 1  : any targeted test genuinely FAILED (or pytest could not even
              collect the target -- fail-closed, never silently exit 0).
  - exit 77 : the ENTIRE targeted suite was skipped by pytest's own
              internal SKIP semantics (e.g. no ``claude`` binary or hook
              execution environment available in this environment).
              Prints ``SKIP: <reason>`` to stdout.

A log evidence file is written to
``artifacts/runtime-verification-AC11-<timestamp>.log`` with the required
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
TARGET_FILE = REPO_ROOT / "scripts" / "agent-guards" / "tests" / (
    "test_root_temporary_residue_advisory_runtime_smoke.py"
)
ARTIFACTS_DIR = REPO_ROOT / "artifacts"

# Bounded overall timeout for the wrapped pytest subprocess -- each test in
# the target suite spawns a real bash/python3 hook subprocess with its own
# bounded 30s timeout; a generous but bounded ceiling keeps this VC from
# hanging indefinitely in a genuinely broken environment (fail-closed: a
# timeout is FAIL, never a fabricated PASS or SKIP).
_SUBPROCESS_TIMEOUT_SECONDS = 300.0

_MAX_OUTPUT_LINES = 500


def _environment_summary() -> str:
    claude_bin = shutil.which("claude")
    bash_bin = shutil.which("bash")
    python3_bin = shutil.which("python3")
    return (
        f"OS={platform.platform()}; Python={platform.python_version()}; "
        f"claude_on_PATH={'yes' if claude_bin else 'no'}; "
        f"bash_on_PATH={'yes' if bash_bin else 'no'}; "
        f"python3_on_PATH={'yes' if python3_bin else 'no'}"
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
    log_path = ARTIFACTS_DIR / f"runtime-verification-AC11-{timestamp}.log"
    combined_output = stdout
    if stderr.strip():
        combined_output += "\n--- stderr ---\n" + stderr
    body = "\n".join(
        [
            "=== Runtime Verification Log ===",
            "AC: AC11 -- root_temporary_residue_advisory.sh live PreToolUse hook "
            ".claude/tmp/** write/read/scan/delete regression (Issue #2007 REPO_TEMP_FOLDER_ADVICE_V2)",
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

    if not TARGET_FILE.is_file():
        reason = f"target test file not found: {TARGET_FILE.relative_to(REPO_ROOT)}"
        print(f"SKIP: {reason}", file=sys.stdout)
        _write_evidence_log(
            now=now,
            input_argv=["<no target>"],
            stdout="",
            stderr="",
            verdict="SKIP",
            exit_code=77,
            reason=reason,
        )
        return 77

    with tempfile.TemporaryDirectory(prefix="ac11-junit-") as tmp_dir:
        junit_path = Path(tmp_dir) / "junit.xml"
        relative_target = str(TARGET_FILE.relative_to(REPO_ROOT))
        argv_cmd = [
            "uv",
            "run",
            "--locked",
            "pytest",
            relative_target,
            "-q",
            "--tb=short",
            f"--junitxml={junit_path}",
        ]
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
            reason = "no tests were collected from the target file (operational failure)"
            print(f"FAIL: {reason}", file=sys.stderr)
            _write_evidence_log(
                now=now, input_argv=argv_cmd, stdout=stdout, stderr=stderr,
                verdict="FAIL", exit_code=1, reason=reason,
            )
            return 1

        if passed == 0:
            # Every collected test hit pytest's own internal SKIP (e.g. no
            # `claude` binary / hook execution environment in this
            # environment) -- the whole targeted suite SKIPs (never
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
