"""Issue #2015 AC12: AGY-native Serena MCP discovery probe tests.

Hermetic tests exercise the probe's pure parsing/materialization logic
without spawning ``agy``/``serena`` (fast, deterministic, run in every CI
lane). The live trial plan class at the bottom genuinely launches the real
``agy`` CLI three consecutive times (Issue #2015 AC12: "専用 probe を最低3回
連続で実行し、3/3 PASS を必要とする") and requires 3/3 genuine PASS -- it
never fabricates a PASS: when the live environment (a real, authenticated
``agy`` binary + network access for the pinned Serena package) is
unavailable, an explicit ``"status": "unavailable"`` artifact is written
(distinct from a genuine PASS/FAIL artifact) before ``pytest.skip()``, in
the same style ``test_run_gemini_headless_live_trial.py`` already
established for AC8/AC14 (Issue #2015 P1 fix, OWNER review #2044).
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import types
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROBE_PATH = (
    _REPO_ROOT
    / ".claude"
    / "skills"
    / "gemini-cli-headless-delegation"
    / "scripts"
    / "probe_agy_native_serena_mcp.py"
)
_ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts" / "agy_native_serena_probe"


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def probe() -> types.ModuleType:
    return _load_module(_PROBE_PATH, "probe_agy_native_serena_mcp")


@pytest.fixture()
def manifest() -> dict[str, Any]:
    return {
        "schema": "serena_tool_manifest_v1",
        "pinned_ref": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        "mcp_command": [
            "uvx",
            "--from",
            "git+https://example.invalid/serena@deadbeef",
            "serena",
            "start-mcp-server",
            "--project-from-cwd",
        ],
        "read_only_allowlist": ["find_file", "search_for_pattern", "get_symbols_overview"],
        "dangerous_denylist": ["execute_shell_command", "write_memory", "create_text_file"],
        "known_tools": [
            "find_file",
            "search_for_pattern",
            "get_symbols_overview",
            "execute_shell_command",
            "write_memory",
            "create_text_file",
        ],
    }


def _fake_repo(tmp_path: Path, manifest: dict[str, Any]) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / ".agents").mkdir(parents=True)
    (repo_root / ".agents" / "mcp_config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "serena": {
                        "command": "uvx",
                        "args": manifest["mcp_command"][1:],
                        "includeTools": manifest["read_only_allowlist"],
                        "excludeTools": manifest["dangerous_denylist"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    manifest_dir = repo_root / ".claude" / "skills" / "gemini-cli-headless-delegation" / "references"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "serena-tool-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(repo_root), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=str(repo_root), check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(repo_root), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(repo_root), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo_root), check=True)
    return repo_root


class TestPreconditionLoading:
    def test_load_manifest_missing_file_raises_precondition_error(self, probe, tmp_path):
        with pytest.raises(probe.ProbePreconditionError):
            probe._load_manifest(tmp_path)

    def test_load_manifest_missing_required_key_raises(self, probe, tmp_path, manifest):
        del manifest["dangerous_denylist"]
        manifest_dir = tmp_path / ".claude" / "skills" / "gemini-cli-headless-delegation" / "references"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "serena-tool-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(probe.ProbePreconditionError):
            probe._load_manifest(tmp_path)

    def test_load_mcp_config_missing_serena_server_raises(self, probe, tmp_path):
        (tmp_path / ".agents").mkdir()
        (tmp_path / ".agents" / "mcp_config.json").write_text(
            json.dumps({"mcpServers": {"other": {}}}), encoding="utf-8"
        )
        with pytest.raises(probe.ProbePreconditionError):
            probe._load_mcp_config(tmp_path)

    def test_git_head_sha_reads_real_repo(self, probe, tmp_path, manifest):
        repo_root = _fake_repo(tmp_path, manifest)
        sha = probe._git_head_sha(repo_root)
        assert len(sha) == 40


class TestMaterializeProbeWorkspace:
    def test_settings_json_allows_only_mcp_serena_and_denies_direct_resources(self, probe, tmp_path, manifest):
        repo_root = _fake_repo(tmp_path, manifest)
        workspace = probe.materialize_probe_workspace(repo_root, manifest, parent_dir=tmp_path)
        settings = json.loads(workspace.settings_path.read_text(encoding="utf-8"))
        assert settings["permissions"]["allow"] == ["mcp(serena)"]
        denied_resources = (
            "command(*)",
            "read_file(*)",
            "write_file(*)",
            "read_url(*)",
            "execute_url(*)",
            "unsandboxed(*)",
        )
        for denied_resource in denied_resources:
            assert denied_resource in settings["permissions"]["deny"]
        assert settings["toolPermission"] == "always-proceed"

    def test_mcp_config_symlink_points_at_tracked_repo_file_not_a_copy(self, probe, tmp_path, manifest):
        repo_root = _fake_repo(tmp_path, manifest)
        workspace = probe.materialize_probe_workspace(repo_root, manifest, parent_dir=tmp_path)
        assert workspace.mcp_config_symlink.is_symlink()
        resolved = workspace.mcp_config_symlink.resolve()
        assert resolved == (repo_root / ".agents" / "mcp_config.json").resolve()

    def test_serena_config_excludes_the_manifest_dangerous_denylist_and_sets_projects(self, probe, tmp_path, manifest):
        repo_root = _fake_repo(tmp_path, manifest)
        workspace = probe.materialize_probe_workspace(repo_root, manifest, parent_dir=tmp_path)
        import yaml

        serena_config = yaml.safe_load(workspace.serena_config_path.read_text(encoding="utf-8"))
        assert sorted(serena_config["excluded_tools"]) == sorted(manifest["dangerous_denylist"])
        # Issue #2015 AC12 fix_delta: `projects` must be present -- Serena's
        # own SerenaConfig.from_config_file() fatally rejects a config file
        # that lacks it, independent of --project-from-cwd (live-verified).
        assert str(repo_root) in serena_config["projects"]

    def test_each_workspace_is_fresh_and_isolated(self, probe, tmp_path, manifest):
        repo_root = _fake_repo(tmp_path, manifest)
        first = probe.materialize_probe_workspace(repo_root, manifest, parent_dir=tmp_path)
        second = probe.materialize_probe_workspace(repo_root, manifest, parent_dir=tmp_path)
        assert first.workspace_dir != second.workspace_dir


class TestStreamJsonParsing:
    def test_extract_mcp_tool_events_filters_by_tool_name(self, probe):
        events = [
            {"event": "init"},
            {"event": "step_update", "step_update": {"tool_name": "list_dir", "state": "DONE"}},
            {
                "event": "step_update",
                "step_update": {
                    "tool_name": "call_mcp_tool",
                    "state": "DONE",
                    "tool_info": {"parameters": {"ServerName": "serena"}},
                },
            },
        ]
        found = probe._extract_mcp_tool_events(events)
        assert len(found) == 1
        assert found[0]["tool_info"]["parameters"]["ServerName"] == "serena"

    def test_parse_stream_json_skips_non_json_uvx_noise_lines(self, probe):
        text = 'Installed 3 packages in 5ms\n{"event": "init", "init": {}}\n\n'
        events = probe._parse_stream_json(text)
        assert len(events) == 1
        assert events[0]["event"] == "init"

    def test_fallback_violation_detected_when_sentinel_tool_succeeds(self, probe):
        events = [
            {
                "event": "step_update",
                "step_update": {"tool_name": "run_command", "state": "DONE", "tool_info": {"parameters": {}}},
            }
        ]
        violations = probe._extract_fallback_violations(events)
        assert len(violations) == 1

    def test_fallback_denied_by_permission_engine_is_not_a_violation(self, probe):
        events = [
            {
                "event": "step_update",
                "step_update": {
                    "tool_name": "run_command",
                    "state": "DONE",
                    "tool_info": {"parameters": {}, "error": {"type": "TOOL_ERROR", "message": "Permission denied"}},
                },
            }
        ]
        violations = probe._extract_fallback_violations(events)
        assert violations == []

    def test_orchestration_only_tools_are_never_flagged_as_fallback(self, probe):
        events = [
            {
                "event": "step_update",
                "step_update": {"tool_name": "define_subagent", "state": "DONE", "tool_info": {"parameters": {}}},
            },
            {
                "event": "step_update",
                "step_update": {"tool_name": "invoke_subagent", "state": "DONE", "tool_info": {"parameters": {}}},
            },
        ]
        assert probe._extract_fallback_violations(events) == []


class TestSerenaLogEvidenceExtraction:
    def test_extracts_tool_application_with_session_and_task_id(self, probe):
        list_tools_line = (
            "INFO 2026-08-11 00:17:53,930 [MainThread] "
            "mcp.server.lowlevel.server:_handle_request:727 - "
            "Processing request of type ListToolsRequest\n"
        )
        call_tool_line = (
            "INFO 2026-08-11 00:17:54,100 [MainThread] "
            "mcp.server.lowlevel.server:_handle_request:727 - "
            "Processing request of type CallToolRequest\n"
        )
        tool_application_line = (
            "INFO 2026-08-11 00:17:54,120 [Task-2:FindFileTool] "
            "serena.tools.tools_base:_log_tool_application:279 - "
            "find_file: file_mask='CLAUDE.md', relative_path='.', "
            "session_id='77210200f860'; session_id: 77210200f860\n"
        )
        log_text = list_tools_line + call_tool_line + tool_application_line
        evidence = probe._extract_serena_evidence(log_text)
        assert evidence["tools_list_confirmed"] is True
        assert evidence["call_tool_request_confirmed"] is True
        assert len(evidence["tool_applications"]) == 1
        application = evidence["tool_applications"][0]
        assert application["tool"] == "find_file"
        assert application["session_id"] == "77210200f860"
        assert application["task_id"] == "FindFileTool"

    def test_no_tool_application_line_yields_empty_list(self, probe):
        evidence = probe._extract_serena_evidence("nothing relevant here\n")
        assert evidence["tool_applications"] == []
        assert evidence["tools_list_confirmed"] is False
        assert evidence["call_tool_request_confirmed"] is False


# ---------------------------------------------------------------------------
# Live trial plan (Issue #2015 AC12: 3 consecutive genuine live PASS required)
# ---------------------------------------------------------------------------


def _agy_native_serena_probe_live_environment_available() -> tuple[bool, str]:
    if shutil.which("agy") is None:
        return False, "agy CLI not found on PATH (cli_missing)"
    manifest_path = (
        _REPO_ROOT
        / ".claude"
        / "skills"
        / "gemini-cli-headless-delegation"
        / "references"
        / "serena-tool-manifest.json"
    )
    if not manifest_path.is_file():
        return False, "serena-tool-manifest.json not found"
    mcp_config_path = _REPO_ROOT / ".agents" / "mcp_config.json"
    if not mcp_config_path.is_file():
        return False, ".agents/mcp_config.json not found"
    return True, "ok"


def _write_unavailable_artifact(reason: str) -> None:
    _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    (_ARTIFACT_DIR / "agy_native_serena_probe_trial.json").write_text(
        json.dumps(
            {
                "schema": "agy_native_serena_mcp_probe_trial/v1",
                "status": "unavailable",
                "reason": reason,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_agy_native_serena_probe_live_trial_plan() -> None:
    """Issue #2015 AC12: run the dedicated live probe 3 consecutive times;
    require genuine 3/3 PASS (never a fabricated/self-reported success).

    Each trial is a fresh, independent probe invocation (its own isolated
    AGY $HOME, its own AGY subprocess, its own Serena MCP connection) --
    not merely one probe run's internal bounded retry. Environment
    unavailability (missing `agy` binary etc) is recorded as an explicit
    `"status": "unavailable"` artifact and the test is skipped -- it is
    never silently indistinguishable from a genuine PASS.
    """
    available, reason = _agy_native_serena_probe_live_environment_available()
    if not available:
        _write_unavailable_artifact(reason)
        pytest.skip(f"agy_native_serena_probe live environment unavailable: {reason}")

    probe = _load_module(_PROBE_PATH, "probe_agy_native_serena_mcp")

    trials: list[dict[str, Any]] = []
    for trial_index in range(1, 4):
        result = probe.run_probe(_REPO_ROOT, max_attempts=1)
        trials.append(
            {
                "trial_index": trial_index,
                "status": result["status"],
                "head_sha": result["head_sha"],
                "excluded_tools_enforced": bool(
                    result.get("excluded_tools_enforcement") and result["excluded_tools_enforcement"].get("enforced")
                ),
                "winning_attempt_index": result.get("winning_attempt_index"),
            }
        )

    _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    (_ARTIFACT_DIR / "agy_native_serena_probe_trial.json").write_text(
        json.dumps(
            {
                "schema": "agy_native_serena_mcp_probe_trial/v1",
                "status": "achieved" if all(t["status"] == "pass" for t in trials) else "partial",
                "trials": trials,
                "pass_count": sum(1 for t in trials if t["status"] == "pass"),
                "trial_count": len(trials),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    pass_count = sum(1 for t in trials if t["status"] == "pass")
    assert pass_count == 3, f"AC12 requires genuine 3/3 live PASS; got {pass_count}/3: {trials}"
    for trial in trials:
        assert trial["excluded_tools_enforced"] is True
