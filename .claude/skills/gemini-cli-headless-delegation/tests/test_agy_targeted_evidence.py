"""Tests for AGY local_asset_research targeted source-evidence contract (Issue #1638).

Covers AC1-AC4 for the ``evidence_targets`` request contract: schema/selector
fail-close (AC1), bounded source-evidence envelopes with full provenance
(AC2), fail-close before AGY launch on unmet/empty/oversized/credential-like
evidence (AC3), and prompt redaction boundaries on the successful path (AC4).
"""
from __future__ import annotations

import importlib.util
import subprocess
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_gemini_headless.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("run_gemini_headless_targeted_evidence", _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rgh = _load_module()


def _make_completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["agy", "-p", "test"], returncode=returncode, stdout=stdout, stderr=stderr)


def _targeted_request(evidence_targets: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    base = {
        "schema": "delegation_request_v1",
        "tool_profile": "local_asset_research",
        "provider": "agy",
        "prompt": "Summarize the requested implementation lines.",
        "objective": "Targeted source evidence request for consumer contract verification",
        "instructions": [
            "Summarize the requested implementation lines.",
            "Use only the provided target evidence.",
        ],
        "output_sections": ["response"],
        "evidence_targets": evidence_targets,
    }
    base.update(kwargs)
    return base


def _forbid_agy_launch(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    raise AssertionError("AGY subprocess must not be launched (fail-closed request)")


def _setup_repo(tmp_path: Path, monkeypatch: Any) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.setattr(rgh, "_repo_root", lambda: repo_root)  # type: ignore[call-arg]
    monkeypatch.setattr(rgh, "_validate_local_asset_research_settings", lambda: [])  # type: ignore[call-arg]
    return repo_root


# ---------------------------------------------------------------------------
# AC1: targeted-evidence request rejects unsafe schema / repo-outside path /
# symlink crossing / unauthorized selector before AGY ever launches.
# ---------------------------------------------------------------------------


def test_targeted_evidence_request_rejects_unsafe_selector(tmp_path, monkeypatch) -> None:
    repo_root = _setup_repo(tmp_path, monkeypatch)
    target_file = repo_root / "foo.py"
    target_file.write_text("\n".join(f"line{i}" for i in range(1, 20)) + "\n", encoding="utf-8")

    with patch.object(rgh, "_run_agy", side_effect=_forbid_agy_launch):
        # Unknown/unauthorized selector kind.
        result = rgh.run_delegation(
            _targeted_request([{"path": "foo.py", "selector": {"kind": "regex", "pattern": ".*"}}]),
            request_path=repo_root / "request.json",
        )
    assert result["ok"] is False
    assert "selector.kind must be one of" in result["failure_reason"]

    with patch.object(rgh, "_run_agy", side_effect=_forbid_agy_launch):
        # Selector line range exceeds the bounded per-target maximum.
        result = rgh.run_delegation(
            _targeted_request(
                [{"path": "foo.py", "selector": {"kind": "line_range", "start_line": 1, "end_line": 5000}}]
            ),
            request_path=repo_root / "request.json",
        )
    assert result["ok"] is False
    assert "must not exceed" in result["failure_reason"]

    with patch.object(rgh, "_run_agy", side_effect=_forbid_agy_launch):
        # Repo-outside path.
        result = rgh.run_delegation(
            _targeted_request(
                [{"path": "../outside.py", "selector": {"kind": "line_range", "start_line": 1, "end_line": 2}}]
            ),
            request_path=repo_root / "request.json",
        )
    assert result["ok"] is False
    assert "must be inside repository" in result["failure_reason"]

    with patch.object(rgh, "_run_agy", side_effect=_forbid_agy_launch):
        # Malformed target entry (not an object).
        result = rgh.run_delegation(
            _targeted_request([123]),  # type: ignore[list-item]
            request_path=repo_root / "request.json",
        )
    assert result["ok"] is False
    assert "evidence_targets[0] must be an object" in result["failure_reason"]


def test_targeted_evidence_request_rejects_symlink_crossing(tmp_path, monkeypatch) -> None:
    repo_root = _setup_repo(tmp_path, monkeypatch)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    secret_file = outside_dir / "secret.py"
    secret_file.write_text("secret = 1\n", encoding="utf-8")
    symlink_dir = repo_root / "linked"
    symlink_dir.symlink_to(outside_dir, target_is_directory=True)

    with patch.object(rgh, "_run_agy", side_effect=_forbid_agy_launch):
        result = rgh.run_delegation(
            _targeted_request(
                [{"path": "linked/secret.py", "selector": {"kind": "line_range", "start_line": 1, "end_line": 1}}]
            ),
            request_path=repo_root / "request.json",
        )
    assert result["ok"] is False
    # The path resolves outside the repository once the symlink is followed,
    # so the repo-boundary check (not just the ancestor symlink scan) is what
    # closes the escape; either message form is an acceptable fail-close.
    assert (
        "must not include symlink paths" in result["failure_reason"]
        or "must be inside repository" in result["failure_reason"]
    )


# ---------------------------------------------------------------------------
# AC2: wrapper-side retrieval builds a bounded evidence envelope with source
# text, repo-relative path, selector, line range, sha256, and source kind.
# ---------------------------------------------------------------------------


def test_targeted_evidence_envelope_contains_source_text_and_provenance(tmp_path, monkeypatch) -> None:
    repo_root = _setup_repo(tmp_path, monkeypatch)
    target_file = repo_root / "pkg" / "foo.py"
    target_file.parent.mkdir(parents=True)
    target_file.write_text(
        "\n".join(["def alpha():", "    return 1", "", "def beta():", "    return 2"]) + "\n",
        encoding="utf-8",
    )

    captured: dict[str, str] = {}

    def _run_agy(prompt: str, timeout_sec: int = rgh.DEFAULT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
        captured["value"] = prompt
        return _make_completed(0, stdout="LOOP_AGY_TARGETED_OK")

    request = _targeted_request(
        [{"path": "pkg/foo.py", "selector": {"kind": "line_range", "start_line": 1, "end_line": 2}}]
    )
    with patch.object(rgh, "_run_agy", side_effect=_run_agy):
        result = rgh.run_delegation(request, request_path=repo_root / "request.json")

    assert result["ok"] is True
    assert result["response_text"] == "LOOP_AGY_TARGETED_OK"
    prompt = captured["value"]
    assert '"repo_relative_path": "pkg/foo.py"' in prompt
    assert '"line_range": [\n      1,\n      2\n    ]' in prompt or '"line_range": [1, 2]' in prompt
    assert '"source_kind": "wrapper_read_only_targeted_evidence"' in prompt
    assert "\"sha256\":" in prompt
    assert "def alpha():" in prompt
    assert "    return 1" in prompt
    # Bounded: line 4-5 content must not leak when only lines 1-2 were requested.
    assert "def beta():" not in prompt

    metadata = result.get("local_asset_retrieval_metadata")
    assert metadata is not None
    assert metadata["retrieval_mode"] == "wrapper_read_only_targeted_evidence"
    assert metadata["evidence_record_count"] == 1


# ---------------------------------------------------------------------------
# AC3: missing / empty / out-of-range / credential-like target evidence fails
# closed before an AGY subprocess is ever launched (never metadata-only success).
# ---------------------------------------------------------------------------


def test_metadata_only_target_evidence_fails_closed_before_agy_launch(tmp_path, monkeypatch) -> None:
    repo_root = _setup_repo(tmp_path, monkeypatch)
    target_file = repo_root / "small.py"
    target_file.write_text("only_one_line = True\n", encoding="utf-8")

    # Selector requests lines that do not exist in the file (target unmet).
    with patch.object(rgh, "_run_agy", side_effect=_forbid_agy_launch):
        result = rgh.run_delegation(
            _targeted_request(
                [{"path": "small.py", "selector": {"kind": "line_range", "start_line": 5, "end_line": 20}}]
            ),
            request_path=repo_root / "request.json",
        )
    assert result["ok"] is False
    assert "target unmet" in result["failure_reason"]
    assert "exceeds file length" in result["failure_reason"]

    # Selector resolves to a blank-only range (empty evidence).
    blank_file = repo_root / "blank.py"
    blank_file.write_text("code = 1\n\n\n\ncode = 2\n", encoding="utf-8")
    with patch.object(rgh, "_run_agy", side_effect=_forbid_agy_launch):
        result = rgh.run_delegation(
            _targeted_request(
                [{"path": "blank.py", "selector": {"kind": "line_range", "start_line": 2, "end_line": 3}}]
            ),
            request_path=repo_root / "request.json",
        )
    assert result["ok"] is False
    assert "empty evidence" in result["failure_reason"]

    # Credential-like target content.
    secret_file = repo_root / "secret.py"
    secret_file.write_text("token = 'sk-1234567890abcdefghij'\n", encoding="utf-8")
    with patch.object(rgh, "_run_agy", side_effect=_forbid_agy_launch):
        result = rgh.run_delegation(
            _targeted_request(
                [{"path": "secret.py", "selector": {"kind": "line_range", "start_line": 1, "end_line": 1}}]
            ),
            request_path=repo_root / "request.json",
        )
    assert result["ok"] is False
    assert "credential-like material" in result["failure_reason"]


# ---------------------------------------------------------------------------
# AC4: successful AGY prompt contains only target evidence -- no repo-absolute
# path, MCP config, direct tool access, or credential-like payload.
# ---------------------------------------------------------------------------


def test_targeted_evidence_prompt_redacts_forbidden_data(tmp_path, monkeypatch) -> None:
    repo_root = _setup_repo(tmp_path, monkeypatch)
    target_file = repo_root / "pkg" / "bar.py"
    target_file.parent.mkdir(parents=True)
    target_file.write_text(
        "\n".join(["class Bar:", "    def run(self):", "        return 'ok'"]) + "\n",
        encoding="utf-8",
    )

    captured: dict[str, str] = {}

    def _run_agy(prompt: str, timeout_sec: int = rgh.DEFAULT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
        captured["value"] = prompt
        return _make_completed(0, stdout="LOOP_AGY_TARGETED_OK")

    request = _targeted_request(
        [{"path": "pkg/bar.py", "selector": {"kind": "line_range", "start_line": 1, "end_line": 3}}]
    )
    with patch.object(rgh, "_run_agy", side_effect=_run_agy):
        result = rgh.run_delegation(request, request_path=repo_root / "request.json")

    assert result["ok"] is True
    prompt = captured["value"]
    assert "AGY is executed in prompt-only wrapper-side evidence mode" in prompt
    assert str(repo_root) not in prompt
    assert str(repo_root.resolve()) not in prompt
    assert "mcpServers" not in prompt
    assert "mcp_config" not in prompt
    assert "no repo path, " in prompt
    assert "no MCP/server access" in prompt
    assert not rgh._contains_credential(prompt)
