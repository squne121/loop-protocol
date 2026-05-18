"""Minimal fixture: representative excerpt of run_gemini_headless.py for R1 context."""
from __future__ import annotations

DEFAULT_MODEL = "gemini-3-flash-preview"


def _build_raw_command(model: str, prompt: str) -> list[str]:
    return [
        "gemini",
        "--model",
        model,
        "--approval-mode",
        "plan",
        "--prompt",
        prompt,
        "--output-format",
        "json",
    ]
