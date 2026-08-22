"""scripts/claude-gpt/tests/test_available_models_fallback_detection.py

Issue #2274 AC12: `availableModels` が Spark を許可しないケースの検出。

Claude Code の model resolution precedence
（https://code.claude.com/docs/en/model-config、Issue #2274 Current Validated
Scope 参照）は、subagent definition が要求する model が `availableModels` に
含まれない場合、subagent を失敗させず継承 model へ silent fallback する。この
挙動は「エラーが出ていない」だけでは PASS 扱いにできない -- 本テストは
`scripts/claude-gpt/launch.sh` の ``detect_available_models_silent_fallback``
（``detect-available-models-fallback`` イベント経由で呼び出される、pure な
evidence analysis 関数。PreToolUse authorization gate 自体には一切配線されて
いない）が、このケースを常に typed reason で報告し、決して PASS へ silent
promote しないことを検証する。

launch.sh の ``SPARK_GATE_WRITER_PY_BEGIN``/``_END`` マーカー間に埋め込まれた
*実際の* python source を対象にする（``test_delegation_directive.py`` /
``test_run_worktree_agent_runtime_smoke_spark_explicit_gate.py`` と同じ抽出
機構）ので、ここで検証しているロジックと実際に走るロジックの間に単一の
source of truth がある。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
LAUNCH_SH = REPO_ROOT / "scripts" / "claude-gpt" / "launch.sh"

REQUESTED_MODEL = "gpt-5.3-codex-spark"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "run_worktree_agent_runtime_smoke_available_models_fallback", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate_script_source() -> str:
    module = _load_module()
    launch_sh_text = LAUNCH_SH.read_text(encoding="utf-8")
    source = module.extract_spark_gate_writer_source(launch_sh_text)
    assert source is not None, "SPARK_GATE_WRITER_PY_BEGIN/_END markers not found in launch.sh"
    assert "detect_available_models_silent_fallback" in source, (
        "gate writer source must contain the Issue #2274 AC12 silent-fallback "
        "detector; if this drifted, this suite would silently stop testing "
        "the actual embedded logic."
    )
    return source


def _render_gate_script(directory: Path, source: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    gate_path = directory / f"spark_gate_{uuid.uuid4().hex}.py"
    # This event never touches the LAUNCH_NONCE placeholder path, so no
    # substitution is needed -- but the marker must still be structurally
    # valid Python (an unreplaced bare identifier would be fine here since
    # it never appears outside a string literal in the embedded source).
    gate_path.write_text(source, encoding="utf-8")
    return gate_path


def _detect(gate_script_source: str, tmp_path: Path, payload: dict) -> dict:
    gate_script_path = _render_gate_script(tmp_path / "gate-scripts", gate_script_source)
    result = subprocess.run(
        [sys.executable, str(gate_script_path), "detect-available-models-fallback"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), result.stderr
    return json.loads(result.stdout)


def test_match_when_resolved_and_models_used_agree(gate_script_source, tmp_path):
    out = _detect(
        gate_script_source,
        tmp_path,
        {
            "requested_model": REQUESTED_MODEL,
            "resolved_model": REQUESTED_MODEL,
            "models_used": [REQUESTED_MODEL],
            "available_models": [REQUESTED_MODEL, "gpt-5.6-terra"],
        },
    )
    assert out["status"] == "pass"
    assert out["reason"] == "match"


def test_match_when_only_resolved_model_observed(gate_script_source, tmp_path):
    out = _detect(
        gate_script_source,
        tmp_path,
        {"requested_model": REQUESTED_MODEL, "resolved_model": REQUESTED_MODEL},
    )
    assert out["status"] == "pass"
    assert out["reason"] == "match"


def test_requested_model_missing_is_blocked(gate_script_source, tmp_path):
    out = _detect(
        gate_script_source,
        tmp_path,
        {"resolved_model": REQUESTED_MODEL},
    )
    assert out["status"] == "blocked"
    assert out["reason"] == "requested_model_missing"


@pytest.mark.parametrize("bad_requested", [None, "", 5, [], {}])
def test_requested_model_non_string_or_empty_is_blocked(gate_script_source, tmp_path, bad_requested):
    out = _detect(
        gate_script_source,
        tmp_path,
        {"requested_model": bad_requested, "resolved_model": REQUESTED_MODEL},
    )
    assert out["status"] == "blocked"
    assert out["reason"] == "requested_model_missing"


def test_no_resolved_evidence_at_all_is_insufficient_evidence(gate_script_source, tmp_path):
    out = _detect(
        gate_script_source,
        tmp_path,
        {"requested_model": REQUESTED_MODEL},
    )
    assert out["status"] == "blocked"
    assert out["reason"] == "insufficient_evidence"


def test_available_models_excludes_requested_is_silent_fallback(gate_script_source, tmp_path):
    """The exact AC12 shape: `availableModels` does not include the
    requested model, and the runtime resolved/used a different (inherited)
    model instead -- never promoted to PASS."""
    out = _detect(
        gate_script_source,
        tmp_path,
        {
            "requested_model": REQUESTED_MODEL,
            "resolved_model": "gpt-5.6-terra",
            "models_used": ["gpt-5.6-terra"],
            "available_models": ["gpt-5.6-terra", "gpt-5.6-sol"],
        },
    )
    assert out["status"] == "blocked"
    assert out["reason"] == "available_models_excludes_requested_silent_fallback"


def test_mismatch_despite_available_models_containing_requested(gate_script_source, tmp_path):
    out = _detect(
        gate_script_source,
        tmp_path,
        {
            "requested_model": REQUESTED_MODEL,
            "resolved_model": "gpt-5.6-terra",
            "available_models": [REQUESTED_MODEL, "gpt-5.6-terra"],
        },
    )
    assert out["status"] == "blocked"
    assert out["reason"] == "resolved_model_mismatch_despite_available"


def test_mismatch_without_available_models_evidence_still_never_pass(gate_script_source, tmp_path):
    out = _detect(
        gate_script_source,
        tmp_path,
        {
            "requested_model": REQUESTED_MODEL,
            "resolved_model": "gpt-5.6-terra",
        },
    )
    assert out["status"] == "blocked"
    assert out["reason"] == "resolved_model_mismatch_without_available_models_evidence"


def test_models_used_disagreement_alone_is_never_pass(gate_script_source, tmp_path):
    out = _detect(
        gate_script_source,
        tmp_path,
        {
            "requested_model": REQUESTED_MODEL,
            "models_used": ["gpt-5.6-terra"],
            "available_models": ["gpt-5.6-terra"],
        },
    )
    assert out["status"] == "blocked"
    assert out["reason"] == "available_models_excludes_requested_silent_fallback"


def test_empty_available_models_list_is_not_treated_as_evidence(gate_script_source, tmp_path):
    """An empty `available_models: []` must never be confused with "the
    runtime confirmed exclusion" -- it is indistinguishable from "not
    observed", so the less specific typed reason applies."""
    out = _detect(
        gate_script_source,
        tmp_path,
        {
            "requested_model": REQUESTED_MODEL,
            "resolved_model": "gpt-5.6-terra",
            "available_models": [],
        },
    )
    assert out["status"] == "blocked"
    assert out["reason"] == "resolved_model_mismatch_without_available_models_evidence"


def test_output_echoes_all_observed_fields(gate_script_source, tmp_path):
    payload = {
        "requested_model": REQUESTED_MODEL,
        "resolved_model": "gpt-5.6-terra",
        "models_used": ["gpt-5.6-terra"],
        "available_models": ["gpt-5.6-terra"],
    }
    out = _detect(gate_script_source, tmp_path, payload)
    assert out["requested_model"] == REQUESTED_MODEL
    assert out["resolved_model"] == "gpt-5.6-terra"
    assert out["models_used"] == ["gpt-5.6-terra"]
    assert out["available_models"] == ["gpt-5.6-terra"]


def test_never_gates_a_tool_call_no_hook_specific_output_shape(gate_script_source, tmp_path):
    """AC12's detector is pure evidence analysis, never a PreToolUse
    authorization decision -- its output must not carry the
    `hookSpecificOutput`/`permissionDecision` shape `cmd_pre_tool_use_agent`
    uses, so nothing downstream could ever mistake it for a gate decision."""
    out = _detect(
        gate_script_source,
        tmp_path,
        {"requested_model": REQUESTED_MODEL, "resolved_model": REQUESTED_MODEL},
    )
    assert "hookSpecificOutput" not in out
    assert "permissionDecision" not in out
