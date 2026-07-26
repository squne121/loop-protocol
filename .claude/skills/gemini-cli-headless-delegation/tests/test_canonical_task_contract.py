"""Tests for provider_auto_dispatch()'s canonical-task candidate
materialization (Issue #1692 AC8/AC9).

AC coverage:
  AC8: the agy candidate and the gemini candidate materialized for a single
       provider="auto" request share an identical task_contract_sha256.
  AC9: the agy candidate's prompt field is always a non-empty string as long
       as objective/instructions/context_files are non-empty (never an
       empty-prompt candidate).
"""
from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "run_gemini_headless.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("run_gemini_headless", _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rgh = _load_module()


def _result(ok: bool, failure_class: str | None = None) -> dict:
    return {
        "schema": "delegation_result/v1",
        "ok": ok,
        "failure_class": failure_class,
        "model_downgrades": [],
        "response_text": "hi" if ok else None,
    }


BASE_AUTO_REQUEST = {
    "schema": "delegation_request_v1",
    "objective": "Summarize the build failure from context",
    "instructions": [
        "Identify the root cause.",
        "List actionable recommendations.",
    ],
    "tool_profile": "no_tools",
    "output_sections": ["Summary"],
    "context_files": ["docs/notes.md"],
}


# ---------------------------------------------------------------------------
# AC8
# ---------------------------------------------------------------------------


def test_agy_and_gemini_candidates_share_task_contract_hash() -> None:
    """GIVEN a single provider="auto" request
    WHEN provider_auto_dispatch() materializes the agy candidate first, then
    (after a fallback-safe agy failure) the gemini candidate
    THEN both materialized candidates carry an identical
    task_contract_sha256 field."""
    captured_requests: list[dict] = []

    def fake_run_delegation(request, request_path=None, _routing=None):
        captured_requests.append(dict(request))
        if request["provider"] == "agy":
            return _result(False, failure_class="agy_rate_limited")
        return _result(True)

    with patch.object(rgh, "run_delegation", side_effect=fake_run_delegation):
        result = rgh.provider_auto_dispatch(dict(BASE_AUTO_REQUEST))

    assert result["ok"] is True
    assert len(captured_requests) == 2
    agy_candidate, gemini_candidate = captured_requests
    assert agy_candidate["provider"] == "agy"
    assert gemini_candidate["provider"] == "gemini"

    assert "task_contract_sha256" in agy_candidate
    assert "task_contract_sha256" in gemini_candidate
    assert agy_candidate["task_contract_sha256"] == gemini_candidate["task_contract_sha256"]


def test_task_contract_hash_changes_when_canonical_fields_change() -> None:
    """GIVEN two provider="auto" requests that differ only in `objective`
    WHEN each is materialized into an agy candidate
    THEN the two agy candidates have different task_contract_sha256 values
    (the hash is not a constant / not accidentally order-insensitive to the
    point of ignoring content)."""
    request_a = dict(BASE_AUTO_REQUEST)
    request_b = dict(BASE_AUTO_REQUEST, objective="A completely different objective")

    candidate_a = rgh._materialize_auto_candidate_request(request_a, "agy")
    candidate_b = rgh._materialize_auto_candidate_request(request_b, "agy")

    assert candidate_a["task_contract_sha256"] != candidate_b["task_contract_sha256"]


def test_task_contract_hash_is_deterministic_across_calls() -> None:
    """GIVEN the same provider="auto" request materialized twice
    WHEN _materialize_auto_candidate_request() is called independently each
    time
    THEN the task_contract_sha256 is byte-identical (no timestamp/random
    component)."""
    request = dict(BASE_AUTO_REQUEST)
    first = rgh._materialize_auto_candidate_request(request, "gemini")
    second = rgh._materialize_auto_candidate_request(request, "gemini")
    assert first["task_contract_sha256"] == second["task_contract_sha256"]


# ---------------------------------------------------------------------------
# AC9
# ---------------------------------------------------------------------------


def test_auto_to_agy_candidate_materializes_nonempty_prompt() -> None:
    """GIVEN a provider="auto" request with non-empty objective/instructions/
    context_files
    WHEN the agy candidate is materialized
    THEN its `prompt` field is a non-empty string (never the empty-prompt
    candidate that would trip agy_empty_prompt)."""
    candidate = rgh._materialize_auto_candidate_request(dict(BASE_AUTO_REQUEST), "agy")
    assert candidate["provider"] == "agy"
    assert isinstance(candidate["prompt"], str)
    assert candidate["prompt"].strip() != ""

    # AC1/AC12-adjacent guardrail: the materialized agy candidate must pass
    # the agy-specific validator (no model, non-empty prompt).
    errors = rgh._validate_agy_request(candidate)
    assert errors == [], f"materialized agy candidate failed _validate_agy_request: {errors}"


def test_auto_to_agy_candidate_prompt_contains_objective_and_instructions() -> None:
    """GIVEN a provider="auto" request
    WHEN the agy candidate's prompt is synthesized
    THEN it is derived from (contains) the canonical objective and every
    instruction line -- it is not a placeholder / unrelated string."""
    candidate = rgh._materialize_auto_candidate_request(dict(BASE_AUTO_REQUEST), "agy")
    prompt = candidate["prompt"]
    assert BASE_AUTO_REQUEST["objective"] in prompt
    for instruction in BASE_AUTO_REQUEST["instructions"]:
        assert instruction in prompt


@pytest.mark.parametrize(
    ("objective", "instructions", "context_files"),
    [
        ("Do the thing", None, None),
        (None, ["Do step one.", "Do step two."], None),
        (None, None, ["a/context.md"]),
    ],
)
def test_agy_candidate_prompt_nonempty_when_any_canonical_field_present(
    objective, instructions, context_files
) -> None:
    """GIVEN a request where only ONE of objective/instructions/context_files
    is non-empty (the other two are absent)
    WHEN the agy candidate is materialized
    THEN the synthesized prompt is still non-empty (AC9: non-empty whenever
    ANY of these fields is non-empty, not only when ALL are present)."""
    request: dict = {"schema": "delegation_request_v1", "tool_profile": "no_tools"}
    if objective is not None:
        request["objective"] = objective
    if instructions is not None:
        request["instructions"] = instructions
    if context_files is not None:
        request["context_files"] = context_files

    candidate = rgh._materialize_auto_candidate_request(request, "agy")
    assert candidate["prompt"].strip() != ""


def test_gemini_candidate_retains_structured_shape_and_task_contract_hash() -> None:
    """GIVEN a provider="auto" request
    WHEN the gemini candidate is materialized
    THEN it keeps the canonical structured request shape (objective /
    instructions / context_files / output_sections unchanged) and gains
    provider="gemini" plus task_contract_sha256."""
    candidate = rgh._materialize_auto_candidate_request(dict(BASE_AUTO_REQUEST), "gemini")
    assert candidate["provider"] == "gemini"
    assert candidate["objective"] == BASE_AUTO_REQUEST["objective"]
    assert candidate["instructions"] == BASE_AUTO_REQUEST["instructions"]
    assert candidate["context_files"] == BASE_AUTO_REQUEST["context_files"]
    assert candidate["output_sections"] == BASE_AUTO_REQUEST["output_sections"]
    assert "task_contract_sha256" in candidate
