"""Issue #2610 AC9: one-time, opt-in, read-only runtime canary against the
REAL canonical Step 2 execution surface (`root_review_pipeline.produce`)
for Issue #2584.

This is explicitly NOT a standing CI live-mutation gate (Out of Scope /
Runtime Verification Applicability skip_conditions). It never runs unless
the caller EXPLICITLY sets `LOOP_CANARY_STEP2_LIVE_ENABLE=1` -- absent
that, this test SKIPs via `pytest.exit(..., returncode=77)` per
`docs/dev/runtime-verification-policy.md`'s SKIP convention (exit code 77,
`SKIP:` stdout prefix; SKIP != PASS). When disabled (the default for any
routine `pytest` run of this file, including CI), collecting/running it is
a no-op SKIP, never a live GitHub call.

`root_review_pipeline.produce` is declared `mutation: False` in
`command_registry.py` -- it fetches the live Issue body (read-only), runs
local checkers, and persists local (gitignored) artifacts under
`.claude/artifacts/issue-refinement-loop/`; it never mutates Issue/PR state
on GitHub. This canary invokes it exactly once against Issue #2584 (the
Issue whose prior exit-143 incident triggered this Issue -- see #2610
"Related / Prior Art"), synchronously waiting for it to exit before making
any assertion. That synchronous wait IS the "join" half of the SKILL.md
Step 2 background+join contract (Issue #2610 AC6): this test process plays
the role of the orchestrator joining a background task by not returning
control until the real completed JSON result has been read from stdout.

Checks performed once live-enabled (Issue #2610 AC9 (1)-(4)):

1. The process was not killed by a signal at all (in particular not at the
   stale, unenforced 90-second registry boundary) -- a negative
   `returncode` on POSIX means the child was terminated by a signal.
2. This test's own synchronous `subprocess.run()` wait IS the join; no
   assertion proceeds until the real completed JSON has actually been read.
3. The emitted top-level `canonical_step2_route` is one of the closed set
   of real values `route_canonical_step2_result()` can return -- i.e. a
   genuine typed route (either an in-loop route or a genuine fail-closed
   classification), never a value this test invented.
4. On any non-`ok` `status`, the route MUST be exactly
   `fail_closed_environment_or_integrity_failure` -- this test asserts the
   ABSENCE of any fabricated `step_2_5` / approve / implementation
   authorization on the timeout/failure path; it never manufactures those
   itself.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REFINEMENT_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_PRODUCE_SCRIPT = _REFINEMENT_SCRIPTS / "run_root_review_pipeline.py"

_ENABLE_ENV_VAR = "LOOP_CANARY_STEP2_LIVE_ENABLE"
_CANARY_ISSUE_NUMBER = 2584
_CANARY_REPO = "squne121/loop-protocol"

# Known closed set `route_canonical_step2_result()` can return (Issue #2389
# / #2397 routing table, mirrored from SKILL.md's canonical Step 2 routing
# table -- not independently invented here).
_KNOWN_CANONICAL_STEP2_ROUTES = frozenset(
    {
        "step_2_5",
        "step_4",
        "step_5_operator_intervention_required",
        "step_5_human_judgment_required",
        "fail_closed_environment_or_integrity_failure",
    }
)

# Bounded, test-harness-only safety net (NOT a new production watchdog):
# generously above the real min-compatibility ReviewBudget total (520s) and
# the #2584-specific full-envelope total (~850s per Issue #2610 "Current
# Evidence"), so a genuinely completing run is never cut short by this
# pytest-level bound.
_SUBPROCESS_TIMEOUT_SECONDS = 1200


def _skip_or_exit(message: str, returncode: int) -> None:
    """SKIP this test without crashing an xdist worker.

    ``pytest.exit()`` terminates the whole test *session* (fine for the
    Issue #2610 AC9 standalone invocation, where it yields the documented
    exit code 77 -- docs/dev/runtime-verification-policy.md's SKIP
    convention), but inside a pytest-xdist worker process it is fatal:
    the controller detects the worker's session-abort as a crashed item
    (``INTERNALERROR> AssertionError`` in ``xdist/dsession.py``), failing
    the entire parallel run this file is now collected into. Detect the
    xdist worker via ``PYTEST_XDIST_WORKER`` (set by pytest-xdist in each
    worker process, unset otherwise) and use a normal ``pytest.skip()``
    there instead -- functionally equivalent for this always-opt-in test,
    and safe under xdist.
    """
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.skip(message)
    else:
        pytest.exit(message, returncode=returncode)


def _write_evidence_log(*, verdict: str, exit_code: int, reason: str, extra: dict) -> Path:
    artifact_dir = Path(os.environ.get("RUNTIME_VERIFICATION_ARTIFACT_DIR", "artifacts"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = artifact_dir / f"runtime-verification-AC9-{timestamp}.log"
    lines = [
        "=== Runtime Verification Log ===",
        "AC: AC9 (Issue #2610) - canonical Step 2 execution surface one-time read-only canary",
        f"Timestamp: {timestamp}",
        f"Environment: python={sys.version.split()[0]} platform={sys.platform}",
        "",
        "--- Input ---",
        f"command: run_root_review_pipeline.py produce --issue-number {_CANARY_ISSUE_NUMBER} --repo {_CANARY_REPO}",
        "",
        "--- Output ---",
        json.dumps(extra, ensure_ascii=False, indent=2),
        "",
        "--- Verdict ---",
        f"Result: {verdict}",
        f"Exit Code: {exit_code}",
        f"Reason: {reason}",
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


def test_ac9_step2_execution_surface_canary_against_issue_2584() -> None:
    if os.environ.get(_ENABLE_ENV_VAR) != "1":
        print(
            f"SKIP: {_ENABLE_ENV_VAR}=1 not set. AC9 is an explicit opt-in, "
            "one-time read-only canary against Issue #2584 -- it is never "
            "run automatically (Out of Scope: standing CI live-mutation gate)."
        )
        _skip_or_exit(f"SKIP: {_ENABLE_ENV_VAR} not enabled", 77)

    gh = shutil.which("gh")
    if gh is None:
        print("SKIP: gh CLI unavailable in PATH; cannot fetch the live Issue #2584 body")
        _skip_or_exit("SKIP: step2_execution_surface_canary unavailable (gh not found)", 77)

    try:
        auth = subprocess.run([gh, "auth", "status"], capture_output=True, text=True, timeout=15)
    except OSError as exc:
        print(f"SKIP: gh auth status could not be executed ({exc})")
        _skip_or_exit("SKIP: step2_execution_surface_canary unavailable (gh auth exec failed)", 77)
    if auth.returncode != 0:
        print("SKIP: gh is not authenticated in this runtime; cannot read Issue #2584 live")
        _skip_or_exit("SKIP: step2_execution_surface_canary unavailable (gh auth unavailable)", 77)

    assert _PRODUCE_SCRIPT.is_file(), f"canonical Step 2 producer script missing: {_PRODUCE_SCRIPT}"

    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            str(_PRODUCE_SCRIPT),
            "produce",
            "--issue-number",
            str(_CANARY_ISSUE_NUMBER),
            "--repo",
            _CANARY_REPO,
        ],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
    )
    elapsed_seconds = time.monotonic() - started

    # (1) never killed by a signal -- in particular not at the stale,
    # unenforced 90-second registry boundary. A negative POSIX returncode
    # means the child was terminated by signal `-returncode`.
    killed_by_signal = result.returncode < 0
    crossed_old_90s_boundary = elapsed_seconds > 90

    if killed_by_signal:
        _write_evidence_log(
            verdict="FAIL",
            exit_code=result.returncode,
            reason=(
                f"process was terminated by signal {-result.returncode} after "
                f"{elapsed_seconds:.1f}s (crossed_old_90s_boundary="
                f"{crossed_old_90s_boundary}) -- premature termination"
            ),
            extra={
                "elapsed_seconds": elapsed_seconds,
                "returncode": result.returncode,
                "crossed_old_90s_boundary": crossed_old_90s_boundary,
                "stderr_tail": result.stderr[-2000:],
            },
        )
        pytest.fail(
            f"root_review_pipeline.produce was killed by signal {-result.returncode} "
            f"after {elapsed_seconds:.1f}s -- this is exactly the premature-termination "
            "regression Issue #2610 AC9 exists to catch"
        )

    # (2) the join: this assertion point is only reached after the
    # synchronous subprocess.run() above returned, i.e. after the real
    # completed JSON was actually available -- not merely at launch time.
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        _write_evidence_log(
            verdict="FAIL",
            exit_code=result.returncode,
            reason="produce did not emit valid JSON on stdout",
            extra={
                "elapsed_seconds": elapsed_seconds,
                "returncode": result.returncode,
                "stdout_tail": result.stdout[-2000:],
                "stderr_tail": result.stderr[-2000:],
            },
        )
        pytest.fail(f"produce stdout was not valid JSON: {result.stdout[-500:]!r}")

    route = payload.get("canonical_step2_route")
    status = payload.get("status")

    # (3) a genuine typed route from the closed set -- never fabricated.
    route_is_known = route in _KNOWN_CANONICAL_STEP2_ROUTES

    # (4) on any non-ok status, the route MUST be the fail-closed route --
    # never an approve / step_2_5 / implementation-authorization route.
    fail_closed_contract_upheld = True
    if status != "ok":
        fail_closed_contract_upheld = route == "fail_closed_environment_or_integrity_failure"

    verdict = "PASS" if (route_is_known and fail_closed_contract_upheld) else "FAIL"
    evidence = {
        "issue_number": _CANARY_ISSUE_NUMBER,
        "repo": _CANARY_REPO,
        "elapsed_seconds": elapsed_seconds,
        "crossed_old_90s_boundary": crossed_old_90s_boundary,
        "returncode": result.returncode,
        "status": status,
        "canonical_step2_route": route,
        "route_is_known": route_is_known,
        "fail_closed_contract_upheld": fail_closed_contract_upheld,
    }
    _write_evidence_log(
        verdict=verdict,
        exit_code=result.returncode,
        reason=(
            "genuine canonical_step2_route obtained without premature termination "
            "or fabricated authorization"
            if verdict == "PASS"
            else f"route={route!r} status={status!r} did not satisfy AC9 (3)/(4)"
        ),
        extra=evidence,
    )

    assert route_is_known, f"unrecognized canonical_step2_route: {route!r} (full payload keys: {list(payload)})"
    assert fail_closed_contract_upheld, (
        f"status={status!r} was not 'ok' but canonical_step2_route={route!r} != "
        "'fail_closed_environment_or_integrity_failure' -- possible fabricated route/authorization"
    )
