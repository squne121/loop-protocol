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

Issue #2616 AC2 adds a second, distinct entry point to this same file: running
it directly as a script (not via pytest) with `--stage2-model-backed`
performs the real, model-backed Stage 2 runtime verification described in
Issue #2616's `## Runtime Verification Applicability` -- a genuine `agy -p
<sentinel> --output-format stream-json` invocation (and, only on PASS, a
flag-acceptance-only `--output-format json --print-timeout 15m` follow-up)
against an *existing* noninteractive account session, gated behind the
explicit `AGY_PREFLIGHT_RUNTIME_ACCOUNT_SESSION_MODE=1` flag (in addition to
the existing `AGY_PREFLIGHT_CONFIRM_RUNTIME_PROBE_COST=1` cost-confirmation
gate). `--caller-context claude-code|claude-gpt` is an optional,
self-reported label (never cryptographically/process verified) recorded on
the emitted evidence only. See
`.claude/skills/gemini-cli-headless-delegation/scripts/preflight_agy.py`'s
"Stage 2 model-backed runtime verification" section for the shared
classification primitives this entry point uses. This script entry point
never fabricates a PASS, never retains raw agy stdout/stderr, and always
emits either `SKIP: <reason>` (exit 77) or a single `runtime_verification_result/v1`
JSON object (exit 0 for PASS, exit 1 for FAIL).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import tempfile
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


# ---------------------------------------------------------------------------
# Issue #2616 AC2/AC8/AC9: Stage 2 model-backed runtime verification CLI
# entry point (`--stage2-model-backed [--caller-context claude-code|claude-gpt]`).
# Invoked directly as a script, never via pytest -- see module docstring.
# ---------------------------------------------------------------------------

_CALLER_CONTEXT_AC_SUFFIX = {"claude-code": "AC8", "claude-gpt": "AC9"}


def _write_stage2_runtime_verification_log(
    *,
    verdict: str,
    reason_code: str,
    caller_context: "str | None",
    binary_identity: "dict[str, object] | None",
    stage1_status: "str | None",
    primary_classification: "dict[str, str] | None",
    secondary_classification: "dict[str, str] | None",
) -> Path:
    """Write the redacted `runtime_verification_result/v1` evidence log.

    Never includes raw agy stdout/stderr, prompt text, credential, or
    account-identity content -- only classification metadata and same-binary
    identity (realpath/sha256/version), matching this Issue's
    `artifact_requirements` (secret-redacted, worktree-local, untracked).
    """
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ac_suffix = _CALLER_CONTEXT_AC_SUFFIX.get(caller_context or "")
    filename_suffix = f"-{ac_suffix}" if ac_suffix else ""
    log_path = _ARTIFACTS_DIR / f"runtime-verification-AC2{filename_suffix}-{timestamp}.log"
    payload = {
        "schema": preflight_agy.RUNTIME_VERIFICATION_SCHEMA,
        "ac": "AC2" + (f"/{ac_suffix}" if ac_suffix else ""),
        "timestamp_utc": timestamp,
        "caller_context": caller_context,
        "binary_identity": binary_identity,
        "stage1_status": stage1_status,
        "primary_execution_classification": primary_classification,
        "secondary_execution_classification": secondary_classification,
        "verdict": verdict,
        "reason_code": reason_code,
    }
    lines = [
        "=== Runtime Verification Log ===",
        "AC: AC2 Stage 2 model-backed runtime verification (Issue #2616)"
        + (f", caller_context={caller_context} ({ac_suffix})" if ac_suffix else ""),
        f"Timestamp: {timestamp}",
        "Environment: real `agy` binary + existing noninteractive account session"
        " (explicit account-session mode); raw stdout/stderr never retained.",
        "",
        "--- Output (redacted -- no raw prompt/response/credential content) ---",
        json.dumps(payload, indent=2, sort_keys=True),
        "",
        "--- Verdict ---",
        f"Result: {verdict}",
        f"Reason: {reason_code}",
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


def _stage2_skip(*, reason_code: str, caller_context: "str | None", **extra: object) -> int:
    _write_stage2_runtime_verification_log(
        verdict="SKIP",
        reason_code=reason_code,
        caller_context=caller_context,
        binary_identity=extra.get("binary_identity"),
        stage1_status=extra.get("stage1_status"),
        primary_classification=None,
        secondary_classification=None,
    )
    print(f"SKIP: {reason_code}")
    return 77


def run_stage2_model_backed_cli(caller_context: "str | None" = None) -> int:
    """Execute Issue #2616 AC2's Stage 2 model-backed runtime verification.

    Returns the process exit code (77 for SKIP, 0 for PASS, 1 for FAIL) --
    never fabricates a PASS, never performs a second diagnostic-
    classification-only call after a non-success primary execution, and
    never persists raw agy stdout/stderr.
    """
    if not preflight_agy._runtime_probe_cost_confirmed():
        return _stage2_skip(reason_code="runtime_probe_cost_not_confirmed", caller_context=caller_context)
    if not preflight_agy.runtime_account_session_mode_enabled():
        return _stage2_skip(reason_code="account_session_mode_not_enabled", caller_context=caller_context)

    agy_bin = shutil.which("agy")
    if agy_bin is None:
        return _stage2_skip(reason_code="agy_cli_unavailable", caller_context=caller_context)

    resolved = Path(agy_bin).resolve()
    binary_identity = {
        "realpath": str(resolved),
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
        "size": resolved.stat().st_size,
    }

    try:
        help_proc = subprocess.run([agy_bin, "--help"], capture_output=True, text=True, timeout=20, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return _stage2_skip(
            reason_code="agy_help_probe_unavailable", caller_context=caller_context, binary_identity=binary_identity
        )
    help_result = {
        "exit_code": help_proc.returncode,
        "stdout": help_proc.stdout or "",
        "stderr": help_proc.stderr or "",
        "binary_identity": preflight_agy.compute_binary_identity(agy_bin),
    }
    stage1 = preflight_agy.structured_output_capability_status(help_result)
    if stage1["status"] != "inconclusive":
        # Issue #2616 AC2: Stage 2 is permitted only when Stage 1 is
        # "inconclusive" -- a same-binary Stage 1 status that has already
        # resolved definitively (supported/unsupported/unavailable/
        # evidence_invalid) is a precondition state, not a runtime
        # invocation outcome, so this is a SKIP rather than a FAIL.
        return _stage2_skip(
            reason_code=f"stage1_not_inconclusive:{stage1['status']}",
            caller_context=caller_context,
            binary_identity=binary_identity,
            stage1_status=stage1["status"],
        )

    env = preflight_agy.runtime_verification_account_session_env()
    with tempfile.TemporaryDirectory(prefix="agy-stage2-runtime-verification-") as temp_dir:
        primary_argv = [
            agy_bin,
            "-p",
            preflight_agy.RUNTIME_VERIFICATION_SENTINEL_PROMPT,
            "--output-format",
            "stream-json",
        ]
        primary_execution = preflight_agy.run_runtime_verification_process_group(
            primary_argv, env=env, cwd=Path(temp_dir)
        )
        primary_classification = preflight_agy.classify_runtime_verification_primary_execution(primary_execution)

        secondary_classification: "dict[str, str] | None" = None
        overall_verdict = primary_classification["verdict"]
        overall_reason = primary_classification["reason_code"]
        if primary_classification["verdict"] == "PASS":
            secondary_argv = [
                agy_bin,
                "-p",
                preflight_agy.RUNTIME_VERIFICATION_SENTINEL_PROMPT,
                "--output-format",
                "json",
                "--print-timeout",
                preflight_agy.RUNTIME_VERIFICATION_PRINT_TIMEOUT_FLAG_VALUE,
            ]
            secondary_execution = preflight_agy.run_runtime_verification_process_group(
                secondary_argv, env=env, cwd=Path(temp_dir)
            )
            secondary_classification = preflight_agy.classify_runtime_verification_flag_acceptance_execution(
                secondary_execution
            )
            overall_verdict = secondary_classification["verdict"]
            overall_reason = secondary_classification["reason_code"]

    _write_stage2_runtime_verification_log(
        verdict=overall_verdict,
        reason_code=overall_reason,
        caller_context=caller_context,
        binary_identity=binary_identity,
        stage1_status=stage1["status"],
        primary_classification=primary_classification,
        secondary_classification=secondary_classification,
    )

    if overall_verdict == "SKIP":
        print(f"SKIP: {overall_reason}")
        return 77

    ac_suffix = (
        f"/{_CALLER_CONTEXT_AC_SUFFIX[caller_context]}"
        if caller_context in _CALLER_CONTEXT_AC_SUFFIX
        else ""
    )
    result_payload = {
        "schema": preflight_agy.RUNTIME_VERIFICATION_SCHEMA,
        "ac": "AC2" + ac_suffix,
        "caller_context": caller_context,
        "verdict": overall_verdict,
        "reason_code": overall_reason,
        "binary_identity": binary_identity,
        "stage1_status": stage1["status"],
    }
    print(json.dumps(result_payload, indent=2, sort_keys=True))
    return 0 if overall_verdict == "PASS" else 1


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage2-model-backed",
        action="store_true",
        help="Run Issue #2616 AC2's real, model-backed Stage 2 runtime verification (never via pytest).",
    )
    parser.add_argument(
        "--caller-context",
        choices=sorted(_CALLER_CONTEXT_AC_SUFFIX),
        default=None,
        help="Self-reported (not cryptographically verified) execution-context label for evidence purposes only.",
    )
    cli_args = parser.parse_args()
    if not cli_args.stage2_model_backed:
        parser.error("--stage2-model-backed is required when invoking this file directly as a script")
    sys.exit(run_stage2_model_backed_cli(cli_args.caller_context))
