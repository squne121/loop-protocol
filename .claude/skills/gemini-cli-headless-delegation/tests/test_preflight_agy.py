"""Tests for preflight_agy.py — agy CLI preflight checks.

Test style mirrors test_preflight_gemini_headless.py:
importlib-based module load + monkeypatch / subprocess mock.
"""

from __future__ import annotations

import json
import importlib.util
import subprocess
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "preflight_agy.py"
    spec = importlib.util.spec_from_file_location("preflight_agy", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_happy_run(module):
    """Factory for a fake _run that returns successful agy responses."""

    def fake_run(argv, cwd=None, timeout=None):
        if argv == [module._resolve_binary(), "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [module._resolve_binary(), "--help"]:
            return _FakeCompleted(
                0,
                "agy — AI gateway CLI\n  -p, --print, --prompt  Run in non-interactive mode\n",
                "",
            )
        if argv[:2] == [module._resolve_binary(), "-p"]:
            return _FakeCompleted(0, "LOOP_AGY_SMOKE_OK\n", "")
        raise AssertionError(f"unexpected command: {argv}")

    return fake_run


def _write_settings(root: Path, *, include_tools: list[str], exclude_tools: list[str] | None = None) -> None:
    settings_dir = root / ".gemini"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings = {
        "mcp": {
            "allowed": ["serena"],
        },
        "mcpServers": {
            "serena": {
                "command": "uvx",
                "args": ["uvx", "serena", "--project-from-cwd"],
                "trust": False,
                "includeTools": include_tools,
                "excludeTools": exclude_tools or [],
            },
        },
    }
    (settings_dir / "settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# AC1: test_cli_missing
# Run as subprocess with AGY_BIN=/nonexistent/agy; check exit 1 and JSON.
# ---------------------------------------------------------------------------


def test_cli_missing(tmp_path):
    """AC1: subprocess invocation with missing binary exits 1 with failure_class:cli_missing."""
    import json
    import sys

    module = load_module()
    script = Path(module.__file__).resolve()
    output_file = tmp_path / "result.json"

    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--json",
            "--output-file",
            str(output_file),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**__import__("os").environ, "AGY_BIN": "/nonexistent/agy"},
        shell=False,
    )

    assert proc.returncode == 1, f"expected exit 1, got {proc.returncode}"
    result = json.loads(proc.stdout)
    assert result["ok"] is False
    assert result["failure_class"] == "cli_missing"
    assert result["failure_reason"] is not None


# ---------------------------------------------------------------------------
# AC2: test_no_shell_invocation
# Monkeypatch subprocess.run and verify argv is list and shell=False.
# Also confirm source does not contain shell=True (belt-and-suspenders).
# ---------------------------------------------------------------------------


def test_no_shell_invocation(monkeypatch):
    """AC2: All subprocess.run calls must use list argv and shell=False."""
    import subprocess as _subprocess

    module = load_module()
    captured = []

    def _mock_run(argv_or_cmd, **kwargs):
        captured.append({"argv": argv_or_cmd, "kwargs": kwargs})
        from types import SimpleNamespace
        if isinstance(argv_or_cmd, list) and "--version" in argv_or_cmd:
            return SimpleNamespace(returncode=0, stdout="agy 1.0.0\n", stderr="")
        if isinstance(argv_or_cmd, list) and "--help" in argv_or_cmd:
            return SimpleNamespace(
                returncode=0,
                stdout="  -p, --print, --prompt  Non-interactive mode\n",
                stderr="",
            )
        # smoke
        return SimpleNamespace(returncode=0, stdout="OK\n", stderr="")

    monkeypatch.setattr(_subprocess, "run", _mock_run)
    module.run_preflight()

    assert len(captured) >= 1, "subprocess.run must be called at least once"
    for call in captured:
        assert isinstance(call["argv"], list), (
            f"argv must be list[str], got {type(call['argv'])}: {call['argv']!r}"
        )
        assert call["kwargs"].get("shell", False) is False, (
            f"shell must be False, got kwargs={call['kwargs']}"
        )

    # Belt-and-suspenders: source must not contain shell=True
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in source, "preflight_agy.py must not use shell=True"


# ---------------------------------------------------------------------------
# AC3: test_help_flag_detection
# mock --help output with -p/--print/--prompt; expect noninteractive_flags True.
# ---------------------------------------------------------------------------


def test_help_flag_detection(monkeypatch):
    """AC3: help output with -p/--print/--prompt → noninteractive_flags all True."""
    module = load_module()

    def fake_run(argv, cwd=None, timeout=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(
                0,
                "Usage: agy [OPTIONS]\n"
                "  -p, --print, --prompt   Non-interactive prompt mode\n"
                "  --output-format         Output format\n",
                "",
            )
        if argv[:2] == [bin_, "-p"]:
            return _FakeCompleted(0, "OK\n", "")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)
    result = module.run_preflight()

    assert result["help"]["noninteractive_flags"]["-p"] is True
    assert result["help"]["noninteractive_flags"]["--print"] is True
    assert result["help"]["noninteractive_flags"]["--prompt"] is True
    assert result["help"]["ok"] is True
    # stdout_sample must be populated from live probe
    assert result["help"]["stdout_sample"] != ""


# ---------------------------------------------------------------------------
# AC4: test_missing_noninteractive_flags
# mock --help with no -p/--print/--prompt → cli_incompatible.
# ---------------------------------------------------------------------------


def test_missing_noninteractive_flags(monkeypatch):
    """AC4: help output without noninteractive flags → failure_class: cli_incompatible."""
    module = load_module()

    def fake_run(argv, cwd=None, timeout=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 0.5.0\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(
                0,
                "Usage: agy [OPTIONS]\n  --model   Model to use\n  --debug   Debug mode\n",
                "",
            )
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)
    result = module.run_preflight()

    assert result["ok"] is False
    assert result["failure_class"] == "cli_incompatible"
    assert result["help"]["noninteractive_flags"]["-p"] is False
    assert result["help"]["noninteractive_flags"]["--print"] is False
    assert result["help"]["noninteractive_flags"]["--prompt"] is False


# ---------------------------------------------------------------------------
# AC5: test_unexpected_capability
# chat/--output-format in help does NOT trigger failure.
# ---------------------------------------------------------------------------


def test_unexpected_capability(monkeypatch):
    """AC5: unexpected capabilities (chat/--output-format) do not cause failure."""
    module = load_module()

    def fake_run(argv, cwd=None, timeout=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(
                0,
                "Usage: agy [OPTIONS]\n"
                "  -p, --print, --prompt   Non-interactive mode\n"
                "  --output-format         Output format (json/text)\n"
                "  chat                    Start interactive chat session\n",
                "",
            )
        if argv[:2] == [bin_, "-p"]:
            return _FakeCompleted(0, "LOOP_AGY_SMOKE_OK\n", "")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)
    result = module.run_preflight()

    # Must not fail due to unexpected capabilities
    assert result["failure_class"] != "cli_incompatible"
    # Unexpected capabilities are recorded but do not block
    unexpected = result["help"]["unexpected_capabilities"]
    assert "chat" in unexpected or "--output-format" in unexpected
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# AC6: test_smoke_timeout
# subprocess.TimeoutExpired → client_subprocess_timeout.
# ---------------------------------------------------------------------------


def test_smoke_timeout(monkeypatch):
    """AC6: smoke check timeout → failure_class: client_subprocess_timeout."""
    module = load_module()

    def fake_run(argv, cwd=None, timeout=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(
                0,
                "  -p, --print, --prompt   Non-interactive mode\n",
                "",
            )
        if argv[:2] == [bin_, "-p"]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout or 20)
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)
    result = module.run_preflight()

    assert result["ok"] is False
    assert result["failure_class"] == "client_subprocess_timeout"
    assert result["smoke"]["timed_out"] is True


# ---------------------------------------------------------------------------
# AC7: test_json_output
# run_preflight() result has schema field; exit code matches ok.
# ---------------------------------------------------------------------------


def test_json_output(monkeypatch, tmp_path):
    """AC7: --json writes agy_preflight_result/v1 schema; exit 0 on ok, exit 1 on fail."""
    import json as json_mod

    module = load_module()

    # Happy path: run with monkeypatched _run
    monkeypatch.setattr(module, "_run", _make_happy_run(module))
    result = module.run_preflight()

    assert result["schema"] == "agy_preflight_result/v1"
    assert isinstance(result["ok"], bool)

    # Verify --json via main()
    output_file = tmp_path / "result.json"
    monkeypatch.setattr(module, "_run", _make_happy_run(module))
    exit_code = module.main(["--json", "--output-file", str(output_file)])

    assert exit_code == 0
    saved = json_mod.loads(output_file.read_text())
    assert saved["schema"] == "agy_preflight_result/v1"
    assert saved["ok"] is True

    # Failure path: missing binary → exit 1
    def fake_run_missing(argv, cwd=None, timeout=None):
        raise FileNotFoundError("/nonexistent/agy")

    monkeypatch.setattr(module, "_run", fake_run_missing)
    exit_code_fail = module.main(["--json"])
    assert exit_code_fail == 1


# ---------------------------------------------------------------------------
# Extra: test_help_flag_parser_no_false_positives
# --prompting / --printable must NOT trigger -p / --print.
# ---------------------------------------------------------------------------


def test_help_flag_parser_no_false_positives():
    """Ensure --prompting / --printable do not trigger -p / --print detection."""
    module = load_module()
    tricky_help = (
        "  --prompting   start a conversation\n"
        "  --printable   output printable chars\n"
        "  no-prompt here\n"
    )
    flags, unexpected = module._parse_help_capabilities(tricky_help)
    assert flags["-p"] is False, "--prompting should not match -p"
    assert flags["--print"] is False, "--printable should not match --print"
    assert flags["--prompt"] is False, "--prompting should not match --prompt"


# ---------------------------------------------------------------------------
# Extra: test_smoke_empty_stdout_fails
# smoke exit 0 with empty stdout → smoke_failed (silent output drop detection).
# ---------------------------------------------------------------------------


def test_smoke_empty_stdout_fails(monkeypatch):
    """Smoke check: exit 0 with empty stdout is classified as agy_empty_stdout."""
    import os

    module = load_module()
    original_ci = os.environ.get("CI")

    def fake_run(argv, cwd=None, timeout=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(0, "  -p, --print, --prompt  mode\n", "")
        if argv[:2] == [bin_, "-p"]:
            return _FakeCompleted(0, "", "")  # exit 0 but empty stdout
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)
    os.environ.pop("CI", None)
    result = module.run_preflight()

    assert result["ok"] is False
    assert result["failure_class"] == "agy_empty_stdout"
    assert result["smoke"]["exit_code"] == 0
    assert result["smoke"]["ok"] is False

    if original_ci is None:
        os.environ.pop("CI", None)
    else:
        os.environ["CI"] = original_ci


def test_smoke_output_mismatch(monkeypatch):
    """Smoke check: stdout mismatch is classified as agy_output_mismatch."""
    module = load_module()

    def fake_run(argv, cwd=None, timeout=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(0, "  -p, --print, --prompt  mode\n", "")
        if argv[:2] == [bin_, "-p"]:
            return _FakeCompleted(0, "LOOP_AGY_SMOKE_BAD\n", "")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)
    result = module.run_preflight()

    assert result["ok"] is False
    assert result["failure_class"] == "agy_output_mismatch"
    assert result["smoke"]["failure_reason"].startswith("agy_output_mismatch")


def test_smoke_output_missing_in_ci(monkeypatch):
    """Smoke check: empty stdout in CI is classified as agy_output_missing."""
    import os

    module = load_module()
    original_ci = os.environ.get("CI")

    def fake_run(argv, cwd=None, timeout=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(0, "  -p, --print, --prompt  mode\n", "")
        if argv[:2] == [bin_, "-p"]:
            return _FakeCompleted(0, "", "")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)
    os.environ["CI"] = "1"
    result = module.run_preflight()

    assert result["ok"] is False
    assert result["failure_class"] == "agy_output_missing"
    assert result["smoke"]["failure_class"] == "agy_output_missing"

    if original_ci is None:
        os.environ.pop("CI", None)
    else:
        os.environ["CI"] = original_ci


def test_smoke_stderr_and_stdout_samples_are_redacted(monkeypatch):
    """Smoke sample fields are redacted and truncated."""
    import os

    module = load_module()
    os.environ["AGY_API_KEY"] = "secret-key-123"

    def fake_run(argv, cwd=None, timeout=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(0, "  -p, --print, --prompt  mode\n", "")
        if argv[:2] == [bin_, "-p"]:
            return _FakeCompleted(0, "LOOP_AGY_SMOKE_OK\n", f"token={os.environ['AGY_API_KEY']}\n")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)
    result = module.run_preflight()

    assert result["ok"] is True
    assert "secret-key-123" not in result["smoke"]["stderr_sample"]
    assert "<redacted>" in result["smoke"]["stderr_sample"]


def test_redaction_happens_before_truncation(monkeypatch):
    """Long secrets that cross the truncation boundary must still be redacted."""
    import os

    module = load_module()
    secret = "x" * 800
    original = os.environ.get("AGY_API_KEY")
    os.environ["AGY_API_KEY"] = secret
    try:
        sample = module._redact_output_sample(f"prefix {secret} suffix")
    finally:
        if original is None:
            os.environ.pop("AGY_API_KEY", None)
        else:
            os.environ["AGY_API_KEY"] = original

    assert "x" * 12 not in sample
    assert "<redacted" in sample


def test_resolved_path_is_masked(monkeypatch):
    """Resolved binary paths are masked before being emitted as evidence."""
    module = load_module()
    monkeypatch.setenv("HOME", "/home/tester")

    assert module._mask_resolved_path("/home/tester/bin/agy") == "$HOME/bin/agy"
    assert module._mask_resolved_path("/usr/local/bin/agy") == "agy"


def test_local_asset_research_contract_validation_success(monkeypatch, tmp_path):
    """--local-asset-research succeeds when Serena contract is valid."""
    module = load_module()
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)  # type: ignore[call-arg]
    _write_settings(
        tmp_path,
        include_tools=sorted(module.SERENA_READ_ONLY_TOOLS),
        exclude_tools=sorted(module.SERENA_DANGEROUS_TOOLS),
    )

    monkeypatch.setattr(module, "_run", _make_happy_run(module))
    result = module.run_preflight(validate_local_asset_contract=True)

    assert result["ok"] is True
    assert result["local_asset_research"]["ok"] is True
    assert result["local_asset_research"]["status"] == "ok"
    assert result["local_asset_research"]["unknown_tool_policy"] == module.LOCAL_ASSET_SERENA_TOOL_POLICY


def test_local_asset_research_contract_validation_rejects_unknown_tool(monkeypatch, tmp_path):
    """--local-asset-research rejects unknown tools / drift in Serena allowlist."""
    module = load_module()
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)  # type: ignore[call-arg]
    _write_settings(
        tmp_path,
        include_tools=["find_file", "find_referencing_symbols", "unknown_serena_tool"],
        exclude_tools=sorted(module.SERENA_DANGEROUS_TOOLS),
    )

    monkeypatch.setattr(module, "_run", _make_happy_run(module))
    result = module.run_preflight(validate_local_asset_contract=True)

    assert result["ok"] is False
    assert result["failure_class"] == "local_asset_contract_invalid"
    assert result["local_asset_research"]["ok"] is False
    assert any("unknown_tool_policy" in item for item in result["local_asset_research"]["errors"])
