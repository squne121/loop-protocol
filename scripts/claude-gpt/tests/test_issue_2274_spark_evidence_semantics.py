"""Issue #2274 corrective iteration (2026-08-22): dedicated AC17/AC18
regression tests for scripts/claude-gpt/runtime_smoke_test.sh's embedded
`SPARK_EVIDENCE_PY` evidence builder.

Why this file exists (Phase 2 P0-C finding, corrective iteration): the
existing
`scripts/agent-ops/tests/test_run_worktree_agent_runtime_smoke_runtime_evidence.py`
does not reference `modelsUsed` / `resolvedModel` /
`claude_code_evidence_schema_unsupported` at all (independently verified
2026-08-22) and therefore never exercised the #2274 AC17/AC18 semantics
implemented in `scripts/claude-gpt/runtime_smoke_test.sh`. That existing
test passing was never proof AC17/AC18 were genuinely satisfied.

This module extracts the `SPARK_EVIDENCE_PY` heredoc body from
`runtime_smoke_test.sh` at test-collection time and drives it as a real
subprocess against synthetic stream-json fixtures, so every fixture below
exercises the exact same code path the live `--spark-delegation` E2E uses
-- never a reimplementation that could silently diverge from production
behavior.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
RUNTIME_SMOKE_SH = pathlib.Path(__file__).resolve().parents[1] / "runtime_smoke_test.sh"

_HEREDOC_RE = re.compile(
    r"cat > \"\$SPARK_EVIDENCE_PY\" <<'SPARK_EVIDENCE_PY_EOF'\n(.*?)\nSPARK_EVIDENCE_PY_EOF\n",
    re.DOTALL,
)

AGENT_NAME = "spark-codex"
EXPECTED_MODEL = "gpt-5.3-codex-spark"
TOOL_USE_ID = "toolu_test_0001"
AGENT_ID = "agent_test_0001"

_ABSENT = object()


def _extract_evidence_script() -> str:
    text = RUNTIME_SMOKE_SH.read_text(encoding="utf-8")
    match = _HEREDOC_RE.search(text)
    assert match, (
        "SPARK_EVIDENCE_PY heredoc not found in runtime_smoke_test.sh -- "
        "script structure changed; update the extraction regex in this test."
    )
    return match.group(1)


def _stream_line(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _build_stream(
    *,
    resolved_model,
    models_used_field,
    status="completed",
    include_lifecycle=True,
    duplicate_start=False,
    duplicate_stop=False,
    missing_start=False,
    missing_stop=False,
    mismatched_agent_id_on_stop=False,
    tool_use_id=TOOL_USE_ID,
    result_tool_use_id=None,
    agent_id=AGENT_ID,
) -> str:
    lines = []
    lines.append(
        _stream_line(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": "Task",
                            "input": {"subagent_type": AGENT_NAME},
                        }
                    ]
                },
            }
        )
    )
    if include_lifecycle and not missing_start:
        lines.append(
            _stream_line(
                {
                    "type": "system",
                    "hook_event": "SubagentStart",
                    "stdout": json.dumps({"agent_id": agent_id, "agent_type": AGENT_NAME}),
                }
            )
        )
        if duplicate_start:
            lines.append(
                _stream_line(
                    {
                        "type": "system",
                        "hook_event": "SubagentStart",
                        "stdout": json.dumps({"agent_id": agent_id, "agent_type": AGENT_NAME}),
                    }
                )
            )

    tool_use_result = {"status": status, "agentId": agent_id}
    if resolved_model is not None:
        tool_use_result["resolvedModel"] = resolved_model
    if models_used_field is not _ABSENT:
        tool_use_result["modelsUsed"] = models_used_field

    lines.append(
        _stream_line(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": result_tool_use_id or tool_use_id,
                            "is_error": False,
                        }
                    ]
                },
                "tool_use_result": tool_use_result,
            }
        )
    )

    if include_lifecycle and not missing_stop:
        stop_agent_id = "other_agent_mismatch" if mismatched_agent_id_on_stop else agent_id
        lines.append(
            _stream_line(
                {
                    "type": "system",
                    "hook_event": "SubagentStop",
                    "stdout": json.dumps({"agent_id": stop_agent_id, "agent_type": AGENT_NAME}),
                }
            )
        )
        if duplicate_stop:
            lines.append(
                _stream_line(
                    {
                        "type": "system",
                        "hook_event": "SubagentStop",
                        "stdout": json.dumps({"agent_id": agent_id, "agent_type": AGENT_NAME}),
                    }
                )
            )

    return "\n".join(lines) + "\n"


_DEFAULT_PROXY_SLICE = (
    json.dumps(
        {
            "msg": "codex_upstream_request_started",
            "fields": {"reqId": "req-1", "model": EXPECTED_MODEL, "transport": "http"},
        },
        separators=(",", ":"),
    )
    + "\n"
)


def _run_evidence(
    tmp_path,
    *,
    stream_text,
    proxy_slice_text=_DEFAULT_PROXY_SLICE,
    claude_code_version="2.1.238 (Claude Code)",
):
    script = _extract_evidence_script()
    script_path = tmp_path / "spark_evidence.py"
    script_path.write_text(script, encoding="utf-8")

    stdout_path = tmp_path / "stdout.jsonl"
    stdout_path.write_text(stream_text, encoding="utf-8")
    proxy_slice_path = tmp_path / "proxy_slice.jsonl"
    proxy_slice_path.write_text(proxy_slice_text, encoding="utf-8")

    args = [
        sys.executable,
        str(script_path),
        str(stdout_path),
        str(proxy_slice_path),
        AGENT_NAME,
        EXPECTED_MODEL,
        "MARKER",
        "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        "false",
        "launchsha",
        "libsha",
        "smokesha",
        "/tmp/proxy",
        "1.0.0",
        "proxysha",
        claude_code_version,
        "2026-08-22T00:00:00Z",
    ]
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    # The evidence builder intentionally exits 1 for any non-"pass" verdict
    # (runtime_smoke_test.sh's own shell caller branches on this), so exit
    # code 0 or 1 both indicate the script ran to completion and emitted a
    # typed verdict on stdout; any other exit code (crash before the final
    # `sys.exit`) is a genuine test infrastructure failure.
    assert result.returncode in (0, 1), (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# modelsUsed semantics matrix (AC18)
# ---------------------------------------------------------------------------


def test_below_floor_absent_models_used_is_blocked(tmp_path):
    stream = _build_stream(resolved_model=EXPECTED_MODEL, models_used_field=_ABSENT)
    evidence = _run_evidence(tmp_path, stream_text=stream, claude_code_version="2.1.211 (Claude Code)")
    assert evidence["verdict"]["status"] == "blocked"
    assert evidence["verdict"]["reason"] == "claude_code_evidence_schema_unsupported"


def test_at_floor_resolved_model_present_models_used_absent_is_pass_candidate(tmp_path):
    stream = _build_stream(resolved_model=EXPECTED_MODEL, models_used_field=_ABSENT)
    evidence = _run_evidence(tmp_path, stream_text=stream, claude_code_version="2.1.212 (Claude Code)")
    assert "claude_code_evidence_schema_unsupported" not in evidence["_debug_reasons"]
    assert evidence["agent"]["models_used_raw_present"] is False
    assert evidence["runtime"]["models_used_version_floor_met"] is True


def test_models_used_single_expected_model_is_pass_candidate(tmp_path):
    stream = _build_stream(resolved_model=EXPECTED_MODEL, models_used_field=[EXPECTED_MODEL])
    evidence = _run_evidence(tmp_path, stream_text=stream, claude_code_version="2.1.238 (Claude Code)")
    assert "claude_code_evidence_schema_unsupported" not in evidence["_debug_reasons"]
    assert not any(r.startswith("models_used_silent_swap_detected") for r in evidence["_debug_reasons"])
    assert evidence["agent"]["models_used_raw_present"] is True


def test_models_used_with_other_model_is_fail(tmp_path):
    stream = _build_stream(
        resolved_model=EXPECTED_MODEL, models_used_field=[EXPECTED_MODEL, "claude-haiku-4-5"]
    )
    evidence = _run_evidence(tmp_path, stream_text=stream, claude_code_version="2.1.238 (Claude Code)")
    assert any(r.startswith("models_used_silent_swap_detected") for r in evidence["_debug_reasons"])
    assert evidence["verdict"]["status"] == "fail"


def test_resolved_model_other_is_fail(tmp_path):
    stream = _build_stream(resolved_model="claude-sonnet-4-5", models_used_field=_ABSENT)
    evidence = _run_evidence(tmp_path, stream_text=stream, claude_code_version="2.1.238 (Claude Code)")
    assert any(r.startswith("resolved_model_mismatch") for r in evidence["_debug_reasons"])
    assert evidence["verdict"]["status"] == "fail"


def test_async_launched_status_is_fail(tmp_path):
    stream = _build_stream(resolved_model=EXPECTED_MODEL, models_used_field=_ABSENT, status="async_launched")
    evidence = _run_evidence(tmp_path, stream_text=stream, claude_code_version="2.1.238 (Claude Code)")
    assert any(r.startswith("agent_status_not_completed") for r in evidence["_debug_reasons"])
    assert evidence["verdict"]["status"] == "fail"


# ---------------------------------------------------------------------------
# lifecycle correlation matrix (AC17)
# ---------------------------------------------------------------------------


def test_exact_lifecycle_pair_matching_agent_id_is_not_flagged(tmp_path):
    stream = _build_stream(resolved_model=EXPECTED_MODEL, models_used_field=[EXPECTED_MODEL])
    evidence = _run_evidence(tmp_path, stream_text=stream, claude_code_version="2.1.238 (Claude Code)")
    assert not any(
        "lifecycle_pair" in r or "no_lifecycle_correlation" in r for r in evidence["_debug_reasons"]
    )


def test_missing_start_is_fail(tmp_path):
    stream = _build_stream(resolved_model=EXPECTED_MODEL, models_used_field=[EXPECTED_MODEL], missing_start=True)
    evidence = _run_evidence(tmp_path, stream_text=stream, claude_code_version="2.1.238 (Claude Code)")
    assert any("lifecycle_pair_not_exactly_one" in r for r in evidence["_debug_reasons"])
    assert evidence["verdict"]["status"] == "fail"


def test_missing_stop_is_fail(tmp_path):
    stream = _build_stream(resolved_model=EXPECTED_MODEL, models_used_field=[EXPECTED_MODEL], missing_stop=True)
    evidence = _run_evidence(tmp_path, stream_text=stream, claude_code_version="2.1.238 (Claude Code)")
    assert any("lifecycle_pair_not_exactly_one" in r for r in evidence["_debug_reasons"])
    assert evidence["verdict"]["status"] == "fail"


def test_duplicate_start_is_fail(tmp_path):
    stream = _build_stream(resolved_model=EXPECTED_MODEL, models_used_field=[EXPECTED_MODEL], duplicate_start=True)
    evidence = _run_evidence(tmp_path, stream_text=stream, claude_code_version="2.1.238 (Claude Code)")
    assert any("lifecycle_pair_not_exactly_one" in r for r in evidence["_debug_reasons"])
    assert evidence["verdict"]["status"] == "fail"


def test_duplicate_stop_is_fail(tmp_path):
    stream = _build_stream(resolved_model=EXPECTED_MODEL, models_used_field=[EXPECTED_MODEL], duplicate_stop=True)
    evidence = _run_evidence(tmp_path, stream_text=stream, claude_code_version="2.1.238 (Claude Code)")
    assert any("lifecycle_pair_not_exactly_one" in r for r in evidence["_debug_reasons"])
    assert evidence["verdict"]["status"] == "fail"


def test_mismatched_agent_id_on_stop_is_fail(tmp_path):
    stream = _build_stream(
        resolved_model=EXPECTED_MODEL, models_used_field=[EXPECTED_MODEL], mismatched_agent_id_on_stop=True
    )
    evidence = _run_evidence(tmp_path, stream_text=stream, claude_code_version="2.1.238 (Claude Code)")
    assert any("lifecycle_pair_not_exactly_one" in r for r in evidence["_debug_reasons"])
    assert evidence["verdict"]["status"] == "fail"


def test_mismatched_tool_use_id_is_fail(tmp_path):
    stream = _build_stream(
        resolved_model=EXPECTED_MODEL,
        models_used_field=[EXPECTED_MODEL],
        tool_use_id=TOOL_USE_ID,
        result_tool_use_id="toolu_other_0002",
    )
    evidence = _run_evidence(tmp_path, stream_text=stream, claude_code_version="2.1.238 (Claude Code)")
    assert "no_tool_result_matched_tool_use_id" in evidence["_debug_reasons"]
    assert evidence["verdict"]["status"] == "fail"
