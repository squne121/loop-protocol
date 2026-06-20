"""
tests/agent_ops/test_check_agent_hook_environment.py

scripts/check_agent_hook_environment.py の pytest テスト。

AC1: /bin/sh が存在しない場合 → status: blocked
AC2: cwd が存在しない場合 → status: environment_failure
AC3: hook command script が存在しない場合 → hook status: missing, handler_id と event 含む
AC4: blocker hook と telemetry hook の分類
AC5: stdout が compact JSON のみで raw command body を含まない
AC6: ${CLAUDE_PROJECT_DIR} の解決が正しく行われる
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

# ─── テスト対象モジュールのロード ─────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_agent_hook_environment.py"

spec = importlib.util.spec_from_file_location("check_agent_hook_environment", SCRIPT_PATH)
assert spec is not None
checker = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(checker)  # type: ignore[attr-defined]


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """一時的なリポジトリルートを作成する。"""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir()
    return tmp_path


@pytest.fixture()
def real_script(tmp_repo: Path) -> Path:
    """実行可能なダミー hook スクリプトを作成する。"""
    script = tmp_repo / ".claude" / "hooks" / "test_hook.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return script


def make_settings(hooks: dict[str, Any]) -> dict[str, Any]:
    """settings.json の最小構造を生成するヘルパー。"""
    return {"hooks": hooks}


def write_settings(repo_root: Path, hooks: dict[str, Any]) -> None:
    """repo_root/.claude/settings.json に hooks を書き込む。"""
    settings_path = repo_root / ".claude" / "settings.json"
    settings_path.write_text(
        json.dumps(make_settings(hooks), ensure_ascii=False),
        encoding="utf-8",
    )


# ─── AC1: /bin/sh missing → status: blocked ───────────────────────────────────

class TestBinShCheck:
    """AC1: /bin/sh が存在しない場合の挙動。"""

    def test_bin_sh_exists_ok(self) -> None:
        """GIVEN /bin/sh が存在する WHEN check_bin_sh を呼ぶ THEN status: ok。"""
        with mock.patch("pathlib.Path.exists", return_value=True):
            result = checker.check_bin_sh()
        assert result["status"] == "ok"

    def test_bin_sh_missing_returns_missing(self) -> None:
        """GIVEN /bin/sh が存在しない WHEN check_bin_sh を呼ぶ THEN status: missing。"""
        with mock.patch.object(Path, "exists", return_value=False):
            result = checker.check_bin_sh()
        assert result["status"] == "missing"

    def test_overall_status_blocked_when_bin_sh_missing(self) -> None:
        """GIVEN /bin/sh missing WHEN _determine_status THEN status: blocked。"""
        result = checker._determine_status(
            bin_sh={"status": "missing"},
            cwd={"status": "ok", "path": "/tmp"},
            hooks=[],
            settings_error=None,
        )
        assert result == "blocked"


# ─── AC2: cwd missing → status: environment_failure ───────────────────────────

class TestCwdCheck:
    """AC2: cwd が存在しない場合の挙動。"""

    def test_cwd_exists_ok(self, tmp_path: Path) -> None:
        """GIVEN cwd が存在する WHEN check_cwd THEN status: ok。"""
        original_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = checker.check_cwd()
            assert result["status"] == "ok"
        finally:
            os.chdir(original_cwd)

    def test_cwd_missing_oserror(self) -> None:
        """GIVEN getcwd が FileNotFoundError を上げる WHEN check_cwd THEN status: missing。"""
        with mock.patch("os.getcwd", side_effect=FileNotFoundError("deleted")):
            result = checker.check_cwd()
        assert result["status"] == "missing"

    def test_overall_status_environment_failure_when_cwd_missing(self) -> None:
        """GIVEN cwd missing WHEN _determine_status THEN environment_failure。"""
        result = checker._determine_status(
            bin_sh={"status": "ok"},
            cwd={"status": "missing", "path": "<deleted>"},
            hooks=[],
            settings_error=None,
        )
        assert result == "environment_failure"

    def test_cwd_missing_takes_priority_over_hook_failures(self) -> None:
        """GIVEN cwd missing + hook failures WHEN _determine_status THEN environment_failure（cwd 優先）。"""
        hooks = [{"event": "PreToolUse", "handler_id": "x", "hook_class": "blocker", "status": "missing"}]
        result = checker._determine_status(
            bin_sh={"status": "ok"},
            cwd={"status": "missing", "path": "<deleted>"},
            hooks=hooks,
            settings_error=None,
        )
        assert result == "environment_failure"


# ─── AC3: hook command が存在しない場合 ────────────────────────────────────────

class TestHookCommandResolution:
    """AC3: hook command script が存在しない場合 → missing, handler_id と event を含む。"""

    def test_missing_script_returns_missing_status(self, tmp_repo: Path) -> None:
        """GIVEN hook script が存在しない WHEN check_hooks THEN status: missing。"""
        hooks = {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/nonexistent_guard.sh",
                            "args": [],
                            "timeout": 10,
                        }
                    ],
                }
            ]
        }
        settings = make_settings(hooks)
        results = checker.check_hooks(settings, tmp_repo)
        assert len(results) == 1
        hook_result = results[0]
        assert hook_result["status"] == "missing"
        assert hook_result["event"] == "PreToolUse"
        assert hook_result["handler_id"] == "nonexistent_guard"

    def test_existing_executable_script_returns_ok(self, tmp_repo: Path, real_script: Path) -> None:
        """GIVEN hook script が存在・実行可能 WHEN check_hooks THEN status: ok。"""
        hook_name = real_script.stem
        hooks = {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"${{CLAUDE_PROJECT_DIR}}/.claude/hooks/{real_script.name}",
                            "args": [],
                            "timeout": 10,
                        }
                    ],
                }
            ]
        }
        settings = make_settings(hooks)
        results = checker.check_hooks(settings, tmp_repo)
        assert len(results) == 1
        assert results[0]["status"] == "ok"
        assert results[0]["handler_id"] == hook_name

    def test_non_executable_script_returns_not_executable(self, tmp_repo: Path) -> None:
        """GIVEN hook script が存在するが実行不可 WHEN check_hooks THEN status: not_executable。"""
        script = tmp_repo / ".claude" / "hooks" / "readonly_guard.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o644)

        hooks = {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/readonly_guard.sh",
                            "args": [],
                            "timeout": 10,
                        }
                    ],
                }
            ]
        }
        settings = make_settings(hooks)
        results = checker.check_hooks(settings, tmp_repo)
        assert len(results) == 1
        assert results[0]["status"] == "not_executable"

    def test_handler_id_and_event_present_in_result(self, tmp_repo: Path) -> None:
        """GIVEN hook script missing WHEN check_hooks THEN handler_id と event が存在する（AC3）。"""
        hooks = {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/missing_stop.sh",
                            "args": [],
                            "timeout": 60,
                        }
                    ]
                }
            ]
        }
        settings = make_settings(hooks)
        results = checker.check_hooks(settings, tmp_repo)
        assert len(results) == 1
        assert "handler_id" in results[0]
        assert "event" in results[0]
        assert results[0]["event"] == "Stop"
        assert results[0]["handler_id"] == "missing_stop"


# ─── AC4: blocker / telemetry 分類 ────────────────────────────────────────────

class TestHookClassification:
    """AC4: blocker hook と telemetry hook の failure を分類する。"""

    def test_pretooluse_classified_as_blocker(self) -> None:
        assert checker.classify_hook("PreToolUse") == "blocker"

    def test_stop_classified_as_blocker(self) -> None:
        assert checker.classify_hook("Stop") == "blocker"

    def test_subagent_stop_classified_as_blocker(self) -> None:
        assert checker.classify_hook("SubagentStop") == "blocker"

    def test_precompact_classified_as_blocker(self) -> None:
        assert checker.classify_hook("PreCompact") == "blocker"

    def test_posttooluse_classified_as_telemetry(self) -> None:
        assert checker.classify_hook("PostToolUse") == "telemetry"

    def test_blocker_failure_causes_blocked_status(self) -> None:
        hooks = [
            {"event": "PreToolUse", "handler_id": "guard", "hook_class": "blocker", "status": "missing"}
        ]
        result = checker._determine_status(
            bin_sh={"status": "ok"},
            cwd={"status": "ok", "path": "/tmp"},
            hooks=hooks,
            settings_error=None,
        )
        assert result == "blocked"

    def test_telemetry_only_failure_causes_warn_status(self) -> None:
        hooks = [
            {"event": "PostToolUse", "handler_id": "recorder", "hook_class": "telemetry", "status": "missing"}
        ]
        result = checker._determine_status(
            bin_sh={"status": "ok"},
            cwd={"status": "ok", "path": "/tmp"},
            hooks=hooks,
            settings_error=None,
        )
        assert result == "warn"

    def test_check_hooks_includes_hook_class_field(self, tmp_repo: Path) -> None:
        hooks = {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.sh",
                            "args": [],
                            "timeout": 10,
                        }
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/recorder.sh",
                            "args": [],
                            "timeout": 60,
                        }
                    ],
                }
            ],
        }
        settings = make_settings(hooks)
        results = checker.check_hooks(settings, tmp_repo)
        assert len(results) == 2
        by_event = {r["event"]: r for r in results}
        assert by_event["PreToolUse"]["hook_class"] == "blocker"
        assert by_event["PostToolUse"]["hook_class"] == "telemetry"


# ─── AC5: stdout は compact JSON のみ、raw command body を含まない ─────────────

class TestStdoutFormat:
    """AC5: stdout が compact JSON のみで raw command body を含まない。"""

    def test_subprocess_stdout_is_compact_json(self, tmp_repo: Path) -> None:
        write_settings(tmp_repo, {})
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--repo-root", str(tmp_repo)],
            capture_output=True,
            text=True,
        )
        stdout = result.stdout.strip()
        assert "\n" not in stdout, f"stdout に改行が含まれる: {stdout!r}"
        parsed = json.loads(stdout)
        assert isinstance(parsed, dict)

    def test_stdout_has_no_raw_command_body(self, tmp_repo: Path) -> None:
        script = tmp_repo / ".claude" / "hooks" / "secret_boundary_guard.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)

        raw_command = "${CLAUDE_PROJECT_DIR}/.claude/hooks/secret_boundary_guard.sh"
        hooks = {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": raw_command,
                            "args": [],
                            "timeout": 10,
                        }
                    ],
                }
            ]
        }
        write_settings(tmp_repo, hooks)
        result = checker.run_checks(tmp_repo)
        output_json = json.dumps(result)
        assert "${CLAUDE_PROJECT_DIR}" not in output_json

    def test_status_field_present_in_output(self, tmp_repo: Path) -> None:
        write_settings(tmp_repo, {})
        result = checker.run_checks(tmp_repo)
        assert "status" in result
        assert result["status"] in ("ok", "warn", "blocked", "environment_failure")

    def test_manifest_drift_ref_is_path_reference_only(self, tmp_repo: Path) -> None:
        write_settings(tmp_repo, {})
        result = checker.run_checks(tmp_repo)
        assert "manifest_drift_ref" in result
        assert "check_hook_boundaries.py" in result["manifest_drift_ref"]


# ─── AC6: ${CLAUDE_PROJECT_DIR} の解決 ────────────────────────────────────────

class TestClaudeProjectDirResolution:
    """AC6: ${CLAUDE_PROJECT_DIR} が --repo-root の絶対パスに正しく解決される。"""

    def test_resolve_command_path_substitutes_project_dir(self, tmp_repo: Path) -> None:
        hook: dict[str, Any] = {
            "type": "command",
            "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.sh",
            "args": [],
        }
        resolved = checker.resolve_command_path(hook, tmp_repo)
        expected = tmp_repo.resolve() / ".claude" / "hooks" / "guard.sh"
        assert resolved == expected

    def test_resolve_command_path_node_wrapper(self, tmp_repo: Path) -> None:
        hook: dict[str, Any] = {
            "type": "command",
            "command": "node",
            "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/manifest.mjs"],
        }
        resolved = checker.resolve_command_path(hook, tmp_repo)
        expected = tmp_repo.resolve() / ".claude" / "hooks" / "manifest.mjs"
        assert resolved == expected

    def test_resolve_handler_id_node_wrapper_uses_args(self) -> None:
        hook: dict[str, Any] = {
            "type": "command",
            "command": "node",
            "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/generate_session_manifest_from_hook.mjs"],
        }
        result = checker.resolve_handler_id(hook)
        assert result == "generate_session_manifest_from_hook"

    def test_full_run_with_project_dir_in_settings(self, tmp_repo: Path, real_script: Path) -> None:
        hooks = {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"${{CLAUDE_PROJECT_DIR}}/.claude/hooks/{real_script.name}",
                            "args": [],
                            "timeout": 10,
                        }
                    ],
                }
            ]
        }
        write_settings(tmp_repo, hooks)
        result = checker.run_checks(tmp_repo)
        hook_results = result["checks"]["hooks"]
        assert len(hook_results) == 1
        assert hook_results[0]["status"] == "ok"


# ─── B3: deleted cwd integration test ─────────────────────────────────────────

class TestDeletedCwdIntegration:
    """B3: cwd が削除済みの場合、JSON を出す前に例外で落ちない。"""

    def test_deleted_cwd_outputs_compact_json_environment_failure(self, tmp_path: Path) -> None:
        """GIVEN cwd が削除済み WHEN subprocess 実行 THEN compact JSON environment_failure / cwd_missing。"""
        work_dir = tmp_path / "work"
        work_dir.mkdir()

        runner = tmp_path / "runner.py"
        runner.write_text(
            f"""
import os, shutil, subprocess, sys
os.chdir({str(work_dir)!r})
shutil.rmtree({str(work_dir)!r})
r = subprocess.run(
    [sys.executable, {str(SCRIPT_PATH)!r}, "--repo-root", "."],
    capture_output=True, text=True,
)
sys.stdout.write(r.stdout)
sys.exit(r.returncode)
""",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(runner)],
            capture_output=True,
            text=True,
        )
        stdout = result.stdout.strip()
        assert stdout, f"stdout が空: stderr={result.stderr!r}"
        parsed = json.loads(stdout)
        assert parsed["status"] == "environment_failure", f"予期しない status: {parsed}"
        assert parsed.get("reason") == "cwd_missing", f"reason フィールド欠落: {parsed}"
        assert result.returncode == 1


# ─── B4: symlink escape ────────────────────────────────────────────────────────

class TestSymlinkEscape:
    """B4: sibling-prefix symlink escape を is_relative_to() で正しく検出する。"""

    def test_sibling_prefix_symlink_escape(self, tmp_path: Path) -> None:
        """GIVEN /tmp/repo と /tmp/repo_evil のような sibling prefix THEN symlink_escape。"""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        evil_dir = tmp_path / "repo_evil"
        evil_dir.mkdir()
        evil_script = evil_dir / "hook.sh"
        evil_script.write_text("#!/bin/sh\nexit 0\n")
        evil_script.chmod(0o755)

        link_dir = repo_dir / ".claude" / "hooks"
        link_dir.mkdir(parents=True)
        symlink = link_dir / "evil_link.sh"
        symlink.symlink_to(evil_script)

        status = checker._check_script_status(symlink, repo_dir)
        assert status == "symlink_escape", f"sibling prefix bypass: got {status!r}"

    def test_legitimate_symlink_within_repo_is_ok(self, tmp_repo: Path) -> None:
        """GIVEN repo 内への symlink THEN ok（symlink escape でない）。"""
        target = tmp_repo / ".claude" / "hooks" / "real_hook.sh"
        target.write_text("#!/bin/sh\nexit 0\n")
        target.chmod(0o755)

        link = tmp_repo / ".claude" / "hooks" / "link_hook.sh"
        link.symlink_to(target)

        status = checker._check_script_status(link, tmp_repo)
        assert status == "ok"


# ─── B4+: directory as hook path ──────────────────────────────────────────────

class TestDirectoryAsScript:
    """B4: hook path がディレクトリの場合は not_executable（executable bit だけでは不十分）。"""

    def test_directory_hook_path_returns_not_executable(self, tmp_repo: Path) -> None:
        """GIVEN hook path がディレクトリ（executable bit あり） THEN not_executable。"""
        dir_path = tmp_repo / ".claude" / "hooks" / "not_a_script"
        dir_path.mkdir(parents=True)
        dir_path.chmod(0o755)

        status = checker._check_script_status(dir_path, tmp_repo)
        assert status == "not_executable"


# ─── B5: node wrapper readable-only .mjs ──────────────────────────────────────

class TestNodeWrapperReadable:
    """B5: node ラッパーパターンでは .mjs が 0644（readable のみ）でも ok。"""

    def test_node_script_readable_only_is_ok(self, tmp_repo: Path) -> None:
        """GIVEN is_node_script=True, .mjs が 0644 THEN ok（executable 不要）。"""
        mjs = tmp_repo / ".claude" / "hooks" / "post_tool_use.mjs"
        mjs.parent.mkdir(parents=True, exist_ok=True)
        mjs.write_text("// telemetry hook\n")
        mjs.chmod(0o644)

        status = checker._check_script_status(mjs, tmp_repo, is_node_script=True)
        assert status == "ok"

    def test_non_node_script_requires_executable(self, tmp_repo: Path) -> None:
        """GIVEN is_node_script=False, script が 0644 THEN not_executable。"""
        script = tmp_repo / ".claude" / "hooks" / "guard.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o644)

        status = checker._check_script_status(script, tmp_repo, is_node_script=False)
        assert status == "not_executable"

    def test_check_hooks_node_wrapper_readable_only_ok(self, tmp_repo: Path) -> None:
        """GIVEN node ラッパー hook, .mjs が 0644 WHEN check_hooks THEN status: ok。"""
        mjs = tmp_repo / ".claude" / "hooks" / "post_tool_use.mjs"
        mjs.parent.mkdir(parents=True, exist_ok=True)
        mjs.write_text("// telemetry\n")
        mjs.chmod(0o644)

        hooks = {
            "PostToolUse": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "node",
                            "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/post_tool_use.mjs"],
                            "timeout": 60,
                        }
                    ]
                }
            ]
        }
        settings = make_settings(hooks)
        results = checker.check_hooks(settings, tmp_repo)
        assert len(results) == 1
        assert results[0]["status"] == "ok"


# ─── B6: unknown event classification ─────────────────────────────────────────

class TestUnknownEventClassification:
    """B6: 未知 event は telemetry でなく unknown として fail-closed 扱い。"""

    def test_unknown_event_returns_unknown(self) -> None:
        """GIVEN 未知の hook event WHEN classify_hook THEN 'unknown'（telemetry でない）。"""
        assert checker.classify_hook("TaskCompleted") == "unknown"
        assert checker.classify_hook("PostToolBatch") == "unknown"
        assert checker.classify_hook("ConfigChange") == "unknown"

    def test_unknown_hook_failure_causes_blocked_status(self) -> None:
        """GIVEN unknown hook class + status: missing WHEN _determine_status THEN blocked。"""
        hooks = [
            {"event": "TaskCompleted", "handler_id": "x", "hook_class": "unknown", "status": "missing"}
        ]
        result = checker._determine_status(
            bin_sh={"status": "ok"},
            cwd={"status": "ok", "path": "/tmp"},
            hooks=hooks,
            settings_error=None,
        )
        assert result == "blocked"

    def test_unknown_hook_ok_does_not_cause_blocked(self) -> None:
        """GIVEN unknown hook class + status: ok WHEN _determine_status THEN blocked でない。"""
        hooks = [
            {"event": "TaskCompleted", "handler_id": "x", "hook_class": "unknown", "status": "ok"}
        ]
        result = checker._determine_status(
            bin_sh={"status": "ok"},
            cwd={"status": "ok", "path": "/tmp"},
            hooks=hooks,
            settings_error=None,
        )
        assert result == "ok"


# ─── top-level schema golden test ─────────────────────────────────────────────

class TestTopLevelSchema:
    """AC1/AC2 の documented reason と schema shape を golden test で固定する。"""

    def test_bin_sh_missing_has_reason_bin_sh_missing(self, tmp_repo: Path) -> None:
        """GIVEN bin_sh missing WHEN run_checks THEN reason: bin_sh_missing。"""
        write_settings(tmp_repo, {})
        with mock.patch.object(checker, "check_bin_sh", return_value={"status": "missing"}):
            result = checker.run_checks(tmp_repo)
        assert result["status"] == "blocked"
        assert result.get("reason") == "bin_sh_missing"

    def test_cwd_missing_has_reason_cwd_missing(self, tmp_repo: Path) -> None:
        """GIVEN cwd missing WHEN run_checks THEN reason: cwd_missing。"""
        write_settings(tmp_repo, {})
        with mock.patch.object(checker, "check_cwd", return_value={"status": "missing", "path": "<deleted>"}):
            result = checker.run_checks(tmp_repo)
        assert result["status"] == "environment_failure"
        assert result.get("reason") == "cwd_missing"

    def test_schema_has_required_top_level_fields(self, tmp_repo: Path) -> None:
        """GIVEN run_checks WHEN 正常環境 THEN status/checks/manifest_drift_ref が存在する。"""
        write_settings(tmp_repo, {})
        result = checker.run_checks(tmp_repo)
        assert "status" in result
        assert "checks" in result
        assert "manifest_drift_ref" in result
        assert "bin_sh" in result["checks"]
        assert "cwd" in result["checks"]
        assert "hooks" in result["checks"]

    def test_normal_result_has_no_reason_field(self, tmp_repo: Path) -> None:
        """GIVEN 正常環境 WHEN run_checks THEN reason フィールドは存在しない（status ok の場合）。"""
        write_settings(tmp_repo, {})
        result = checker.run_checks(tmp_repo)
        if result["status"] == "ok":
            assert "reason" not in result
