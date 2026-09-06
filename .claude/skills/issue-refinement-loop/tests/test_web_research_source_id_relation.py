"""Regression coverage for `sources[]` / `evidence[].source_id` referential
integrity in the WEB_RESEARCH_RESULT_V1 consumer (#2042, #2038 follow-up).

Reuses the existing `test_web_research_routing.py` fixture helpers
(`_input` / `_successful_web_result`) rather than duplicating them.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
TESTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(TESTS_DIR))

from route_web_research_result import (  # noqa: E402
    NEXT_ACTION_HUMAN_JUDGMENT_REQUIRED,
    NEXT_ACTION_PROCEED,
    TRANSPORT_STATUS_ENVIRONMENT_FAILURE,
    route_web_research_result,
)
from test_web_research_routing import (  # noqa: E402
    _input,
    _successful_web_result,
)


def _source(*, source_id: str, url: str, source_kind: str = "native_web", **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "source_id": source_id,
        "url": url,
        "title": "Example title",
        "source_kind": source_kind,
    }
    entry.update(overrides)
    return entry


def test_legacy_evidence_without_source_id_remains_valid():
    """AC3: evidence without `source_id` is unaffected by the additive registry."""
    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=_successful_web_result(),
        )
    )

    assert result["transport_status"] == "ok"
    assert result["next_action"] == NEXT_ACTION_PROCEED


def test_valid_source_id_reference_is_accepted():
    """AC4: `source_id` resolving to a `sources[]` entry whose `url` matches `ref`."""
    ref = "https://example.invalid/hook-semantics"
    web_research = _successful_web_result(
        sources=[_source(source_id="src-1", url=ref)],
    )
    web_research["claims"][0]["evidence"][0]["source_id"] = "src-1"

    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=web_research,
        )
    )

    assert result["transport_status"] == "ok"
    assert result["next_action"] == NEXT_ACTION_PROCEED


def test_valid_source_id_reference_rejects_url_mismatch():
    """AC4 (negative half): a `source_id` whose registry `url` diverges from `ref` is rejected."""
    web_research = _successful_web_result(
        sources=[_source(source_id="src-1", url="https://example.invalid/other-page")],
    )
    web_research["claims"][0]["evidence"][0]["source_id"] = "src-1"

    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=web_research,
        )
    )

    assert result["transport_status"] == TRANSPORT_STATUS_ENVIRONMENT_FAILURE
    assert result["next_action"] == NEXT_ACTION_HUMAN_JUDGMENT_REQUIRED


def test_unknown_source_id_reference_is_rejected():
    """AC5: an evidence item referencing a nonexistent `source_id` is rejected."""
    web_research = _successful_web_result(sources=[])
    web_research["claims"][0]["evidence"][0]["source_id"] = "does-not-exist"

    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=web_research,
        )
    )

    assert result["transport_status"] == TRANSPORT_STATUS_ENVIRONMENT_FAILURE
    assert result["next_action"] == NEXT_ACTION_HUMAN_JUDGMENT_REQUIRED


def test_duplicate_source_id_reference_within_same_claim_is_rejected():
    """AC6: the same `source_id` referenced more than once in one claim's evidence is rejected."""
    ref = "https://example.invalid/hook-semantics"
    web_research = _successful_web_result(
        sources=[_source(source_id="src-1", url=ref)],
    )
    evidence_item = web_research["claims"][0]["evidence"][0]
    evidence_item["source_id"] = "src-1"
    web_research["claims"][0]["evidence"] = [
        evidence_item,
        {
            "kind": "web",
            "ref": ref,
            "summary": "Duplicate reference to the same source.",
            "source_id": "src-1",
        },
    ]

    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=web_research,
        )
    )

    assert result["transport_status"] == TRANSPORT_STATUS_ENVIRONMENT_FAILURE
    assert result["next_action"] == NEXT_ACTION_HUMAN_JUDGMENT_REQUIRED


def test_evidence_less_claimed_success_is_rejected():
    """AC7: a claimed-success claim with no usable evidence at all is rejected."""
    web_research = _successful_web_result()
    web_research["claims"][0]["evidence"] = []

    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=web_research,
        )
    )

    assert result["transport_status"] == TRANSPORT_STATUS_ENVIRONMENT_FAILURE
    assert result["next_action"] == NEXT_ACTION_HUMAN_JUDGMENT_REQUIRED


def test_orphan_source_not_rejected():
    """AC8: a `sources[]` entry referenced by no claim is allowed, not a rejection reason."""
    web_research = _successful_web_result(
        sources=[
            _source(source_id="referenced", url="https://example.invalid/hook-semantics"),
            _source(source_id="orphan", url="https://example.invalid/never-referenced"),
        ],
    )
    web_research["claims"][0]["evidence"][0]["source_id"] = "referenced"

    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=web_research,
        )
    )

    assert result["transport_status"] == "ok"
    assert result["next_action"] == NEXT_ACTION_PROCEED


@pytest.mark.parametrize("source_kind", ["agy", "native_web"])
def test_agy_and_native_web_source_kind_parity(source_kind: str):
    """AC9: `agy` and `native_web` sources are validated identically (no special-casing)."""
    ref = "https://example.invalid/hook-semantics"
    web_research = _successful_web_result(
        sources=[_source(source_id="src-1", url=ref, source_kind=source_kind)],
    )
    web_research["claims"][0]["evidence"][0]["source_id"] = "src-1"

    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=web_research,
        )
    )

    assert result["transport_status"] == "ok"
    assert result["next_action"] == NEXT_ACTION_PROCEED

    # And the mismatched-url rejection path also applies identically to both kinds.
    mismatched = copy.deepcopy(web_research)
    mismatched["sources"][0]["url"] = "https://example.invalid/other-page"
    mismatched_result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=mismatched,
        )
    )
    assert mismatched_result["transport_status"] == TRANSPORT_STATUS_ENVIRONMENT_FAILURE


def test_malformed_sources_registry_is_rejected_even_when_unreferenced():
    """A structurally-broken `sources[]` (present but malformed) is fail-closed."""
    web_research = _successful_web_result(sources=[{"source_id": "src-1"}])  # missing url

    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=web_research,
        )
    )

    assert result["transport_status"] == TRANSPORT_STATUS_ENVIRONMENT_FAILURE
    assert result["next_action"] == NEXT_ACTION_HUMAN_JUDGMENT_REQUIRED


def test_source_id_reference_runtime_subprocess_smoke(tmp_path: Path):
    """AC12: run `route_web_research_result.py` as a real subprocess against a
    `sources[]`/`source_id` fixture and check the stdout JSON `transport_status`.

    SKIPs with exit 77 (pytest.skip) if `uv`/`python3` is unavailable in the
    execution environment. A fallback path is never treated as PASS.
    """
    uv_path = shutil.which("uv")
    python3_path = shutil.which("python3")
    if uv_path is None or python3_path is None:
        pytest.skip("uv or python3 not available in this execution environment")

    ref = "https://example.invalid/hook-semantics"
    web_research = _successful_web_result(
        sources=[_source(source_id="src-1", url=ref)],
    )
    web_research["claims"][0]["evidence"][0]["source_id"] = "src-1"
    fixture_input = _input(
        repository_status="determined",
        disposition="close_not_planned",
        role="non_dispositive",
        web_research=web_research,
    )

    fixture_file = tmp_path / "web_research_routing_input.json"
    fixture_file.write_text(json.dumps(fixture_input), encoding="utf-8")

    script_path = SCRIPTS_DIR / "route_web_research_result.py"
    command = [
        uv_path,
        "run",
        "--locked",
        python3_path,
        str(script_path),
        "--input-file",
        str(fixture_file),
    ]

    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        cwd=str(SKILL_ROOT.parent.parent.parent),
        timeout=120,
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifacts_dir = SKILL_ROOT.parent.parent.parent / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    log_path = artifacts_dir / f"runtime-verification-AC12-{timestamp}.log"

    verdict = "FAIL"
    reason = ""
    parsed: dict[str, object] | None = None
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        reason = f"stdout was not valid JSON: {exc}"

    if parsed is not None:
        transport_status = parsed.get("transport_status")
        # Never let a fallback path count as PASS: `_*_fallback: true` style
        # flags anywhere in the payload must not be interpreted as success.
        has_fallback_flag = any(
            key.endswith("_fallback") and value is True for key, value in parsed.items()
        )
        if proc.returncode == 0 and transport_status == "ok" and not has_fallback_flag:
            verdict = "PASS"
            reason = "transport_status == ok, no fallback flag present"
        else:
            reason = (
                f"exit_code={proc.returncode} transport_status={transport_status!r} "
                f"fallback_flag={has_fallback_flag}"
            )

    log_path.write_text(
        "=== Runtime Verification Log ===\n"
        "AC: AC12 - route_web_research_result.py subprocess smoke test for sources[]/source_id\n"
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}\n"
        f"Environment: {sys.platform}, python3={python3_path}, uv={uv_path}\n"
        "\n--- Input ---\n"
        f"Command: {' '.join(command)}\n"
        f"Fixture: {json.dumps(fixture_input, ensure_ascii=False)}\n"
        "\n--- Output ---\n"
        f"stdout: {proc.stdout}\n"
        f"stderr: {proc.stderr}\n"
        "\n--- Verdict ---\n"
        f"Result: {verdict}\n"
        f"Exit Code: {proc.returncode}\n"
        f"Reason: {reason}\n",
        encoding="utf-8",
    )

    assert verdict == "PASS", f"runtime verification failed: {reason} (see {log_path})"
