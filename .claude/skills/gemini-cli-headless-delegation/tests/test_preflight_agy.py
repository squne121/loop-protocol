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


def _write_settings(
    root: Path,
    *,
    include_tools: list[str],
    exclude_tools: list[str] | None = None,
    pinned: bool = True,
) -> None:
    serena_source = (
        "git+https://github.com/oraios/serena@0123456789abcdef"
        if pinned
        else "git+https://github.com/oraios/serena"
    )
    settings_dir = root / ".gemini"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings = {
        "mcp": {
            "allowed": ["serena"],
        },
        "mcpServers": {
            "serena": {
                "command": "uvx",
                "args": ["--from", serena_source, "serena", "start-mcp-server", "--project-from-cwd"],
                "trust": False,
                "includeTools": include_tools,
                "excludeTools": exclude_tools or [],
            },
        },
    }
    (settings_dir / "settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    agents_dir = root / ".agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agy_mcp_config = {"mcpServers": settings["mcpServers"]}
    (agents_dir / "mcp_config.json").write_text(
        json.dumps(agy_mcp_config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_manifest(
    module,
    root: Path,
    *,
    pinned_ref: str = "0123456789abcdef",
    known_tools: list[str] | None = None,
) -> None:
    path = root / module.SERENA_TOOL_MANIFEST_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": "serena_tool_manifest_v1",
        "source": "https://github.com/oraios/serena",
        "pinned_ref": pinned_ref,
        "generated_at_utc": "2026-07-02T00:00:00Z",
        "mcp_command": [
            "uvx",
            "--from",
            f"git+https://github.com/oraios/serena@{pinned_ref}",
            "serena",
        "start-mcp-server",
        "--project-from-cwd",
        ],
        "read_only_allowlist": sorted(module.SERENA_READ_ONLY_TOOLS),
        "dangerous_denylist": sorted(module.SERENA_DANGEROUS_TOOLS),
        "known_tools": known_tools or sorted(module.SERENA_READ_ONLY_TOOLS | module.SERENA_DANGEROUS_TOOLS),
        "notes": [],
    }
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


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


def test_grounded_research_probe_success(monkeypatch, tmp_path):
    """--grounded-research runs a bounded AGY websearch probe and returns evidence URLs.

    Success requires a machine-verifiable structured `tool_calls` trace naming a
    recognized web tool — a bare URL string alone is never sufficient (Issue #1266
    Blocker 1, reopened in preflight_agy.py)."""
    module = load_module()
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)  # type: ignore[call-arg]

    def fake_run(argv, cwd=None, timeout=None):
        if argv == [module._resolve_binary(), "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [module._resolve_binary(), "--help"]:
            return _FakeCompleted(
                0,
                "Usage: agy [OPTIONS]\n"
                "  -p, --print, --prompt  Non-interactive mode\n",
                "",
            )
        if argv[:2] == [module._resolve_binary(), "-p"]:
            if argv[2] == module.SMOKE_PROMPT:
                return _FakeCompleted(0, "LOOP_AGY_SMOKE_OK\n", "")
            return _FakeCompleted(
                0,
                "Sources: https://example.com/one https://example.com/two\n"
                'AGY_GROUNDED_RESEARCH: {"tool_calls": [{"name": "web_search"}]}\n',
                "",
            )
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)

    result = module.run_preflight(grounded_research=True)

    assert result["ok"] is True
    assert result["grounded_research"]["ok"] is True
    check = result["grounded_research"]["check"]
    assert check["ok"] is True
    # Issue #1266 Major 1: 1 query / 1 URL quota-bound contract — evidence is clamped to 1
    # URL even when AGY returns more than one.
    assert check["evidence_urls"] == ["https://example.com/one"]
    assert check["web_tool_call_count"] == 1
    assert check["url_citation_count"] == 1
    assert check["stdout_line_count"] > 0
    assert check["tool_calls_verified"] is True


def test_grounded_research_probe_fails_when_url_only_no_tool_call_trace(monkeypatch, tmp_path):
    """A bare URL string with no structured tool_calls trace is fail-closed with
    grounding_failure_class agy_web_grounding_tool_call_missing (Issue #1266 Blocker 1,
    reopened in preflight_agy.py: URL presence alone must never be treated as a
    web tool-call execution proof)."""
    module = load_module()
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)  # type: ignore[call-arg]

    def fake_run(argv, cwd=None, timeout=None):
        if argv == [module._resolve_binary(), "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [module._resolve_binary(), "--help"]:
            return _FakeCompleted(
                0,
                "Usage: agy [OPTIONS]\n"
                "  -p, --print, --prompt  Non-interactive mode\n",
                "",
            )
        if argv[:2] == [module._resolve_binary(), "-p"]:
            if argv[2] == module.SMOKE_PROMPT:
                return _FakeCompleted(0, "LOOP_AGY_SMOKE_OK\n", "")
            return _FakeCompleted(0, "Sources: https://example.com/one\n", "")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)

    result = module.run_preflight(grounded_research=True)

    assert result["ok"] is False
    assert result["failure_class"] == "agy_web_grounding_tool_call_missing"
    check = result["grounded_research"]["check"]
    assert check["ok"] is False
    assert check["failure_class"] == "agy_web_grounding_tool_call_missing"
    assert check["web_tool_call_count"] == 0
    assert check["tool_calls_verified"] is False
    # A bare URL is still surfaced as weak/unverified evidence for audit purposes only.
    assert check["evidence_urls"] == ["https://example.com/one"]


def test_grounded_research_probe_fails_when_no_urls(monkeypatch, tmp_path):
    """--grounded-research fails with explicit failure_class if no web evidence URL is found."""
    module = load_module()
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)  # type: ignore[call-arg]

    def fake_run(argv, cwd=None, timeout=None):
        if argv == [module._resolve_binary(), "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [module._resolve_binary(), "--help"]:
            return _FakeCompleted(
                0,
                "Usage: agy [OPTIONS]\n"
                "  -p, --print, --prompt  Non-interactive mode\n",
                "",
            )
        if argv[:2] == [module._resolve_binary(), "-p"]:
            if argv[2] == module.SMOKE_PROMPT:
                return _FakeCompleted(0, "LOOP_AGY_SMOKE_OK\n", "")
            return _FakeCompleted(0, "No web result returned.\n", "")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)

    result = module.run_preflight(grounded_research=True)

    assert result["ok"] is False
    assert result["failure_class"] == "agy_grounded_research_no_evidence"
    assert result["grounded_research"]["check"]["failure_reason"] == "agy_grounded_research no_evidence_urls_found"
    assert result["grounded_research"]["ok"] is False


def test_agy_grounded_research_quota_exhausted_bounded_no_retry_storm(monkeypatch, tmp_path):
    """agy_grounded_research_quota_exhausted: bounded probe has no retry storm and is
    classified via the dedicated quota-exhaustion classifier, not a generic exit_nonzero
    (Issue #1266 Major 1)."""
    module = load_module()
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)  # type: ignore[call-arg]
    grounded_calls = {"count": 0}

    def fake_run(argv, cwd=None, timeout=None):
        if argv == [module._resolve_binary(), "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [module._resolve_binary(), "--help"]:
            return _FakeCompleted(
                0,
                "Usage: agy [OPTIONS]\n"
                "  -p, --print, --prompt  Non-interactive mode\n",
                "",
            )
        if argv[:2] == [module._resolve_binary(), "-p"]:
            if argv[2] == module.SMOKE_PROMPT:
                return _FakeCompleted(0, "LOOP_AGY_SMOKE_OK\n", "")
            grounded_calls["count"] += 1
            return _FakeCompleted(1, "", "quota_exhausted")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)

    result = module.run_preflight(grounded_research=True)

    assert result["ok"] is False
    assert result["failure_class"] == "agy_grounded_research_quota_exhausted"
    assert grounded_calls["count"] == 1


def test_agy_grounded_research_quota_exhausted_resource_exhausted_text(monkeypatch, tmp_path):
    """RESOURCE_EXHAUSTED in stdout (exit 0) is also classified as quota exhaustion, not
    treated as a successful probe (Issue #1266 Major 1)."""
    module = load_module()
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)  # type: ignore[call-arg]

    def fake_run(argv, cwd=None, timeout=None):
        if argv == [module._resolve_binary(), "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [module._resolve_binary(), "--help"]:
            return _FakeCompleted(
                0,
                "Usage: agy [OPTIONS]\n"
                "  -p, --print, --prompt  Non-interactive mode\n",
                "",
            )
        if argv[:2] == [module._resolve_binary(), "-p"]:
            if argv[2] == module.SMOKE_PROMPT:
                return _FakeCompleted(0, "LOOP_AGY_SMOKE_OK\n", "")
            return _FakeCompleted(0, "RESOURCE_EXHAUSTED: Individual quota reached", "")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)

    result = module.run_preflight(grounded_research=True)

    assert result["ok"] is False
    assert result["failure_class"] == "agy_grounded_research_quota_exhausted"
# ---------------------------------------------------------------------------
# Extra: test_help_flag_parser_no_false_positives
# --prompting / --printable must NOT trigger -p / --print.
# ---------------------------------------------------------------------------


def test_evidence_doc_is_generated_from_preflight_json_not_hand_authored(monkeypatch, tmp_path):
    """docs/dev/agy-grounded-research-evidence.md must be generated from the same
    grounded_research preflight JSON as the PR body — no independently hand-authored
    citation list or placeholder sha256 (Issue #1266 Blocker 4)."""
    module = load_module()
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)  # type: ignore[call-arg]

    def fake_run(argv, cwd=None, timeout=None):
        if argv == [module._resolve_binary(), "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [module._resolve_binary(), "--help"]:
            return _FakeCompleted(
                0,
                "Usage: agy [OPTIONS]\n"
                "  -p, --print, --prompt  Non-interactive mode\n",
                "",
            )
        if argv[:2] == [module._resolve_binary(), "-p"]:
            if argv[2] == module.SMOKE_PROMPT:
                return _FakeCompleted(0, "LOOP_AGY_SMOKE_OK\n", "")
            return _FakeCompleted(0, "Source: https://example.com/one reliable-update\n", "")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)

    result = module.run_preflight(grounded_research=True)
    envelope = module.build_evidence_envelope(result, issue_number=1266, captured_at="2026-07-04T00:00:00Z")
    markdown = module.render_evidence_markdown(envelope)

    evidence = envelope["agy_web_grounding_evidence_v1"]
    check = result["grounded_research"]["check"]

    # Parity: every rendered value traces back to the exact same preflight result — no
    # independently authored citation list, count, or placeholder sha256.
    assert evidence["url_citation_count"] == check["url_citation_count"]
    assert evidence["web_tool_call_count"] == check["web_tool_call_count"]
    assert [c["url"] for c in evidence["citations"]] == check["evidence_urls"]
    assert evidence["command_exit_code"] == check["exit_code"]
    assert evidence["agy_cli_version"] == result["agy"]["version"]
    assert evidence["transcript_evidence"][0]["sha256"] == module.hashlib.sha256(
        check["stdout_sample"].encode("utf-8")
    ).hexdigest()
    assert evidence["transcript_evidence"][0]["sha256"] != "captured_in_preflight_stdout_sample"

    for citation in evidence["citations"]:
        assert citation["url"] in markdown
    assert f"agy_cli_version: \"{result['agy']['version']}\"" in markdown
    assert "captured_in_preflight_stdout_sample" not in markdown


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
    _write_manifest(module, tmp_path)
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
    assert result["local_asset_research"]["config_path"] == ".agents/mcp_config.json"


def test_local_asset_research_live_serena_result_surface(monkeypatch, tmp_path):
    """AC10: --live-serena returns called tools, transcript, and evidence count."""
    module = load_module()
    monkeypatch.chdir(tmp_path)
    _write_manifest(module, tmp_path)
    _write_settings(
        tmp_path,
        include_tools=sorted(module.SERENA_READ_ONLY_TOOLS),
        exclude_tools=sorted(module.SERENA_DANGEROUS_TOOLS),
    )
    monkeypatch.setattr(module, "_run", _make_happy_run(module))

    def fake_live(repo_root, manifest, mcp_config_path=None, timeout_sec=60.0):
        return {
            "ok": True,
            "transport": "mcp_stdio",
            "server_started": True,
            "initialized": True,
            "tools_list_checked": True,
            "tools_seen": ["find_file", "search_for_pattern", "get_symbols_overview"],
            "called_tools": ["find_file", "search_for_pattern", "get_symbols_overview"],
            "evidence_envelope_count": 3,
            "transcript": [
                {"event": "mcp_server_launch"},
                {"event": "mcp_response", "method": "tools/list"},
                {"event": "evidence_envelope_created", "tool_name": "find_file"},
            ],
        }

    monkeypatch.setattr(module, "_call_serena_mcp_live", fake_live)
    result = module.run_preflight(validate_local_asset_contract=True, live_serena=True)

    assert result["ok"] is True
    assert result["local_asset_research"]["serena"]["called_tools"] == [
        "find_file",
        "search_for_pattern",
        "get_symbols_overview",
    ]
    assert result["local_asset_research"]["serena"]["evidence_envelope_count"] == 3
    assert result["local_asset_research"]["live_transcript"][0]["event"] == "mcp_server_launch"


def test_local_asset_research_contract_validation_rejects_unknown_tool(monkeypatch, tmp_path):
    """--local-asset-research rejects unknown tools / drift in Serena allowlist."""
    module = load_module()
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)  # type: ignore[call-arg]
    _write_manifest(module, tmp_path)
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


def test_local_asset_research_contract_validation_rejects_unpinned_serena(monkeypatch, tmp_path):
    """--local-asset-research rejects an unpinned Serena source."""
    module = load_module()
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)  # type: ignore[call-arg]
    _write_manifest(module, tmp_path)
    _write_settings(
        tmp_path,
        include_tools=sorted(module.SERENA_READ_ONLY_TOOLS),
        exclude_tools=sorted(module.SERENA_DANGEROUS_TOOLS),
        pinned=False,
    )

    monkeypatch.setattr(module, "_run", _make_happy_run(module))
    result = module.run_preflight(validate_local_asset_contract=True)

    assert result["ok"] is False
    assert result["failure_class"] == "local_asset_contract_invalid"
    assert any("pinned_serena_manifest_mismatch" in item for item in result["local_asset_research"]["errors"])


def test_local_asset_research_contract_validation_rejects_manifest_settings_mismatch(monkeypatch, tmp_path):
    """--local-asset-research rejects a manifest/settings pinned_ref mismatch."""
    module = load_module()
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)  # type: ignore[call-arg]
    _write_manifest(module, tmp_path, pinned_ref="abcdef0123456789")
    _write_settings(
        tmp_path,
        include_tools=sorted(module.SERENA_READ_ONLY_TOOLS),
        exclude_tools=sorted(module.SERENA_DANGEROUS_TOOLS),
        pinned=True,
    )

    monkeypatch.setattr(module, "_run", _make_happy_run(module))
    result = module.run_preflight(validate_local_asset_contract=True)

    assert result["ok"] is False
    assert result["failure_class"] == "local_asset_contract_invalid"
    assert any("pinned_serena_manifest_mismatch" in item for item in result["local_asset_research"]["errors"])


def test_local_asset_research_contract_validation_rejects_missing_manifest(monkeypatch, tmp_path):
    """--local-asset-research requires the checked-in Serena manifest."""
    module = load_module()
    monkeypatch.setattr(module, "_repo_root", lambda: tmp_path)  # type: ignore[call-arg]
    _write_settings(
        tmp_path,
        include_tools=sorted(module.SERENA_READ_ONLY_TOOLS),
        exclude_tools=sorted(module.SERENA_DANGEROUS_TOOLS),
    )

    monkeypatch.setattr(module, "_run", _make_happy_run(module))
    result = module.run_preflight(validate_local_asset_contract=True)

    assert result["ok"] is False
    assert result["failure_class"] == "local_asset_contract_invalid"
    assert any("serena manifest validation failed" in item for item in result["local_asset_research"]["errors"])


# ---------------------------------------------------------------------------
# Issue #1267: agy_auth_diagnostics_v1 — auth mode / keyring / TTY / WSL2 diagnostics
# ---------------------------------------------------------------------------


def test_auth_diagnostics_present_in_every_result_path(monkeypatch):
    """AC2: every agy_preflight_result/v1 path (here: cli_missing) includes auth."""
    module = load_module()

    def fake_run(argv, cwd=None, timeout=None):
        raise FileNotFoundError("agy not found")

    monkeypatch.setattr(module, "_run", fake_run)
    result = module.run_preflight()

    assert result["failure_class"] == "cli_missing"
    assert result["auth"]["checked"] is True
    assert "auth_mode" in result["auth"]
    assert "keyring" in result["auth"]
    assert "tty" in result["auth"]
    assert "platform" in result["auth"]


def test_auth_diagnostics_noninteractive_tty_detected(monkeypatch):
    """GIVEN stdin/stdout/stderr are not a TTY
    WHEN _detect_tty is called
    THEN noninteractive_mode is True and all isatty flags are False."""
    module = load_module()

    class _NonTTYStream:
        def isatty(self):
            return False

    monkeypatch.setattr(module.sys, "stdin", _NonTTYStream())
    monkeypatch.setattr(module.sys, "stdout", _NonTTYStream())
    monkeypatch.setattr(module.sys, "stderr", _NonTTYStream())

    tty_info = module._detect_tty()

    assert tty_info["stdin_isatty"] is False
    assert tty_info["stdout_isatty"] is False
    assert tty_info["stderr_isatty"] is False
    assert tty_info["noninteractive_mode"] is True


def test_auth_diagnostics_wsl2_detected_via_env(monkeypatch):
    """GIVEN WSL_DISTRO_NAME is present
    WHEN _detect_platform is called
    THEN is_wsl is True with a wsl_hint, and no env value leaks into the hint."""
    module = load_module()

    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-22.04")
    platform_info = module._detect_platform()

    assert platform_info["is_wsl"] is True
    assert platform_info["wsl_hint"] == "env:WSL_DISTRO_NAME"
    assert "Ubuntu-22.04" not in str(platform_info)


def test_auth_diagnostics_keyring_unavailable_on_wsl_without_dbus(monkeypatch):
    """GIVEN WSL2 platform AND no D-Bus session bus
    WHEN _detect_keyring is called
    THEN failure_class is system_keyring_unavailable (known WSL2 issue)."""
    module = load_module()

    env_snapshot = {
        "DBUS_SESSION_BUS_ADDRESS_present": False,
        "DISPLAY_present": False,
        "WAYLAND_DISPLAY_present": False,
        "WSL_INTEROP_present": True,
        "WSL_DISTRO_NAME_present": True,
    }
    platform_info = {"os": "linux", "is_wsl": True, "wsl_hint": "env:WSL_DISTRO_NAME"}

    keyring_info = module._detect_keyring(env_snapshot, platform_info)

    assert keyring_info["available"] is False
    assert keyring_info["failure_class"] == "system_keyring_unavailable"


def test_auth_diagnostics_dbus_missing_hint_without_wsl(monkeypatch):
    """GIVEN no D-Bus session bus and no display, not WSL
    WHEN _detect_keyring is called
    THEN failure_class is system_keyring_probe_unsupported (unknown, not asserted False)."""
    module = load_module()

    env_snapshot = {
        "DBUS_SESSION_BUS_ADDRESS_present": False,
        "DISPLAY_present": False,
        "WAYLAND_DISPLAY_present": False,
        "WSL_INTEROP_present": False,
        "WSL_DISTRO_NAME_present": False,
    }
    platform_info = {"os": "linux", "is_wsl": False, "wsl_hint": None}

    keyring_info = module._detect_keyring(env_snapshot, platform_info)

    assert keyring_info["available"] is None
    assert keyring_info["failure_class"] == "system_keyring_probe_unsupported"


def test_auth_signal_google_sign_in_required(monkeypatch):
    """GIVEN agy stderr mentions Google Sign-In requirement
    WHEN smoke fails with this stderr
    THEN failure_class is reclassified to google_sign_in_required (explicit evidence)."""
    module = load_module()

    def fake_run(argv, cwd=None, timeout=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(0, "  -p, --print, --prompt  mode\n", "")
        if argv[:2] == [bin_, "-p"]:
            return _FakeCompleted(1, "", "Error: please sign in with your Google account to continue.")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)
    result = module.run_preflight()

    assert result["ok"] is False
    assert result["failure_class"] == "google_sign_in_required"
    assert result["smoke"]["failure_class"] == "google_sign_in_required"
    assert result["auth"]["auth_mode"] == "google_sign_in_required"
    assert result["auth"]["auth_mode_confidence"] == "observed"


def test_auth_signal_keyring_locked_reclassifies_smoke_failure(monkeypatch):
    """GIVEN agy stderr mentions the keyring being locked
    WHEN smoke fails with this stderr
    THEN failure_class is reclassified to system_keyring_locked."""
    module = load_module()

    def fake_run(argv, cwd=None, timeout=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(0, "  -p, --print, --prompt  mode\n", "")
        if argv[:2] == [bin_, "-p"]:
            return _FakeCompleted(1, "", "Error: the system keyring is locked; cannot read credentials.")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)
    result = module.run_preflight()

    assert result["ok"] is False
    assert result["failure_class"] == "system_keyring_locked"
    assert result["auth"]["keyring"]["failure_class"] == "system_keyring_locked"
    assert result["auth"]["keyring"]["available"] is False


def test_empty_stdout_is_not_reclassified_as_auth_failure(monkeypatch):
    """Required Result Contract: empty stdout with no auth/keyring evidence in
    stderr/stdout MUST remain an output-surface failure (agy_empty_stdout), not an
    auth failure."""
    import os

    module = load_module()
    original_ci = os.environ.get("CI")
    os.environ.pop("CI", None)

    def fake_run(argv, cwd=None, timeout=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(0, "  -p, --print, --prompt  mode\n", "")
        if argv[:2] == [bin_, "-p"]:
            return _FakeCompleted(0, "", "")  # exit 0, empty stdout, empty stderr
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)
    result = module.run_preflight()

    assert result["ok"] is False
    assert result["failure_class"] == "agy_empty_stdout"
    assert result["smoke"]["failure_class"] == "agy_empty_stdout"

    if original_ci is None:
        os.environ.pop("CI", None)
    else:
        os.environ["CI"] = original_ci


def test_auth_diagnostics_redacts_env_values_and_secrets(monkeypatch):
    """AC4: auth diagnostics must never include raw env var values, only booleans,
    and secrets in stderr must remain redacted in the surfaced result."""
    import os

    module = load_module()
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    monkeypatch.setenv("AGY_API_KEY", "sk-SENTINEL-DO-NOT-LEAK-0001")

    def fake_run(argv, cwd=None, timeout=None):
        bin_ = module._resolve_binary()
        if argv == [bin_, "--version"]:
            return _FakeCompleted(0, "agy 1.0.0\n", "")
        if argv == [bin_, "--help"]:
            return _FakeCompleted(0, "  -p, --print, --prompt  mode\n", "")
        if argv[:2] == [bin_, "-p"]:
            return _FakeCompleted(
                1,
                "",
                "auth failed: credential sk-SENTINEL-DO-NOT-LEAK-0001 rejected, keyring backend not found",
            )
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module, "_run", fake_run)
    result = module.run_preflight()

    serialized = json.dumps(result)
    assert "unix:path=/run/user/1000/bus" not in serialized
    assert "sk-SENTINEL-DO-NOT-LEAK-0001" not in serialized
    assert result["auth"]["keyring"]["failure_class"] == "system_keyring_backend_missing"
