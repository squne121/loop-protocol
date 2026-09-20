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
gate). See
`.claude/skills/gemini-cli-headless-delegation/scripts/preflight_agy.py`'s
"Stage 2 model-backed runtime verification" section for the shared
classification primitives this entry point uses. This script entry point
never fabricates a PASS, never retains raw agy stdout/stderr, and always
emits either `SKIP: <reason>` (exit 77) or a single `runtime_verification_result/v1`
JSON object (exit 0 for PASS, exit 1 for FAIL).

`--caller-context claude-code|claude-gpt` is a self-reported label (never
cryptographically/process verified) recorded on the emitted evidence, and
(Issue #2616 fix_delta P1-1) also SELECTS a structurally different route:
supplying it routes AC8/AC9 through
`run_canonical_delegation_route_probe()`, which calls the CANONICAL
`run_gemini_headless.py::run_delegation()` delegation path (the same
function a real Claude Code / Claude-GPT SubAgent's own delegation calls
use) and classifies its normalized `ok`/`failure_class`/`response_text`
result -- never `run_stage2_model_backed_cli()`'s own direct-CLI probe,
which remains AC2's job and is reached only when `--caller-context` is
omitted. This separation exists because a direct-CLI headless-contract
probe (AC2: does `agy` itself honor the documented `--output-format`/
`--print-timeout` surface?) and a canonical-route reachability probe
(AC8/AC9: does the actual production SubAgent delegation code path reach a
real `agy` child process?) are different claims and must never share one
evidence source.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest


_PREFLIGHT_AGY_PATH = Path(__file__).resolve().parents[1] / "scripts" / "preflight_agy.py"
_AGY_PERMISSION_POLICY_PATH = Path(__file__).resolve().parents[1] / "scripts" / "agy_permission_policy.py"
_REPO_ROOT = Path(__file__).resolve().parents[4]
_ARTIFACTS_DIR = _REPO_ROOT / "artifacts"


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclass()'s postponed-annotation resolution needs sys.modules[__module__]
    # registered before exec (agy_permission_policy.py uses `@dataclass` under
    # `from __future__ import annotations` -- Issue #2670).
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


preflight_agy = _load_module(_PREFLIGHT_AGY_PATH, "preflight_agy")
# Issue #2670: independently resolves the same closed handoff-selection
# classification `materialize_isolated_agy_workspace()` (invoked inside
# `run_delegation()` below) computed for THIS exact invocation -- both are
# pure functions of the same `AGY_OAUTH_TOKEN_HANDOFF_ROOT` /
# `_SOURCE` env vars and filesystem state, so they always agree without this
# verifier needing to modify `run_gemini_headless.py` (Out of Scope) to
# thread the classification through its own return value.
agy_permission_policy = _load_module(_AGY_PERMISSION_POLICY_PATH, "agy_permission_policy_stage2_handoff")


@pytest.fixture(autouse=True)
def _clear_agy_oauth_token_handoff_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #2670: keep this file's pre-existing (#2616) tests deterministic
    regardless of ambient host state -- clears both dedicated handoff env
    vars for every test; the dedicated Issue #2670 tests below set them
    explicitly per case (mirrors the identical fixture in
    `test_agy_permission_policy_oauth_token.py` /
    `test_agy_permission_policy_readonly_boundary.py`)."""
    monkeypatch.delenv("AGY_OAUTH_TOKEN_HANDOFF_ROOT", raising=False)
    monkeypatch.delenv("AGY_OAUTH_TOKEN_HANDOFF_SOURCE", raising=False)


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


# ---------------------------------------------------------------------------
# Issue #2670 AC5/AC6: dedicated sanitized handoff-selection summary
# artifact. The AC5 verifier (`run_canonical_delegation_route_probe()`
# below) consumes the canonical policy/route's closed sanitized
# handoff-selection classification (`agy_permission_policy.
# resolve_agy_oauth_token_source()`) and writes this artifact. Allowlist-only
# payload -- see module docstring; never raw stdout/stderr/response/
# root/path/credential/account/token/HOME/XDG values.
# ---------------------------------------------------------------------------

_AC5_HANDOFF_SUMMARY_ALLOWED_KEYS = frozenset(
    {
        "schema",
        "ac",
        "route",
        "caller_context",
        "timestamp_utc",
        "executed_command",
        "binary_identity",
        "verdict",
        "reason_code",
        "exit_status",
        "artifact_path",
        "canonical_result_classification",
        "handoff_classification",
        "launch_provenance_source_check",
    }
)


def _write_ac5_handoff_summary_artifact(
    *,
    caller_context: str,
    executed_command: "list[str]",
    binary_identity: "dict[str, object] | None",
    verdict: str,
    reason_code: str,
    exit_status: int,
    canonical_result_classification: "dict[str, str] | None",
    handoff_classification: "str | None",
    launch_provenance_source_check: "dict[str, object] | None" = None,
) -> Path:
    """Issue #2670 AC5/AC6: write the dedicated sanitized handoff-selection
    summary artifact.

    Allowlist-only payload -- `executed_command`/`binary_identity` never
    contain secret content (argv literals + realpath/sha256/version, matching
    this file's pre-existing evidence posture), `handoff_classification` is
    always one of the closed four labels
    (`agy_permission_policy.AGY_HANDOFF_CLASSIFICATIONS`) and never a
    root/path/token/credential value itself, and this function never accepts
    (or therefore ever writes) a raw stdout/stderr/response/root/path/
    credential/account/token/HOME/XDG value -- there is no parameter for any
    of those.
    """
    if handoff_classification is not None:
        assert handoff_classification in agy_permission_policy.AGY_HANDOFF_CLASSIFICATIONS, handoff_classification
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ac_suffix = _CALLER_CONTEXT_AC_SUFFIX.get(caller_context, "")
    artifact_path = _ARTIFACTS_DIR / f"runtime-verification-AC5-AC6-handoff-{ac_suffix or 'na'}-{timestamp}.log"
    payload = {
        "schema": "agy_oauth_token_handoff_summary/v1",
        "ac": "AC5/AC6",
        "route": "canonical_delegation",
        "caller_context": caller_context,
        "timestamp_utc": timestamp,
        "executed_command": list(executed_command),
        "binary_identity": binary_identity,
        "verdict": verdict,
        "reason_code": reason_code,
        "exit_status": exit_status,
        "artifact_path": str(artifact_path.relative_to(_REPO_ROOT)),
        "canonical_result_classification": canonical_result_classification,
        "handoff_classification": handoff_classification,
        "launch_provenance_source_check": launch_provenance_source_check,
    }
    assert set(payload) <= _AC5_HANDOFF_SUMMARY_ALLOWED_KEYS, set(payload) - _AC5_HANDOFF_SUMMARY_ALLOWED_KEYS
    lines = [
        "=== AGY OAuth Token Handoff Summary Artifact (Issue #2670 AC5/AC6) ===",
        f"Timestamp: {timestamp}",
        "",
        "--- Output (allowlist-only -- no raw stdout/stderr/response/root/path/"
        "credential/account/token/HOME/XDG content) ---",
        json.dumps(payload, indent=2, sort_keys=True),
        "",
        "--- Verdict ---",
        f"Result: {verdict}",
        f"Reason: {reason_code}",
    ]
    artifact_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return artifact_path


def _stage2_skip(*, reason_code: str, caller_context: "str | None", **extra: object) -> int:
    _write_stage2_runtime_verification_log(
        verdict="SKIP",
        reason_code=reason_code,
        caller_context=caller_context,
        binary_identity=extra.get("binary_identity"),
        stage1_status=extra.get("stage1_status"),
        primary_classification=extra.get("primary_classification"),
        secondary_classification=extra.get("secondary_classification"),
    )
    print(f"SKIP: {reason_code}")
    return 77


def _stage2_fail(*, reason_code: str, caller_context: "str | None", **extra: object) -> int:
    """Emit a FAIL verdict (Issue #2616 P2-3: e.g. a same-binary identity
    mismatch observed mid-run) -- never promoted to PASS or silently
    downgraded to SKIP."""
    _write_stage2_runtime_verification_log(
        verdict="FAIL",
        reason_code=reason_code,
        caller_context=caller_context,
        binary_identity=extra.get("binary_identity"),
        stage1_status=extra.get("stage1_status"),
        primary_classification=extra.get("primary_classification"),
        secondary_classification=extra.get("secondary_classification"),
    )
    print(f"FAIL: {reason_code}")
    return 1


def _compute_stage2_binary_identity(resolved: Path) -> "dict[str, object]":
    """Same-binary identity fingerprint (canonical absolute path, SHA-256,
    size) for Issue #2616 P2-3.

    Computed once at pin time from the given already-resolved canonical
    path, and again at each recheck checkpoint
    (`_stage2_binary_identity_unchanged()`) by independently re-resolving
    the ORIGINAL (possibly-symlink) input path -- so a mid-run symlink
    retarget is observable as an identity mismatch even though every actual
    invocation argv always uses the pinned canonical path from the first
    resolution (never a live re-resolution of a possibly-swapped symlink).
    """
    return {
        "realpath": str(resolved),
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
        "size": resolved.stat().st_size,
    }


def _stage2_binary_identity_unchanged(agy_bin: str, pinned_identity: "dict[str, object]") -> bool:
    """Re-resolve *agy_bin* (the original, possibly-symlink input path) and
    return True iff its current identity still matches *pinned_identity*
    (Issue #2616 P2-3). A path that is no longer resolvable/readable at
    recheck time is always a mismatch (fail-closed)."""
    try:
        current = _compute_stage2_binary_identity(Path(agy_bin).resolve())
    except OSError:
        return False
    return (
        current.get("realpath") == pinned_identity.get("realpath")
        and current.get("sha256") == pinned_identity.get("sha256")
        and current.get("size") == pinned_identity.get("size")
    )


def run_stage2_model_backed_cli(caller_context: "str | None" = None) -> int:
    """Execute Issue #2616 AC2/AC8/AC9's Stage 2 model-backed runtime
    verification.

    AC2 (``caller_context is None``) performs a direct-CLI headless-contract
    probe: it invokes the pinned `agy` binary itself, bypassing
    `run_gemini_headless.py` entirely, to verify the documented
    `--output-format`/`--print-timeout` stream-json/JSON surface.

    AC8/AC9 (``caller_context in {"claude-code", "claude-gpt"}``) instead
    route through the CANONICAL delegation path,
    `run_gemini_headless.run_delegation()`, and classify its normalized
    `ok`/`failure_class`/`response_text` result (Issue #2616 fix_delta
    P1-1) -- this is a structurally different code path from AC2's direct
    probe, and is the only route that actually proves the Claude
    Code/Claude-GPT SubAgent-facing canonical delegation route reaches a
    real `agy` child process.

    Returns the process exit code (77 for SKIP, 0 for PASS, 1 for FAIL) --
    never fabricates a PASS, never performs a second diagnostic-
    classification-only call after a non-success primary execution, and
    never persists raw agy stdout/stderr/response text.
    """
    if not preflight_agy._runtime_probe_cost_confirmed():
        return _stage2_skip(reason_code="runtime_probe_cost_not_confirmed", caller_context=caller_context)
    if not preflight_agy.runtime_account_session_mode_enabled():
        return _stage2_skip(reason_code="account_session_mode_not_enabled", caller_context=caller_context)

    if caller_context in _CALLER_CONTEXT_AC_SUFFIX:
        # Issue #2616 fix_delta P1-1: AC8/AC9 must exercise the canonical
        # `run_gemini_headless.py::run_delegation()` delegation route, never
        # this function's own direct-CLI probe below (that remains AC2's
        # job, reached only when caller_context is None).
        return run_canonical_delegation_route_probe(caller_context)

    agy_bin = shutil.which("agy")
    if agy_bin is None:
        return _stage2_skip(reason_code="agy_cli_unavailable", caller_context=caller_context)

    resolved = Path(agy_bin).resolve()
    binary_identity = _compute_stage2_binary_identity(resolved)
    try:
        version_proc = subprocess.run(
            [str(resolved), "--version"], capture_output=True, text=True, timeout=20, check=False
        )
        binary_identity["version_stdout"] = (version_proc.stdout or "").strip()
    except (subprocess.TimeoutExpired, OSError):
        binary_identity["version_stdout"] = None

    try:
        # Issue #2616 P2-3: every actual invocation argv uses the pinned
        # canonical `resolved` path (obtained above from a ONE-TIME
        # resolution of *agy_bin*), never the possibly-still-a-symlink
        # `agy_bin` string itself -- execution is therefore immune to a
        # later symlink retarget, while `_stage2_binary_identity_unchanged()`
        # below independently re-resolves `agy_bin` at each checkpoint to
        # make such a retarget observable as a fail-closed mismatch.
        help_proc = subprocess.run(
            [str(resolved), "--help"], capture_output=True, text=True, timeout=20, check=False
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return _stage2_skip(
            reason_code="agy_help_probe_unavailable", caller_context=caller_context, binary_identity=binary_identity
        )
    if not _stage2_binary_identity_unchanged(agy_bin, binary_identity):
        return _stage2_fail(
            reason_code="binary_identity_mismatch_after_help_probe",
            caller_context=caller_context,
            binary_identity=binary_identity,
        )
    help_result = {
        "exit_code": help_proc.returncode,
        "stdout": help_proc.stdout or "",
        "stderr": help_proc.stderr or "",
        "binary_identity": preflight_agy.compute_binary_identity(str(resolved)),
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
            str(resolved),
            "-p",
            preflight_agy.RUNTIME_VERIFICATION_SENTINEL_PROMPT,
            "--output-format",
            "stream-json",
        ]
        primary_execution = preflight_agy.run_runtime_verification_process_group(
            primary_argv, env=env, cwd=Path(temp_dir)
        )
        if not _stage2_binary_identity_unchanged(agy_bin, binary_identity):
            return _stage2_fail(
                reason_code="binary_identity_mismatch_after_primary_execution",
                caller_context=caller_context,
                binary_identity=binary_identity,
                stage1_status=stage1["status"],
            )
        primary_classification = preflight_agy.classify_runtime_verification_primary_execution(primary_execution)

        secondary_classification: "dict[str, str] | None" = None
        overall_verdict = primary_classification["verdict"]
        overall_reason = primary_classification["reason_code"]
        if primary_classification["verdict"] == "PASS":
            secondary_argv = [
                str(resolved),
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
            if not _stage2_binary_identity_unchanged(agy_bin, binary_identity):
                return _stage2_fail(
                    reason_code="binary_identity_mismatch_after_secondary_execution",
                    caller_context=caller_context,
                    binary_identity=binary_identity,
                    stage1_status=stage1["status"],
                    primary_classification=primary_classification,
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
        "route": "direct_cli",
        "caller_context": caller_context,
        "verdict": overall_verdict,
        "reason_code": overall_reason,
        "binary_identity": binary_identity,
        "stage1_status": stage1["status"],
    }
    print(json.dumps(result_payload, indent=2, sort_keys=True))
    return 0 if overall_verdict == "PASS" else 1


# ---------------------------------------------------------------------------
# Issue #2616 fix_delta P1-1: AC8/AC9 canonical delegation route (distinct
# from AC2's direct-CLI probe above). Routes through
# `run_gemini_headless.py::run_delegation()` -- the SAME code path a Claude
# Code / Claude-GPT SubAgent's own delegation calls use -- rather than
# invoking `agy` directly, so a PASS here actually proves the canonical
# route reaches a real `agy` child process and returns a normalized
# structured result.
# ---------------------------------------------------------------------------

_RUN_GEMINI_HEADLESS_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_gemini_headless.py"


def _load_run_gemini_headless_module() -> types.ModuleType:
    return _load_module(_RUN_GEMINI_HEADLESS_PATH, "run_gemini_headless_stage2_canonical_route")


def _classify_canonical_delegation_route_result(
    result: dict[str, Any], handoff_classification: str
) -> "dict[str, str]":
    """Classify a `run_delegation()` normalized result for Issue #2616
    AC8/AC9 (Issue #2616 fix_delta P1-1), gated by Issue #2670 AC5's closed
    sanitized handoff-selection classification.

    Never inspects raw stdout/stderr/response TEXT content -- only the
    normalized `ok`/`failure_class`/`response_text` (presence-only, never
    read) fields `run_delegation()` itself already computed from the actual
    `agy` child process invocation it performed. A genuine
    `failure_class == "agy_auth_required"` (production's own existing
    auth-required classification, `_classify_agy_failure()`) is the ONLY
    signal treated as `account_session_unavailable` SKIP -- every other
    non-`ok` result is FAIL, and this function never promotes a SKIP to
    PASS.

    Issue #2670 AC5: an otherwise-PASS-shaped result (`ok is True` with a
    non-empty `response_text`) is PASS-eligible ONLY when
    *handoff_classification* is exactly
    `agy_permission_policy.AGY_HANDOFF_VALIDATED_SELECTED` -- the other
    three closed classifications (`invalid_handoff_rejected` /
    `source_absent` / `no_handoff_ordinary_lookup`) are SKIP/incomplete even
    when the sentinel matches, never promoted to PASS.
    """
    if not isinstance(result, dict):
        return {"verdict": "FAIL", "reason_code": "canonical_delegation_route_result_malformed"}
    if result.get("ok") is True:
        response_text = result.get("response_text")
        if not isinstance(response_text, str) or not response_text.strip():
            return {
                "verdict": "FAIL",
                "reason_code": "canonical_delegation_route_ok_without_response_text",
            }
        if handoff_classification != agy_permission_policy.AGY_HANDOFF_VALIDATED_SELECTED:
            return {
                "verdict": "SKIP",
                "reason_code": f"handoff_classification_not_validated:{handoff_classification}",
            }
        return {"verdict": "PASS", "reason_code": "canonical_delegation_route_success"}
    failure_class = result.get("failure_class")
    if failure_class == "agy_auth_required":
        return {"verdict": "SKIP", "reason_code": "account_session_unavailable"}
    return {
        "verdict": "FAIL",
        "reason_code": f"canonical_delegation_route_failure:{failure_class}",
    }


def run_canonical_delegation_route_probe(caller_context: str) -> int:
    """AC8/AC9: route Issue #2616 Stage 2 model-backed verification through
    the canonical `run_gemini_headless.py::run_delegation()` delegation
    route (Issue #2616 fix_delta P1-1).

    Never fabricates a PASS. Because production dispatch redirects
    `HOME`/`XDG_*` into a fresh isolated workspace for every recognized
    `tool_profile` (Issue #1705's permission-isolation safety boundary,
    which this Issue's Allowed Paths and Stop Conditions do not authorize
    changing), a genuine existing noninteractive account session tied to
    the real `$HOME` is typically NOT reachable through this exact
    production path -- an honest `SKIP: account_session_unavailable` result
    is therefore an expected, not a failing, outcome; it is never promoted
    to PASS.
    """
    agy_bin = os.environ.get("AGY_BIN") or shutil.which("agy")
    if agy_bin is None:
        return _stage2_skip(reason_code="agy_cli_unavailable", caller_context=caller_context)

    resolved = Path(agy_bin).resolve()
    binary_identity = _compute_stage2_binary_identity(resolved)

    run_gemini_headless = _load_run_gemini_headless_module()
    request: dict[str, Any] = {
        "schema": "delegation_request_v1",
        "tool_profile": "no_tools",
        "provider": "agy",
        "prompt": preflight_agy.RUNTIME_VERIFICATION_SENTINEL_PROMPT,
        "objective": "Issue #2616 AC8/AC9 canonical delegation route runtime verification.",
        "instructions": ["Return exactly the requested sentinel text.", "Do not use any tools."],
        "output_sections": ["response"],
        "context_files": [],
        "timeout_sec": preflight_agy.RUNTIME_VERIFICATION_OUTER_DEADLINE_SECONDS,
    }
    result = run_gemini_headless.run_delegation(request)
    # Issue #2670 AC5: independently resolve the SAME closed sanitized
    # handoff-selection classification `materialize_isolated_agy_workspace()`
    # (invoked inside `run_delegation()` above, via its own separately
    # module-loaded `agy_permission_policy`) computed for this exact
    # invocation -- both are pure functions of the same
    # `AGY_OAUTH_TOKEN_HANDOFF_ROOT` / `_SOURCE` env vars and filesystem
    # state, so they always agree without requiring any change to
    # `run_gemini_headless.py` (Out of Scope; not an Allowed Path).
    handoff_result = agy_permission_policy.resolve_agy_oauth_token_source()
    classification = _classify_canonical_delegation_route_result(result, handoff_result.classification)
    exit_status = 77 if classification["verdict"] == "SKIP" else (0 if classification["verdict"] == "PASS" else 1)
    executed_command = [sys.executable] + sys.argv

    _write_stage2_runtime_verification_log(
        verdict=classification["verdict"],
        reason_code=classification["reason_code"],
        caller_context=caller_context,
        binary_identity=binary_identity,
        stage1_status=None,
        primary_classification=classification,
        secondary_classification=None,
    )
    # Issue #2670 AC5/AC6: the dedicated sanitized handoff-selection summary
    # artifact -- allowlist-only, never raw stdout/stderr/response/root/
    # path/credential/account/token/HOME/XDG content (see
    # `_write_ac5_handoff_summary_artifact()` docstring).
    _write_ac5_handoff_summary_artifact(
        caller_context=caller_context,
        executed_command=executed_command,
        binary_identity=binary_identity,
        verdict=classification["verdict"],
        reason_code=classification["reason_code"],
        exit_status=exit_status,
        canonical_result_classification=classification,
        handoff_classification=handoff_result.classification,
    )

    if classification["verdict"] == "SKIP":
        print(f"SKIP: {classification['reason_code']}")
        return 77

    ac_suffix = _CALLER_CONTEXT_AC_SUFFIX[caller_context]
    result_payload = {
        "schema": preflight_agy.RUNTIME_VERIFICATION_SCHEMA,
        "ac": f"AC2/{ac_suffix}",
        "route": "canonical_delegation",
        "caller_context": caller_context,
        "verdict": classification["verdict"],
        "reason_code": classification["reason_code"],
        "binary_identity": binary_identity,
        "handoff_classification": handoff_result.classification,
    }
    print(json.dumps(result_payload, indent=2, sort_keys=True))
    return 0 if classification["verdict"] == "PASS" else 1


# ---------------------------------------------------------------------------
# Hermetic regression tests (no real `agy` binary, no network/model cost).
# ---------------------------------------------------------------------------


def _write_fake_agy(tmp_path: Path, name: str, body: str) -> Path:
    """Write a strict-argv-guarded fake `agy` executable (mirrors
    `test_agy_real_subprocess.py`'s pattern) that only accepts the exact
    `-p <RUNTIME_VERIFICATION_SENTINEL_PROMPT>` argv `_build_agy_inner_argv()`
    produces for the `no_tools` tool_profile (no `--model`/`--output-format`
    flags)."""
    guard = (
        'test "$#" -eq 2 || exit 90\n'
        'test "$1" = "-p" || exit 91\n'
        f'test "$2" = "{preflight_agy.RUNTIME_VERIFICATION_SENTINEL_PROMPT}" || exit 92\n'
    )
    binary = tmp_path / name
    binary.write_text("#!/bin/sh\n" + guard + body, encoding="utf-8")
    binary.chmod(0o700)
    return binary


# --- Issue #2616 fix_delta P2-3: same-binary identity pin/recheck ----------


def test_stage2_binary_identity_unchanged_matches_when_binary_untouched(tmp_path: Path) -> None:
    binary = tmp_path / "agy-stable"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o700)

    pinned = _compute_stage2_binary_identity(binary.resolve())

    assert _stage2_binary_identity_unchanged(str(binary), pinned) is True


def test_stage2_binary_identity_unchanged_detects_symlink_retarget(tmp_path: Path) -> None:
    """Issue #2616 P2-3: swapping the symlink `agy_bin` points at (a
    different executable, same or different content) between the pin-time
    resolution and a recheck must be observable as a fail-closed mismatch,
    even though actual invocation argv always uses the FIRST resolution's
    canonical path (never a live re-resolution of the symlink)."""
    binary_a = tmp_path / "agy-binary-a"
    binary_a.write_text("#!/bin/sh\nprintf 'A'\nexit 0\n", encoding="utf-8")
    binary_a.chmod(0o700)
    binary_b = tmp_path / "agy-binary-b"
    binary_b.write_text("#!/bin/sh\nprintf 'B'\nexit 0\n", encoding="utf-8")
    binary_b.chmod(0o700)

    symlink_path = tmp_path / "agy"
    symlink_path.symlink_to(binary_a)

    pinned = _compute_stage2_binary_identity(symlink_path.resolve())
    assert pinned["realpath"] == str(binary_a.resolve())

    symlink_path.unlink()
    symlink_path.symlink_to(binary_b)

    assert _stage2_binary_identity_unchanged(str(symlink_path), pinned) is False


def test_stage2_binary_identity_unchanged_detects_same_path_content_overwrite(tmp_path: Path) -> None:
    """A content overwrite AT the pinned canonical path itself (not just a
    symlink retarget) must also be caught by the SHA-256/size comparison."""
    binary = tmp_path / "agy-in-place"
    binary.write_text("#!/bin/sh\nprintf 'ORIGINAL'\nexit 0\n", encoding="utf-8")
    binary.chmod(0o700)

    pinned = _compute_stage2_binary_identity(binary.resolve())

    binary.write_text("#!/bin/sh\nprintf 'TAMPERED-WITH-DIFFERENT-CONTENT'\nexit 0\n", encoding="utf-8")
    binary.chmod(0o700)

    assert _stage2_binary_identity_unchanged(str(binary), pinned) is False


def test_stage2_binary_identity_unchanged_missing_path_is_mismatch(tmp_path: Path) -> None:
    binary = tmp_path / "agy-vanishing"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o700)
    pinned = _compute_stage2_binary_identity(binary.resolve())

    binary.unlink()

    assert _stage2_binary_identity_unchanged(str(binary), pinned) is False


# --- Issue #2616 fix_delta P1-1: AC8/AC9 canonical delegation route --------


def test_classify_canonical_delegation_route_result_pass_requires_ok_and_response_text() -> None:
    verdict = _classify_canonical_delegation_route_result(
        {"ok": True, "failure_class": None, "response_text": "LOOP_AGY_STAGE2_RUNTIME_OK"},
        agy_permission_policy.AGY_HANDOFF_VALIDATED_SELECTED,
    )
    assert verdict == {"verdict": "PASS", "reason_code": "canonical_delegation_route_success"}


def test_classify_canonical_delegation_route_result_ok_without_response_text_is_fail() -> None:
    """`ok: True` with no actual response text is never trusted as PASS
    evidence (Issue #2616 fix_delta P1-1 -- classification is scoped to the
    normalized fields `run_delegation()` computed, but a structurally
    incoherent `ok: True` with nothing to show for it still fails closed)."""
    verdict = _classify_canonical_delegation_route_result(
        {"ok": True, "failure_class": None, "response_text": None},
        agy_permission_policy.AGY_HANDOFF_VALIDATED_SELECTED,
    )
    assert verdict["verdict"] == "FAIL"
    assert verdict["reason_code"] == "canonical_delegation_route_ok_without_response_text"


def test_classify_canonical_delegation_route_result_auth_required_is_skip() -> None:
    """Issue #2616 AC9 Notes for Reviewer: a genuine `agy_auth_required`
    result from the canonical route -- expected when production's own
    tool-profile isolation (Issue #1705) redirects HOME away from the real
    account session -- is an honest SKIP, never promoted to PASS."""
    verdict = _classify_canonical_delegation_route_result(
        {"ok": False, "failure_class": "agy_auth_required", "response_text": None},
        agy_permission_policy.AGY_HANDOFF_NO_HANDOFF_ORDINARY_LOOKUP,
    )
    assert verdict == {"verdict": "SKIP", "reason_code": "account_session_unavailable"}


def test_classify_canonical_delegation_route_result_other_failure_is_fail() -> None:
    verdict = _classify_canonical_delegation_route_result(
        {"ok": False, "failure_class": "agy_exit_nonzero", "response_text": None},
        agy_permission_policy.AGY_HANDOFF_NO_HANDOFF_ORDINARY_LOOKUP,
    )
    assert verdict == {
        "verdict": "FAIL",
        "reason_code": "canonical_delegation_route_failure:agy_exit_nonzero",
    }


# --- Issue #2670 AC5: PASS requires BOTH the ok/response_text shape AND the
#     closed handoff-selection classification to be exactly
#     `validated_handoff_selected` -- the other three classifications are
#     SKIP/incomplete even when the sentinel matches. ------------------------


@pytest.mark.parametrize(
    "handoff_classification",
    [
        pytest.param(
            "invalid_handoff_rejected",
            id="invalid_handoff_rejected",
        ),
        pytest.param("source_absent", id="source_absent"),
        pytest.param(
            "no_handoff_ordinary_lookup",
            id="no_handoff_ordinary_lookup",
        ),
    ],
)
def test_classify_canonical_delegation_route_result_skips_when_handoff_not_validated_even_with_matching_sentinel(
    handoff_classification: str,
) -> None:
    verdict = _classify_canonical_delegation_route_result(
        {"ok": True, "failure_class": None, "response_text": "LOOP_AGY_STAGE2_RUNTIME_OK"},
        handoff_classification,
    )
    assert verdict == {
        "verdict": "SKIP",
        "reason_code": f"handoff_classification_not_validated:{handoff_classification}",
    }


def test_run_canonical_delegation_route_probe_fails_when_canonical_route_binary_fails(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Issue #2616 fix_delta P1-1: AC8/AC9's verifier must actually depend
    on `run_gemini_headless.run_delegation()` succeeding -- a fake `AGY_BIN`
    that the canonical route invokes and which fails (nonzero exit) must
    make this probe FAIL, never PASS, proving the verifier is not
    independent of the real canonical-route invocation outcome."""
    fake_agy = _write_fake_agy(tmp_path, "agy-broken", "echo 'boom' >&2\nexit 23\n")
    monkeypatch.setenv("AGY_BIN", str(fake_agy))
    monkeypatch.chdir(tmp_path)

    exit_code = run_canonical_delegation_route_probe("claude-code")

    assert exit_code == 1


def test_run_canonical_delegation_route_probe_fails_when_canonical_route_returns_malformed_json(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A fake `AGY_BIN` that exits 0 but emits no usable stdout must also
    fail closed (never PASS) through the canonical route."""
    fake_agy = _write_fake_agy(tmp_path, "agy-empty", "exit 0\n")
    monkeypatch.setenv("AGY_BIN", str(fake_agy))
    monkeypatch.chdir(tmp_path)

    exit_code = run_canonical_delegation_route_probe("claude-code")

    assert exit_code == 1


def _set_validated_handoff_env(tmp_path: Path, monkeypatch: Any) -> None:
    """Issue #2670: set up a validated launcher handoff (dummy fixture --
    never a real credential) so `resolve_agy_oauth_token_source()` resolves
    to `AGY_HANDOFF_VALIDATED_SELECTED` for the duration of a test."""
    root = tmp_path / "handoff-root-stage2"
    root.mkdir(parents=True, exist_ok=True)
    source = root / "antigravity-oauth-token"
    source.write_text("dummy-fixture-token-value-stage2", encoding="utf-8")
    monkeypatch.setenv("AGY_OAUTH_TOKEN_HANDOFF_ROOT", str(root))
    monkeypatch.setenv("AGY_OAUTH_TOKEN_HANDOFF_SOURCE", str(source))


def test_run_canonical_delegation_route_probe_passes_when_canonical_route_succeeds(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The positive counterpart to the two FAIL cases above: when the
    canonical route's own `agy` child process genuinely succeeds AND a
    validated launcher handoff is present, the probe reaches PASS --
    demonstrating the verifier's PASS is actually contingent on
    `run_delegation()`'s real outcome (Issue #2616 fix_delta P1-1) AND the
    closed handoff-selection classification (Issue #2670 AC5), not
    independent of either."""
    fake_agy = _write_fake_agy(
        tmp_path,
        "agy-success",
        f"printf '%s\\n' '{preflight_agy.RUNTIME_VERIFICATION_SENTINEL_PROMPT}'\nexit 0\n",
    )
    monkeypatch.setenv("AGY_BIN", str(fake_agy))
    monkeypatch.chdir(tmp_path)
    _set_validated_handoff_env(tmp_path, monkeypatch)

    exit_code = run_canonical_delegation_route_probe("claude-code")

    assert exit_code == 0


def test_run_canonical_delegation_route_probe_skips_when_handoff_not_validated_even_with_matching_sentinel(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Issue #2670 AC5: the exact same successful `agy` child process
    (matching sentinel, exit 0) as the PASS test above, but WITHOUT a
    validated handoff (no dedicated handoff env vars set, and the ambient
    `HOME` ordinary-lookup fixture has no real source either) -- this must
    SKIP (`no_handoff_ordinary_lookup`), never PASS, proving the probe's
    PASS is genuinely contingent on the handoff classification and not just
    the sentinel match."""
    fake_agy = _write_fake_agy(
        tmp_path,
        "agy-success-no-handoff",
        f"printf '%s\\n' '{preflight_agy.RUNTIME_VERIFICATION_SENTINEL_PROMPT}'\nexit 0\n",
    )
    monkeypatch.setenv("AGY_BIN", str(fake_agy))
    monkeypatch.chdir(tmp_path)
    # Force the ordinary-lookup ambient HOME to a source-free fixture so this
    # test is deterministic regardless of the actual host's real $HOME state.
    ordinary_home = tmp_path / "ordinary-home-no-real-token"
    ordinary_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(ordinary_home))

    exit_code = run_canonical_delegation_route_probe("claude-code")

    assert exit_code == 77


# --- Issue #2670 AC6: the dedicated sanitized handoff-selection summary
#     artifact writer. ---------------------------------------------------


def test_write_ac5_handoff_summary_artifact_payload_is_allowlist_only(tmp_path: Path) -> None:
    # Writes under the real repo-root `artifacts/` dir (worktree-local,
    # untracked -- `.gitignore` excludes it), matching this file's
    # pre-existing evidence-writer test convention (e.g.
    # `test_structured_output_capability_runtime_probe_same_binary_evidence`).
    artifact_path = _write_ac5_handoff_summary_artifact(
        caller_context="claude-gpt",
        executed_command=[sys.executable, "-m", "pytest", "--stage2-model-backed", "--caller-context", "claude-gpt"],
        binary_identity={"realpath": str(tmp_path / "fake-agy"), "sha256": "deadbeef", "size": 123},
        verdict="PASS",
        reason_code="canonical_delegation_route_success",
        exit_status=0,
        canonical_result_classification={"verdict": "PASS", "reason_code": "canonical_delegation_route_success"},
        handoff_classification=agy_permission_policy.AGY_HANDOFF_VALIDATED_SELECTED,
    )
    assert artifact_path.exists()
    content = artifact_path.read_text(encoding="utf-8")
    output_marker = "content) ---\n"
    verdict_marker = "\n\n--- Verdict ---"
    start = content.index(output_marker) + len(output_marker)
    end = content.index(verdict_marker)
    payload = json.loads(content[start:end])
    assert set(payload) <= _AC5_HANDOFF_SUMMARY_ALLOWED_KEYS
    assert payload["handoff_classification"] == "validated_handoff_selected"
    assert payload["verdict"] == "PASS"
    assert payload["exit_status"] == 0
    assert payload["artifact_path"] == str(artifact_path.relative_to(_REPO_ROOT))


def test_write_ac5_handoff_summary_artifact_rejects_unknown_handoff_classification() -> None:
    """Fail-closed guard: an out-of-vocabulary handoff_classification value
    (a bug, never a real production value -- `resolve_agy_oauth_token_source()`
    only ever returns one of the closed four) must raise, not silently write
    a corrupted artifact."""
    with pytest.raises(AssertionError):
        _write_ac5_handoff_summary_artifact(
            caller_context="claude-gpt",
            executed_command=["fake"],
            binary_identity=None,
            verdict="PASS",
            reason_code="x",
            exit_status=0,
            canonical_result_classification=None,
            handoff_classification="not_a_real_classification",
        )


def test_write_ac5_handoff_summary_artifact_never_contains_forbidden_content() -> None:
    """Adversarial check: even if a caller accidentally passed a
    credential-shaped string through `reason_code` (the only free-text
    field this writer accepts besides `executed_command` literals), the
    writer itself never adds any NEW forbidden field -- there is no
    parameter for raw stdout/stderr/response/root/path/credential/account/
    token/HOME/XDG content at all, so this test proves-by-construction that
    the writer's fixed key set can never carry one under its own key name.
    """
    artifact_path = _write_ac5_handoff_summary_artifact(
        caller_context="claude-gpt",
        executed_command=[sys.executable, "-m", "pytest"],
        binary_identity=None,
        verdict="SKIP",
        reason_code="handoff_classification_not_validated:source_absent",
        exit_status=77,
        canonical_result_classification=None,
        handoff_classification=agy_permission_policy.AGY_HANDOFF_SOURCE_ABSENT,
    )
    content = artifact_path.read_text(encoding="utf-8")
    for forbidden_key in ("stdout", "stderr", "response_text", "root_path", "source_path", "credential", "HOME", "XDG_CONFIG_HOME"):
        assert f'"{forbidden_key}"' not in content


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
