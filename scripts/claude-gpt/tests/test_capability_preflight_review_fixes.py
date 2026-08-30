"""scripts/claude-gpt/tests/test_capability_preflight_review_fixes.py

Issue #2401: bounded follow-up for the merged PR #2394 capability-preflight
review findings. Covers, in a new file per the Issue's canonical VC shape
(Issue #1285 / PR #1305 -- a not-yet-created file, not new node-ids on an
existing test file):

  - AC1: required GitHub probes (`github_auth` -> `github_repo_read` when
    `github_auth` completed -> `controlled_github_read` independently of
    root auth outcome) run BEFORE the optional `spark_env_only` probe.
  - AC2: when the required probes complete and the optional Spark probe
    consumes the remaining shared deadline, `preferred`/`allowed` degrades
    rather than blocks, and the GitHub probe results remain `ready`.
  - AC3: invalid JSON / structurally invalid Spark payloads return a
    structured malformed-output reason without an uncaught exception.
  - AC4: `assess(deadline_ns=None)` creates exactly one local absolute
    deadline via `_local_deadline_ns()` and passes it unchanged to every
    applicable probe.
  - AC5: a hermetic test that crosses the REAL root-to-producer subprocess
    boundary (`root_entry_router.capability_preflight_result()` ->
    real `workflow_capability_preflight.py` child process), faking only
    external leaf CLIs (`gh`).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent.parent
_GUARDS_DIR = _REPO_ROOT / "scripts" / "agent-guards"
_ROOT_ROUTER_DIR = _REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
_FAKE_GH_FIXTURE = _TESTS_DIR / "fixtures" / "fake_gh.py"

for _path in (_SCRIPTS_DIR, _GUARDS_DIR, _ROOT_ROUTER_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import root_entry_router as rer  # noqa: E402
import workflow_capability_preflight as wcp  # noqa: E402

_REPO = "squne121/loop-protocol"


def _ready_uv(*_args, **_kwargs):
    return {"status": wcp.trusted_uv_mod.STATUS_OK, "reason": "resolved", "resolved_path": "/fake/uv"}


def _completed(argv, *, stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")


# =============================================================================
# AC1 + AC2: required-probe ordering ahead of the optional Spark probe, and
# starvation of the Spark probe by the shared deadline degrades (not blocks).
# =============================================================================


def test_required_probes_run_before_optional_spark_and_starvation_degrades(monkeypatch):
    """GIVEN the three required GitHub probes complete WITHIN budget (a
    genuinely POSITIVE remaining-timeout share reaches the optional Spark
    probe), WHEN assess() runs with spark_mode=preferred/spark_fallback=
    allowed and the optional spark_env_only probe spawns and then receives a
    real `subprocess.TimeoutExpired`, THEN the required probes are observed
    to run BEFORE the optional probe (`gh, gh, gh, sh` order), the overall
    decision is `degraded` (not `blocked`), and the GitHub probe results
    remain ready.

    Issue #2401 P2-1 fix_delta: this is AC2's actual scenario -- a spawned
    Spark probe starved by `TimeoutExpired` mid-flight -- not the separate
    before-spawn deadline-exhaustion case, which
    `test_expired_deadline_spawns_no_new_process` in
    `test_workflow_capability_preflight_deadline.py` already covers (AC7's
    focused suite includes that file)."""
    monkeypatch.setattr(wcp.trusted_uv_mod, "check_trusted_uv", _ready_uv)
    # One monotonic read per probe attempt: the three required probes each
    # advance the clock but stay well within the shared 100s-relative
    # budget, leaving the optional spark_env_only probe a genuinely positive
    # remaining-timeout share when `_run_probe_with_deadline` checks it.
    clock_values = iter((90_000_000_000, 91_000_000_000, 92_000_000_000, 93_000_000_000))
    monkeypatch.setattr(wcp.time, "monotonic_ns", lambda: next(clock_values))

    calls: list[list[str]] = []
    spark_timeouts: list[float] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[0] == "sh":
            spark_timeouts.append(kwargs["timeout"])
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return _completed(argv)

    monkeypatch.setattr(wcp.subprocess, "run", fake_run)

    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_REPO,
        spark_mode="preferred",
        spark_fallback="allowed",
        planned_operations=[],
        deadline_ns=100_000_000_000,
    )

    # The three required `gh` probes ran BEFORE the optional `sh` (spark)
    # probe, which then spawned (not starved before spawn) and timed out.
    assert [call[0] for call in calls] == ["gh", "gh", "gh", "sh"]
    assert spark_timeouts and spark_timeouts[0] > 0
    assert "preflight_probe_timeout:spark_env_only" in result["reasons"]

    assert result["checks"]["github"]["auth"] is True
    assert result["checks"]["github"]["repo_read"] is True
    assert result["actor_capabilities"]["root_github_read"]["status"] == "ready"
    assert result["actor_capabilities"]["controlled_github_read"]["status"] == "ready"

    assert result["checks"]["spark"]["status"] == wcp.SPARK_FALLBACK_ONLY
    assert result["decision"] == wcp.DECISION_DEGRADED


def test_controlled_github_read_runs_independently_of_root_auth_failure(monkeypatch):
    """GIVEN root `github_auth` fails, WHEN assess() runs, THEN root
    `github_repo_read` is SKIPPED (it is only attempted when `github_auth`
    completed), but `controlled_github_read` still runs -- it is
    independent of the root auth outcome, not gated on it (AC1's second
    ordering assertion)."""
    monkeypatch.setattr(wcp.trusted_uv_mod, "check_trusted_uv", _ready_uv)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        if argv[:2] == ["gh", "auth"]:
            return _completed(argv, returncode=1)
        return _completed(argv)

    monkeypatch.setattr(wcp.subprocess, "run", fake_run)

    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations=[],
    )

    argv_kinds = [call[:2] for call in calls]
    assert argv_kinds.count(["gh", "auth"]) == 1
    # root github_repo_read must be SKIPPED after a failed github_auth.
    assert argv_kinds.count(["gh", "repo"]) == 0
    # controlled_github_read must still have run (independent of root auth).
    assert any(call[:2] == ["gh", "api"] for call in calls)

    assert result["checks"]["github"]["auth"] is False
    assert result["checks"]["github"]["repo_read"] is False
    assert result["actor_capabilities"]["controlled_github_read"]["status"] == "ready"
    # Root auth failure alone still fails closed for the overall decision.
    assert result["decision"] == wcp.DECISION_BLOCKED


# =============================================================================
# AC3: malformed Spark payloads normalize to the structured malformed-output
# reason, without an uncaught exception, for every consumed field.
# =============================================================================


@pytest.mark.parametrize(
    "stdout",
    (
        "not-json",  # invalid JSON
        json.dumps([1, 2, 3]),  # non-dict top level
        json.dumps({"binary_available": "yes", "chatgpt_auth": {"available": True}}),  # non-bool binary_available
        json.dumps({"binary_available": True}),  # missing chatgpt_auth
        json.dumps({"binary_available": True, "chatgpt_auth": "connected"}),  # non-dict chatgpt_auth
        json.dumps({"binary_available": True, "chatgpt_auth": {"available": "yes"}}),  # non-bool chatgpt_auth.available
    ),
    ids=(
        "invalid_json",
        "non_dict_top_level",
        "non_bool_binary_available",
        "missing_chatgpt_auth",
        "non_dict_chatgpt_auth",
        "non_bool_chatgpt_auth_available",
    ),
)
def test_malformed_spark_payload_returns_structured_reason(monkeypatch, stdout):
    monkeypatch.setattr(wcp.trusted_uv_mod, "check_trusted_uv", _ready_uv)
    monkeypatch.setattr(wcp, "_github_auth_probe", lambda deadline_ns: wcp.ProbeOutcome(wcp.PROBE_COMPLETED))
    monkeypatch.setattr(
        wcp, "_github_repo_read_probe", lambda repo, deadline_ns: wcp.ProbeOutcome(wcp.PROBE_COMPLETED)
    )
    monkeypatch.setattr(
        wcp, "_controlled_github_read_probe", lambda repo, deadline_ns: wcp.ProbeOutcome(wcp.PROBE_COMPLETED)
    )
    monkeypatch.setattr(
        wcp,
        "_run_env_only_preflight",
        lambda deadline_ns: wcp.ProbeOutcome(wcp.PROBE_COMPLETED, stdout=stdout),
    )

    # preferred/allowed: malformed Spark output degrades (fallback allowed),
    # and -- the crux of this AC -- assess() must not raise.
    degraded_result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_REPO,
        spark_mode="preferred",
        spark_fallback="allowed",
        planned_operations=[],
    )
    assert "preflight_probe_malformed_output:spark_env_only" in degraded_result["reasons"]
    assert degraded_result["checks"]["spark"]["status"] == wcp.SPARK_FALLBACK_ONLY
    assert degraded_result["decision"] == wcp.DECISION_DEGRADED

    # required/forbidden: malformed Spark output blocks (fail closed, no
    # silent fallback).
    blocked_result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_REPO,
        spark_mode="required",
        spark_fallback="forbidden",
        planned_operations=[],
    )
    assert blocked_result["checks"]["spark"]["status"] == wcp.SPARK_UNAVAILABLE
    assert blocked_result["decision"] == wcp.DECISION_BLOCKED


def test_run_env_only_preflight_rejects_null_chatgpt_auth_via_real_function(monkeypatch):
    """Issue #2401 P2-2 fix_delta: unlike the parametrized policy-mapping
    cases above (which monkeypatch `_run_env_only_preflight` itself and hand
    `assess()` a pre-built `ProbeOutcome`, never exercising this function's
    own JSON-parse + `_validate_spark_env_payload()` boundary), this test
    calls the REAL `_run_env_only_preflight()` directly with only
    `subprocess.run` faked, confirming a structurally-invalid
    `chatgpt_auth: null` payload becomes `PROBE_MALFORMED_OUTPUT` through
    that function's own parse/validate path."""

    def fake_run(argv, **kwargs):
        assert argv[0] == "sh"
        return _completed(argv, stdout=json.dumps({"binary_available": True, "chatgpt_auth": None}))

    monkeypatch.setattr(wcp.subprocess, "run", fake_run)

    outcome = wcp._run_env_only_preflight(deadline_ns=wcp._local_deadline_ns())

    assert outcome.kind == wcp.PROBE_MALFORMED_OUTPUT


# =============================================================================
# AC4: assess(deadline_ns=None) creates exactly one local absolute deadline
# via _local_deadline_ns() and passes it UNCHANGED to every applicable probe.
# =============================================================================


def test_assess_without_deadline_creates_one_shared_local_deadline(monkeypatch):
    sentinel_deadline = 123_456_789_000
    local_deadline_calls = {"count": 0}

    def fake_local_deadline_ns():
        local_deadline_calls["count"] += 1
        return sentinel_deadline

    monkeypatch.setattr(wcp, "_local_deadline_ns", fake_local_deadline_ns)
    monkeypatch.setattr(wcp.trusted_uv_mod, "check_trusted_uv", _ready_uv)

    seen_deadlines: list[int] = []

    def _github_auth_probe(deadline_ns):
        seen_deadlines.append(deadline_ns)
        return wcp.ProbeOutcome(wcp.PROBE_COMPLETED)

    def _github_repo_read_probe(repo, deadline_ns):
        seen_deadlines.append(deadline_ns)
        return wcp.ProbeOutcome(wcp.PROBE_COMPLETED)

    def _controlled_github_read_probe(repo, deadline_ns):
        seen_deadlines.append(deadline_ns)
        return wcp.ProbeOutcome(wcp.PROBE_COMPLETED)

    def _run_env_only_preflight(deadline_ns):
        seen_deadlines.append(deadline_ns)
        return wcp.ProbeOutcome(
            wcp.PROBE_COMPLETED,
            stdout=json.dumps({"binary_available": True, "chatgpt_auth": {"available": True}}),
        )

    monkeypatch.setattr(wcp, "_github_auth_probe", _github_auth_probe)
    monkeypatch.setattr(wcp, "_github_repo_read_probe", _github_repo_read_probe)
    monkeypatch.setattr(wcp, "_controlled_github_read_probe", _controlled_github_read_probe)
    monkeypatch.setattr(wcp, "_run_env_only_preflight", _run_env_only_preflight)

    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_REPO,
        spark_mode="preferred",
        spark_fallback="allowed",
        planned_operations=[],
        deadline_ns=None,
    )

    # Exactly one local absolute deadline created for this call to the REAL
    # assess() (not a monkeypatched replacement of assess() itself).
    assert local_deadline_calls["count"] == 1
    # The SAME deadline value reached every applicable probe, unchanged.
    assert seen_deadlines == [sentinel_deadline] * 4
    assert result["decision"] == wcp.DECISION_READY


# =============================================================================
# AC5: hermetic real root-to-producer subprocess boundary test. Only
# external leaf CLIs (`gh`) are faked via a PATH wrapper -- neither the
# producer subprocess nor its result is replaced with a fixture payload
# (reusing the `_install_fake_gh`/`_FAKE_GH_FIXTURE` pattern already used by
# `test_real_producer_blocked_on_fake_gh_auth_failure_posts_no_comment` in
# `test_root_entry_router_workflow_capability.py`, without modifying that
# file).
# =============================================================================


def _install_fake_gh(bin_dir: Path) -> Path:
    """Install a fake `gh` binary on PATH that delegates to the real
    `_FAKE_GH_FIXTURE` (unmodified -- not in this Issue's Allowed Paths).

    Issue #2401 P1-1 fix_delta (c): this wrapper is itself a small Python
    script (not a plain `exec`-only shell shim) so it can OPTIONALLY log its
    own exact `sys.argv[1:]` -- one JSON line per invocation, in call order
    -- to the file named by the `FAKE_GH_CALL_LOG` env var (when set) before
    replacing itself (`os.execv`, preserving exit code/stdout/stderr
    unchanged) with the real fixture. This lets a test assert the EXACT
    argv/order of every real `gh` invocation the real producer subprocess
    issued, without modifying the shared fixture file itself."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "gh"
    wrapper_source = (
        "#!{python}\n"
        "import json, os, sys\n"
        "_log_path = os.environ.get('FAKE_GH_CALL_LOG')\n"
        "if _log_path:\n"
        "    with open(_log_path, 'a', encoding='utf-8') as fh:\n"
        "        fh.write(json.dumps(sys.argv[1:]) + chr(10))\n"
        "os.execv({python!r}, [{python!r}, {fixture!r}] + sys.argv[1:])\n"
    ).format(python=sys.executable, fixture=str(_FAKE_GH_FIXTURE))
    wrapper.write_text(wrapper_source)
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper


def test_root_to_producer_transports_deadline_with_fake_external_clis(tmp_path, monkeypatch):
    """GIVEN the REAL root consumer (`root_entry_router.
    capability_preflight_result()`) driving the REAL producer child process
    (`workflow_capability_preflight.py`, spawned by `sys.executable`, no
    `subprocess.run` monkeypatch at the root_entry_router/
    workflow_capability_preflight boundary), with only the external leaf
    `gh` CLI faked via a PATH wrapper
    WHEN capability_preflight_result() is called
    THEN the real producer receives and honors the transported
    `--deadline-monotonic-ns` absolute deadline and its own shared-deadline
    probes, reporting the REAL GitHub read capability observed through the
    fake `gh` (not a canned `subprocess.run` JSON payload).

    Issue #2401 P1-1 fix_delta: this test now (a) wraps the REAL
    `subprocess.run` (delegating to it, not replacing it) at the root
    boundary to record the exact argv the root built and confirm the
    transported `--deadline-monotonic-ns` value, (b)/(c) reads back the fake
    `gh` invocations the real producer actually issued to confirm their
    exact argv and order (not merely a normalized/aggregated `calls[]`
    trace), and (d) asserts the fixed `decision == "ready"` (this test's
    spark_mode=None condition has no known-blocking reason, so `degraded`
    would indicate a real regression, not an acceptable alternative)."""
    bin_dir = tmp_path / "bin"
    _install_fake_gh(bin_dir)
    state_path = tmp_path / "fake_gh_state.json"
    call_log_path = tmp_path / "fake_gh_calls.jsonl"

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_GH_STATE", str(state_path))
    monkeypatch.setenv("FAKE_GH_AUTH_OK", "1")
    monkeypatch.setenv("FAKE_GH_REPO_READ_OK", "1")
    monkeypatch.setenv("FAKE_GH_CONTROLLED_READ_OK", "1")
    monkeypatch.setenv("FAKE_GH_CALL_LOG", str(call_log_path))

    real_subprocess_run = subprocess.run
    root_argv_calls: list[list[str]] = []

    def _recording_subprocess_run(argv, *args, **kwargs):
        root_argv_calls.append(list(argv))
        return real_subprocess_run(argv, *args, **kwargs)

    monkeypatch.setattr(rer.subprocess, "run", _recording_subprocess_run)

    # spark_mode=None: this boundary test's scope is the shared-deadline
    # root-to-producer transport and the `gh`-backed GitHub probes; it does
    # not additionally require faking the `preflight.sh` Spark leaf CLI.
    result = rer.capability_preflight_result(repo=_REPO)

    # (a)/(b): exactly one real producer subprocess spawned, transporting
    # exactly one `--deadline-monotonic-ns <int>` -- the actual observed
    # root-to-producer transport, not an assumption about it.
    assert len(root_argv_calls) == 1
    producer_argv = root_argv_calls[0]
    assert producer_argv.count("--deadline-monotonic-ns") == 1
    deadline_value = producer_argv[producer_argv.index("--deadline-monotonic-ns") + 1]
    assert deadline_value.isdigit()
    assert int(deadline_value) > 0

    # (c): the fake `gh` wrapper's own call log confirms the EXACT argv and
    # order of the three `gh` invocations the real producer issued -- not
    # the fixture's separate, explicitly non-exact `calls[]` trace.
    gh_calls = [json.loads(line) for line in call_log_path.read_text(encoding="utf-8").splitlines()]
    assert gh_calls == [
        ["auth", "status", "--active", "--hostname", "github.com"],
        ["repo", "view", _REPO, "--json", "name"],
        ["api", "--hostname", "github.com", f"repos/{_REPO}", "--jq", "{name}"],
    ]

    # (d): with spark_mode=None fixed for this test, the real producer's
    # decision is exactly `ready`.
    assert result["decision"] == "ready"
    assert result["checks"]["github"]["auth"] is True
    assert result["checks"]["github"]["repo_read"] is True
    assert result["actor_capabilities"]["root_github_read"]["status"] == "ready"
    assert result["actor_capabilities"]["controlled_github_read"]["status"] == "ready"
    # Fail-closed synthetic-blocked reasons (producer_watchdog_timeout /
    # producer_invocation_failed / producer_result_malformed) must be
    # ABSENT -- a genuinely-executed real producer, not a transport failure
    # masquerading as a capability verdict.
    assert not any(reason.startswith("producer_") for reason in result["reasons"])


def test_producer_zero_deadline_spawns_no_github_subprocess(tmp_path):
    """GIVEN the REAL producer script invoked directly (not via
    root_entry_router) with an already-exhausted `--deadline-monotonic-ns
    0` budget
    THEN no GitHub subprocess is spawned -- the fake `gh` call log stays
    empty/absent -- and the deadline-exhausted outcome fails closed to
    `blocked` (Issue #2401 P1-1 fix_delta item e)."""
    bin_dir = tmp_path / "bin"
    _install_fake_gh(bin_dir)
    state_path = tmp_path / "fake_gh_state.json"
    call_log_path = tmp_path / "fake_gh_calls.jsonl"

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_GH_STATE"] = str(state_path)
    env["FAKE_GH_CALL_LOG"] = str(call_log_path)

    producer_script = _SCRIPTS_DIR / "workflow_capability_preflight.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(producer_script),
            "--profile",
            "issue-to-impl",
            "--repo",
            _REPO,
            "--deadline-monotonic-ns",
            "0",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    assert proc.returncode == 0
    result = json.loads(proc.stdout)

    assert not call_log_path.exists() or call_log_path.read_text(encoding="utf-8") == ""
    assert any(reason.startswith("preflight_deadline_exhausted:") for reason in result["reasons"])
    assert result["decision"] == wcp.DECISION_BLOCKED


def test_root_to_producer_boundary_reports_blocked_on_fake_gh_auth_failure(tmp_path, monkeypatch):
    """GIVEN the same REAL root-to-producer subprocess boundary as above,
    but with the fake `gh auth status` forced to fail
    THEN the REAL producer's structured `decision: blocked` (not a
    transport-level synthetic failure) reaches the REAL root consumer
    unchanged -- proving the boundary carries the producer's own verdict,
    not a canned payload."""
    bin_dir = tmp_path / "bin"
    _install_fake_gh(bin_dir)
    state_path = tmp_path / "fake_gh_state.json"

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_GH_STATE", str(state_path))
    monkeypatch.setenv("FAKE_GH_AUTH_OK", "0")

    result = rer.capability_preflight_result(repo=_REPO)

    assert result["decision"] == "blocked"
    assert result["checks"]["github"]["auth"] is False
    assert any("github:auth_unavailable" in reason for reason in result["reasons"])
    assert not any(reason.startswith("producer_") for reason in result["reasons"])
