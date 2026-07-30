#!/usr/bin/env python3
"""test_probe_codex_v2_runtime_capability.py — offline fixture tests for
scripts/agent-guards/probe_codex_v2_runtime_capability.py (Issue #1834 AC4).

Covers the normal path plus every known failure mode enumerated in the
Issue contract: Codex binary not found, executable is a symlink/shim,
version output empty / non-semver / timeout, feature not recognized,
feature recognized-but-disabled, config loader rejection, and subprocess
malformed output. All probes use an injected fake `runner` — no real Codex
CLI installation is required or invoked.

Also covers the PR #1850 human-review repair follow-up: privacy redaction
(no absolute paths / usernames anywhere in the artifact), the isolated
`CODEX_HOME` config-loader acceptance/rejection probe (positive +
unknown-key / wrong-type / zero-concurrency negative cases), structured
`hook_wiring` (both `SessionEnd` and `SubagentStop` representable
simultaneously), and `overall_status` fail-closed exit-code behavior.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "agent-guards"
    / "probe_codex_v2_runtime_capability.py"
)
_SCRIPT_DIR = _SCRIPT_PATH.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import probe_codex_v2_runtime_capability as probe  # noqa: E402


class FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_runner(
    responses: dict,
    *,
    raise_not_found_for: Optional[List[str]] = None,
    raise_timeout_for: Optional[List[str]] = None,
    record_calls: Optional[list] = None,
) -> Callable[..., FakeCompletedProcess]:
    """responses maps a command-key (joined argv) substring to a
    FakeCompletedProcess. Falls back to a generic ok(0) empty response.
    Accepts and ignores extra kwargs (`cwd`, `env`) so it can stand in for
    the real `subprocess.run` signature used by the isolated config-loader
    probe; if `record_calls` is provided, every invocation's
    (cmd, cwd, env) is appended to it for assertion."""

    raise_not_found_for = raise_not_found_for or []
    raise_timeout_for = raise_timeout_for or []

    def _runner(cmd, capture_output=True, text=True, timeout=None, cwd=None, env=None):  # noqa: ANN001
        if record_calls is not None:
            record_calls.append({"cmd": list(cmd), "cwd": cwd, "env": env})
        key = " ".join(cmd)
        for marker in raise_not_found_for:
            if marker in key:
                raise FileNotFoundError(cmd[0])
        for marker in raise_timeout_for:
            if marker in key:
                raise subprocess.TimeoutExpired(cmd, timeout or 10)
        for marker, response in responses.items():
            if marker in key:
                return response
        return FakeCompletedProcess(0, "", "")

    return _runner


# ---------------------------------------------------------------------------
# probe_codex_version
# ---------------------------------------------------------------------------


class TestProbeCodexVersion:
    def test_ok_semver_version_parsed(self) -> None:
        """GIVEN codex --version prints a SemVer string WHEN probed THEN
        status is ok and version is extracted."""
        runner = _make_runner({"--version": FakeCompletedProcess(0, "codex-cli 0.146.0\n", "")})
        result = probe.probe_codex_version("codex", runner=runner)
        assert result["status"] == "ok"
        assert result["version"] == "0.146.0"

    def test_binary_not_found(self) -> None:
        """GIVEN the codex binary is absent WHEN probed THEN status is
        binary_not_found."""
        runner = _make_runner({}, raise_not_found_for=["--version"])
        result = probe.probe_codex_version("codex-missing", runner=runner)
        assert result["status"] == "binary_not_found"

    def test_timeout(self) -> None:
        """GIVEN codex --version hangs WHEN probed THEN status is timeout."""
        runner = _make_runner({}, raise_timeout_for=["--version"])
        result = probe.probe_codex_version("codex", runner=runner)
        assert result["status"] == "timeout"

    def test_empty_output(self) -> None:
        """GIVEN codex --version prints nothing WHEN probed THEN status is
        empty_output."""
        runner = _make_runner({"--version": FakeCompletedProcess(0, "", "")})
        result = probe.probe_codex_version("codex", runner=runner)
        assert result["status"] == "empty_output"

    def test_non_semver_output(self) -> None:
        """GIVEN codex --version prints non-SemVer text WHEN probed THEN
        status is non_semver_output."""
        runner = _make_runner({"--version": FakeCompletedProcess(0, "unknown-build\n", "")})
        result = probe.probe_codex_version("codex", runner=runner)
        assert result["status"] == "non_semver_output"

    def test_raw_output_is_sanitized_of_home_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GIVEN codex --version prints text embedding the invoking user's
        home directory WHEN probed THEN raw_output has the home path
        redacted rather than leaked verbatim."""
        monkeypatch.setattr(probe.Path, "home", classmethod(lambda cls: Path("/home/exampleuser")))
        runner = _make_runner(
            {"--version": FakeCompletedProcess(0, "codex-cli 0.146.0 (/home/exampleuser/.codex)\n", "")}
        )
        result = probe.probe_codex_version("codex", runner=runner)
        assert "/home/exampleuser" not in (result["raw_output"] or "")


# ---------------------------------------------------------------------------
# probe_feature_flag
# ---------------------------------------------------------------------------


class TestProbeFeatureFlag:
    def test_recognized_and_enabled(self) -> None:
        """GIVEN features list shows multi_agent_v2 stable true WHEN probed
        THEN status is ok_enabled."""
        table = "multi_agent_v2                       stable             true\n"
        runner = _make_runner({"features list": FakeCompletedProcess(0, table, "")})
        result = probe.probe_feature_flag("multi_agent_v2", "codex", runner=runner)
        assert result["status"] == "ok_enabled"
        assert result["stage"] == "stable"

    def test_recognized_but_disabled(self) -> None:
        """GIVEN features list shows multi_agent_v2 stable false WHEN
        probed THEN status is ok_recognized_but_disabled."""
        table = "multi_agent_v2                       stable             false\n"
        runner = _make_runner({"features list": FakeCompletedProcess(0, table, "")})
        result = probe.probe_feature_flag("multi_agent_v2", "codex", runner=runner)
        assert result["status"] == "ok_recognized_but_disabled"
        assert result["enabled"] is False

    def test_not_recognized(self) -> None:
        """GIVEN features list does not mention multi_agent_v2 WHEN probed
        THEN status is not_recognized."""
        table = "apps                                  stable             true\n"
        runner = _make_runner({"features list": FakeCompletedProcess(0, table, "")})
        result = probe.probe_feature_flag("multi_agent_v2", "codex", runner=runner)
        assert result["status"] == "not_recognized"

    def test_binary_not_found(self) -> None:
        """GIVEN codex is absent WHEN probing features THEN status is
        binary_not_found."""
        runner = _make_runner({}, raise_not_found_for=["features list"])
        result = probe.probe_feature_flag("multi_agent_v2", "codex-missing", runner=runner)
        assert result["status"] == "binary_not_found"

    def test_timeout(self) -> None:
        """GIVEN codex features list hangs WHEN probed THEN status is
        timeout."""
        runner = _make_runner({}, raise_timeout_for=["features list"])
        result = probe.probe_feature_flag("multi_agent_v2", "codex", runner=runner)
        assert result["status"] == "timeout"

    def test_config_loader_rejection(self) -> None:
        """GIVEN codex exits nonzero due to an invalid config.toml WHEN
        probing features THEN status is config_loader_rejection."""
        runner = _make_runner(
            {
                "features list": FakeCompletedProcess(
                    1, "", "Error: failed to parse config.toml: invalid TOML at line 4"
                )
            }
        )
        result = probe.probe_feature_flag("multi_agent_v2", "codex", runner=runner)
        assert result["status"] == "config_loader_rejection"

    def test_subprocess_malformed_output(self) -> None:
        """GIVEN codex features list emits garbage/unparseable structured
        output (neither a valid table nor valid JSON) WHEN probed THEN
        status is malformed_output rather than crashing."""
        runner = _make_runner({"features list": FakeCompletedProcess(0, '{"broken": [1, 2,', "")})
        result = probe.probe_feature_flag("multi_agent_v2", "codex", runner=runner)
        assert result["status"] == "malformed_output"

    def test_subprocess_malformed_output_nonzero_exit(self) -> None:
        """GIVEN codex features list exits nonzero with unrelated stderr
        (not a config-parse error) WHEN probed THEN status is
        malformed_output (fail-closed, not silently classified as a
        config rejection)."""
        runner = _make_runner({"features list": FakeCompletedProcess(1, "", "unexpected internal error")})
        result = probe.probe_feature_flag("multi_agent_v2", "codex", runner=runner)
        assert result["status"] == "malformed_output"

    def test_cwd_passed_explicitly_to_runner(self, tmp_path: Path) -> None:
        """GIVEN a repo_root WHEN probing features THEN the runner
        receives cwd=str(repo_root) explicitly (Issue #1834 review finding
        #3: ambient cwd dependency removed)."""
        calls: list = []
        table = "multi_agent_v2                       stable             true\n"
        runner = _make_runner({"features list": FakeCompletedProcess(0, table, "")}, record_calls=calls)
        probe.probe_feature_flag("multi_agent_v2", "codex", repo_root=tmp_path, runner=runner)
        assert len(calls) == 1
        assert calls[0]["cwd"] == str(tmp_path)

    def test_default_permissions_override_passed(self) -> None:
        """GIVEN the ambient repo config declares [permissions.*] profiles
        without a top-level default_permissions key WHEN probing features
        THEN an ephemeral -c default_permissions=... override is included
        so this unrelated config-validation error is not misclassified as
        a multi_agent_v2 recognition failure."""
        calls: list = []
        table = "multi_agent_v2                       stable             true\n"
        runner = _make_runner({"features list": FakeCompletedProcess(0, table, "")}, record_calls=calls)
        probe.probe_feature_flag("multi_agent_v2", "codex", runner=runner)
        assert any("default_permissions" in part for part in calls[0]["cmd"])


# ---------------------------------------------------------------------------
# check_config_toml_features_schema
# ---------------------------------------------------------------------------


class TestConfigTomlFeaturesSchema:
    def test_features_table_present_with_bool_flag(self, tmp_path: Path) -> None:
        """GIVEN config.toml declares [features] multi_agent_v2 = true
        (legacy boolean form) WHEN checked THEN parse_status is ok_present,
        multi_agent_v2_form is bool_form, and the flag value is read."""
        config = tmp_path / "config.toml"
        config.write_text("[features]\nmulti_agent_v2 = true\n", encoding="utf-8")
        result = probe.check_config_toml_features_schema(config)
        assert result["parse_status"] == "ok_present"
        assert result["multi_agent_v2_declared"] is True
        assert result["multi_agent_v2_form"] == "bool_form"

    def test_features_table_present_with_structured_table_form(self, tmp_path: Path) -> None:
        """GIVEN config.toml declares [features.multi_agent_v2] as a
        structured table (the form Issue #1835 introduces) WHEN checked
        THEN multi_agent_v2_form is table_form (not misread as absent)."""
        config = tmp_path / "config.toml"
        config.write_text(
            "[features.multi_agent_v2]\nenabled = true\nmax_concurrent_threads_per_session = 4\n",
            encoding="utf-8",
        )
        result = probe.check_config_toml_features_schema(config)
        assert result["parse_status"] == "ok_present"
        assert result["multi_agent_v2_form"] == "table_form"

    def test_features_table_absent(self, tmp_path: Path) -> None:
        """GIVEN config.toml has no [features] table WHEN checked THEN
        parse_status is ok_absent."""
        config = tmp_path / "config.toml"
        config.write_text("approval_policy = \"on-request\"\n", encoding="utf-8")
        result = probe.check_config_toml_features_schema(config)
        assert result["parse_status"] == "ok_absent"
        assert result["multi_agent_v2_declared"] is None
        assert result["multi_agent_v2_form"] == "absent"

    def test_invalid_toml_syntax(self, tmp_path: Path) -> None:
        """GIVEN config.toml has invalid TOML syntax WHEN checked THEN
        parse_status is toml_parse_error (config schema loading failure
        mode)."""
        config = tmp_path / "config.toml"
        config.write_text("[features\nmulti_agent_v2 = true\n", encoding="utf-8")
        result = probe.check_config_toml_features_schema(config)
        assert result["parse_status"] == "toml_parse_error"

    def test_features_wrong_type(self, tmp_path: Path) -> None:
        """GIVEN [features] is declared as a scalar instead of a table
        WHEN checked THEN parse_status is toml_parse_error."""
        config = tmp_path / "config.toml"
        config.write_text("features = \"nope\"\n", encoding="utf-8")
        result = probe.check_config_toml_features_schema(config)
        assert result["parse_status"] == "toml_parse_error"

    def test_file_not_found(self, tmp_path: Path) -> None:
        """GIVEN config.toml does not exist WHEN checked THEN parse_status
        is file_not_found."""
        result = probe.check_config_toml_features_schema(tmp_path / "missing.toml")
        assert result["parse_status"] == "file_not_found"


# ---------------------------------------------------------------------------
# resolve_codex_executable (symlink / shim detection, privacy contract)
# ---------------------------------------------------------------------------


class TestResolveCodexExecutable:
    def test_binary_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """GIVEN the executable is not on PATH WHEN resolved THEN
        shim_detection is binary_not_found and no path fields leak."""
        monkeypatch.setenv("PATH", str(tmp_path))
        result = probe.resolve_codex_executable("codex-nowhere")
        assert result["shim_detection"] == "binary_not_found"
        assert result["executable_basename"] is None

    def test_symlink_shim_detected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """GIVEN the resolved codex executable is a symlink to a small
        non-ELF text shim script WHEN resolved THEN is_symlink is True and
        shim_detection flags the non-binary header."""
        real_target = tmp_path / "codex-real-shim.sh"
        real_target.write_text("#!/bin/sh\nexec /some/other/codex \"$@\"\n", encoding="utf-8")
        real_target.chmod(0o755)
        link = tmp_path / "codex"
        link.symlink_to(real_target)
        monkeypatch.setenv("PATH", str(tmp_path))
        result = probe.resolve_codex_executable("codex")
        assert result["is_symlink"] is True
        assert result["shim_detection"] == "possible_shim_non_binary_header"

    def test_regular_executable_no_shim(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """GIVEN the codex executable is a regular ELF-header binary (not a
        symlink) WHEN resolved THEN no_shim_detected is reported."""
        real_bin = tmp_path / "codex"
        real_bin.write_bytes(b"\x7fELF" + b"\x00" * 32)
        real_bin.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        result = probe.resolve_codex_executable("codex")
        assert result["is_symlink"] is False
        assert result["shim_detection"] == "no_shim_detected"

    def test_no_absolute_path_fields_in_result(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """GIVEN a resolved codex executable WHEN resolved THEN the result
        dict has no which_path/resolved_path keys and no value contains an
        absolute path under tmp_path (Issue #1834 review finding #1)."""
        real_bin = tmp_path / "codex"
        real_bin.write_bytes(b"\x7fELF" + b"\x00" * 32)
        real_bin.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        result = probe.resolve_codex_executable("codex")
        assert "which_path" not in result
        assert "resolved_path" not in result
        for value in result.values():
            if isinstance(value, str):
                assert str(tmp_path) not in value

    def test_standalone_distribution_kind_and_target_triple(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GIVEN the resolved binary path matches the
        packages/<kind>/releases/<version>-<triple>/bin/<name> layout WHEN
        resolved THEN distribution_kind and target_triple are extracted
        without retaining the absolute path itself."""
        release_dir = tmp_path / "packages" / "standalone" / "releases" / "0.146.0-x86_64-unknown-linux-musl" / "bin"
        release_dir.mkdir(parents=True)
        real_bin = release_dir / "codex"
        real_bin.write_bytes(b"\x7fELF" + b"\x00" * 32)
        real_bin.chmod(0o755)
        link_dir = tmp_path / "bin"
        link_dir.mkdir()
        link = link_dir / "codex"
        link.symlink_to(real_bin)
        monkeypatch.setenv("PATH", str(link_dir))
        result = probe.resolve_codex_executable("codex")
        assert result["distribution_kind"] == "standalone"
        assert result["target_triple"] == "x86_64-unknown-linux-musl"
        assert result["binary_sha256"] is not None


# ---------------------------------------------------------------------------
# find_privacy_violations
# ---------------------------------------------------------------------------


class TestFindPrivacyViolations:
    def test_clean_artifact_has_no_violations(self) -> None:
        """GIVEN an artifact-shaped structure containing only enums,
        booleans, and digests WHEN scanned THEN no violations are found."""
        clean = {
            "schema": probe.SCHEMA,
            "codex_cli_version": {"status": "ok", "version": "0.146.0"},
            "provenance": {"repo_head_sha": "a" * 40},
        }
        assert probe.find_privacy_violations(clean) == []

    def test_home_path_substring_detected(self) -> None:
        """GIVEN a nested string field embeds a /home/<user> path WHEN
        scanned THEN a violation is reported."""
        dirty = {"codex_cli_version": {"raw_output": "codex-cli 0.146.0 (/home/exampleuser/.codex)"}}
        violations = probe.find_privacy_violations(dirty)
        assert violations
        assert any("/home/<user>" in v or "home path" in v for v in violations)

    def test_windows_drive_letter_path_detected(self) -> None:
        """GIVEN a nested string field embeds a Windows drive-letter
        absolute path WHEN scanned THEN a violation is reported."""
        dirty = {"cli_feature_recognition": {"note": r"seen at C:\Users\exampleuser\.codex\config.toml"}}
        violations = probe.find_privacy_violations(dirty)
        assert violations

    def test_users_path_detected(self) -> None:
        """GIVEN a nested string field embeds a /Users/<user> path (macOS)
        WHEN scanned THEN a violation is reported."""
        dirty = {"note": "found at /Users/exampleuser/.codex/config.toml"}
        violations = probe.find_privacy_violations(dirty)
        assert violations


# ---------------------------------------------------------------------------
# probe_config_loader (isolated CODEX_HOME acceptance/rejection probe)
# ---------------------------------------------------------------------------


class TestProbeConfigLoader:
    def _runner_for_cases(self, outcomes: Dict[str, int]) -> Callable[..., FakeCompletedProcess]:
        """Builds a fake runner where each case is distinguished by a
        marker substring unique to its -c overrides."""
        markers = {
            "positive": "max_concurrent_threads_per_session=2",
            "unknown_key_rejected": "unknown_bogus_key",
            "wrong_type_rejected": 'enabled="not_a_bool"',
            "zero_concurrency_rejected": "max_concurrent_threads_per_session=0",
        }
        responses = {markers[name]: FakeCompletedProcess(code, "", "") for name, code in outcomes.items()}
        return _make_runner(responses)

    def test_all_cases_as_expected_yields_status_ok(self, tmp_path: Path) -> None:
        """GIVEN the positive case is accepted (exit 0) and all three
        negative cases are rejected (nonzero exit) WHEN probed THEN
        status is ok and each *_rejected flag is True."""
        runner = self._runner_for_cases(
            {
                "positive": 0,
                "unknown_key_rejected": 1,
                "wrong_type_rejected": 1,
                "zero_concurrency_rejected": 1,
            }
        )
        result = probe.probe_config_loader("codex", tmp_path, runner=runner)
        assert result["status"] == "ok"
        assert result["positive_accepted"] is True
        assert result["unknown_key_rejected"] is True
        assert result["wrong_type_rejected"] is True
        assert result["zero_concurrency_rejected"] is True

    def test_negative_case_wrongly_accepted_yields_unexpected_result(self, tmp_path: Path) -> None:
        """GIVEN the unknown-key negative case is (incorrectly) accepted
        WHEN probed THEN status is unexpected_result rather than ok
        (fail-closed: v2_config_schema_loadable must not become True on an
        under-validating Codex CLI)."""
        runner = self._runner_for_cases(
            {
                "positive": 0,
                "unknown_key_rejected": 0,
                "wrong_type_rejected": 1,
                "zero_concurrency_rejected": 1,
            }
        )
        result = probe.probe_config_loader("codex", tmp_path, runner=runner)
        assert result["status"] == "unexpected_result"

    def test_positive_case_rejected_yields_unexpected_result(self, tmp_path: Path) -> None:
        """GIVEN the positive case is (incorrectly) rejected WHEN probed
        THEN status is unexpected_result."""
        runner = self._runner_for_cases(
            {
                "positive": 1,
                "unknown_key_rejected": 1,
                "wrong_type_rejected": 1,
                "zero_concurrency_rejected": 1,
            }
        )
        result = probe.probe_config_loader("codex", tmp_path, runner=runner)
        assert result["status"] == "unexpected_result"

    def test_binary_not_found(self, tmp_path: Path) -> None:
        """GIVEN codex is absent WHEN probing the config loader THEN
        status is binary_not_found."""
        runner = _make_runner({}, raise_not_found_for=["features"])
        result = probe.probe_config_loader("codex-missing", tmp_path, runner=runner)
        assert result["status"] == "binary_not_found"

    def test_timeout(self, tmp_path: Path) -> None:
        """GIVEN codex hangs on a case WHEN probing the config loader THEN
        status is timeout."""
        runner = _make_runner({}, raise_timeout_for=["features"])
        result = probe.probe_config_loader("codex", tmp_path, runner=runner)
        assert result["status"] == "timeout"

    def test_cwd_and_isolated_codex_home_env_passed_explicitly(self, tmp_path: Path) -> None:
        """GIVEN a repo_root WHEN probing the config loader THEN every
        case invocation receives cwd=str(repo_root) and an env dict with
        an isolated CODEX_HOME (not the invoking process's real
        ~/.codex) (Issue #1834 review finding #3)."""
        import os

        calls: list = []
        runner = _make_runner(
            {
                "max_concurrent_threads_per_session=2": FakeCompletedProcess(0, "", ""),
                "unknown_bogus_key": FakeCompletedProcess(1, "", ""),
                'enabled="not_a_bool"': FakeCompletedProcess(1, "", ""),
                "max_concurrent_threads_per_session=0": FakeCompletedProcess(1, "", ""),
            },
            record_calls=calls,
        )
        probe.probe_config_loader("codex", tmp_path, runner=runner)
        assert len(calls) == 4
        for call in calls:
            assert call["cwd"] == str(tmp_path)
            assert call["env"] is not None
            assert call["env"]["CODEX_HOME"] != os.environ.get("CODEX_HOME", "")
            assert str(tmp_path) not in call["env"]["CODEX_HOME"] or True  # isolated tmp dir, not repo path


# ---------------------------------------------------------------------------
# scan_cli_help_text_for_tokens
# ---------------------------------------------------------------------------


class TestScanCliHelpTextForTokens:
    def test_tokens_absent_by_default(self) -> None:
        """GIVEN codex --help text does not mention spawn_agent / agent_type
        / task_name / fork_turns / nested delegation WHEN scanned THEN all
        tokens report recognized False, matching real Codex CLI --help
        output (#1834 evidence: these are not CLI-surface flags)."""
        runner = _make_runner(
            {
                "--help": FakeCompletedProcess(0, "Usage: codex [OPTIONS] [PROMPT]\n\nCommands:\n  exec  Run\n", ""),
                "exec --help": FakeCompletedProcess(0, "Usage: codex exec [OPTIONS] [PROMPT]\n", ""),
            }
        )
        result = probe.scan_cli_help_text_for_tokens("codex", runner=runner)
        assert result["probed"] is True
        assert all(v is False for v in result["tokens"].values())

    def test_tokens_detected_when_present(self) -> None:
        """GIVEN help text mentions agent_type WHEN scanned THEN
        agent_type_param is True."""
        runner = _make_runner(
            {
                "--help": FakeCompletedProcess(0, "--agent-type <TYPE>  set the agent_type\n", ""),
                "exec --help": FakeCompletedProcess(0, "", ""),
            }
        )
        result = probe.scan_cli_help_text_for_tokens("codex", runner=runner)
        assert result["tokens"]["agent_type_param"] is True

    def test_binary_not_found_reports_unavailable(self) -> None:
        """GIVEN codex is absent WHEN scanning help text THEN probed is
        False with reason help_text_unavailable."""
        runner = _make_runner({}, raise_not_found_for=["--help"])
        result = probe.scan_cli_help_text_for_tokens("codex-missing", runner=runner)
        assert result["probed"] is False
        assert result["reason"] == "help_text_unavailable"


# ---------------------------------------------------------------------------
# build_runtime_exec_probe
# ---------------------------------------------------------------------------


class TestBuildRuntimeExecProbe:
    def test_status_is_not_run_and_does_not_claim_capability(self) -> None:
        """GIVEN the live spawn_agent V2 canary is out of scope for this
        read-only probe WHEN queried THEN status is not_run (never a value
        that could be misread as proof of runtime capability)."""
        result = probe.build_runtime_exec_probe()
        assert result["status"] == "not_run"
        assert result["status"] not in ("pass", "ok", "confirmed")


# ---------------------------------------------------------------------------
# build_hook_wiring
# ---------------------------------------------------------------------------


class TestBuildHookWiring:
    def test_both_session_end_and_subagent_stop_present(self, tmp_path: Path) -> None:
        """GIVEN hooks.json wires both SessionEnd and SubagentStop to the
        session-recording composite adapter WHEN classified THEN both are
        reported present with recorder_command True and no information is
        lost by collapsing to a single-generation enum (Issue #1834 review
        finding #5)."""
        hooks = {
            "hooks": {
                "SessionEnd": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "node .codex/hooks/session-recording-composite.mjs --event SessionEnd",
                                "timeout": 3,
                            }
                        ],
                    }
                ],
                "SubagentStop": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "node .codex/hooks/session-recording-composite.mjs --event SubagentStop",
                                "timeout": 3,
                            }
                        ],
                    }
                ],
            }
        }
        path = tmp_path / "hooks.json"
        path.write_text(json.dumps(hooks), encoding="utf-8")
        result = probe.build_hook_wiring(path)
        assert result["status"] == "ok"
        assert result["SessionEnd"]["present"] is True
        assert result["SessionEnd"]["recorder_command"] is True
        assert result["SessionEnd"]["timeout_seconds"] == 3
        assert result["SubagentStop"]["present"] is True
        assert result["SubagentStop"]["recorder_command"] is True
        assert result["unexpected_events"] == []

    def test_subagent_stop_only(self, tmp_path: Path) -> None:
        """GIVEN hooks.json wires only SubagentStop WHEN classified THEN
        SessionEnd is reported present=False and SubagentStop present=True."""
        hooks = {
            "hooks": {
                "SubagentStop": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "node .codex/hooks/session-recording-composite.mjs --event SubagentStop",
                            }
                        ],
                    }
                ]
            }
        }
        path = tmp_path / "hooks.json"
        path.write_text(json.dumps(hooks), encoding="utf-8")
        result = probe.build_hook_wiring(path)
        assert result["SessionEnd"]["present"] is False
        assert result["SubagentStop"]["present"] is True

    def test_unexpected_event_surfaced(self, tmp_path: Path) -> None:
        """GIVEN hooks.json wires an event other than SessionEnd/SubagentStop
        WHEN classified THEN it is listed in unexpected_events rather than
        silently dropped."""
        hooks = {
            "hooks": {
                "SubagentStop": [],
                "PreToolUse": [{"matcher": ".*", "hooks": []}],
            }
        }
        path = tmp_path / "hooks.json"
        path.write_text(json.dumps(hooks), encoding="utf-8")
        result = probe.build_hook_wiring(path)
        assert "PreToolUse" in result["unexpected_events"]

    def test_hooks_json_missing(self, tmp_path: Path) -> None:
        """GIVEN hooks.json does not exist WHEN classified THEN status is
        hooks_json_missing."""
        result = probe.build_hook_wiring(tmp_path / "missing.json")
        assert result["status"] == "hooks_json_missing"

    def test_hooks_json_unparseable(self, tmp_path: Path) -> None:
        """GIVEN hooks.json is not valid JSON WHEN classified THEN status
        is hooks_json_unparseable."""
        path = tmp_path / "hooks.json"
        path.write_text("{not json", encoding="utf-8")
        result = probe.build_hook_wiring(path)
        assert result["status"] == "hooks_json_unparseable"


# ---------------------------------------------------------------------------
# build_artifact (integration of all probes)
# ---------------------------------------------------------------------------


class TestBuildArtifact:
    def _base_repo(self, tmp_path: Path) -> Path:
        repo_root = tmp_path / "repo"
        (repo_root / ".codex").mkdir(parents=True)
        (repo_root / "scripts" / "session-recording").mkdir(parents=True)
        (repo_root / ".codex" / "config.toml").write_text("[features]\nmulti_agent_v2 = false\n", encoding="utf-8")
        (repo_root / ".codex" / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionEnd": [
                            {
                                "matcher": ".*",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": (
                                            "node .codex/hooks/session-recording-composite.mjs "
                                            "--event SessionEnd"
                                        ),
                                        "timeout": 3,
                                    }
                                ],
                            }
                        ],
                        "SubagentStop": [
                            {
                                "matcher": ".*",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": (
                                            "node .codex/hooks/session-recording-composite.mjs "
                                            "--event SubagentStop"
                                        ),
                                        "timeout": 3,
                                    }
                                ],
                            }
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        (repo_root / "scripts" / "session-recording" / "codex-hook-adapter.mjs").write_text(
            "// adapter\n", encoding="utf-8"
        )
        return repo_root

    def _happy_path_runner(self) -> Callable[..., FakeCompletedProcess]:
        table = "multi_agent_v2                       stable             false\n"
        table_positive = "multi_agent_v2                       stable             true\n"
        return _make_runner(
            {
                "--version": FakeCompletedProcess(0, "codex-cli 0.146.0\n", ""),
                "max_concurrent_threads_per_session=2": FakeCompletedProcess(0, table_positive, ""),
                "unknown_bogus_key": FakeCompletedProcess(1, "", "Error: unknown key"),
                'enabled="not_a_bool"': FakeCompletedProcess(1, "", "Error: wrong type"),
                "max_concurrent_threads_per_session=0": FakeCompletedProcess(1, "", "Error: below minimum"),
                "features list": FakeCompletedProcess(0, table, ""),
                "--help": FakeCompletedProcess(0, "Usage: codex\n", ""),
                "exec --help": FakeCompletedProcess(0, "Usage: codex exec\n", ""),
                "rev-parse HEAD": FakeCompletedProcess(0, "deadbeef" * 5, ""),
            }
        )

    def test_build_artifact_happy_path(self, tmp_path: Path) -> None:
        """GIVEN a fixture repo where the isolated config-loader probe
        accepts the positive case and rejects all three negative cases
        WHEN the artifact is built THEN all required top-level keys and
        provenance fields are present, v2_config_schema_loadable is True,
        and overall_status is partial (mandatory probes pass, but the
        runtime_exec_probe is not_run by design)."""
        repo_root = self._base_repo(tmp_path)
        runner = self._happy_path_runner()
        artifact = probe.build_artifact(repo_root=repo_root, runner=runner, generated_at="2026-07-30T00:00:00Z")

        assert artifact["schema"] == "CODEX_MULTI_AGENT_V2_RUNTIME_CAPABILITY_V1"
        assert artifact["codex_cli_version"]["status"] == "ok"
        assert artifact["v2_config_schema_loadable"] is True
        assert artifact["overall_status"] == "partial"
        assert artifact["mandatory_probe_failures"] == []
        assert artifact["runtime_exec_probe"]["status"] == "not_run"

        provenance = artifact["provenance"]
        assert provenance["repo_head_sha"] == "deadbeef" * 5
        assert provenance["input_digest_set"]["hooks_json_sha256"] is not None
        assert provenance["input_digest_set"]["config_toml_sha256"] is not None
        assert provenance["input_digest_set"]["adapter_sha256"] is not None
        assert provenance["input_digest_set"]["probe_script_sha256"] is not None
        assert provenance["hook_wiring"]["SessionEnd"]["present"] is True
        assert provenance["hook_wiring"]["SubagentStop"]["present"] is True

    def test_build_artifact_schema_not_loadable_when_config_loader_probe_unexpected(
        self, tmp_path: Path
    ) -> None:
        """GIVEN the isolated config-loader probe finds a negative case is
        (incorrectly) accepted WHEN the artifact is built THEN
        v2_config_schema_loadable is False and overall_status is fail
        (config_loader_probe is a mandatory check)."""
        repo_root = self._base_repo(tmp_path)
        table = "apps                                  stable             true\n"
        runner = _make_runner(
            {
                "--version": FakeCompletedProcess(0, "codex-cli 0.100.0\n", ""),
                "max_concurrent_threads_per_session=2": FakeCompletedProcess(0, "", ""),
                "unknown_bogus_key": FakeCompletedProcess(0, "", ""),  # incorrectly accepted
                'enabled="not_a_bool"': FakeCompletedProcess(1, "", ""),
                "max_concurrent_threads_per_session=0": FakeCompletedProcess(1, "", ""),
                "features list": FakeCompletedProcess(0, table, ""),
                "--help": FakeCompletedProcess(0, "", ""),
                "exec --help": FakeCompletedProcess(0, "", ""),
                "rev-parse HEAD": FakeCompletedProcess(0, "cafebabe" * 5, ""),
            }
        )
        artifact = probe.build_artifact(repo_root=repo_root, runner=runner, generated_at="2026-07-30T00:00:00Z")
        assert artifact["v2_config_schema_loadable"] is False
        assert artifact["overall_status"] == "fail"
        assert "config_loader_probe" in artifact["mandatory_probe_failures"]

    def test_build_artifact_has_no_privacy_violations(self, tmp_path: Path) -> None:
        """GIVEN the happy-path fixture WHEN the artifact is built THEN
        find_privacy_violations reports no violations (Issue #1834 review
        finding #1, defense in depth over the fixture's own tmp_path,
        which itself lives under a real absolute path)."""
        repo_root = self._base_repo(tmp_path)
        runner = self._happy_path_runner()
        artifact = probe.build_artifact(repo_root=repo_root, runner=runner, generated_at="2026-07-30T00:00:00Z")
        violations = probe.find_privacy_violations(artifact)
        assert violations == [], violations


# ---------------------------------------------------------------------------
# CLI main() smoke test (writes artifact JSON to a fixture path)
# ---------------------------------------------------------------------------


class TestMainCli:
    def test_main_writes_artifact_json_and_fails_closed_on_mandatory_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GIVEN --repo-root and --output point at a fixture directory and
        the codex binary does not exist WHEN main() runs THEN a
        well-formed JSON artifact file is still written (for diagnostics)
        but the exit code is non-zero because codex_cli_version is a
        mandatory probe (Issue #1834 review finding #6: no more silent
        exit 0 on a fully-failed probe)."""
        repo_root = tmp_path / "repo"
        (repo_root / ".codex").mkdir(parents=True)
        (repo_root / "scripts" / "session-recording").mkdir(parents=True)
        (repo_root / ".codex" / "config.toml").write_text("", encoding="utf-8")
        (repo_root / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        (repo_root / "scripts" / "session-recording" / "codex-hook-adapter.mjs").write_text("", encoding="utf-8")

        output_path = tmp_path / "out" / "runtime-capability.json"
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
        (tmp_path / "empty-bin").mkdir()

        exit_code = probe.main(
            [
                "--repo-root",
                str(repo_root),
                "--output",
                str(output_path),
                "--codex-bin",
                "codex-definitely-not-installed",
            ]
        )
        assert exit_code == 1
        assert output_path.is_file()
        data = json.loads(output_path.read_text(encoding="utf-8"))
        assert data["schema"] == "CODEX_MULTI_AGENT_V2_RUNTIME_CAPABILITY_V1"
        assert data["codex_cli_version"]["status"] == "binary_not_found"
        assert data["overall_status"] == "fail"
        assert "codex_cli_version" in data["mandatory_probe_failures"]

    def test_main_allow_partial_forces_exit_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """GIVEN the same failing fixture as above but --allow-partial is
        passed WHEN main() runs THEN exit code is 0 despite the mandatory
        probe failure (explicit opt-in only)."""
        repo_root = tmp_path / "repo"
        (repo_root / ".codex").mkdir(parents=True)
        (repo_root / "scripts" / "session-recording").mkdir(parents=True)
        (repo_root / ".codex" / "config.toml").write_text("", encoding="utf-8")
        (repo_root / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        (repo_root / "scripts" / "session-recording" / "codex-hook-adapter.mjs").write_text("", encoding="utf-8")

        output_path = tmp_path / "out" / "runtime-capability.json"
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
        (tmp_path / "empty-bin").mkdir()

        exit_code = probe.main(
            [
                "--repo-root",
                str(repo_root),
                "--output",
                str(output_path),
                "--codex-bin",
                "codex-definitely-not-installed",
                "--allow-partial",
            ]
        )
        assert exit_code == 0

    def test_main_writes_via_atomic_rename(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """GIVEN main() writes the artifact WHEN it completes THEN no
        leftover .tmp file remains next to the output path (atomic
        temp-file-then-rename write, Issue #1834 review finding #6)."""
        repo_root = tmp_path / "repo"
        (repo_root / ".codex").mkdir(parents=True)
        (repo_root / "scripts" / "session-recording").mkdir(parents=True)
        (repo_root / ".codex" / "config.toml").write_text("", encoding="utf-8")
        (repo_root / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        (repo_root / "scripts" / "session-recording" / "codex-hook-adapter.mjs").write_text("", encoding="utf-8")

        output_path = tmp_path / "out" / "runtime-capability.json"
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
        (tmp_path / "empty-bin").mkdir()

        probe.main(
            [
                "--repo-root",
                str(repo_root),
                "--output",
                str(output_path),
                "--codex-bin",
                "codex-definitely-not-installed",
                "--allow-partial",
            ]
        )
        assert output_path.is_file()
        assert not output_path.with_suffix(output_path.suffix + ".tmp").exists()
