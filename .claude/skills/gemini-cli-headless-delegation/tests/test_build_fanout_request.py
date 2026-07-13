"""Tests for build_fanout_request.py (Issue #1273 AC3, iteration 3 Major 3:
--provider-concurrency / --profile-concurrency CLI support)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "build_fanout_request.py"
    module_name = "build_fanout_request_test_module"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _write_subtask_request(tmp_path: Path, name: str) -> Path:
    ctx_file = tmp_path / "ctx.md"
    ctx_file.write_text("context", encoding="utf-8")
    request = {
        "schema": "delegation_request_v1",
        "provider": "gemini",
        "tool_profile": "no_tools",
        "objective": "Investigate scripts/build_fanout_request.py test fixture",
        "instructions": ["a", "b"],
        "output_sections": ["Summary"],
        "context_files": [str(ctx_file)],
    }
    path = tmp_path / name
    path.write_text(json.dumps(request), encoding="utf-8")
    return path


def test_provider_and_profile_concurrency_are_included(tmp_path):
    module = load_module()
    subtask_path = _write_subtask_request(tmp_path, "subtask.json")
    output_path = tmp_path / "fanout-request.json"

    exit_code = module.main(
        [
            "--subtask-request",
            str(subtask_path),
            "--provider-concurrency",
            "gemini=2",
            "--provider-concurrency",
            "agy=1",
            "--profile-concurrency",
            "github_research=1",
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    request = json.loads(output_path.read_text(encoding="utf-8"))
    assert request["provider_concurrency"] == {"gemini": 2, "agy": 1}
    assert request["profile_concurrency"] == {"github_research": 1}


def test_malformed_concurrency_entry_rejected(tmp_path):
    module = load_module()
    subtask_path = _write_subtask_request(tmp_path, "subtask.json")
    output_path = tmp_path / "fanout-request.json"

    exit_code = module.main(
        [
            "--subtask-request",
            str(subtask_path),
            "--provider-concurrency",
            "gemini",  # missing '='
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 1
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["failure_class"] == "validation_error"


def test_no_concurrency_flags_omits_keys_entirely(tmp_path):
    module = load_module()
    subtask_path = _write_subtask_request(tmp_path, "subtask.json")
    output_path = tmp_path / "fanout-request.json"

    exit_code = module.main(
        ["--subtask-request", str(subtask_path), "--output", str(output_path)]
    )

    assert exit_code == 0
    request = json.loads(output_path.read_text(encoding="utf-8"))
    assert "provider_concurrency" not in request
    assert "profile_concurrency" not in request
