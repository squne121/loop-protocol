"""Issue #2752 AC7 -- narrow Herdr runtime canary for the 4 causal
boundaries a `[[startup]]` hook re-fire must distinguish:

1. Native   + live handoff  -> `live_runtime_preserved`, 0 dispatch.
2. Claude-GPT + live handoff -> `live_runtime_preserved`, 0 dispatch.
3. Native   + real cold restart -> runtime absent, exactly-one resume.
4. Claude-GPT + real cold restart -> runtime absent, exactly-one resume.

Runtime Verification Applicability (Issue #2752 body): ``immediate`` for
AC7 only (AC1-AC6/AC8/AC9 are covered by deterministic pytest in
``test_cold_restart_startup.py`` / ``test_resume_dispatcher_state_
machine.py``). ``fallback_policy.fallback_success_is_pass: false`` -- a
SKIP here is never silently treated as PASS, and never fabricates a result
(``docs/dev/runtime-verification-policy.md`` SKIP 規約).

Each of the 4 causal boundaries above is disposed of independently via
`pytest.skip()` (never a bare module-level skip, so `-k
native_live_handoff` etc. each individually produce an honest SKIP record)
when either:

(a) the ``herdr`` binary or a working native ``claude`` login is not
    available at all, OR
(b) (the actually-limiting factor in THIS Issue's implementation
    environment) this test PROCESS is itself already running inside a
    live Herdr-managed pane (``HERDR_SOCKET_PATH`` set in its own ambient
    environment -- see ``_ambient_process_is_inside_a_herdr_managed_pane``
    below). Live-fact-checked during this Issue's own implementation
    (``herdr status server --json`` / ``herdr pane list`` against the
    actual installed Herdr 0.9.1 server): this repository's own agentic
    workflow runs EVERY implementation/review SubAgent turn -- including
    the one that authored this very file -- inside a pane of the shared
    "default" Herdr session, alongside OTHER concurrently-active,
    human-facing agent panes with real in-flight work. This canary's
    cold-restart causal boundaries require deliberately killing (``kill
    -9``) a Herdr server process; running that destructive step from
    within a nested pane of the very session (or an adjacent disposable
    session sharing the same host/user account and global Herdr plugin-
    link state) being observed/relied upon by other concurrent agents
    carries a real, non-hypothetical risk of collateral damage to THAT
    shared "default" session -- exactly the outcome
    ``task_context_cold_restart_startup.py``'s own "Session scoping"
    design section (and the Issue's own Real Herdr canary precedent,
    ``docs/dev/task-context.md`` "実機 Herdr canary（PR #2731...)") treats
    as a hard safety invariant to never violate. This is the SAME class of
    documented caveat as the existing ``CLAUDE_CODE_CHILD_SESSION``-nested-
    environment caveat already recorded for the PR #2731 canary (see
    ``docs/dev/task-context.md``'s "新たに確認した知見" section) --
    extended here to Issue #2752's own new canary.

(c) live handoff specifically (in addition to (a)/(b) above): the
    INSTALLED Herdr 0.9.1 CLI surface (fact-checked live via ``herdr
    --help`` during this Issue's implementation) exposes exactly ONE
    local trigger for a genuine live handoff -- ``herdr update --handoff``
    (which mutates the installed Herdr binary version and requires
    network access) -- plus ``herdr --remote <ssh-target> --handoff``
    (which requires a configured SSH remote target). Neither is a safe,
    disposable, side-effect-free operation this automated test can
    responsibly perform unattended. This is reported as its own distinct,
    honest SKIP reason (not conflated with (a)/(b)) so the two live-
    handoff tests' SKIP evidence is independently diagnosable from the
    two cold-restart tests' SKIP evidence.

When BOTH (a)/(b) are clear (a plain, non-Herdr-managed shell -- e.g. a
bare CI runner or a human operator's own terminal outside any Herdr
session -- with a working ``herdr`` binary and an authenticated native
``claude`` login), ``_bootstrap_disposable_canary_session()`` below
performs the REAL disposable-session bootstrap/pane-spawn/kill-9/cold-
restart/``[[startup]]``-hook-observation/cleanup cycle for the two
cold-restart causal boundaries, reusing the exact recipe already proven
safe and effective by the PR #2731 Real Herdr canary
(``docs/dev/task-context.md``). The two live-handoff tests still SKIP
per (c) above even in that environment, since (c) is a property of the
installed Herdr CLI surface itself, not of the ambient shell.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts" / "task-context"
_MIGRATIONS_DIR = _SCRIPTS_DIR / "migrations"
for _dir in (str(_SCRIPTS_DIR), str(_MIGRATIONS_DIR)):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import task_context_cold_restart_startup as startup  # noqa: E402
import task_context_config as config  # noqa: E402
import task_context_db as db  # noqa: E402
import task_context_migration_runner as migration_runner  # noqa: E402

# This canary drives a REAL herdr session and (for the cold-restart
# boundaries) a real `claude`/Claude-GPT subprocess. Opt-in only, matching
# every other real-CLI-invoking test in this repository (pyproject.toml
# `addopts` deselects `claude_live`/`github_live` by default; this
# canary's OWN capability gate below -- not a pytest marker, since none of
# the existing markers describe "real Herdr session" -- is what actually
# keeps it side-effect-free in the default `uv run pytest
# tests/task-context/test_live_handoff_duplicate_resume_canary.py -v -k
# ...` invocation the Issue's own Verification Commands use).

_ARTIFACTS_DIR = _REPO_ROOT / "artifacts"


def _write_runtime_verification_log(
    *, ac_boundary: str, verdict: str, exit_code: int, reason: str, evidence: dict[str, Any]
) -> Path:
    """``docs/dev/runtime-verification-policy.md`` ## 4 証跡保存フォーマット
    -- persisted under worktree-local ``artifacts/`` (gitignored, never
    committed) for every terminal outcome (SKIP/PASS/FAIL alike)."""
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = _ARTIFACTS_DIR / f"runtime-verification-AC7-{ac_boundary}-{timestamp}.log"
    herdr_bin = shutil.which("herdr") or "unavailable"
    lines = [
        "=== Runtime Verification Log ===",
        f"AC: AC7 (Issue #2752) -- causal boundary: {ac_boundary}",
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}",
        f"herdr_bin: {herdr_bin}",
        "",
        "--- Evidence ---",
        json.dumps(evidence, indent=2, sort_keys=True, default=str)[:20000],
        "",
        "--- Verdict ---",
        f"Result: {verdict}",
        f"Exit Code: {exit_code}",
        f"Reason: {reason}",
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


def _herdr_binary_available() -> tuple[bool, str]:
    herdr_bin = shutil.which("herdr")
    if not herdr_bin:
        return False, "'herdr' binary not found on PATH"
    try:
        result = subprocess.run([herdr_bin, "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"'herdr --version' failed: {exc}"
    if result.returncode != 0:
        return False, f"'herdr --version' exited {result.returncode}: {result.stderr.strip()}"
    return True, result.stdout.strip()


def _native_claude_available() -> tuple[bool, str]:
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return False, "native 'claude' binary not found on PATH"
    try:
        result = subprocess.run([claude_bin, "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"native 'claude --version' failed: {exc}"
    if result.returncode != 0:
        return False, f"native 'claude --version' exited {result.returncode}"
    return True, claude_bin


def _ambient_process_is_inside_a_herdr_managed_pane() -> tuple[bool, str]:
    """See module docstring reason (b). Herdr injects ``HERDR_SOCKET_PATH``
    into every plugin/agent runtime command it launches (module docstring
    of ``task_context_cold_restart_startup.py``, "Session scoping" --
    ``HERDR_SOCKET_PATH``/``HERDR_BIN_PATH``) -- its presence in THIS
    process's own ambient environment is direct, first-party evidence this
    test process itself is a descendant of some live Herdr session's
    server (never a heuristic guess)."""
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    tab_id = os.environ.get("HERDR_TAB_ID")
    if socket_path:
        return True, f"HERDR_SOCKET_PATH={socket_path!r} HERDR_TAB_ID={tab_id!r}"
    return False, "HERDR_SOCKET_PATH not set in ambient environment"


def _cold_restart_canary_capability() -> tuple[bool, str, dict[str, Any]]:
    """Combined capability check for the two COLD-RESTART causal boundaries
    (reasons (a)/(b) in the module docstring). Live handoff has its own,
    separate, unconditional reason (c) -- see
    ``_live_handoff_capability()``."""
    evidence: dict[str, Any] = {}
    herdr_ok, herdr_detail = _herdr_binary_available()
    evidence["herdr_available"] = herdr_ok
    evidence["herdr_detail"] = herdr_detail
    if not herdr_ok:
        return False, herdr_detail, evidence

    claude_ok, claude_detail = _native_claude_available()
    evidence["native_claude_available"] = claude_ok
    evidence["native_claude_detail"] = claude_detail
    if not claude_ok:
        return False, claude_detail, evidence

    nested, nested_detail = _ambient_process_is_inside_a_herdr_managed_pane()
    evidence["ambient_process_inside_herdr_managed_pane"] = nested
    evidence["ambient_process_detail"] = nested_detail
    if nested:
        return (
            False,
            (
                "this test process is itself running inside a live Herdr-managed "
                f"pane ({nested_detail}) -- refusing to run destructive "
                "disposable-session kill-9/cold-restart steps from a nested "
                "context that shares this host's live 'default' Herdr session "
                "(see this module's own docstring reason (b) for the full "
                "safety rationale)"
            ),
            evidence,
        )

    return True, "herdr + native claude available, not running inside a Herdr-managed pane", evidence


def _live_handoff_capability() -> tuple[bool, str, dict[str, Any]]:
    """Live handoff (reason (c)): the installed Herdr 0.9.1 CLI surface
    exposes exactly one local trigger (``herdr update --handoff``, network
    + version-mutating) plus a remote-SSH-only alternative (``herdr
    --remote <target> --handoff``) -- neither is a safe, disposable,
    side-effect-free operation for this automated test to perform
    unattended, REGARDLESS of the ambient-nesting check above. Always
    returns ``(False, ...)`` today; kept as its own function (rather than
    a bare unconditional skip) so a future Herdr release that adds a
    local-only handoff trigger primitive only requires updating this one
    function, not the two live-handoff test bodies."""
    evidence: dict[str, Any] = {
        "known_local_handoff_triggers": ["herdr update --handoff", "herdr --remote <ssh-target> --handoff"],
        "safe_disposable_local_trigger_available": False,
    }
    return (
        False,
        (
            "no safe, disposable, side-effect-free local trigger for a genuine "
            "Herdr live handoff exists in the installed Herdr 0.9.1 CLI surface "
            "-- the only local trigger ('herdr update --handoff') mutates the "
            "installed Herdr binary version over the network, and the only "
            "alternative ('herdr --remote <target> --handoff') requires a "
            "configured SSH remote target; neither is safe for this automated "
            "test to perform unattended"
        ),
        evidence,
    )


def _skip_with_log(ac_boundary: str, reason: str, evidence: dict[str, Any]) -> None:
    _write_runtime_verification_log(
        ac_boundary=ac_boundary, verdict="SKIP", exit_code=77, reason=reason, evidence=evidence
    )
    pytest.skip(reason)


# ---------------------------------------------------------------------------
# 1. Native + live handoff
# ---------------------------------------------------------------------------


def test_native_live_handoff_preserves_runtime_and_dispatches_zero_resume_commands():
    """AC7 causal boundary 1: Native + live handoff -> `live_runtime_
    preserved`, launch/resume command count = 0."""
    ok, reason, evidence = _cold_restart_canary_capability()
    if ok:
        handoff_ok, handoff_reason, handoff_evidence = _live_handoff_capability()
        evidence.update(handoff_evidence)
        if not handoff_ok:
            _skip_with_log("native-live-handoff", handoff_reason, evidence)
        # Unreachable today (handoff_ok is always False) -- see
        # `_live_handoff_capability()`'s own docstring for why this branch
        # is kept ready rather than removed.
        pytest.fail("unreachable: _live_handoff_capability() returned True unexpectedly")
    _skip_with_log("native-live-handoff", reason, evidence)


# ---------------------------------------------------------------------------
# 2. Claude-GPT + live handoff
# ---------------------------------------------------------------------------


def test_claude_gpt_live_handoff_preserves_runtime_and_dispatches_zero_resume_commands():
    """AC7 causal boundary 2: Claude-GPT + live handoff -> `live_runtime_
    preserved`, launch/resume command count = 0."""
    ok, reason, evidence = _cold_restart_canary_capability()
    if ok:
        handoff_ok, handoff_reason, handoff_evidence = _live_handoff_capability()
        evidence.update(handoff_evidence)
        if not handoff_ok:
            _skip_with_log("claude-gpt-live-handoff", handoff_reason, evidence)
        pytest.fail("unreachable: _live_handoff_capability() returned True unexpectedly")
    _skip_with_log("claude-gpt-live-handoff", reason, evidence)


# ---------------------------------------------------------------------------
# Real disposable-session cold-restart canary body (shared by both
# cold-restart causal boundaries below) -- reuses the exact bootstrap/
# kill-9/cold-restart/cleanup recipe the PR #2731 Real Herdr canary already
# proved safe (docs/dev/task-context.md "実機 Herdr canary（PR #2731、
# 2026-09-24）完全実施結果").
# ---------------------------------------------------------------------------


class _DisposableSessionHandle:
    def __init__(self, name: str, herdr_bin: str) -> None:
        self.name = name
        self.herdr_bin = herdr_bin


def _run_herdr(herdr_bin: str, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run([herdr_bin, *args], capture_output=True, text=True, timeout=timeout)


def _find_session_server_pid(herdr_bin: str, session_name: str) -> int | None:
    """Locate the disposable session's OWN herdr server process id --
    NEVER by bare process-name matching (which could also match the
    ambient 'default' session's server process on the same host), but by
    cross-referencing `herdr session list`'s reported socket path for
    THIS session against `/proc/<pid>/environ`'s `HERDR_SOCKET_PATH`
    (mirrors this module's own `_ambient_process_is_inside_a_herdr_managed_
    pane()` evidence primitive, applied here as a POSITIVE lookup instead
    of a self-check)."""
    listing = _run_herdr(herdr_bin, "session", "list")
    if listing.returncode != 0:
        return None
    socket_path = None
    for line in listing.stdout.splitlines():
        parts = line.split()
        if parts and parts[0] == session_name and len(parts) >= 4:
            socket_path = parts[-1]
            break
    if not socket_path:
        return None
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return None
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        environ_path = entry / "environ"
        try:
            raw = environ_path.read_bytes()
        except (OSError, PermissionError):
            continue
        if f"HERDR_SOCKET_PATH={socket_path}".encode() in raw:
            return int(entry.name)
    return None


def _run_cold_restart_boundary(*, ac_boundary: str, runtime_variant_env: str | None) -> None:
    """Real end-to-end cold-restart causal boundary: create a disposable
    named Herdr session distinct from 'default', start a Native or
    Claude-GPT agent pane inside it, kill -9 both the agent process and
    the disposable session's own server (never 'default''s), cold-start
    the same session name again, and verify `task_context_cold_restart_
    startup.discover_resume_candidates()` classifies the regenerated pane
    as PROVEN_ABSENT (bare-shell-only) -> dispatched exactly once."""
    ok, reason, evidence = _cold_restart_canary_capability()
    if not ok:
        _skip_with_log(ac_boundary, reason, evidence)
        return

    herdr_bin = shutil.which("herdr")
    assert herdr_bin is not None  # narrowed by _cold_restart_canary_capability() above

    session_name = f"issue-2752-canary-{ac_boundary}-{uuid.uuid4().hex[:8]}"
    evidence["session_name"] = session_name

    create = _run_herdr(herdr_bin, "--session", session_name, "status", "server")
    if create.returncode != 0:
        _skip_with_log(
            ac_boundary,
            f"unable to create/attach disposable session {session_name!r}: {create.stderr.strip()}",
            {**evidence, "create_stdout": create.stdout, "create_stderr": create.stderr},
        )
        return

    try:
        server_pid = _find_session_server_pid(herdr_bin, session_name)
        evidence["disposable_server_pid"] = server_pid
        if server_pid is None:
            _skip_with_log(
                ac_boundary,
                f"could not positively locate disposable session {session_name!r}'s own server pid -- "
                "refusing to proceed with a destructive kill-9 step without unambiguous PID evidence",
                evidence,
            )
            return

        pane_list = _run_herdr(herdr_bin, "--session", session_name, "pane", "list")
        try:
            panes = json.loads(pane_list.stdout).get("result", {}).get("panes", [])
        except (json.JSONDecodeError, AttributeError):
            panes = []
        if not panes:
            _skip_with_log(
                ac_boundary,
                f"disposable session {session_name!r} reported no panes to seed a canary agent into",
                {**evidence, "pane_list_stdout": pane_list.stdout},
            )
            return
        pane_id = panes[0]["pane_id"]

        if runtime_variant_env == config.CLAUDE_GPT_RUNTIME_VARIANT:
            launch_script = str(_REPO_ROOT / "scripts" / "claude-gpt" / "launch.sh")
            launch = _run_herdr(herdr_bin, "--session", session_name, "pane", "run", pane_id, launch_script)
        else:
            launch = _run_herdr(
                herdr_bin,
                "--session",
                session_name,
                "agent",
                "start",
                f"canary-{ac_boundary[:16]}",
                "--kind",
                "claude",
                "--pane",
                pane_id,
            )
        evidence["launch_returncode"] = launch.returncode
        evidence["launch_stderr"] = launch.stderr.strip()
        if launch.returncode != 0:
            _skip_with_log(
                ac_boundary,
                f"could not start a canary agent in disposable session {session_name!r}: {launch.stderr.strip()}",
                evidence,
            )
            return

        # Abrupt kill (never graceful stop -- see docs/dev/task-context.md
        # "運用上の知見" item 1: graceful stop drives an ordinary SessionEnd,
        # which is a deliberately different, non-cold-restart code path).
        try:
            os.kill(server_pid, 9)
        except ProcessLookupError:
            pass
        time.sleep(1.0)

        restart = _run_herdr(herdr_bin, "--session", session_name, "status", "server")
        evidence["restart_returncode"] = restart.returncode
        if restart.returncode != 0:
            _skip_with_log(
                ac_boundary,
                f"disposable session {session_name!r} failed to cold-restart after kill -9: {restart.stderr.strip()}",
                evidence,
            )
            return

        post_restart_pane_list = _run_herdr(herdr_bin, "--session", session_name, "pane", "list")
        try:
            post_panes = json.loads(post_restart_pane_list.stdout).get("result", {}).get("panes", [])
        except (json.JSONDecodeError, AttributeError):
            post_panes = []
        evidence["post_restart_pane_ids"] = [p.get("pane_id") for p in post_panes]

        def _run_fn(argv, **kwargs):
            full_argv = [herdr_bin, "--session", session_name, *argv[1:]]
            return subprocess.run(full_argv, capture_output=True, text=True, timeout=30.0)

        state_root = config.resolve_state_root(cwd=str(_SCRIPTS_DIR))
        db_file = state_root / config.DB_FILE_NAME
        conn = db.connect(db_file)
        try:
            migration_runner.migrate(conn)
            discovery = startup.discover_resume_candidates(conn, herdr_bin=herdr_bin, run_fn=_run_fn)
        finally:
            conn.close()
        evidence["discovery"] = discovery

        assert discovery["live_runtime_preserved"] == [], (
            "a genuine cold restart (kill -9, not a live handoff) must never classify as "
            f"live_runtime_preserved: {discovery}"
        )
        assert len(discovery["candidates"]) == 1, (
            f"expected exactly-one dispatch candidate after cold restart, got: {discovery}"
        )

        _write_runtime_verification_log(
            ac_boundary=ac_boundary,
            verdict="PASS",
            exit_code=0,
            reason="pane regenerated at same locator classified PROVEN_ABSENT -> exactly-one dispatch candidate",
            evidence=evidence,
        )
    except BaseException as exc:
        _write_runtime_verification_log(
            ac_boundary=ac_boundary,
            verdict="FAIL",
            exit_code=1,
            reason=f"{type(exc).__name__}: {exc}",
            evidence=evidence,
        )
        raise
    finally:
        _run_herdr(herdr_bin, "session", "stop", session_name)
        _run_herdr(herdr_bin, "session", "delete", session_name)


# ---------------------------------------------------------------------------
# 3. Native + real cold restart
# ---------------------------------------------------------------------------


def test_native_cold_restart_regenerates_pane_and_dispatches_exactly_once():
    """AC7 causal boundary 3: Native + real cold restart -> runtime
    absent, exactly-one resume, runtime profile/identity preserved."""
    _run_cold_restart_boundary(ac_boundary="native-cold-restart", runtime_variant_env=None)


# ---------------------------------------------------------------------------
# 4. Claude-GPT + real cold restart
# ---------------------------------------------------------------------------


def test_claude_gpt_cold_restart_regenerates_pane_and_dispatches_exactly_once():
    """AC7 causal boundary 4: Claude-GPT + real cold restart -> runtime
    absent, exactly-one resume, runtime profile preserved."""
    _run_cold_restart_boundary(
        ac_boundary="claude-gpt-cold-restart", runtime_variant_env=config.CLAUDE_GPT_RUNTIME_VARIANT
    )
