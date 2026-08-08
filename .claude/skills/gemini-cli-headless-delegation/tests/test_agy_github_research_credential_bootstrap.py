"""Broker-owned credential bootstrap contract (Issue #2012 AC3-AC7).

`run_agy_github_research_broker.py` resolves a stored `gh` credential
entirely inside itself (never the E2E/orchestrator process) when no
GH_TOKEN/GITHUB_TOKEN env var is already present in its own startup
environment: `bootstrap_gh_token()` runs `gh auth token --hostname <host>`
against the *ambient* `gh` credential store, fails closed (never PASS) on
any non-zero exit / empty output / malformed output, and the resolved token
is used only in-memory -- never printed, logged, or included in any
diagnostic output, IPC result, route artifact, or checked-in doc/fixture.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SECRET_SCANNER_PATH = Path(__file__).resolve().parents[3] / "scripts" / "secret_exposure_scanner.py"

# Token-shaped sentinel matching the broker's own `_TOKEN_SHAPE_RE`
# (`gh[pousr]_...`) so redaction-before-truncate logic is genuinely
# exercised, but never a real credential.
TOKEN_SHAPED_SENTINEL = "gho_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # noqa: S105 - test fixture


def _load(name: str, filename: str) -> types.ModuleType:
    path = _SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _load_secret_scanner() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("secret_exposure_scanner_under_test", _SECRET_SCANNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["secret_exposure_scanner_under_test"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture()
def broker():
    return _load("run_agy_github_research_broker_credbootstrap", "run_agy_github_research_broker.py")


@pytest.fixture()
def e2e(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    module = _load(f"run_agy_github_research_e2e_credbootstrap_{id(tmp_path)}", "run_agy_github_research_e2e.py")
    monkeypatch.setattr(module, "_agy_version_and_permission_gate", lambda _bin: (True, None, {}))
    return module


def _write_fake_gh(
    tmp_path: Path,
    *,
    name: str = "fake-gh",
    token_line: str = TOKEN_SHAPED_SENTINEL,
    exit_code: int = 0,
    log_path: Path | None = None,
) -> Path:
    """A fake `gh` CLI: `auth token --hostname <host>` prints *token_line*
    and exits *exit_code*; any other subcommand (the research command)
    prints a harmless line and exits 0. If *log_path* is given, every
    invocation appends its own `$GH_CONFIG_DIR` value to it (used to prove
    isolation between the bootstrap call and the research call)."""
    script = tmp_path / name
    log_line = f'echo "$GH_CONFIG_DIR" >> "{log_path}"\n' if log_path is not None else ""
    script.write_text(
        "#!/bin/sh\n"
        f"{log_line}"
        'if [ "$1" = "auth" ] && [ "$2" = "token" ]; then\n'
        f'  echo "{token_line}"\n'
        f"  exit {exit_code}\n"
        "fi\n"
        'echo "fake gh research output"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


# ---------------------------------------------------------------------------
# AC3: credential bootstrap pins an explicit --hostname
# ---------------------------------------------------------------------------


def test_ac3_credential_bootstrap_pins_explicit_hostname(broker, tmp_path):
    argv_log = tmp_path / "argv.log"
    script = tmp_path / "recording-gh"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{argv_log}"\n'
        f'echo "{TOKEN_SHAPED_SENTINEL}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    token, reason = broker.bootstrap_gh_token(host="github.com", gh_bin=str(script))
    assert reason is None
    assert token == TOKEN_SHAPED_SENTINEL

    recorded_argv = argv_log.read_text().strip()
    assert "--hostname" in recorded_argv
    assert "github.com" in recorded_argv
    # Never omitted, and never influenced by any caller-supplied "host" param
    # smuggled through operation params (Issue #2012 external_spec_resolution:
    # --hostname is this route's own deterministic host-binding requirement).
    assert recorded_argv.index("--hostname") < len(recorded_argv)


def test_ac3_credential_bootstrap_hostname_is_never_omitted_for_alternate_host(broker, tmp_path):
    argv_log = tmp_path / "argv2.log"
    script = tmp_path / "recording-gh-2"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{argv_log}"\n'
        f'echo "{TOKEN_SHAPED_SENTINEL}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    broker.bootstrap_gh_token(host="github.example.com", gh_bin=str(script))
    recorded_argv = argv_log.read_text().strip()
    assert "--hostname github.example.com" in recorded_argv


# ---------------------------------------------------------------------------
# AC4: credential bootstrap failure is a structured fail/SKIP, never PASS
# ---------------------------------------------------------------------------


def test_ac4_credential_bootstrap_failure_is_skip_not_pass(broker, tmp_path):
    # (1) gh CLI entirely unavailable.
    token, reason = broker.bootstrap_gh_token(host="github.com", gh_bin=str(tmp_path / "does-not-exist"))
    assert token is None
    assert reason == broker.REASON_CREDENTIAL_BOOTSTRAP_GH_CLI_UNAVAILABLE

    # (2) `gh auth token` exits non-zero (stored credential unavailable).
    nonzero_script = tmp_path / "nonzero-gh"
    nonzero_script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    nonzero_script.chmod(0o755)
    token, reason = broker.bootstrap_gh_token(host="github.com", gh_bin=str(nonzero_script))
    assert token is None
    assert reason == broker.REASON_CREDENTIAL_BOOTSTRAP_NONZERO_EXIT

    # (3) `gh auth token` exits zero but prints nothing (empty token).
    empty_script = tmp_path / "empty-gh"
    empty_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    empty_script.chmod(0o755)
    token, reason = broker.bootstrap_gh_token(host="github.com", gh_bin=str(empty_script))
    assert token is None
    assert reason == broker.REASON_CREDENTIAL_BOOTSTRAP_EMPTY_TOKEN

    # (4) `gh auth token` prints malformed (multi-line / whitespace-embedded)
    # output instead of a single bare token line.
    malformed_script = tmp_path / "malformed-gh"
    malformed_script.write_text(
        "#!/bin/sh\necho 'not a token'\necho 'second line'\nexit 0\n", encoding="utf-8"
    )
    malformed_script.chmod(0o755)
    token, reason = broker.bootstrap_gh_token(host="github.com", gh_bin=str(malformed_script))
    assert token is None
    assert reason == broker.REASON_CREDENTIAL_BOOTSTRAP_MALFORMED_OUTPUT

    # No failure branch ever fabricates/guesses a usable token value.
    for reason_value in (
        broker.REASON_CREDENTIAL_BOOTSTRAP_GH_CLI_UNAVAILABLE,
        broker.REASON_CREDENTIAL_BOOTSTRAP_NONZERO_EXIT,
        broker.REASON_CREDENTIAL_BOOTSTRAP_EMPTY_TOKEN,
        broker.REASON_CREDENTIAL_BOOTSTRAP_MALFORMED_OUTPUT,
    ):
        assert isinstance(reason_value, str) and reason_value


def test_ac4_credential_bootstrap_timeout_is_classified_and_cleans_up_process_group(broker, tmp_path):
    """Issue #2036 P1-2b fix_delta: a dedicated test for the
    `credential_bootstrap_timeout` branch (previously only CLI-unavailable /
    nonzero-exit / empty-output / malformed-output were exercised here).
    Confirms it classifies as `credential_unavailable` (Issue #2036 AC4
    fix_delta -- ties into the failure-classification work) and that the
    entire process group, including a grandchild, is reaped rather than
    left as an orphan (P0-3)."""
    marker = tmp_path / "grandchild-pid-marker"
    hang = tmp_path / "hang-auth-token.sh"
    hang.write_text(
        "#!/usr/bin/env bash\n"
        "( while true; do sleep 100; done ) &\n"
        f'echo $! > "{marker}"\n'
        "sleep 100\n",
        encoding="utf-8",
    )
    hang.chmod(0o755)

    token, reason = broker.bootstrap_gh_token(host="github.com", gh_bin=str(hang), timeout_seconds=1)
    assert token is None
    assert reason == broker.REASON_CREDENTIAL_BOOTSTRAP_TIMEOUT
    assert broker.classify_broker_failure_reason(reason) == broker.FAILURE_CLASS_CREDENTIAL_UNAVAILABLE

    # Process-group cleanup: the grandchild spawned by the hung `gh auth
    # token` script must have been reaped (TERM-then-KILL escalation of the
    # whole process group), not left running as an orphan.
    import os as _os
    import time as _time

    _time.sleep(0.5)
    grandchild_pid = int(marker.read_text().strip())
    with pytest.raises(ProcessLookupError):
        _os.kill(grandchild_pid, 0)


def test_ac4_execute_cli_reports_structured_fail_not_pass_on_bootstrap_failure(broker, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    nonzero_script = tmp_path / "nonzero-gh"
    nonzero_script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    nonzero_script.chmod(0o755)

    exit_code = broker.main(["execute", "get_repo", "--gh-bin", str(nonzero_script)])
    assert exit_code == 5

    captured_stdout = capsys.readouterr().out
    payload = json.loads(captured_stdout.strip())
    assert payload["ok"] is False
    assert payload["reason"] == broker.REASON_CREDENTIAL_BOOTSTRAP_NONZERO_EXIT
    assert "token" not in payload
    assert TOKEN_SHAPED_SENTINEL not in captured_stdout


# ---------------------------------------------------------------------------
# AC5: isolated GH_CONFIG_DIR (research command) stays fresh/empty even
# after a credential-bootstrap step that used the *ambient* gh config.
# ---------------------------------------------------------------------------


def test_ac5_isolated_gh_config_dir_still_fresh_after_credential_bootstrap(broker, tmp_path, monkeypatch):
    ambient_gh_config_dir = tmp_path / "ambient-gh-config"
    ambient_gh_config_dir.mkdir()
    marker = ambient_gh_config_dir / "hosts.yml"
    marker.write_text("github.com:\n    oauth_token: unrelated-fixture-marker\n", encoding="utf-8")
    monkeypatch.setenv("GH_CONFIG_DIR", str(ambient_gh_config_dir))

    seen_config_dirs_log = tmp_path / "seen-gh-config-dirs.log"
    fake_gh = _write_fake_gh(tmp_path, log_path=seen_config_dirs_log)

    # Bootstrap runs with the *ambient* environment (env=None -> inherits
    # this test process's GH_CONFIG_DIR, i.e. the fixture's ambient dir).
    token, reason = broker.bootstrap_gh_token(host="github.com", gh_bin=str(fake_gh))
    assert reason is None
    assert token == TOKEN_SHAPED_SENTINEL

    # The subsequent research command execution must use a fresh, isolated,
    # empty GH_CONFIG_DIR -- never the ambient one just used for bootstrap.
    record = broker.execute_operation("get_repo", {}, gh_token=token, gh_bin=str(fake_gh))
    assert record["exit_code"] == 0

    seen_dirs = [line for line in seen_config_dirs_log.read_text().splitlines() if line]
    assert len(seen_dirs) == 2
    bootstrap_seen_dir, research_seen_dir = seen_dirs
    assert bootstrap_seen_dir == str(ambient_gh_config_dir)
    assert research_seen_dir != str(ambient_gh_config_dir)
    assert "agy-github-research-broker-" in research_seen_dir

    # The ambient credential store is never touched by the broker.
    assert marker.read_text() == "github.com:\n    oauth_token: unrelated-fixture-marker\n"


# ---------------------------------------------------------------------------
# AC6: the (bootstrapped) token never appears in stdout/stderr/IPC
# result/route artifact/checked-in docs/test fixtures, verified both by a
# token-shaped-sentinel hermetic test and by the static secret scanner.
# ---------------------------------------------------------------------------


def test_ac6_bootstrap_token_never_appears_in_ipc_argv_stdout_stderr_or_artifact(e2e, monkeypatch, tmp_path):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    fake_gh = _write_fake_gh(tmp_path)
    monkeypatch.setattr(e2e, "_resolve_agy_binary", lambda: "/usr/bin/agy")
    monkeypatch.setattr(e2e, "_resolve_gh_binary", lambda: str(fake_gh))

    real_run = subprocess.run
    real_popen = subprocess.Popen
    captured_stdout_chunks: list[str] = []
    captured_stderr_chunks: list[str] = []

    def _spy_run(argv, **kwargs):
        assert all(TOKEN_SHAPED_SENTINEL not in str(item) for item in argv)
        completed = real_run(argv, **kwargs)
        captured_stdout_chunks.append(completed.stdout or "")
        captured_stderr_chunks.append(completed.stderr or "")
        assert TOKEN_SHAPED_SENTINEL not in (completed.stdout or "")
        assert TOKEN_SHAPED_SENTINEL not in (completed.stderr or "")
        return completed

    class _SpyPopen(real_popen):
        def __init__(self, argv, **kwargs):
            assert all(TOKEN_SHAPED_SENTINEL not in str(item) for item in argv)
            is_broker_invocation = str(e2e._BROKER_SCRIPT_PATH) in argv
            if is_broker_invocation:
                # The broker-subprocess spawn itself always carries an
                # explicit, scrubbed `env=` kwarg (Issue #2036 AC2 fix_delta)
                # -- never the default `env=None` full ambient-env
                # inheritance this used to rely on.
                env_kwarg = kwargs.get("env")
                assert env_kwarg is not None
                assert "GH_TOKEN" not in env_kwarg
                assert "GITHUB_TOKEN" not in env_kwarg
            super().__init__(argv, **kwargs)

        def communicate(self, *args, **kwargs):
            stdout_text, stderr_text = super().communicate(*args, **kwargs)
            captured_stdout_chunks.append(stdout_text or "")
            captured_stderr_chunks.append(stderr_text or "")
            assert TOKEN_SHAPED_SENTINEL not in (stdout_text or "")
            assert TOKEN_SHAPED_SENTINEL not in (stderr_text or "")
            return stdout_text, stderr_text

    monkeypatch.setattr(e2e.subprocess, "run", _spy_run)
    monkeypatch.setattr(e2e.subprocess, "Popen", _SpyPopen)

    monkeypatch.setattr(e2e, "_run_agy_turn", lambda **_kwargs: ('{"action": "stop", "summary": "done"}', ""))
    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )

    assert TOKEN_SHAPED_SENTINEL not in json.dumps(result)
    assert TOKEN_SHAPED_SENTINEL not in "".join(captured_stdout_chunks)
    assert TOKEN_SHAPED_SENTINEL not in "".join(captured_stderr_chunks)

    evidence_path = Path(result["result_surface"]["primary_artifact"])
    evidence_text = evidence_path.read_text()
    assert TOKEN_SHAPED_SENTINEL not in evidence_text


def test_ac6_secret_scanner_reports_zero_findings_on_changed_scripts():
    """Scan all 7 files that are actually part of this Issue's Allowed Paths
    / this PR's changed-file set (Issue #2036 P1-2c fix_delta -- the
    previous 5-file list silently omitted the contract test and the e2e
    test files that are also part of this change)."""
    scanner = _load_secret_scanner()
    changed_relative_paths = [
        ".claude/skills/gemini-cli-headless-delegation/scripts/run_agy_github_research_broker.py",
        ".claude/skills/gemini-cli-headless-delegation/scripts/run_agy_github_research_e2e.py",
        ".claude/skills/gemini-cli-headless-delegation/tests/test_agy_github_research_e2e.py",
        ".claude/skills/gemini-cli-headless-delegation/tests/test_agy_github_research_contract.py",
        ".claude/skills/gemini-cli-headless-delegation/tests/test_agy_github_research_broker_process_boundary.py",
        ".claude/skills/gemini-cli-headless-delegation/tests/test_agy_github_research_credential_bootstrap.py",
        "docs/dev/agy-github-research-gh-token-provisioning.md",
    ]
    all_findings: list[dict[str, object]] = []
    for relative_path in changed_relative_paths:
        target = _REPO_ROOT / relative_path
        assert target.is_file(), f"expected changed file missing: {relative_path}"
        all_findings.extend(scanner.scan_file(target, _REPO_ROOT))
    assert all_findings == []


# ---------------------------------------------------------------------------
# AC7(a): credential-available -> pass, credential-unavailable -> skip;
# skip never promoted to pass. (AC7(b) -- the genuine live run recorded in
# docs/dev/agy-github-research-gh-token-provisioning.md -- is out of scope
# for hermetic pytest and is produced by an actual invocation described in
# that doc.)
# ---------------------------------------------------------------------------


def test_ac7_live_credential_resolution_pass_vs_skip_distinction(e2e, monkeypatch):
    def _standard(monkeypatch_: pytest.MonkeyPatch) -> None:
        monkeypatch_.setattr(e2e, "_resolve_agy_binary", lambda: "/usr/bin/agy")
        monkeypatch_.setattr(e2e, "_resolve_gh_binary", lambda: "/usr/bin/gh")
        monkeypatch_.setattr(e2e, "_probe_agy_version", lambda _b: "1.1.10")

    # Credential-available (mocked): preflight probe succeeds, AGY then
    # genuinely drives one `next` turn (an allowed operation the broker
    # executes with exit 0) followed by a normal `stop` -- a genuine
    # positive-path proof of `status: pass`, not merely "not skip" (Issue
    # #2036 P1-2a fix_delta: the previous version of this test asserted
    # only `!= "skip"`, which is also true of a `fail` outcome and does not
    # prove the PR body's actual "credential-available -> pass" claim).
    _standard(monkeypatch)
    monkeypatch.setattr(
        e2e,
        "_execute_via_broker_subprocess",
        lambda *_a, **_k: {
            "schema": e2e.broker.SCHEMA_COMMAND_RESULT,
            "exit_code": 0,
            "argv": ["repo", "view"],
            "redacted_stdout_sample": "ok",
            "redacted_stderr_sample": "",
            "redacted_output_digest": "sha256:x",
            "truncated": False,
            "timed_out": False,
            "output_limit_exceeded": False,
            "duration_ms": 5,
        },
    )
    turns = iter(
        [
            ('{"action": "next", "operation": "get_repo", "params": {}}', ""),
            ('{"action": "stop", "summary": "done"}', ""),
        ]
    )
    monkeypatch.setattr(e2e, "_run_agy_turn", lambda **_kwargs: next(turns))
    pass_result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    assert pass_result["exit_code"] == 0
    assert pass_result["ok"] is True
    pass_evidence = json.loads(Path(pass_result["result_surface"]["primary_artifact"]).read_text())
    assert pass_evidence["status"] == "pass"
    assert pass_evidence["close_evidence"]["positive_run"]["observed"] is True
    assert pass_evidence["close_evidence"]["positive_run"]["exit_code"] == 0

    # Credential-unavailable (mocked): the broker subprocess denies the
    # preflight probe entirely -> status skip, exit_code 77, never "pass".
    def _deny(*_a, **_k):
        raise e2e.broker.BrokerDenied("credential_bootstrap_gh_cli_unavailable")

    monkeypatch.setattr(e2e, "_execute_via_broker_subprocess", _deny)
    skip_result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    assert skip_result["exit_code"] == 77
    assert skip_result["ok"] is False
    skip_evidence = json.loads(Path(skip_result["result_surface"]["primary_artifact"]).read_text())
    assert skip_evidence["status"] == "skip"
    assert skip_evidence["status"] != "pass"


# ---------------------------------------------------------------------------
# Issue #2036 P1-1: fixed GH_TOKEN > GITHUB_TOKEN > stored_credential
# precedence, with explicit provenance (`credential_source`) and the
# stored-credential path's own child env stripped of both token env vars so
# `gh auth token` can never silently pick one up and be mislabeled.
# ---------------------------------------------------------------------------


def test_p1_1_gh_token_only_resolves_via_gh_token_env(broker, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh-token-value")  # noqa: S105 - test fixture
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def _boom(*_a, **_k):
        raise AssertionError("bootstrap_gh_token() must never run when GH_TOKEN is present")

    monkeypatch.setattr(broker, "bootstrap_gh_token", _boom)

    token, reason, bootstrap_used, credential_source = broker.resolve_gh_token()
    assert token == "gh-token-value"
    assert reason is None
    assert bootstrap_used is False
    assert credential_source == broker.CREDENTIAL_SOURCE_GH_TOKEN_ENV


def test_p1_1_github_token_only_resolves_via_github_token_env(broker, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "github-token-value")  # noqa: S105 - test fixture

    def _boom(*_a, **_k):
        raise AssertionError("bootstrap_gh_token() must never run when GITHUB_TOKEN is present")

    monkeypatch.setattr(broker, "bootstrap_gh_token", _boom)

    token, reason, bootstrap_used, credential_source = broker.resolve_gh_token()
    assert token == "github-token-value"
    assert reason is None
    assert bootstrap_used is False
    assert credential_source == broker.CREDENTIAL_SOURCE_GITHUB_TOKEN_ENV


def test_p1_1_gh_token_wins_when_both_present(broker, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh-token-value")  # noqa: S105 - test fixture
    monkeypatch.setenv("GITHUB_TOKEN", "github-token-value")  # noqa: S105 - test fixture

    token, reason, bootstrap_used, credential_source = broker.resolve_gh_token()
    assert token == "gh-token-value"
    assert reason is None
    assert bootstrap_used is False
    assert credential_source == broker.CREDENTIAL_SOURCE_GH_TOKEN_ENV


def test_p1_1_neither_present_falls_through_to_stored_credential(broker, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    captured_bootstrap_kwargs: dict[str, object] = {}

    def _fake_bootstrap(*, host, gh_bin, env=None, **_kwargs):  # noqa: ARG001
        captured_bootstrap_kwargs["env"] = env
        return TOKEN_SHAPED_SENTINEL, None

    monkeypatch.setattr(broker, "bootstrap_gh_token", _fake_bootstrap)

    token, reason, bootstrap_used, credential_source = broker.resolve_gh_token()
    assert token == TOKEN_SHAPED_SENTINEL
    assert reason is None
    assert bootstrap_used is True
    assert credential_source == broker.CREDENTIAL_SOURCE_STORED_CREDENTIAL

    # The stored-credential path's own child env has both token env vars
    # explicitly stripped -- it can never silently pick either up and be
    # mislabeled as a "stored_credential" resolution (Issue #2036 P1-1).
    stripped_env = captured_bootstrap_kwargs["env"]
    assert stripped_env is not None
    assert "GH_TOKEN" not in stripped_env
    assert "GITHUB_TOKEN" not in stripped_env


def test_p1_1_github_token_env_only_never_silently_resolved_via_gh_auth_token(broker, monkeypatch, tmp_path):
    """Regression for the provenance bug this fix_delta corrects: a
    `GITHUB_TOKEN`-only environment must resolve via the fixed
    `github_token_env` precedence step directly, not by falling through to
    `bootstrap_gh_token()` (whose `gh auth token` invocation could itself
    internally resolve `GITHUB_TOKEN`, mislabeling the source)."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SHAPED_SENTINEL)

    def _boom(*_a, **_k):
        raise AssertionError("bootstrap_gh_token() must never run when GITHUB_TOKEN is present")

    monkeypatch.setattr(broker, "bootstrap_gh_token", _boom)

    token, _reason, _bootstrap_used, credential_source = broker.resolve_gh_token()
    assert token == TOKEN_SHAPED_SENTINEL
    assert credential_source == broker.CREDENTIAL_SOURCE_GITHUB_TOKEN_ENV
