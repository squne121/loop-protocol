"""Security-boundary regressions for Issue #1511's pnpm package-script gates."""

from __future__ import annotations

import importlib.util
import json
import sys
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

    monkeypatch.setattr(baseline.subprocess, "run", forbidden)
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
    monkeypatch.setattr(baseline.subprocess, "run", lambda *a, **k: calls.append(list(a[0])))
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

    class Result:
        returncode = 1
        stdout = ""
        stderr = "ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY"

    def fake_run(argv, **kwargs):
        captured.append({"argv": argv, "env": kwargs["env"]})
        return Result()

    monkeypatch.setattr(baseline.subprocess, "run", fake_run)
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
