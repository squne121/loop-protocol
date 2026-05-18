from __future__ import annotations

import importlib.util
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "preflight_gemini_headless.py"
    spec = importlib.util.spec_from_file_location("preflight_gemini_headless", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _FakeCompleted:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_gh_ok(command):
    """gh コマンドのデフォルト成功レスポンスを返すヘルパー。"""
    if command == ["gh", "--version"]:
        return _FakeCompleted(0, "gh version 2.70.0 (2025-04-22)\n", "")
    if command == ["gh", "auth", "status"]:
        return _FakeCompleted(0, "Logged in to github.com account test-user\n", "")
    return None


def _make_fake_run(module):
    def fake_run(command, cwd=None):
        if command == ["gemini", "--version"]:
            return _FakeCompleted(0, "0.34.0\n", "")
        if command == ["gemini", "--help"]:
            return _FakeCompleted(
                0,
                "Use -p/--prompt for non-interactive mode.\n--model\n--prompt\n--output-format\n--approval-mode\n--skip-trust\nquery\nquery ...\n--prompt                   Prompt. Appended to input on stdin.\n",
                "",
            )
        if command[:2] == ["gemini", "--model"]:
            return _FakeCompleted(
                0,
                '{"response": "OK", "stats": {"models": {"gemini-3-flash-preview": {}}}}',
                "",
            )
        gh_result = _fake_gh_ok(command)
        if gh_result is not None:
            return gh_result
        raise AssertionError(f"unexpected command: {command}")

    return fake_run


def test_run_preflight_happy_path(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "_run", _make_fake_run(module))
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])

    result = module.run_preflight()

    assert result["ok"] is True
    assert result["validated_tool_profiles"] == [
        "no_tools",
        "grounded_research",
        "local_asset_research",
        "proposal_only",
        "github_research",
    ]
    assert result["local_asset_research"]["ok"] is True
    assert result["proposal_only"]["ok"] is True
    assert "implementation_draft" in result["proposal_only"]["allowed_outputs"]
    assert "GitHub mutation" in result["proposal_only"]["forbidden_capabilities"]
    assert result["proposal_only"]["write_owner"] == "Codex"
    assert result["version"]["ok"] is True
    assert result["help"]["ok"] is True
    assert result["smoke"]["ok"] is True
    assert result["local_asset_research"]["prompt_stdin_supported"] is True
    assert result["smoke"]["response_text"] == "OK"


def test_run_preflight_fails_closed_on_unverified_local_asset_research_settings(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "_run", _make_fake_run(module))
    monkeypatch.setattr(
        module,
        "_validate_local_asset_research_settings",
        lambda: ["local_asset_research includes dangerous Serena MCP tools: write_file"],
    )

    result = module.run_preflight()

    assert result["ok"] is False
    assert result["failure_reason"] == "local_asset_research includes dangerous Serena MCP tools: write_file"
    assert result["local_asset_research"]["ok"] is False
    assert any("dangerous Serena MCP tools" in warning for warning in result["warnings"])


def test_run_preflight_fails_closed_when_local_asset_prompt_stdin_support_is_missing(monkeypatch):
    module = load_module()

    def fake_run_without_prompt_stdin(command, cwd=None):
        if command == ["gemini", "--version"]:
            return _FakeCompleted(0, "0.34.0\n", "")
        if command == ["gemini", "--help"]:
            return _FakeCompleted(
                0,
                "Use -p/--prompt for non-interactive mode.\n--model\n--prompt\n--output-format\n--approval-mode\n--skip-trust\n",
                "",
            )
        if command[:2] == ["gemini", "--model"]:
            return _FakeCompleted(
                0,
                '{"response": "OK", "stats": {"models": {"gemini-3-flash-preview": {}}}}',
                "",
            )
        gh_result = _fake_gh_ok(command)
        if gh_result is not None:
            return gh_result
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(module, "_run", fake_run_without_prompt_stdin)
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])

    result = module.run_preflight()

    assert result["ok"] is False
    assert (
        "gemini --help requires --prompt + stdin append support for long local_asset_research context"
        in result["failure_reason"]
    )
    assert result["local_asset_research"]["prompt_stdin_supported"] is False


def test_strip_verbose_subfields_removes_verbose_fields(monkeypatch):
    module = load_module()
    monkeypatch.setattr(module, "_run", _make_fake_run(module))
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])

    full_result = module.run_preflight()
    stripped_result = module._strip_verbose_subfields(full_result)

    # essential fields preserved
    assert stripped_result["ok"] is True
    assert stripped_result["failure_reason"] is None
    assert stripped_result["warnings"] == []
    assert stripped_result["validated_tool_profiles"][-1] == "github_research"
    assert stripped_result["proposal_only"]["write_owner"] == "Codex"
    assert stripped_result["version"]["ok"] is True
    assert stripped_result["version"]["value"] == "0.34.0"
    assert stripped_result["help"]["ok"] is True
    assert stripped_result["help"]["missing_flags"] == []
    assert stripped_result["smoke"]["ok"] is True
    assert stripped_result["smoke"]["response_text"] == "OK"

    # verbose fields removed from version
    assert "stdout" not in stripped_result["version"]
    assert "stderr" not in stripped_result["version"]

    # verbose fields removed from help
    assert "stdout" not in stripped_result["help"]
    assert "stderr" not in stripped_result["help"]
    assert "required_flags" not in stripped_result["help"]

    # verbose fields removed from smoke
    assert "command" not in stripped_result["smoke"]
    assert "stdout" not in stripped_result["smoke"]
    assert "stderr" not in stripped_result["smoke"]
    assert "stats" not in stripped_result["smoke"]


def test_strip_verbose_subfields_on_failure_result(monkeypatch):
    """compact が ok=False の preflight 結果（早期 return パス）でも正常動作することを検証する。

    gemini --version 失敗時は smoke セクションが空のままで run_preflight() が返る。
    _strip_verbose_subfields() はその状態でも TypeError を起こさず、essential フィールドを保持する。
    """
    module = load_module()
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])

    def fake_run_version_fail(command, cwd=None):
        if command == ["gemini", "--version"]:
            return _FakeCompleted(1, "", "gemini: command not found")
        raise AssertionError(f"unexpected command after version failure: {command}")

    monkeypatch.setattr(module, "_run", fake_run_version_fail)
    failure_result = module.run_preflight()

    assert failure_result["ok"] is False
    assert failure_result["failure_reason"] == "gemini --version failed"

    stripped_result = module._strip_verbose_subfields(failure_result)

    # essential top-level fields preserved
    assert stripped_result["ok"] is False
    assert stripped_result["failure_reason"] == "gemini --version failed"
    assert isinstance(stripped_result["warnings"], list)

    # version section: verbose fields removed, essential retained
    assert stripped_result["version"]["ok"] is False
    assert "stdout" not in stripped_result["version"]
    assert "stderr" not in stripped_result["version"]

    # smoke section: still a dict (empty values) with verbose fields removed
    assert "command" not in stripped_result["smoke"]
    assert "stdout" not in stripped_result["smoke"]
    assert "stderr" not in stripped_result["smoke"]
    assert "stats" not in stripped_result["smoke"]
    # ok and response_text are non-verbose; they must be retained
    assert "ok" in stripped_result["smoke"]
    assert "response_text" in stripped_result["smoke"]


def test_stdout_summary_ok_true(monkeypatch, capsys, tmp_path):
    """ok: true 時に version 付きの成功メッセージと保存先パスが stdout に出力される。"""
    module = load_module()
    monkeypatch.setattr(module, "_run", _make_fake_run(module))
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])

    output_file = tmp_path / "result.json"
    exit_code = module.main(["--output-file", str(output_file)])

    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "[gemini-preflight] ok: Gemini CLI 0.34.0 is ready" in captured
    assert f"[gemini-preflight] result saved to: {output_file}" in captured


def test_stdout_summary_ok_false_with_reason(monkeypatch, capsys, tmp_path):
    """ok: false 時に failure_reason が stdout に出力される。"""
    module = load_module()
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])

    def fake_run_version_fail(command, cwd=None):
        if command == ["gemini", "--version"]:
            return _FakeCompleted(1, "", "gemini: command not found")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(module, "_run", fake_run_version_fail)

    output_file = tmp_path / "result.json"
    exit_code = module.main(["--output-file", str(output_file)])

    assert exit_code == 1
    captured = capsys.readouterr().out
    assert "[gemini-preflight] error: gemini --version failed" in captured
    assert f"[gemini-preflight] result saved to: {output_file}" in captured


def test_file_not_found_returns_fail_closed_result(monkeypatch, capsys, tmp_path):
    """gemini バイナリ未存在時に crash せず fail-closed な JSON を返す。"""
    module = load_module()
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])

    def fake_run_raises_file_not_found(command, cwd=None):
        raise FileNotFoundError("gemini")

    monkeypatch.setattr(module, "_run", fake_run_raises_file_not_found)

    output_file = tmp_path / "result.json"
    exit_code = module.main(["--output-file", str(output_file)])

    assert exit_code == 1
    result = module._load_json(output_file)
    assert result["ok"] is False
    assert result["failure_reason"] == "gemini: command not found"
    assert "gemini: command not found" in result["warnings"]

    captured = capsys.readouterr().out
    assert "[gemini-preflight] error: gemini: command not found" in captured
    assert f"[gemini-preflight] result saved to: {output_file}" in captured


def test_trusted_directory_smoke_parse_failure_reports_recovery(monkeypatch, capsys, tmp_path):
    """trusted directory 未設定で JSON parse failure になる根本原因を診断に残す。"""
    module = load_module()
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])
    trusted_directory_stderr = (
        "Gemini CLI is not running in a trusted directory. To proceed, either use "
        "--skip-trust, set GEMINI_CLI_TRUST_WORKSPACE=true, or trust this directory "
        "in interactive mode.\n"
    )

    def fake_run_trusted_directory_failure(command, cwd=None):
        if command == ["gemini", "--version"]:
            return _FakeCompleted(0, "0.34.0\n", "")
        if command == ["gemini", "--help"]:
            return _FakeCompleted(
                0,
                "Use -p/--prompt for non-interactive mode.\n--model\n--prompt\n--output-format\n--approval-mode\n--skip-trust\nquery\nquery ...\n--prompt                   Prompt. Appended to input on stdin.\n",
                "",
            )
        if command[:2] == ["gemini", "--model"]:
            return _FakeCompleted(1, "", trusted_directory_stderr)
        gh_result = _fake_gh_ok(command)
        if gh_result is not None:
            return gh_result
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(module, "_run", fake_run_trusted_directory_failure)

    output_file = tmp_path / "result.json"
    exit_code = module.main(["--output-file", str(output_file)])

    assert exit_code == 1
    result = module.run_preflight()
    assert result["ok"] is False
    assert "smoke JSON parse failed" in result["failure_reason"]
    assert "trusted directory" in result["failure_reason"]
    assert "GEMINI_CLI_TRUST_WORKSPACE" in result["failure_reason"]
    assert any("trusted directory" in warning for warning in result["warnings"])
    assert any("GEMINI_CLI_TRUST_WORKSPACE" in warning for warning in result["warnings"])
    # AC1: machine-readable failure_class
    assert result["failure_class"] == "trusted_workspace_required"
    # AC2: recovery_action contains GEMINI_CLI_TRUST_WORKSPACE=true
    assert result["recovery_action"] is not None
    assert "GEMINI_CLI_TRUST_WORKSPACE=true" in result["recovery_action"]

    captured = capsys.readouterr().out
    # AC3: stdout shows trusted_workspace_required and GEMINI_CLI_TRUST_WORKSPACE=true
    assert "trusted_workspace_required" in captured
    assert "GEMINI_CLI_TRUST_WORKSPACE=true" in captured
    assert f"[gemini-preflight] result saved to: {output_file}" in captured


def test_trusted_directory_failure_class_fields_in_json_output(monkeypatch, tmp_path):
    """trusted directory stderr 時の failure_class/recovery_action が JSON ファイルに書き込まれる。"""
    module = load_module()
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])
    trusted_directory_stderr = (
        "Approval mode overridden to \"default\" because the current folder is not trusted. "
        "Gemini CLI is not running in a trusted directory. "
        "set GEMINI_CLI_TRUST_WORKSPACE=true to proceed.\n"
    )

    def fake_run(command, cwd=None):
        if command == ["gemini", "--version"]:
            return _FakeCompleted(0, "0.34.0\n", "")
        if command == ["gemini", "--help"]:
            return _FakeCompleted(
                0,
                "--model\n--prompt\n--output-format\n--approval-mode\n--skip-trust\n--prompt                   Prompt. Appended to input on stdin.\n",
                "",
            )
        if command[:2] == ["gemini", "--model"]:
            return _FakeCompleted(1, "", trusted_directory_stderr)
        gh_result = _fake_gh_ok(command)
        if gh_result is not None:
            return gh_result
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(module, "_run", fake_run)

    output_file = tmp_path / "preflight_result.json"
    exit_code = module.main(["--output-file", str(output_file)])

    assert exit_code == 1
    saved = module._load_json(output_file)
    # AC1: JSON ファイルに failure_class が含まれる
    assert saved["failure_class"] == "trusted_workspace_required"
    # AC2: JSON ファイルに recovery_action が含まれ GEMINI_CLI_TRUST_WORKSPACE=true を示す
    assert saved["recovery_action"] is not None
    assert "GEMINI_CLI_TRUST_WORKSPACE=true" in saved["recovery_action"]


def test_stdout_summary_ok_false_no_reason(monkeypatch, capsys, tmp_path):
    """ok: false かつ failure_reason が None の場合に fallback メッセージが出力される。"""
    module = load_module()
    monkeypatch.setattr(module, "_run", _make_fake_run(module))
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])

    # run_preflight で ok=True になるが、手動で failure_reason=None, ok=False に書き換えて
    # _print_stdout_summary の fallback パスをテストする
    output_file = tmp_path / "result.json"
    result = module.run_preflight()
    result["ok"] = False
    result["failure_reason"] = None
    module._dump_json(output_file, result)
    module._print_stdout_summary(result, output_file)

    captured = capsys.readouterr().out
    assert "[gemini-preflight] error: preflight failed (no failure reason available; see result JSON)" in captured
    assert f"[gemini-preflight] result saved to: {output_file}" in captured


# ---------------------------------------------------------------------------
# Iteration 3 — MEDIUM: gh_cli 分離テスト (gh not found でも ok=True)
# ---------------------------------------------------------------------------


def test_run_preflight_gh_not_found_proposal_only_ok_true(monkeypatch):
    """gh not found でも proposal_only 利用者が影響を受けないため top-level ok=True であること。"""
    module = load_module()

    def fake_run_gh_not_found(command, cwd=None):
        if command[0] == "gh":
            raise FileNotFoundError("gh: command not found")
        return _make_fake_run(module)(command, cwd=cwd)

    monkeypatch.setattr(module, "_run", fake_run_gh_not_found)
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])

    result = module.run_preflight()

    # proposal_only / grounded_research / no_tools 利用者は影響を受けない
    assert result["ok"] is True
    # gh_cli セクションは失敗を記録する
    assert result["gh_cli"]["ok"] is False
    assert any("command not found" in e for e in result["gh_cli"]["errors"])


def test_run_preflight_gh_not_found_gh_cli_ok_false_and_warning(monkeypatch):
    """gh not found 時に gh_cli.ok=False であり warnings に gh_cli 失敗が含まれること。"""
    module = load_module()

    def fake_run_gh_not_found(command, cwd=None):
        if command[0] == "gh":
            raise FileNotFoundError("gh: command not found")
        return _make_fake_run(module)(command, cwd=cwd)

    monkeypatch.setattr(module, "_run", fake_run_gh_not_found)
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])

    result = module.run_preflight()

    assert result["gh_cli"]["ok"] is False
    # warnings に gh_cli 失敗が含まれること（observability 維持）
    assert any("gh_cli" in w or "command not found" in w or "github_research" in w for w in result["warnings"]), (
        f"expected gh_cli failure in warnings, got: {result['warnings']}"
    )


def test_smoke_command_includes_skip_trust(monkeypatch):
    """smoke コマンドに --skip-trust が既定で含まれることを検証する（Issue #1824）。"""
    module = load_module()
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])

    captured_commands: list[list[str]] = []

    def fake_run_capture(command, cwd=None):
        captured_commands.append(list(command))
        if command == ["gemini", "--version"]:
            return _FakeCompleted(0, "0.34.0\n", "")
        if command == ["gemini", "--help"]:
            return _FakeCompleted(
                0,
                "--model\n--prompt\n--output-format\n--approval-mode\n--skip-trust\n--prompt                   Prompt. Appended to input on stdin.\n",
                "",
            )
        if command[:2] == ["gemini", "--model"]:
            return _FakeCompleted(
                0,
                '{"response": "OK", "stats": {"models": {"gemini-3-flash-preview": {}}}}',
                "",
            )
        gh_result = _fake_gh_ok(command)
        if gh_result is not None:
            return gh_result
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(module, "_run", fake_run_capture)
    result = module.run_preflight()

    assert result["ok"] is True
    smoke_commands = [c for c in captured_commands if c[:2] == ["gemini", "--model"]]
    assert len(smoke_commands) == 1, f"expected 1 smoke command, got: {smoke_commands}"
    assert "--skip-trust" in smoke_commands[0], (
        f"--skip-trust missing from smoke command: {smoke_commands[0]}"
    )


def test_stdout_summary_ok_true_version_none(capsys, tmp_path):
    """ok: true かつ version.value が None の場合に 'unknown' fallback が出力される。

    gemini --version が exit 0 を返しても stdout が空ならば
    version.value = None のまま ok=True に到達しうる。
    本テストでその fallback パスを直接検証する。
    """
    module = load_module()

    # ok=True, version.value=None の状態を手動構築して fallback パスを検証
    output_file = tmp_path / "result.json"
    result = {
        "schema": "gemini_headless_preflight_result/v1",
        "ok": True,
        "failure_reason": None,
        "version": {"ok": True, "value": None},
        "warnings": [],
    }
    module._dump_json(output_file, result)
    module._print_stdout_summary(result, output_file)

    captured = capsys.readouterr().out
    assert "[gemini-preflight] ok: Gemini CLI unknown is ready" in captured
    assert f"[gemini-preflight] result saved to: {output_file}" in captured


# ---------------------------------------------------------------------------
# gh_cli preflight section tests
# ---------------------------------------------------------------------------


def _make_fake_run_with_gh(module, gh_version_rc=0, gh_auth_rc=0):
    """_make_fake_run の gh コマンド対応版を返す。"""
    base_run = _make_fake_run(module)

    def fake_run_with_gh(command, cwd=None):
        if command == ["gh", "--version"]:
            if gh_version_rc == 0:
                return _FakeCompleted(0, "gh version 2.70.0 (2025-04-22)\n", "")
            return _FakeCompleted(1, "", "gh: command not found")
        if command == ["gh", "auth", "status"]:
            if gh_auth_rc == 0:
                return _FakeCompleted(0, "Logged in to github.com account squne121\n", "")
            return _FakeCompleted(1, "", "You are not logged into any GitHub hosts. Run gh auth login to authenticate.")
        return base_run(command, cwd=cwd)

    return fake_run_with_gh


def test_run_preflight_gh_cli_section_present_on_success(monkeypatch):
    """preflight 結果に gh_cli セクションが存在し、ok: true になること。"""
    module = load_module()
    monkeypatch.setattr(module, "_run", _make_fake_run_with_gh(module))
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])

    result = module.run_preflight()

    assert "gh_cli" in result
    assert result["gh_cli"]["ok"] is True
    assert result["gh_cli"]["version"] is not None
    assert result["ok"] is True


def test_run_preflight_gh_cli_fails_when_not_authenticated(monkeypatch):
    """gh auth status が失敗する場合、gh_cli.ok=False かつ warnings に gh_cli 失敗が含まれるが
    proposal_only 等の他プロファイル利用者への影響を避けるため top-level ok=True を維持すること。"""
    module = load_module()
    monkeypatch.setattr(module, "_run", _make_fake_run_with_gh(module, gh_auth_rc=1))
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])

    result = module.run_preflight()

    # gh_cli 失敗は gh_cli セクションに記録される
    assert "gh_cli" in result
    assert result["gh_cli"]["ok"] is False
    assert any("not authenticated" in e or "auth status failed" in e for e in result["gh_cli"]["errors"])
    # gh_cli 失敗は warnings に追記される（observability 維持）
    assert any("gh_cli check failed" in w or "not authenticated" in w or "auth status failed" in w for w in result["warnings"])
    # 他プロファイル（proposal_only 等）は影響を受けないため top-level ok=True を維持する
    assert result["ok"] is True


def test_run_preflight_gh_cli_fails_when_gh_not_found(monkeypatch):
    """gh コマンドが存在しない場合、gh_cli.ok=False かつ warnings に記録されるが
    proposal_only 等の他プロファイル利用者への影響を避けるため top-level ok=True を維持すること。"""
    module = load_module()

    def fake_run_gh_not_found(command, cwd=None):
        if command[0] == "gh":
            raise FileNotFoundError("gh: command not found")
        return _make_fake_run(module)(command, cwd=cwd)

    monkeypatch.setattr(module, "_run", fake_run_gh_not_found)
    monkeypatch.setattr(module, "_validate_local_asset_research_settings", lambda: [])

    result = module.run_preflight()

    # gh_cli 失敗は gh_cli セクションに記録される
    assert "gh_cli" in result
    assert result["gh_cli"]["ok"] is False
    assert any("command not found" in e for e in result["gh_cli"]["errors"])
    # gh_cli 失敗は warnings に追記される（observability 維持）
    assert any("gh_cli" in w or "command not found" in w for w in result["warnings"])
    # 他プロファイル（proposal_only 等）は影響を受けないため top-level ok=True を維持する
    assert result["ok"] is True
