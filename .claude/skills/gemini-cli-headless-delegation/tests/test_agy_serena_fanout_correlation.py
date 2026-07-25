"""Tests for fan-out ``local_asset_research`` Serena evidence task-linked
hash chain / correlation (Issue #1706).

Builds on the #1638 targeted-evidence contract (repo-relative path / bounded
``line_range`` selector / fail-close evidence collection) and adds, for
requests that ``fan_out_orchestrator.run_fanout()`` stamps with
``parent_run_id`` / ``subtask_id`` / ``attempt_id``:

- an objective-derived Serena ``tools/call`` selector instead of the legacy
  fixed ``"local_asset_research"`` smoke-query literal (AC1);
- a deterministic ``objective_sha256`` / ``target_contract_sha256`` hash pair
  (AC2);
- a tamper-evident ``evidence_sha256`` over the canonical evidence record set
  (AC3);
- an ``prompt_envelope_sha256`` deterministically derived from
  ``evidence_sha256`` and injected into the literal AGY prompt text (AC4);
- a ``result_binding_sha256`` that ties the child result back to the same
  evidence/prompt hashes, verifiable (and tamper-detectable) independently of
  the wrapper via ``verify_serena_hash_chain()`` (AC5);
- full actor/correlation/provenance fields on the evidence records (AC6);
- an explicit ``retrieval_actor`` / ``analysis_actor`` /
  ``agy_direct_mcp_access`` actor boundary, with no repo-absolute path, MCP
  config, or direct tool access ever reaching the AGY subprocess (AC7);
- fail-close before AGY launch when evidence is unrelated to the subtask
  objective, empty, or metadata-only (AC8).

AC9 (no regression on #1638's existing AC1-AC6 tests) is covered by
``test_agy_targeted_evidence.py`` itself, unmodified by this Issue.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
_SCRIPT_PATH = _SCRIPTS_DIR / "run_gemini_headless.py"
_REAL_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1] / "references" / "serena-tool-manifest.json"
)


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("run_gemini_headless_fanout_correlation", _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rgh = _load_module()


def _make_completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["agy", "-p", "test"], returncode=returncode, stdout=stdout, stderr=stderr)


def _forbid_agy_launch(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    raise AssertionError("AGY subprocess must not be launched (fail-closed request)")


def _setup_repo(tmp_path: Path, monkeypatch: Any, *, with_manifest: bool = False) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.setattr(rgh, "_repo_root", lambda: repo_root)  # type: ignore[call-arg]
    monkeypatch.setattr(rgh, "_validate_local_asset_research_settings", lambda: [])  # type: ignore[call-arg]
    if with_manifest:
        manifest_dest = repo_root / rgh.SERENA_TOOL_MANIFEST_RELATIVE_PATH
        manifest_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_REAL_MANIFEST_PATH, manifest_dest)
    return repo_root


def _fanout_request(
    evidence_targets: list[dict[str, Any]],
    *,
    objective: str,
    parent_run_id: str = "parent-run-1",
    subtask_id: str = "subtask-1",
    attempt_id: str = "attempt-1",
    **kwargs: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": "delegation_request_v1",
        "tool_profile": "local_asset_research",
        "provider": "agy",
        "prompt": "Summarize the requested implementation lines.",
        "objective": objective,
        "instructions": [
            "Summarize the requested implementation lines.",
            "Use only the provided target evidence.",
        ],
        "output_sections": ["response"],
        "evidence_targets": evidence_targets,
        "parent_run_id": parent_run_id,
        "subtask_id": subtask_id,
        "attempt_id": attempt_id,
    }
    base.update(kwargs)
    return base


def _write_alpha_file(repo_root: Path) -> Path:
    target_file = repo_root / "pkg" / "alpha_widget.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(
        "\n".join(["def alpha_widget():", "    return 'alpha widget result'", "", "def unrelated():", "    pass"])
        + "\n",
        encoding="utf-8",
    )
    return target_file


# ---------------------------------------------------------------------------
# AC1: Serena selector derived from the objective's targeted-evidence
# contract, not a fixed "local_asset_research" smoke query.
# ---------------------------------------------------------------------------


def test_local_asset_research_serena_selector_derived_from_objective(tmp_path, monkeypatch) -> None:
    repo_root = _setup_repo(tmp_path, monkeypatch, with_manifest=True)
    _write_alpha_file(repo_root)

    validated_targets = [
        {
            "repo_relative_path": "pkg/alpha_widget.py",
            "selector": {"kind": "line_range", "start_line": 1, "end_line": 2},
        }
    ]
    evidence_envelopes, errors = rgh._collect_targeted_source_evidence(
        [
            {
                **validated_targets[0],
                "resolved_path": repo_root / "pkg" / "alpha_widget.py",
            }
        ],
        repo_root,
    )
    assert not errors

    calls = rgh._derive_serena_selector_calls(validated_targets, evidence_envelopes)
    tool_names = [call["tool_name"] for call in calls]
    assert tool_names == ["find_file", "search_for_pattern", "get_symbols_overview"]

    search_call = next(call for call in calls if call["tool_name"] == "search_for_pattern")
    # The derived selector must never be the legacy hardcoded literal, and
    # must instead reflect the actual selected evidence content.
    assert search_call["arguments"]["substring_pattern"] != "local_asset_research"
    assert search_call["arguments"]["substring_pattern"] == "def alpha_widget():"

    find_call = next(call for call in calls if call["tool_name"] == "find_file")
    assert find_call["arguments"]["file_mask"] == "alpha_widget.py"

    overview_call = next(call for call in calls if call["tool_name"] == "get_symbols_overview")
    assert overview_call["arguments"]["relative_path"] == "pkg/alpha_widget.py"


# ---------------------------------------------------------------------------
# AC2: objective and target-contract hashes are deterministic.
# ---------------------------------------------------------------------------


def test_objective_and_target_contract_hash_are_deterministic() -> None:
    objective = "Investigate alpha widget behaviour for regression triage"
    assert rgh._hash_objective(objective) == rgh._hash_objective(objective)
    assert rgh._hash_objective(objective) != rgh._hash_objective(objective + " different")
    assert rgh._hash_objective(None) is None
    assert rgh._hash_objective("   ") is None

    targets_a = [{"repo_relative_path": "pkg/foo.py", "selector": {"kind": "line_range", "start_line": 1, "end_line": 2}}]
    targets_b = [{"repo_relative_path": "pkg/foo.py", "selector": {"kind": "line_range", "start_line": 1, "end_line": 2}}]
    targets_c = [{"repo_relative_path": "pkg/foo.py", "selector": {"kind": "line_range", "start_line": 1, "end_line": 3}}]
    assert rgh._hash_target_contract(targets_a) == rgh._hash_target_contract(targets_b)
    assert rgh._hash_target_contract(targets_a) != rgh._hash_target_contract(targets_c)


# ---------------------------------------------------------------------------
# AC3: evidence_sha256 changes when evidence content is tampered by even one
# byte (tamper detection).
# ---------------------------------------------------------------------------


def test_evidence_sha256_changes_on_tampered_evidence() -> None:
    manifest = {"pinned_ref": "abc123"}
    validated_targets = [
        {"repo_relative_path": "pkg/foo.py", "selector": {"kind": "line_range", "start_line": 1, "end_line": 1}}
    ]
    envelopes = [
        {
            "repo_relative_path": "pkg/foo.py",
            "selector": {"kind": "line_range", "start_line": 1, "end_line": 1},
            "line_range": [1, 1],
            "sha256": "x" * 64,
            "source_kind": "wrapper_read_only_targeted_evidence",
            "content": "def alpha():",
        }
    ]
    correlation = {"parent_run_id": "p", "subtask_id": "s", "attempt_id": "a"}
    records_original = rgh._build_serena_evidence_records(validated_targets, envelopes, manifest, correlation)
    original_hash = rgh._hash_evidence(records_original)

    tampered_envelopes = [{**envelopes[0], "content": "def alpha():X"}]  # 1-byte tamper
    records_tampered = rgh._build_serena_evidence_records(validated_targets, tampered_envelopes, manifest, correlation)
    tampered_hash = rgh._hash_evidence(records_tampered)

    assert original_hash != tampered_hash


# ---------------------------------------------------------------------------
# AC4: prompt_envelope_sha256 is deterministically derived from
# evidence_sha256 (and is present in the literal AGY prompt envelope).
# ---------------------------------------------------------------------------


def test_prompt_envelope_sha256_derived_from_evidence_sha256() -> None:
    h1 = rgh._hash_prompt_envelope("e" * 64, "o" * 64, "t" * 64, "local_asset_research")
    h2 = rgh._hash_prompt_envelope("e" * 64, "o" * 64, "t" * 64, "local_asset_research")
    assert h1 == h2
    h3 = rgh._hash_prompt_envelope("f" * 64, "o" * 64, "t" * 64, "local_asset_research")
    assert h1 != h3


def test_prompt_envelope_sha256_present_in_agy_prompt(tmp_path, monkeypatch) -> None:
    repo_root = _setup_repo(tmp_path, monkeypatch, with_manifest=True)
    _write_alpha_file(repo_root)

    captured: dict[str, str] = {}

    def _run_agy(prompt: str, timeout_sec: int = rgh.DEFAULT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
        captured["value"] = prompt
        return _make_completed(0, stdout="LOOP_AGY_TARGETED_OK")

    request = _fanout_request(
        [{"path": "pkg/alpha_widget.py", "selector": {"kind": "line_range", "start_line": 1, "end_line": 2}}],
        objective="Investigate alpha_widget behaviour",
    )
    with patch.object(rgh, "_run_agy", side_effect=_run_agy):
        result = rgh.run_delegation(request, request_path=repo_root / "request.json")

    assert result["ok"] is True
    metadata = result["local_asset_retrieval_metadata"]
    prompt_envelope_sha256 = metadata["prompt_envelope_sha256"]
    assert prompt_envelope_sha256
    assert f'"prompt_envelope_sha256": "{prompt_envelope_sha256}"' in captured["value"]
    expected = rgh._hash_prompt_envelope(
        metadata["evidence_sha256"],
        metadata["objective_sha256"],
        metadata["target_contract_sha256"],
        "local_asset_research",
    )
    assert prompt_envelope_sha256 == expected


# ---------------------------------------------------------------------------
# AC5: result_binding_sha256 matches evidence/prompt hashes and independent
# verification fails when tampered.
# ---------------------------------------------------------------------------


def test_result_binding_sha256_matches_evidence_and_fails_on_tamper(tmp_path, monkeypatch) -> None:
    repo_root = _setup_repo(tmp_path, monkeypatch, with_manifest=True)
    _write_alpha_file(repo_root)

    def _run_agy(prompt: str, timeout_sec: int = rgh.DEFAULT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
        return _make_completed(0, stdout="LOOP_AGY_TARGETED_OK")

    request = _fanout_request(
        [{"path": "pkg/alpha_widget.py", "selector": {"kind": "line_range", "start_line": 1, "end_line": 2}}],
        objective="Investigate alpha_widget behaviour",
    )
    with patch.object(rgh, "_run_agy", side_effect=_run_agy):
        result = rgh.run_delegation(request, request_path=repo_root / "request.json")

    assert result["ok"] is True
    metadata = result["local_asset_retrieval_metadata"]

    assert rgh.verify_serena_hash_chain(
        {
            "tool_profile": "local_asset_research",
            "evidence_sha256": metadata["evidence_sha256"],
            "objective_sha256": metadata["objective_sha256"],
            "target_contract_sha256": metadata["target_contract_sha256"],
            "prompt_envelope_sha256": metadata["prompt_envelope_sha256"],
            "result_binding_sha256": metadata["result_binding_sha256"],
        }
    ) is True

    tampered = {
        "tool_profile": "local_asset_research",
        "evidence_sha256": "0" * 64,  # evidence swapped after the fact
        "objective_sha256": metadata["objective_sha256"],
        "target_contract_sha256": metadata["target_contract_sha256"],
        "prompt_envelope_sha256": metadata["prompt_envelope_sha256"],
        "result_binding_sha256": metadata["result_binding_sha256"],
    }
    assert rgh.verify_serena_hash_chain(tampered) is False
    assert rgh.verify_serena_hash_chain({}) is False


# ---------------------------------------------------------------------------
# AC6: child result / evidence record carries all required correlation and
# provenance fields.
# ---------------------------------------------------------------------------


def test_serena_evidence_record_contains_required_fields(tmp_path, monkeypatch) -> None:
    repo_root = _setup_repo(tmp_path, monkeypatch, with_manifest=True)
    _write_alpha_file(repo_root)

    def _run_agy(prompt: str, timeout_sec: int = rgh.DEFAULT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
        return _make_completed(0, stdout="LOOP_AGY_TARGETED_OK")

    request = _fanout_request(
        [{"path": "pkg/alpha_widget.py", "selector": {"kind": "line_range", "start_line": 1, "end_line": 2}}],
        objective="Investigate alpha_widget behaviour",
        parent_run_id="parent-run-42",
        subtask_id="subtask-7",
        attempt_id="attempt-3",
    )
    with patch.object(rgh, "_run_agy", side_effect=_run_agy):
        result = rgh.run_delegation(request, request_path=repo_root / "request.json")

    assert result["ok"] is True
    metadata = result["local_asset_retrieval_metadata"]

    top_level_required = {
        "actor",
        "retrieval_actor",
        "analysis_actor",
        "agy_direct_mcp_access",
        "parent_run_id",
        "subtask_id",
        "attempt_id",
        "request_sha256",
        "objective_sha256",
        "target_contract_sha256",
        "evidence_sha256",
        "prompt_envelope_sha256",
        "result_binding_sha256",
        "serena_pinned_ref",
        "serena_manifest_id",
        "serena_evidence_records",
    }
    assert top_level_required.issubset(metadata.keys())
    assert metadata["actor"] == "wrapper_serena_mcp"
    assert metadata["retrieval_actor"] == "wrapper_serena_mcp"
    assert metadata["parent_run_id"] == "parent-run-42"
    assert metadata["subtask_id"] == "subtask-7"
    assert metadata["attempt_id"] == "attempt-3"
    assert metadata["serena_pinned_ref"]
    assert metadata["serena_manifest_id"].startswith("serena_tool_manifest_v1:")

    records = metadata["serena_evidence_records"]
    assert records
    per_record_required = {
        "actor",
        "parent_run_id",
        "subtask_id",
        "attempt_id",
        "tool_name",
        "args_sha256",
        "is_error",
        "repo_relative_path",
        "selector",
        "line_range",
        "content_sha256",
        "source_kind",
        "serena_pinned_ref",
        "serena_manifest_id",
    }
    for record in records:
        assert per_record_required.issubset(record.keys())
        assert record["is_error"] is False
        assert record["repo_relative_path"] == "pkg/alpha_widget.py"


# ---------------------------------------------------------------------------
# AC7: AGY never receives direct Serena/MCP access; retrieval vs analysis
# actor boundary is explicit.
# ---------------------------------------------------------------------------


def test_agy_never_receives_direct_serena_mcp_access(tmp_path, monkeypatch) -> None:
    repo_root = _setup_repo(tmp_path, monkeypatch, with_manifest=True)
    _write_alpha_file(repo_root)

    captured: dict[str, str] = {}

    def _run_agy(prompt: str, timeout_sec: int = rgh.DEFAULT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
        captured["value"] = prompt
        return _make_completed(0, stdout="LOOP_AGY_TARGETED_OK")

    request = _fanout_request(
        [{"path": "pkg/alpha_widget.py", "selector": {"kind": "line_range", "start_line": 1, "end_line": 2}}],
        objective="Investigate alpha_widget behaviour",
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

    metadata = result["local_asset_retrieval_metadata"]
    assert metadata["agy_direct_mcp_access"] is False
    assert metadata["retrieval_actor"] == "wrapper_serena_mcp"
    assert metadata["analysis_actor"] == "antigravity_cli"
    assert rgh.AGY_DIRECT_MCP_ACCESS is False
    assert rgh.RETRIEVAL_ACTOR_WRAPPER_SERENA_MCP == "wrapper_serena_mcp"
    assert rgh.ANALYSIS_ACTOR_ANTIGRAVITY_CLI == "antigravity_cli"


# ---------------------------------------------------------------------------
# AC8: evidence irrelevant to the objective, empty, or metadata-only fails
# closed before AGY ever launches.
# ---------------------------------------------------------------------------


def test_irrelevant_or_metadata_only_evidence_fails_closed(tmp_path, monkeypatch) -> None:
    repo_root = _setup_repo(tmp_path, monkeypatch, with_manifest=True)
    _write_alpha_file(repo_root)

    # Objective shares no deterministic token with the retrieved evidence
    # content or path -- must fail closed before AGY launches.
    with patch.object(rgh, "_run_agy", side_effect=_forbid_agy_launch):
        result = rgh.run_delegation(
            _fanout_request(
                [
                    {
                        "path": "pkg/alpha_widget.py",
                        "selector": {"kind": "line_range", "start_line": 1, "end_line": 2},
                    }
                ],
                objective="Draft quarterly finance projections spreadsheet",
            ),
            request_path=repo_root / "request.json",
        )
    assert result["ok"] is False
    assert "unrelated to the subtask objective" in result["failure_reason"]

    # Blank objective -- fails closed even earlier, via the shared request
    # validator's non-empty-objective requirement (no tokens to match at
    # all, so it could never pass the relevance gate either).
    with patch.object(rgh, "_run_agy", side_effect=_forbid_agy_launch):
        result = rgh.run_delegation(
            _fanout_request(
                [
                    {
                        "path": "pkg/alpha_widget.py",
                        "selector": {"kind": "line_range", "start_line": 1, "end_line": 2},
                    }
                ],
                objective="   ",
            ),
            request_path=repo_root / "request.json",
        )
    assert result["ok"] is False
    assert rgh._hash_objective("   ") is None
    assert not rgh._evidence_matches_objective("   ", [{"repo_relative_path": "x", "content": "x"}])

    # Empty-evidence selector range (#1638 fail-close reused, not
    # re-implemented) must still fail closed for fan-out-correlated requests.
    blank_file = repo_root / "blank.py"
    blank_file.write_text("code = 1\n\n\n\ncode = 2\n", encoding="utf-8")
    with patch.object(rgh, "_run_agy", side_effect=_forbid_agy_launch):
        result = rgh.run_delegation(
            _fanout_request(
                [{"path": "blank.py", "selector": {"kind": "line_range", "start_line": 2, "end_line": 3}}],
                objective="Investigate blank module blank.py",
            ),
            request_path=repo_root / "request.json",
        )
    assert result["ok"] is False
    assert "empty evidence" in result["failure_reason"]


def test_objective_relevant_evidence_still_succeeds(tmp_path, monkeypatch) -> None:
    """Positive control for AC8: when the objective *does* deterministically
    overlap with the evidence, the fan-out task-linked gate must not
    false-positive and block a legitimate request."""
    repo_root = _setup_repo(tmp_path, monkeypatch, with_manifest=True)
    _write_alpha_file(repo_root)

    def _run_agy(prompt: str, timeout_sec: int = rgh.DEFAULT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
        return _make_completed(0, stdout="LOOP_AGY_TARGETED_OK")

    request = _fanout_request(
        [{"path": "pkg/alpha_widget.py", "selector": {"kind": "line_range", "start_line": 1, "end_line": 2}}],
        objective="Investigate alpha_widget implementation for a regression report",
    )
    with patch.object(rgh, "_run_agy", side_effect=_run_agy):
        result = rgh.run_delegation(request, request_path=repo_root / "request.json")
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# Standalone (non-fan-out) targeted-evidence requests are untouched: no
# hash-chain fields, no Serena manifest lookup, no objective-relevance gate.
# This is the AC9 regression contract at the unit-boundary level (the full
# #1638 suite is exercised unmodified in test_agy_targeted_evidence.py).
# ---------------------------------------------------------------------------


def test_standalone_request_without_fanout_correlation_is_unaffected(tmp_path, monkeypatch) -> None:
    repo_root = _setup_repo(tmp_path, monkeypatch, with_manifest=False)
    _write_alpha_file(repo_root)

    def _run_agy(prompt: str, timeout_sec: int = rgh.DEFAULT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
        return _make_completed(0, stdout="LOOP_AGY_TARGETED_OK")

    request = {
        "schema": "delegation_request_v1",
        "tool_profile": "local_asset_research",
        "provider": "agy",
        "prompt": "Summarize.",
        "objective": "Totally unrelated objective text with no overlap",
        "instructions": ["Summarize.", "Use only the provided target evidence."],
        "output_sections": ["response"],
        "evidence_targets": [
            {"path": "pkg/alpha_widget.py", "selector": {"kind": "line_range", "start_line": 1, "end_line": 2}}
        ],
        # Deliberately no parent_run_id / subtask_id / attempt_id.
    }
    with patch.object(rgh, "_run_agy", side_effect=_run_agy):
        result = rgh.run_delegation(request, request_path=repo_root / "request.json")

    assert result["ok"] is True
    metadata = result["local_asset_retrieval_metadata"]
    assert "evidence_sha256" not in metadata
    assert "serena_evidence_records" not in metadata


def test_is_fanout_correlated_request_helper() -> None:
    assert rgh._is_fanout_correlated_request({"parent_run_id": "p1"}) is True
    assert rgh._is_fanout_correlated_request({"subtask_id": "s1"}) is True
    assert rgh._is_fanout_correlated_request({"attempt_id": "a1"}) is True
    assert rgh._is_fanout_correlated_request({}) is False
    assert rgh._is_fanout_correlated_request({"parent_run_id": ""}) is False
    assert rgh._is_fanout_correlated_request({"parent_run_id": "   "}) is False
