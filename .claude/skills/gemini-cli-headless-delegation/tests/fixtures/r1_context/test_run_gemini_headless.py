"""Minimal fixture: representative excerpt of test_run_gemini_headless.py for R1 context."""
from __future__ import annotations

import importlib.util
from pathlib import Path



def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "run_gemini_headless.py"
    spec = importlib.util.spec_from_file_location("run_gemini_headless", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_validate_request_rejects_vague_request(tmp_path, monkeypatch):
    module = load_module()
    monkeypatch.chdir(tmp_path)
    request = {
        "schema": "delegation_request_v1",
        "objective": "investigate",
        "instructions": ["Summarize", "Compare"],
        "tool_profile": "no_tools",
        "output_sections": ["Summary"],
        "context_files": ["context.md"],
    }
    (tmp_path / "context.md").write_text("context", encoding="utf-8")
    errors = module.validate_request(request)
    assert "objective is too vague" in errors
