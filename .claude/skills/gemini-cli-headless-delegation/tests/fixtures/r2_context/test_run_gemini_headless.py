"""Minimal fixture: representative excerpt of test_run_gemini_headless.py for R2 context."""
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


def test_build_raw_command_contains_model(tmp_path):
    module = load_module()
    command = module._build_raw_command("gemini-3-flash-preview", "prompt")
    assert "--model" in command
