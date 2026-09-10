"""Issue #2610 AC9: one-time, opt-in, read-only PRODUCER SMOKE canary for
the REAL canonical Step 2 execution surface (`root_review_pipeline.produce`)
against Issue #2584.

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
any assertion.

IMPORTANT SCOPE CORRECTION (PR #2622 review comment 5617732209, P0): this
test is a **producer-level smoke test** -- a direct Python `subprocess`
invocation of `run_root_review_pipeline.py produce`, run and synchronously
awaited exactly once by this pytest test process. It never crosses, and is
not a stand-in for, Claude Code's REAL `run_in_background: true` execution
surface + background-task-completion-notification join used by the actual
SKILL.md Step 2 orchestrator. A plain `subprocess.run()`/`Popen().wait()`
inside a pytest test function is a synchronous, in-process wait -- it does
NOT exercise Claude Code's asynchronous background-task boundary (separate
tool-call lifecycle, completion notification delivered on a later turn,
etc.). Passing this test is therefore NOT evidence that SKILL.md's
background+join contract (Issue #2610 AC6) holds end-to-end; it is only
evidence about the producer script itself (below). The actual end-to-end
evidence for the background+join contract must come from a separate,
one-time, read-only canary run directly from Claude Code's main thread via
the Bash tool with `run_in_background: true`, joined by the real background
task completion notification -- that live run is out of scope for this
test file and is performed by the root/main thread, not by any pytest
process.

What THIS test actually proves, once live-enabled (Issue #2610 AC9 (1)-(4)):

1. The process was not killed by a signal at all (in particular not at the
   stale, unenforced 90-second registry boundary) -- a negative
   `returncode` on POSIX means the child was terminated by a signal.
2. This test's own synchronous wait for the child process to exit means no
   assertion proceeds until the real completed JSON has actually been read
   from stdout -- i.e. the producer script itself does not hang or crash
   when run to completion. (This is a same-process synchronous wait, not
   Claude Code's background-task join -- see the scope correction above.)
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
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REFINEMENT_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_PRODUCE_SCRIPT = _REFINEMENT_SCRIPTS / "run_root_review_pipeline.py"

if str(_REFINEMENT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_REFINEMENT_SCRIPTS))
import run_root_review_pipeline as pipeline  # noqa: E402

_ENABLE_ENV_VAR = "LOOP_CANARY_STEP2_LIVE_ENABLE"
_CANARY_ISSUE_NUMBER = 2584
_CANARY_REPO = "squne121/loop-protocol"

# Known closed set `route_canonical_step2_result()` can return, imported
# directly from the production SSOT (`run_root_review_pipeline.py`) rather
# than re-declared as independent string literals here (PR #2622 review
# comment 5617732209, P1: avoids SSOT drift between this test and the
# production routing table). `STEP_4_5` is intentionally excluded: it is
# not a value this producer's `_cmd_produce()` entry point can emit (it is
# only reachable from a different call path), so including it here would
# widen the "known route" set beyond what this canary can actually observe.
_KNOWN_CANONICAL_STEP2_ROUTES = frozenset(
    {
        pipeline.STEP_2_5,
        pipeline.STEP_4,
        pipeline.STEP_5_OPERATOR_INTERVENTION_REQUIRED,
        pipeline.STEP_5_HUMAN_JUDGMENT_REQUIRED,
        pipeline.FAIL_CLOSED_ENVIRONMENT_OR_INTEGRITY_FAILURE,
    }
)

# Bounded, test-harness-only safety net (NOT a new production watchdog):
# generously above the real min-compatibility ReviewBudget total (520s) and
# the #2584-specific full-envelope total (~850s per Issue #2610 "Current
# Evidence"), so a genuinely completing run is never cut short by this
# pytest-level bound.
_SUBPROCESS_TIMEOUT_SECONDS = 1200

# Grace window for the SIGTERM->SIGKILL process-group cleanup ladder below.
_TERMINATE_GRACE_SECONDS = 3


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
        "AC: AC9 (Issue #2610) - canonical Step 2 execution surface producer smoke canary",
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


def _terminate_process_group_and_reap(process: subprocess.Popen) -> None:
    """Local, test-only mirror of `reviewer_transport.run_reviewer_transport()`'s
    SIGTERM -> bounded grace -> SIGKILL process-group cleanup (PR #2622
    review comment 5617732209, P1). `subprocess.run()`/`Popen.wait()`'s own
    `TimeoutExpired` handling only ever targets the single direct child
    process, not its process group -- a producer that has spawned
    descendants (e.g. a `gh`/reviewer subprocess) could otherwise be left
    running past this outer safety-net timeout. This is a scoped-down,
    local helper closed over this test file only -- it is not a new shared
    production control-plane or watchdog module.
    """
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def test_ac9_producer_smoke_canary_against_issue_2584() -> None:
    if os.environ.get(_ENABLE_ENV_VAR) != "1":
        print(
            f"SKIP: {_ENABLE_ENV_VAR}=1 not set. AC9 is an explicit opt-in, "
            "one-time read-only producer smoke canary against Issue #2584 -- "
            "it is never run automatically (Out of Scope: standing CI "
            "live-mutation gate)."
        )
        _skip_or_exit(f"SKIP: {_ENABLE_ENV_VAR} not enabled", 77)

    gh = shutil.which("gh")
    if gh is None:
        print("SKIP: gh CLI unavailable in PATH; cannot fetch the live Issue #2584 body")
        _skip_or_exit("SKIP: producer smoke canary unavailable (gh not found)", 77)

    try:
        auth = subprocess.run([gh, "auth", "status"], capture_output=True, text=True, timeout=15)
    except OSError as exc:
        print(f"SKIP: gh auth status could not be executed ({exc})")
        _skip_or_exit("SKIP: producer smoke canary unavailable (gh auth exec failed)", 77)
    if auth.returncode != 0:
        print("SKIP: gh is not authenticated in this runtime; cannot read Issue #2584 live")
        _skip_or_exit("SKIP: producer smoke canary unavailable (gh auth unavailable)", 77)

    assert _PRODUCE_SCRIPT.is_file(), f"canonical Step 2 producer script missing: {_PRODUCE_SCRIPT}"

    started = time.monotonic()
    process = subprocess.Popen(
        [
            sys.executable,
            str(_PRODUCE_SCRIPT),
            "produce",
            "--issue-number",
            str(_CANARY_ISSUE_NUMBER),
            "--repo",
            _CANARY_REPO,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        process.wait(timeout=_SUBPROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # Outer test-harness safety net exceeded: clean up the whole
        # process group (not just the direct child) before failing, per
        # PR #2622 review comment 5617732209 P1.
        _terminate_process_group_and_reap(process)
        elapsed_seconds = time.monotonic() - started
        try:
            stdout_text, stderr_text = process.communicate(timeout=1)
        except Exception:  # noqa: BLE001 - best-effort drain after forced termination
            stdout_text, stderr_text = "", ""
        _write_evidence_log(
            verdict="FAIL",
            exit_code=process.returncode if process.returncode is not None else -1,
            reason=(
                f"process exceeded the {_SUBPROCESS_TIMEOUT_SECONDS}s outer "
                f"test-harness safety-net timeout after {elapsed_seconds:.1f}s "
                "and was terminated (SIGTERM/SIGKILL) via process-group cleanup"
            ),
            extra={
                "elapsed_seconds": elapsed_seconds,
                "returncode": process.returncode,
                "stderr_tail": (stderr_text or "")[-2000:],
            },
        )
        pytest.fail(
            f"root_review_pipeline.produce exceeded the {_SUBPROCESS_TIMEOUT_SECONDS}s "
            f"outer test-harness safety-net timeout after {elapsed_seconds:.1f}s -- "
            "process group was terminated and reaped by this test-local cleanup"
        )

    stdout_text, stderr_text = process.communicate()
    returncode = process.returncode
    elapsed_seconds = time.monotonic() - started

    # (1) never killed by a signal -- in particular not at the stale,
    # unenforced 90-second registry boundary. A negative POSIX returncode
    # means the child was terminated by signal `-returncode`. NOTE: this
    # detection relies on `subprocess`/`Popen` invoking the Python
    # interpreter DIRECTLY (not via a Bash exec layer); on POSIX, a
    # negative `returncode` is how Python surfaces signal termination for
    # a direct child. If this invocation is ever changed to go through a
    # Bash `exec` wrapper, signal termination instead manifests as a
    # positive `128+N` exit code (e.g. SIGTERM -> 143), and this
    # `< 0` check would silently stop detecting it -- do not reuse this
    # exact check unmodified in that scenario.
    killed_by_signal = returncode < 0
    crossed_old_90s_boundary = elapsed_seconds > 90
    # Evidence-only clarification (PR #2622 review comment 5617732209, P2):
    # `crossed_old_90s_boundary` is never asserted on -- a run that
    # completes in well under 90s is still a PASS. This string makes that
    # explicit in the evidence log so a human reviewing a sub-90s PASS
    # does not mistake it for having verified the old 90s boundary.
    boundary_claim = (
        "90s boundary crossed"
        if crossed_old_90s_boundary
        else "completed within 90s; old 90s boundary claim not applicable"
    )

    if killed_by_signal:
        _write_evidence_log(
            verdict="FAIL",
            exit_code=returncode,
            reason=(
                f"process was terminated by signal {-returncode} after "
                f"{elapsed_seconds:.1f}s (crossed_old_90s_boundary="
                f"{crossed_old_90s_boundary}) -- premature termination"
            ),
            extra={
                "elapsed_seconds": elapsed_seconds,
                "returncode": returncode,
                "crossed_old_90s_boundary": crossed_old_90s_boundary,
                "boundary_claim": boundary_claim,
                "stderr_tail": stderr_text[-2000:],
            },
        )
        pytest.fail(
            f"root_review_pipeline.produce was killed by signal {-returncode} "
            f"after {elapsed_seconds:.1f}s -- this is exactly the premature-termination "
            "regression Issue #2610 AC9 exists to catch"
        )

    # (2) this assertion point is only reached after the child process has
    # actually exited, i.e. after the real completed JSON was actually
    # available on stdout -- not merely at launch time. (This is this
    # test process's own synchronous wait, not Claude Code's background
    # task join -- see module docstring's scope correction.)
    try:
        payload = json.loads(stdout_text)
    except json.JSONDecodeError:
        _write_evidence_log(
            verdict="FAIL",
            exit_code=returncode,
            reason="produce did not emit valid JSON on stdout",
            extra={
                "elapsed_seconds": elapsed_seconds,
                "returncode": returncode,
                "stdout_tail": stdout_text[-2000:],
                "stderr_tail": stderr_text[-2000:],
            },
        )
        pytest.fail(f"produce stdout was not valid JSON: {stdout_text[-500:]!r}")

    route = payload.get("canonical_step2_route")
    status = payload.get("status")

    # (3) a genuine typed route from the closed set -- never fabricated.
    route_is_known = route in _KNOWN_CANONICAL_STEP2_ROUTES

    # (4) on any non-ok status, the route MUST be the fail-closed route --
    # never an approve / step_2_5 / implementation-authorization route.
    fail_closed_contract_upheld = True
    if status != "ok":
        fail_closed_contract_upheld = route == pipeline.FAIL_CLOSED_ENVIRONMENT_OR_INTEGRITY_FAILURE

    verdict = "PASS" if (route_is_known and fail_closed_contract_upheld) else "FAIL"
    evidence = {
        "issue_number": _CANARY_ISSUE_NUMBER,
        "repo": _CANARY_REPO,
        "elapsed_seconds": elapsed_seconds,
        "crossed_old_90s_boundary": crossed_old_90s_boundary,
        "boundary_claim": boundary_claim,
        "returncode": returncode,
        "status": status,
        "canonical_step2_route": route,
        "route_is_known": route_is_known,
        "fail_closed_contract_upheld": fail_closed_contract_upheld,
    }
    _write_evidence_log(
        verdict=verdict,
        exit_code=returncode,
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
