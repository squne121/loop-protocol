#!/usr/bin/env python3
"""test_probe_codex_v2_runtime_capability.py — offline fixture tests for
scripts/agent-guards/probe_codex_v2_runtime_capability.py (Issue #1834 AC4).

Covers the normal path plus every known failure mode enumerated in the
Issue contract: Codex binary not found, executable is a symlink/shim,
version output empty / non-semver / timeout, feature not recognized,
feature recognized-but-disabled, config loader rejection, and subprocess
malformed output. All probes use an injected fake `runner` — no real Codex
CLI installation is required or invoked.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional

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
) -> Callable[..., FakeCompletedProcess]:
    """responses maps a command-key (joined argv) substring to a
    FakeCompletedProcess. Falls back to a generic ok(0) empty response."""

    raise_not_found_for = raise_not_found_for or []
    raise_timeout_for = raise_timeout_for or []

    def _runner(cmd, capture_output=True, text=True, timeout=None):  # noqa: ANN001
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


# ---------------------------------------------------------------------------
# check_config_toml_features_schema
# ---------------------------------------------------------------------------


class TestConfigTomlFeaturesSchema:
    def test_features_table_present_with_flag(self, tmp_path: Path) -> None:
        """GIVEN config.toml declares [features] multi_agent_v2 = true
        WHEN checked THEN parse_status is ok_present and the flag value is
        read."""
        config = tmp_path / "config.toml"
        config.write_text("[features]\nmulti_agent_v2 = true\n", encoding="utf-8")
        result = probe.check_config_toml_features_schema(config)
        assert result["parse_status"] == "ok_present"
        assert result["multi_agent_v2_declared"] is True

    def test_features_table_absent(self, tmp_path: Path) -> None:
        """GIVEN config.toml has no [features] table WHEN checked THEN
        parse_status is ok_absent."""
        config = tmp_path / "config.toml"
        config.write_text("approval_policy = \"on-request\"\n", encoding="utf-8")
        result = probe.check_config_toml_features_schema(config)
        assert result["parse_status"] == "ok_absent"
        assert result["multi_agent_v2_declared"] is None

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
# resolve_codex_executable (symlink / shim detection)
# ---------------------------------------------------------------------------


class TestResolveCodexExecutable:
    def test_binary_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """GIVEN the executable is not on PATH WHEN resolved THEN
        shim_detection is binary_not_found."""
        monkeypatch.setenv("PATH", str(tmp_path))
        result = probe.resolve_codex_executable("codex-nowhere")
        assert result["shim_detection"] == "binary_not_found"
        assert result["which_path"] is None

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


# ---------------------------------------------------------------------------
# classify_recorder_generation
# ---------------------------------------------------------------------------


class TestClassifyRecorderGeneration:
    def test_post_1830_advisory_subagent_stop_only(self, tmp_path: Path) -> None:
        """GIVEN hooks.json wires SubagentStop to the session-recording
        composite adapter and has no SessionEnd hook WHEN classified THEN
        generation is post_1830_advisory_subagent_stop_only."""
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
        assert probe.classify_recorder_generation(path) == "post_1830_advisory_subagent_stop_only"

    def test_session_end_recorder_present(self, tmp_path: Path) -> None:
        """GIVEN hooks.json wires SessionEnd to the session-recording
        composite adapter WHEN classified THEN generation is
        session_end_recorder_present."""
        hooks = {
            "hooks": {
                "SessionEnd": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "node .codex/hooks/session-recording-composite.mjs --event SessionEnd",
                            }
                        ],
                    }
                ]
            }
        }
        path = tmp_path / "hooks.json"
        path.write_text(json.dumps(hooks), encoding="utf-8")
        assert probe.classify_recorder_generation(path) == "session_end_recorder_present"

    def test_hooks_json_missing(self, tmp_path: Path) -> None:
        """GIVEN hooks.json does not exist WHEN classified THEN generation
        is hooks_json_missing."""
        assert probe.classify_recorder_generation(tmp_path / "missing.json") == "hooks_json_missing"

    def test_hooks_json_unparseable(self, tmp_path: Path) -> None:
        """GIVEN hooks.json is not valid JSON WHEN classified THEN
        generation is hooks_json_unparseable."""
        path = tmp_path / "hooks.json"
        path.write_text("{not json", encoding="utf-8")
        assert probe.classify_recorder_generation(path) == "hooks_json_unparseable"


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
                                    }
                                ],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        (repo_root / "scripts" / "session-recording" / "codex-hook-adapter.mjs").write_text(
            "// adapter\n", encoding="utf-8"
        )
        return repo_root

    def test_build_artifact_happy_path(self, tmp_path: Path) -> None:
        """GIVEN a fixture repo with a recognized-but-disabled
        multi_agent_v2 feature WHEN the artifact is built THEN all
        required top-level keys and provenance fields are present and
        v2_config_schema_loadable is True (schema is syntactically
        loadable even though the flag itself is disabled)."""
        repo_root = self._base_repo(tmp_path)
        table = "multi_agent_v2                       stable             false\n"
        runner = _make_runner(
            {
                "--version": FakeCompletedProcess(0, "codex-cli 0.146.0\n", ""),
                "features list": FakeCompletedProcess(0, table, ""),
                "--help": FakeCompletedProcess(0, "Usage: codex\n", ""),
                "exec --help": FakeCompletedProcess(0, "Usage: codex exec\n", ""),
                "rev-parse HEAD": FakeCompletedProcess(0, "deadbeef" * 5, ""),
            }
        )
        artifact = probe.build_artifact(repo_root=repo_root, runner=runner, generated_at="2026-07-30T00:00:00Z")

        assert artifact["schema"] == "CODEX_MULTI_AGENT_V2_RUNTIME_CAPABILITY_V1"
        assert artifact["codex_cli_version"]["status"] == "ok"
        assert artifact["v2_config_schema_loadable"] is True
        provenance = artifact["provenance"]
        assert provenance["repo_head_sha"] == "deadbeef" * 5
        assert provenance["codex_hooks_json_sha256"] is not None
        assert provenance["codex_hook_adapter_sha256"] is not None
        assert provenance["recorder_generation"] == "post_1830_advisory_subagent_stop_only"

    def test_build_artifact_schema_not_loadable_when_feature_unrecognized(self, tmp_path: Path) -> None:
        """GIVEN the installed Codex CLI does not recognize multi_agent_v2
        at all WHEN the artifact is built THEN v2_config_schema_loadable is
        False."""
        repo_root = self._base_repo(tmp_path)
        table = "apps                                  stable             true\n"
        runner = _make_runner(
            {
                "--version": FakeCompletedProcess(0, "codex-cli 0.100.0\n", ""),
                "features list": FakeCompletedProcess(0, table, ""),
                "--help": FakeCompletedProcess(0, "", ""),
                "exec --help": FakeCompletedProcess(0, "", ""),
                "rev-parse HEAD": FakeCompletedProcess(0, "cafebabe" * 5, ""),
            }
        )
        artifact = probe.build_artifact(repo_root=repo_root, runner=runner, generated_at="2026-07-30T00:00:00Z")
        assert artifact["v2_config_schema_loadable"] is False


# ---------------------------------------------------------------------------
# CLI main() smoke test (writes artifact JSON to a fixture path)
# ---------------------------------------------------------------------------


class TestMainCli:
    def test_main_writes_artifact_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """GIVEN --repo-root and --output point at a fixture directory
        WHEN main() runs THEN a well-formed JSON artifact file is written
        (exercises the real subprocess.run default runner against a
        nonexistent codex binary, which must fail closed to
        binary_not_found rather than raising)."""
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
        assert exit_code == 0
        assert output_path.is_file()
        data = json.loads(output_path.read_text(encoding="utf-8"))
        assert data["schema"] == "CODEX_MULTI_AGENT_V2_RUNTIME_CAPABILITY_V1"
        assert data["codex_cli_version"]["status"] == "binary_not_found"
