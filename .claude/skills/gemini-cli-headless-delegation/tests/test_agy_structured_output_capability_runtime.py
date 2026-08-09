"""Runtime verification for Issue #2038's structured-output capability
predicate (AC1/AC4, `decision: immediate` per `## Runtime Verification
Applicability`).

Unlike `test_agy_structured_output.py` (pure unit tests against
hermetically-mocked probe results), this file performs a real, bounded
`agy --help` / `agy --version` probe against whatever `agy` binary is on
PATH in the execution environment, feeds the *real* result through
`preflight_agy.structured_output_capability_status()`, and writes the
same-binary evidence (realpath / SHA-256 / version) plus the verdict to
`artifacts/` (repo-root-relative, worktree-local; never committed --
`.gitignore` excludes `artifacts/`).

Per this Issue's `skip_conditions`:
- `agy` not present on PATH -> SKIP (never fabricated PASS).
- The capability matrix status is anything other than `"supported"` -> SKIP
  (a live `--output-format` invocation is never attempted when the
  same-binary evidence does not already confirm support -- attempting it
  anyway would not be a meaningful confirmation of the capability predicate
  under test, and Issue #1941's evidence-priority policy means `help`
  evidence alone can never itself promote to `"supported"`).

SKIP is never converted to PASS (`fallback_success_is_pass: false`).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest


_PREFLIGHT_AGY_PATH = Path(__file__).resolve().parents[1] / "scripts" / "preflight_agy.py"
_REPO_ROOT = Path(__file__).resolve().parents[4]
_ARTIFACTS_DIR = _REPO_ROOT / "artifacts"


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


preflight_agy = _load_module(_PREFLIGHT_AGY_PATH, "preflight_agy")


def _write_runtime_verification_log(
    *,
    verdict: str,
    reason: str,
    binary_identity: dict[str, "str | int | None"] | None,
    help_result: dict[str, "int | str | None"] | None,
    capability_record: dict[str, "str | None"] | None,
) -> Path:
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = _ARTIFACTS_DIR / f"runtime-verification-AC1-AC4-{timestamp}.log"
    payload = {
        "ac": "AC1/AC4 -- structured-output (--output-format) capability, Issue #2038",
        "timestamp_utc": timestamp,
        "binary_identity": binary_identity,
        "help_probe_result": help_result,
        "capability_record": capability_record,
        "verdict": verdict,
        "reason": reason,
    }
    lines = [
        "=== Runtime Verification Log ===",
        "AC: AC1/AC4 -- structured-output (--output-format) capability (Issue #2038)",
        f"Timestamp: {timestamp}",
        "Environment: real `agy` binary on PATH (bounded, no live prompt invocation)",
        "",
        "--- Input ---",
        f"[agy_bin, '--help'] / [agy_bin, '--version'] (binary_identity={binary_identity})",
        "",
        "--- Output ---",
        json.dumps(payload, indent=2, sort_keys=True),
        "",
        "--- Verdict ---",
        f"Result: {verdict}",
        f"Reason: {reason}",
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


def test_structured_output_capability_runtime_probe_same_binary_evidence() -> None:
    """GIVEN the real `agy` binary present in this execution environment (if any)
    WHEN a bounded `agy --help` / `agy --version` probe is run and classified through
    `preflight_agy.structured_output_capability_status()` (the same SSOT
    `run_gemini_headless.py` consumes)
    THEN same-binary evidence (realpath/SHA-256/version) and the resulting capability
    verdict are written to `artifacts/`, and the test SKIPs (never fabricates PASS)
    unless the capability status is genuinely "supported" (Issue #2038 Runtime
    Verification Applicability skip_conditions).
    """
    agy_bin = shutil.which("agy")
    if agy_bin is None:
        _write_runtime_verification_log(
            verdict="SKIP",
            reason="agy CLI not found on PATH in this execution environment",
            binary_identity=None,
            help_result=None,
            capability_record=None,
        )
        pytest.skip("SKIP: agy CLI not found on PATH -- see docs/dev/runtime-verification-policy.md SKIP convention")

    resolved = Path(agy_bin).resolve()
    binary_identity = {
        "realpath": str(resolved),
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
        "size": resolved.stat().st_size,
    }

    help_proc = subprocess.run(
        [agy_bin, "--help"], capture_output=True, text=True, timeout=20, check=False
    )
    version_proc = subprocess.run(
        [agy_bin, "--version"], capture_output=True, text=True, timeout=20, check=False
    )
    binary_identity["version_stdout"] = (version_proc.stdout or "").strip()

    help_result = {
        "exit_code": help_proc.returncode,
        "stdout": help_proc.stdout or "",
        "stderr": help_proc.stderr or "",
    }
    capability_record = preflight_agy.structured_output_capability_status(help_result)

    if capability_record["status"] != "supported":
        _write_runtime_verification_log(
            verdict="SKIP",
            reason=(
                "structured-output capability status is "
                f"{capability_record['status']!r} (reason_code={capability_record['reason_code']!r}), "
                "not \"supported\" -- per Issue #2038 skip_conditions, a live --output-format "
                "invocation is not attempted (help evidence alone never promotes to \"supported\" "
                "per Issue #1941's evidence-priority policy)"
            ),
            binary_identity=binary_identity,
            help_result={"exit_code": help_result["exit_code"], "stdout_excerpt": help_result["stdout"][:2000]},
            capability_record=capability_record,
        )
        pytest.skip(
            "SKIP: structured-output capability status is "
            f"{capability_record['status']!r}, not \"supported\" -- see artifacts/ log"
        )

    # If a future agy version's evidence ever resolves to "supported" here,
    # this branch intentionally still only confirms the *capability record*
    # (same-binary evidence) rather than performing a live, cost-incurring
    # `--output-format json` prompt invocation -- that live invocation is
    # explicitly Out of Scope wiring for this Issue (see #2038 Notes for
    # Reviewer P1-5: structured route default-on wiring is a follow-up
    # concern, not this AC's runtime verification target).
    _write_runtime_verification_log(
        verdict="PASS",
        reason="structured-output capability status resolved to \"supported\" from real same-binary evidence",
        binary_identity=binary_identity,
        help_result={"exit_code": help_result["exit_code"], "stdout_excerpt": help_result["stdout"][:2000]},
        capability_record=capability_record,
    )
