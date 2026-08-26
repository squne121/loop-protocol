#!/usr/bin/env python3
"""Regression tests for Issue #2348.

`_structured_output_from_result_compat()` in ``run_retrospective.py`` must
recover ``structured_output`` from ``result`` text that carries a fenced
JSON block surrounded by explanatory prose -- the real ``--agent
retrospective-runtime-observer --output-format json --json-schema ...
--no-session-persistence`` response shape observed against the live
``claude`` CLI -- not only when the fence spans the *entire* (stripped)
``result`` text (the pre-#2348 whole-string ``^...$`` anchor requirement).

Fixture/mock-based only, hermetic: the ``runner`` callable passed to
``rr.invoke_agent`` is dependency-injected exactly as in
``test_run_retrospective.py``; no real subprocess is started and no
``claude_live`` marker is used (AC3's real-CLI round trip is a separate,
opt-in check performed by
``verify_run_retrospective_live_cli.sh --select
test_real_claude_cli_analytical_prompt_structured_output_shape``, per the
Issue #2348 Runtime Verification Applicability: ``decision: immediate``).

Covers AC1/AC2 (In Scope items 1-2 of Issue #2348):
  test_prose_fence_shape1_fence_then_trailing_prose_recovered
      sanitized shape 1: starts_with_fence=true / ends_with_fence=false
  test_prose_fence_shape2_leading_and_trailing_prose_recovered
      sanitized shape 2: starts_with_fence=false / ends_with_fence=false
  test_prose_fence_schema_invalid_fence_stays_missing_structured_output
      negative case: schema-incompatible fenced block never accepted
  test_prose_fence_multiple_schema_valid_candidates_ambiguous_rejected
      negative case: ambiguous multi-fence match is fail-closed, not
      silently resolved by picking one candidate or greedily merging fences
      (guards the Stop Condition against a new cross-fence-merge defect)
  test_prose_fence_foreign_language_fence_before_json_fence_recovered
      P1 regression: foreign-language fence (e.g. ```text) before the
      real ```json fence must not corrupt fence pairing and swallow the
      real JSON candidate
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import run_retrospective as rr  # noqa: E402


def _schema(tmp_path: Path) -> Path:
    schema_path = tmp_path / "prose_fence_schema.json"
    schema_path.write_text(
        json.dumps({"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}),
        encoding="utf-8",
    )
    return schema_path


def _invocation_request(schema_path: str) -> rr.AgentInvocationRequest:
    return rr.AgentInvocationRequest(
        agent_name="retrospective-runtime-observer",
        prompt="observe",
        json_schema_path=schema_path,
        cwd="/repo",
    )


def _runner_for_result(result_text: str) -> Any:
    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        wrapper = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": result_text,
        }
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps(wrapper), stderr="")

    return _runner


def test_prose_fence_shape1_fence_then_trailing_prose_recovered(tmp_path: Path) -> None:
    """GIVEN a `result` text that starts with a fenced JSON block immediately
    followed by explanatory prose (Issue #2348 Background sanitized shape 1:
    `starts_with_fence=true` / `ends_with_fence=false`)
    WHEN `invoke_agent` runs compatibility recovery
    THEN the fenced JSON is extracted and schema-validated as
    `structured_output` instead of failing closed with
    `missing_structured_output`."""
    schema_path = _schema(tmp_path)
    business_payload = {"a": "from-fence-shape1"}
    result_text = (
        "```json\n"
        + json.dumps(business_payload)
        + "\n```\n\nThis analysis covered 3 findings across the retrospective run."
    )
    runner = _runner_for_result(result_text)

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=runner)

    assert result.status == "ok"
    assert result.structured_output == business_payload


def test_prose_fence_shape2_leading_and_trailing_prose_recovered(tmp_path: Path) -> None:
    """GIVEN a `result` text with explanatory prose both before and after the
    fenced JSON block (Issue #2348 Background sanitized shape 2:
    `starts_with_fence=false` / `ends_with_fence=false`)
    WHEN `invoke_agent` runs compatibility recovery
    THEN the fenced JSON is extracted and schema-validated as
    `structured_output` instead of failing closed with
    `missing_structured_output`."""
    schema_path = _schema(tmp_path)
    business_payload = {"a": "from-fence-shape2"}
    result_text = (
        "Here is the structured analysis you requested:\n\n"
        "```json\n"
        + json.dumps(business_payload)
        + "\n```\n\nLet me know if you need anything else."
    )
    runner = _runner_for_result(result_text)

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=runner)

    assert result.status == "ok"
    assert result.structured_output == business_payload


def test_prose_fence_schema_invalid_fence_stays_missing_structured_output(tmp_path: Path) -> None:
    """GIVEN a `result` text whose only fenced block is present but does NOT
    conform to the target JSON Schema (missing the required "a" field)
    WHEN `invoke_agent` runs compatibility recovery
    THEN the fence-anchor relaxation (Issue #2348) does not widen
    acceptance: `structured_output` recovery still fails closed and
    `reason_code` remains `missing_structured_output` (negative case
    guarding against schema validation being weakened by the fix)."""
    schema_path = _schema(tmp_path)
    non_conformant_payload = {"b": "no-required-a-field"}
    result_text = (
        "Summary of findings follows.\n\n"
        "```json\n"
        + json.dumps(non_conformant_payload)
        + "\n```\n\nEnd of analysis."
    )
    runner = _runner_for_result(result_text)

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=runner)

    assert result.status == "malformed_output"
    assert result.reason_code == "missing_structured_output"


def test_prose_fence_multiple_schema_valid_candidates_ambiguous_rejected(tmp_path: Path) -> None:
    """GIVEN a `result` text containing two separate fenced JSON blocks that
    BOTH independently validate against the target schema
    WHEN `invoke_agent` runs compatibility recovery
    THEN the ambiguity is rejected fail-closed (`missing_structured_output`)
    rather than the non-greedy fence enumeration silently picking one of the
    two candidates, or a greedy-match defect merging both fences into a
    single unparseable/incorrect candidate."""
    schema_path = _schema(tmp_path)
    first_payload = {"a": "first-fence"}
    second_payload = {"a": "second-fence"}
    result_text = (
        "```json\n"
        + json.dumps(first_payload)
        + "\n```\n\nAn earlier draft is shown above; the final result is below.\n\n"
        "```json\n"
        + json.dumps(second_payload)
        + "\n```\n"
    )
    runner = _runner_for_result(result_text)

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=runner)

    assert result.status == "malformed_output"
    assert result.reason_code == "missing_structured_output"


def test_prose_fence_foreign_language_fence_before_json_fence_recovered(tmp_path: Path) -> None:
    """GIVEN a `result` text containing a foreign-language fenced block
    (e.g. ```text) BEFORE the real fenced JSON block (Issue #2348 P1
    regression: the opener-only fence regex misread the foreign block's own
    closing fence as a new bare opening fence, corrupting fence pairing so
    the real ```json block's closing fence -- and thus the real JSON
    candidate itself -- never appeared in `finditer()` results, reproducing
    `missing_structured_output` for the common "analysis with a text/code
    example, then the final JSON answer" LLM response shape)
    WHEN `invoke_agent` runs compatibility recovery
    THEN the real fenced JSON block is still correctly extracted and
    schema-validated as `structured_output`."""
    schema_path = _schema(tmp_path)
    business_payload = {"a": "from-json-fence-after-foreign-fence"}
    result_text = (
        "Here is an example of the input format:\n\n"
        "```text\n"
        "some non-json illustrative content\n"
        "```\n\n"
        "Final answer:\n\n"
        "```json\n"
        + json.dumps(business_payload)
        + "\n```\n"
    )
    runner = _runner_for_result(result_text)

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=runner)

    assert result.status == "ok"
    assert result.structured_output == business_payload
