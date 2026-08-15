"""Security-boundary regressions for Issue #1511's pnpm package-script gates."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parents[4]
SCRIPTS = Path(__file__).parent.parent / "scripts"
BASELINE_PATH = SCRIPTS / "baseline_vc_preflight.py"
TRIAGE_PATH = (
    Path(__file__).parents[2]
    / "impl-review-loop"
    / "scripts"
    / "triage_contract_blockers.py"
)

sys.path.insert(0, str(SCRIPTS))
import pnpm_gate_registry as registry  # noqa: E402


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


baseline = _load(BASELINE_PATH, "baseline_vc_preflight_issue_1511")
triage = _load(TRIAGE_PATH, "triage_contract_blockers_issue_1511")


def _write_manifest(root: Path, scripts: dict[str, str] | None = None) -> None:
    expected = {name: body for name, body in registry.expected_scripts().items()}
    if scripts:
        expected.update(scripts)
    (root / "package.json").write_text(
        json.dumps({"packageManager": registry.PACKAGE_MANAGER, "scripts": expected}),
        encoding="utf-8",
    )


def test_registry_is_the_only_pnpm_gate_authority():
    """GIVEN both consumers WHEN gates are queried THEN one descriptor set is used."""
    expected = {tuple(item.request_argv) for item in registry.iter_gate_descriptors()}
    assert expected == {
        ("pnpm", "typecheck"),
        ("pnpm", "lint"),
        ("pnpm", "test"),
        ("pnpm", "build"),
        ("pnpm", "typecheck:e2e"),
        ("pnpm", "lint:docs"),
        ("pnpm", "retro-live-verification:generate"),
        ("pnpm", "retro-live-verification:verify"),
    }
    assert baseline._canonical_pnpm_gate(["pnpm", "lint:docs"]) == ("pnpm", "lint:docs")
    assert triage.registry.gate_for_request(["pnpm", "lint:docs"]) is not None


@pytest.mark.parametrize(
    "argv",
    [
        ["./pnpm", "typecheck:e2e"],
        ["/tmp/pnpm", "lint:docs"],
        ["node_modules/.bin/pnpm", "lint:docs"],
        ["pnpm", "TYPECHECK:E2E"],
        ["pnpm", "LINT:DOCS"],
        ["pnpm", "lint:docs", "--if-present"],
    ],
)
def test_noncanonical_pnpm_requests_are_blocked_without_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
):
    """GIVEN a nonexact request WHEN preflight runs THEN no script subprocess launches."""
    _write_manifest(tmp_path)
    calls: list[list[str]] = []

    def forbidden(*args, **kwargs):
        calls.append(list(args[0]))
        raise AssertionError("noncanonical pnpm request must not launch")

    # Issue #2165 P1-2 (PR #2177 fix_delta iteration 3): run_command() now
    # launches via subprocess.Popen(start_new_session=True) instead of
    # subprocess.run(), so the launch-prevention mock targets Popen.
    monkeypatch.setattr(baseline.subprocess, "Popen", forbidden)
    command = " ".join(argv)
    assert baseline.classify_static_command(command, tmp_path) is not None
    assert baseline.run_command(command, 1, str(tmp_path))[0] == -1
    assert calls == []


@pytest.mark.parametrize(
    "scripts",
    [
        {"lint:prose": "unexpected --write"},
        {"lint:docs": None},
        {"prelint:docs": "node unexpected-hook.mjs"},
    ],
)
def test_manifest_hook_and_closure_drift_blocks_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripts: dict[str, str | None]
):
    """GIVEN manifest drift WHEN a gate is prepared THEN launch is denied."""
    _write_manifest(tmp_path, scripts)  # type: ignore[arg-type]
    calls: list[list[str]] = []
    # Issue #2165 P1-2 (PR #2177 fix_delta iteration 3): run_command() now
    # launches via subprocess.Popen(start_new_session=True) instead of
    # subprocess.run(), so the launch-prevention mock targets Popen.
    monkeypatch.setattr(baseline.subprocess, "Popen", lambda *a, **k: calls.append(list(a[0])))
    code, _, stderr, _, _ = baseline.run_command("pnpm lint:docs", 1, str(tmp_path))
    assert code == -1
    assert "manifest_integrity" in stderr
    assert calls == []


def test_producer_evidence_round_trips_to_triage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """GIVEN producer evidence WHEN triage consumes it THEN the registry gate is lossless."""
    _write_manifest(tmp_path)
    monkeypatch.setattr(registry, "resolve_trusted_pnpm", lambda _root: "/usr/bin/pnpm")
    captured: list[dict] = []

    class FakeProcess:
        """Minimal stand-in for the Popen instance run_command() now uses.

        Issue #2165 P1-2 (PR #2177 fix_delta iteration 3): run_command()
        switched from subprocess.run() to subprocess.Popen(...) +
        communicate(timeout=...) so it can reap the whole process group
        (not just the direct child) on timeout. This fake mirrors the
        subset of the Popen interface run_command() actually touches on
        the non-timeout path: communicate() returning (stdout, stderr) and
        a post-communicate .returncode attribute.
        """

        returncode = 1

        def communicate(self, timeout=None):
            return "", "ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY"

    def fake_popen(argv, **kwargs):
        captured.append({"argv": argv, "env": kwargs["env"]})
        return FakeProcess()

    monkeypatch.setattr(baseline.subprocess, "Popen", fake_popen)
    result = baseline.run_command("pnpm typecheck:e2e", 1, str(tmp_path))
    evidence = baseline._pnpm_gate_evidence_for_command(
        "pnpm typecheck:e2e", str(tmp_path)
    )
    assert captured[0]["argv"] == ["/usr/bin/pnpm", "run", "typecheck:e2e"]
    assert result[4] == {"CI": "true"}
    assert evidence["runner_env_delta"] == {"CI": "true"}
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "results": [
            {
                "ac": "AC4",
                "command_hash": "sha256:" + "a" * 64,
                "category": "package_manager_no_tty_prompt",
                "decision": "blocked",
                "raw_command": "pnpm typecheck:e2e",
                "runner_env_delta": {},
                "pnpm_gate_evidence_required": True,
                "pnpm_gate_evidence": evidence,
            }
        ],
    }
    output = triage.triage_contract_blockers(payload)
    assert output["status"] == "ok"
    assert output["suggested_actions"][0]["argv"] == ["pnpm", "typecheck:e2e"]
    payload["results"][0].pop("pnpm_gate_evidence")
    rejected = triage.triage_contract_blockers(payload)
    assert rejected["status"] == "incomplete_evidence"
    assert rejected["suggested_actions"] == []


def test_repository_manifest_test_script_matches_registry() -> None:
    """GIVEN the real repo package.json WHEN compared to the registry THEN the test script matches exactly.

    (Major 1, PR #1559 review). Regression tests above only compare
    package.json against a synthetic manifest built from
    registry.expected_scripts() itself, which cannot detect drift between
    the real repository package.json and the registry. Production
    validate_manifest() performs a byte-for-byte comparison against the
    real package.json and fail-closes on manifest_integrity:closure_drift:test,
    so this test reads the actual repository package.json to close that gap.
    """
    manifest = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    assert manifest["scripts"]["test"] == registry.expected_scripts()["test"]


def test_retro_live_verification_post_is_not_a_generic_gate():
    """GIVEN the mutation-capable post command WHEN gates are queried THEN it is absent.

    (Issue #1709 PR review P0-5.) `retro-live-verification:post` can create
    or update a live GitHub comment; it must never be reachable through this
    generic, agent-facing gate registry -- only the protected
    `post-canonical-comment` job in
    `.github/workflows/retro-live-verification.yml` may invoke it.
    """
    assert registry.gate_for_request(["pnpm", "retro-live-verification:post"]) is None
    assert "retro-live-verification:post" not in registry.expected_scripts()


@pytest.mark.parametrize(
    "argv",
    [
        ["pnpm", "retro-live-verification:generate"],
        ["pnpm", "retro-live-verification:verify"],
    ],
)
def test_retro_live_verification_gates_actually_execute_the_intended_check(
    argv: list[str],
):
    """GIVEN the real repository fixtures WHEN a registered gate is launched THEN it
    performs its intended validation successfully instead of failing with a CLI
    usage error.

    (Issue #1709 PR review P0-5, required negative test 8.) Before this fix, the
    registry never forwarded any argv to the underlying CLI even though both
    `generate-retro-live-verification.mjs` and `check-retro-live-
    verification.mjs` have required arguments, so any canonical two-token
    invocation was guaranteed to fail with a CLI usage error (exit code 2),
    never to actually run the validation it claims to gate.

    This test needs a real `pnpm` + Node.js toolchain (the `python-test` CI
    lane deliberately does not install one; see
    `.claude/skills/ci-test-performance/SKILL.md` Operative Status). Skip
    rather than fail-closed-block the python-only lane; `node-backed-hook-
    tests` / local development both have pnpm available and will exercise
    this contract for real.
    """
    if shutil.which("pnpm") is None:
        pytest.skip("pnpm is not installed in this CI lane (python-test does not bootstrap Node/pnpm)")
    launch_argv, evidence, error = registry.prepare_launch(argv, str(REPO_ROOT))
    assert error is None, error
    assert evidence is not None
    result = subprocess.run(
        launch_argv,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "CI": "true"},
    )
    assert result.returncode == 0, (
        f"expected the wrapped {argv[1]} gate to exit 0 (real validation, not a "
        f"usage error); got exit {result.returncode}\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload.get("status") == "ok" or payload.get("verification_status") == "pass", (
        f"gate ran but did not report a passing validation result: {payload}"
    )


def _wait_until_pid_gone(pid: int, timeout_seconds: float = 5.0) -> bool:
    """Poll os.kill(pid, 0) until it raises ProcessLookupError or timeout."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # Still alive but owned by someone else -- treat as "not gone".
            pass
        time.sleep(0.05)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_run_command_timeout_reaps_grandchild_process_not_just_direct_child(
    tmp_path: Path,
):
    """GIVEN a VC command whose direct child spawns its own grandchild
    WHEN run_command() times out THEN BOTH the direct child pid and the
    grandchild pid are terminated within bounded time (not merely the
    direct child, per Python's subprocess.run(timeout=...) doc caveat).

    Issue #2165 P1-2 (OWNER 2026-08-15 REQUEST_CHANGES merge condition #5,
    PR #2177 fix_delta iteration 3, human/OWNER-authorized Allowed Paths
    expansion to include this file). This is the fault-injection test the
    merge condition explicitly required and which was missing from all
    prior iterations of this PR.
    """
    pidfile = tmp_path / "pids.json"
    fixture_script = tmp_path / "grandchild_fixture.py"
    fixture_script.write_text(
        "import json\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "\n"
        "grandchild = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)']\n"
        ")\n"
        "with open(sys.argv[1], 'w') as f:\n"
        "    json.dump({'child_pid': os.getpid(), 'grandchild_pid': grandchild.pid}, f)\n"
        "    f.flush()\n"
        "    os.fsync(f.fileno())\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    command = f"{sys.executable} {fixture_script} {pidfile}"
    exit_code, stdout, stderr, duration_ms, _ = baseline.run_command(
        command, timeout_seconds=2, cwd=str(tmp_path)
    )

    assert exit_code == -1
    assert stderr == "timeout"

    assert pidfile.exists(), (
        "fixture never wrote its pidfile -- either it did not start in "
        "time (flaky environment) or run_command() killed it before it "
        "could write, which would itself defeat this test's premise"
    )
    pids = json.loads(pidfile.read_text(encoding="utf-8"))
    child_pid = pids["child_pid"]
    grandchild_pid = pids["grandchild_pid"]
    assert child_pid != grandchild_pid

    assert _wait_until_pid_gone(child_pid), (
        f"direct child pid {child_pid} was still alive after run_command() "
        "timeout + bounded grace period"
    )
    assert _wait_until_pid_gone(grandchild_pid), (
        f"grandchild pid {grandchild_pid} (spawned BY the direct child, "
        "not by run_command() itself) was still alive after run_command() "
        "timeout + bounded grace period -- process-tree reaping is not "
        "reaping the whole tree, only the direct child"
    )
