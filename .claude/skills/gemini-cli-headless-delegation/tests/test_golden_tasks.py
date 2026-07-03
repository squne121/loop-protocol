"""
Golden task deterministic-ish tests.

Covers verification-plan.md Section 3:
  D-1..D-3   request validation
  D-5..D-7   prompt construction
  D-8..D-11  CLI command construction
  D-12..D-13 result shape (mock Gemini)

Intentionally NOT covered here:
  D-4  build_prompt が context_files 内容を反映する
       → test_run_gemini_headless.py::test_build_prompt_includes_context_and_sections で既存カバー済み

Fixture path note (R1/R2):
  verification-plan.md の request JSON テンプレートは context_files に
  "tests/test_run_gemini_headless.py" 等の実ファイルパスを示しているが、
  本 fixture では実行環境依存を避けるため、小さな固定テキストの代表抜粋を
  tests/fixtures/r{1,2}_context/ に配置し、context_files パスをその構造に合わせている。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "run_gemini_headless.py"
    spec = importlib.util.spec_from_file_location("run_gemini_headless", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# D-1 / D-2 / D-3  Request validation
# ---------------------------------------------------------------------------

def test_d1_r0_request_validates():
    module = load_module()
    request = json.loads((FIXTURES_DIR / "r0_request.json").read_text(encoding="utf-8"))
    errors = module.validate_request(request, request_path=FIXTURES_DIR / "r0_request.json")
    assert errors == []


def test_d2_r1_request_validates():
    module = load_module()
    request = json.loads((FIXTURES_DIR / "r1_request.json").read_text(encoding="utf-8"))
    errors = module.validate_request(request, request_path=FIXTURES_DIR / "r1_request.json")
    assert errors == []


def test_d3_r2_request_validates():
    module = load_module()
    request = json.loads((FIXTURES_DIR / "r2_request.json").read_text(encoding="utf-8"))
    errors = module.validate_request(request, request_path=FIXTURES_DIR / "r2_request.json")
    assert errors == []


# ---------------------------------------------------------------------------
# D-5  R0 output_sections in prompt
# ---------------------------------------------------------------------------

def test_d5_r0_output_sections_in_prompt(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = {
        "objective": "ビルドログ logs/build.log の失敗原因を特定し根拠を列挙する",
        "instructions": [
            "ビルド失敗の主因を 1 文で要約してください。",
            "失敗の根拠となる行を 5 件以上、行番号付きで列挙してください。",
            "context_files 以外の情報源を参照しないでください。",
        ],
        "tool_profile": "no_tools",
        "output_sections": ["Summary", "Findings", "Evidence"],
        "model": "gemini-3-flash-preview",
    }
    prompt = module.build_prompt(request, [{"path": "r0_build.log", "content": "build log"}])
    assert "- Summary" in prompt
    assert "- Findings" in prompt
    assert "- Evidence" in prompt


# ---------------------------------------------------------------------------
# D-6  R1 output_sections in prompt
# ---------------------------------------------------------------------------

def test_d6_r1_output_sections_in_prompt(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = {
        "objective": "run_gemini_headless.py の _build_raw_command 関数に対する pytest テストスケルトンを生成する",
        "instructions": [
            "既存の test_run_gemini_headless.py のパターンに従ってください。",
            "テスト名は test_ prefix で始めてください。",
            "構造（import、fixture、assert 文）は完全なものにしてください。",
        ],
        "tool_profile": "no_tools",
        "output_sections": ["Draft", "Notes"],
        "model": "gemini-3-flash-preview",
    }
    prompt = module.build_prompt(request, [{"path": "context.py", "content": "test context"}])
    assert "- Draft" in prompt
    assert "- Notes" in prompt


# ---------------------------------------------------------------------------
# D-7  inline_context is included in prompt
# ---------------------------------------------------------------------------

def test_d7_inline_context_in_prompt():
    module = load_module()
    request = {
        "objective": "Investigate build failure in logs/build.log",
        "instructions": ["Summarize the failure.", "List likely root causes."],
        "tool_profile": "no_tools",
        "output_sections": ["Summary"],
        "inline_context": "Extra inline context for this request",
        "model": "gemini-3-flash-preview",
    }
    prompt = module.build_prompt(request, [])
    assert "Extra inline context for this request" in prompt


# ---------------------------------------------------------------------------
# D-8  --approval-mode plan in _build_raw_command output
# ---------------------------------------------------------------------------

def test_d8_build_raw_command_contains_approval_mode():
    module = load_module()
    command = module._build_raw_command("gemini-3-flash-preview")
    assert "--approval-mode" in command
    idx = command.index("--approval-mode")
    assert command[idx + 1] == "plan"


# ---------------------------------------------------------------------------
# D-9  --output-format json in _build_raw_command output
# ---------------------------------------------------------------------------

def test_d9_build_raw_command_contains_output_format():
    module = load_module()
    command = module._build_raw_command("gemini-3-flash-preview")
    assert "--output-format" in command
    idx = command.index("--output-format")
    assert command[idx + 1] == "json"


# ---------------------------------------------------------------------------
# D-10  --model <model> in _build_raw_command output
# ---------------------------------------------------------------------------

def test_d10_build_raw_command_contains_model():
    module = load_module()
    model = "gemini-3-flash-preview"
    command = module._build_raw_command(model)
    assert "--model" in command
    idx = command.index("--model")
    assert command[idx + 1] == model


# ---------------------------------------------------------------------------
# D-11  grounded_research → "AGY native WebSearch" injected
#        no_tools → NOT injected
# (note: implemented via build_prompt text injection, not --tools CLI flag)
# ---------------------------------------------------------------------------

def test_d11_grounded_research_prompt_contains_google_search():
    module = load_module()
    request = {
        "objective": "Investigate build failure in logs/build.log",
        "instructions": ["Summarize the failure.", "List likely root causes."],
        "tool_profile": "grounded_research",
        "output_sections": ["Summary"],
        "model": "gemini-3-flash-preview",
    }
    prompt = module.build_prompt(request, [])
    assert "AGY native WebSearch" in prompt


def test_d11_no_tools_prompt_does_not_contain_google_search():
    module = load_module()
    request = {
        "objective": "Investigate build failure in logs/build.log",
        "instructions": ["Summarize the failure.", "List likely root causes."],
        "tool_profile": "no_tools",
        "output_sections": ["Summary"],
        "model": "gemini-3-flash-preview",
    }
    prompt = module.build_prompt(request, [])
    assert "AGY native WebSearch" not in prompt


# ---------------------------------------------------------------------------
# D-12  R0 mock run produces delegation_result_v1 with all required fields
# ---------------------------------------------------------------------------

_REQUIRED_RESULT_FIELDS = {
    "ok",
    "requested_model",
    "actual_model",
    "tool_profile",
    "exit_code",
    "response_text",
    "stats",
    "stderr",
    "warnings",
    "raw_command",
}

_R0_MOCK_RESPONSE = (
    "## Summary\nBuild failed due to ImportError.\n"
    "## Findings\nLine 4: ImportError: No module named 'bar'\n"
    "## Evidence\ntests/test_importer.py::test_import_bar - ImportError"
)

_R0_MOCK_STDOUT = json.dumps({
    "response": _R0_MOCK_RESPONSE,
    "stats": {"models": {"gemini-3-flash-preview": {"api": {"totalRequests": 1}}}},
})


def _make_r0_request():
    return {
        "schema": "delegation_request_v1",
        "objective": "ビルドログ logs/build.log の失敗原因を特定し根拠を列挙する",
        "instructions": [
            "ビルド失敗の主因を 1 文で要約してください。",
            "失敗の根拠となる行を 5 件以上、行番号付きで列挙してください。",
            "context_files 以外の情報源を参照しないでください。",
        ],
        "tool_profile": "no_tools",
        "output_sections": ["Summary", "Findings", "Evidence"],
        "context_files": ["context.md"],
        "model": "gemini-3-flash-preview",
        "timeout_sec": 30,
    }


def test_d12_r0_mock_result_has_all_required_fields(tmp_path, monkeypatch):
    module = load_module()
    # monkeypatch.chdir は不要: request_path=tmp_path/"request.json" を渡すため
    # validate_request は request_path.parent = tmp_path を基準に context_files を解決する。
    (tmp_path / "context.md").write_text("build log content", encoding="utf-8")

    class MockCompleted:
        returncode = 0
        stdout = _R0_MOCK_STDOUT
        stderr = ""

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: MockCompleted())

    result = module.run_delegation(_make_r0_request(), request_path=tmp_path / "request.json")

    for field in _REQUIRED_RESULT_FIELDS:
        assert field in result, f"missing required field: {field}"


# ---------------------------------------------------------------------------
# D-13  R0 mock: ok==True, response_text contains Summary/Findings/Evidence
# ---------------------------------------------------------------------------

def test_d13_r0_mock_response_contains_sections(tmp_path, monkeypatch):
    module = load_module()
    # monkeypatch.chdir は不要: request_path=tmp_path/"request.json" を渡すため
    # validate_request は request_path.parent = tmp_path を基準に context_files を解決する。
    (tmp_path / "context.md").write_text("build log content", encoding="utf-8")

    class MockCompleted:
        returncode = 0
        stdout = _R0_MOCK_STDOUT
        stderr = ""

    monkeypatch.setattr(module, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: MockCompleted())

    result = module.run_delegation(_make_r0_request(), request_path=tmp_path / "request.json")

    assert result["ok"] is True
    assert "Summary" in result["response_text"]
    assert "Findings" in result["response_text"]
    assert "Evidence" in result["response_text"]
