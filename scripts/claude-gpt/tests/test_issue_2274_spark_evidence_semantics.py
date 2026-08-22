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

2026-08-22 AC17 corrective iteration addendum: the evidence builder's proxy
correlation window used to be sliced by the CALLER (runtime_smoke_test.sh's
bash, via an invocation-wide before/after-the-whole-launch.sh-call
approximation) before ever reaching this script. It is now sliced INSIDE
this script from hook-time byte offsets recorded by the SubagentStart/
SubagentStop observational hooks themselves (see launch.sh's
SPARK_LIFECYCLE_OFFSET_WRITER) and passed in via the stream-json lifecycle
events' `proxy_log_byte_offset_at_hook_time` field, plus a new
`proxy_captured_log_size` argv entry for beyond-EOF validation. The
"lifecycle correlation matrix" tests below therefore also cover the
hook-time window matrix (offset presence/type/range, and the
contamination-before-start / bounded-window differential), not merely
agent_id/tool_use_id correlation.

PR #2285 OWNER fix-delta (iteration 1) argv/schema update: the former bare
`proxy_captured_log_size` argv entry is now a JSON blob
`{"size", "dev", "ino", "mtime_ns"}` describing a single-fd fstat() snapshot
identity (P1-5), and a new trailing `agents_json_audit_raw` argv entry
carries the real `--agents` JSON launch.sh audited before exec, used to
independently verify `definition.source` (P0-3) instead of self-reporting a
fixed constant. The lifecycle SubagentStart/SubagentStop hook payloads also
now carry `proxy_log_dev_at_hook_time` / `proxy_log_ino_at_hook_time` /
`proxy_log_mtime_ns_at_hook_time` identity fields alongside the byte offset,
validated the same fail-closed way. The proxy-request cardinality/reqId
matrix (P0-2) and the identity-consistency matrix (P1-5) are covered below.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import pytest

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
UNRELATED_MODEL = "gpt-5.6-terra"

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


DEFAULT_DEV = 64512
DEFAULT_INO = 1000001
DEFAULT_MTIME_NS = 1700000000000000000


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
    start_byte_offset=0,
    stop_byte_offset=None,
    omit_start_byte_offset=False,
    omit_stop_byte_offset=False,
    start_dev=DEFAULT_DEV,
    start_ino=DEFAULT_INO,
    start_mtime_ns=DEFAULT_MTIME_NS,
    stop_dev=DEFAULT_DEV,
    stop_ino=DEFAULT_INO,
    stop_mtime_ns=DEFAULT_MTIME_NS,
    omit_start_identity=False,
    omit_stop_identity=False,
) -> str:
    if stop_byte_offset is None:
        stop_byte_offset = _DEFAULT_PROXY_SLICE_LEN
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
        start_payload = {"agent_id": agent_id, "agent_type": AGENT_NAME}
        if not omit_start_byte_offset:
            start_payload["proxy_log_byte_offset_at_hook_time"] = start_byte_offset
        if not omit_start_identity:
            start_payload["proxy_log_dev_at_hook_time"] = start_dev
            start_payload["proxy_log_ino_at_hook_time"] = start_ino
            start_payload["proxy_log_mtime_ns_at_hook_time"] = start_mtime_ns
        lines.append(
            _stream_line(
                {
                    "type": "system",
                    "hook_event": "SubagentStart",
                    "stdout": json.dumps(start_payload),
                }
            )
        )
        if duplicate_start:
            lines.append(
                _stream_line(
                    {
                        "type": "system",
                        "hook_event": "SubagentStart",
                        "stdout": json.dumps(start_payload),
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
        stop_payload = {"agent_id": stop_agent_id, "agent_type": AGENT_NAME}
        if not omit_stop_byte_offset:
            stop_payload["proxy_log_byte_offset_at_hook_time"] = stop_byte_offset
        if not omit_stop_identity:
            stop_payload["proxy_log_dev_at_hook_time"] = stop_dev
            stop_payload["proxy_log_ino_at_hook_time"] = stop_ino
            stop_payload["proxy_log_mtime_ns_at_hook_time"] = stop_mtime_ns
        lines.append(
            _stream_line(
                {
                    "type": "system",
                    "hook_event": "SubagentStop",
                    "stdout": json.dumps(stop_payload),
                }
            )
        )
        if duplicate_stop:
            duplicate_stop_payload = {"agent_id": agent_id, "agent_type": AGENT_NAME}
            if not omit_stop_byte_offset:
                duplicate_stop_payload["proxy_log_byte_offset_at_hook_time"] = stop_byte_offset
            lines.append(
                _stream_line(
                    {
                        "type": "system",
                        "hook_event": "SubagentStop",
                        "stdout": json.dumps(duplicate_stop_payload),
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
_DEFAULT_PROXY_SLICE_LEN = len(_DEFAULT_PROXY_SLICE.encode("utf-8"))


DEFAULT_AGENTS_JSON_AUDIT = json.dumps({AGENT_NAME: {"model": EXPECTED_MODEL}})


def _run_evidence(
    tmp_path,
    *,
    stream_text,
    proxy_slice_text=_DEFAULT_PROXY_SLICE,
    captured_log_size=None,
    captured_log_dev=DEFAULT_DEV,
    captured_log_ino=DEFAULT_INO,
    captured_log_mtime_ns=DEFAULT_MTIME_NS,
    agents_json_audit_raw=DEFAULT_AGENTS_JSON_AUDIT,
    claude_code_version="2.1.238 (Claude Code)",
):
    script = _extract_evidence_script()
    script_path = tmp_path / "spark_evidence.py"
    script_path.write_text(script, encoding="utf-8")

    stdout_path = tmp_path / "stdout.jsonl"
    stdout_path.write_text(stream_text, encoding="utf-8")
    # 2026-08-22 AC17 corrective iteration: this file is no longer a
    # pre-sliced window -- it is the FULL captured proxy log snapshot the
    # evidence builder itself now slices using the hook-time byte offsets
    # extracted from `stream_text`'s SubagentStart/SubagentStop events.
    proxy_full_log_path = tmp_path / "proxy_full_log.jsonl"
    proxy_full_log_path.write_text(proxy_slice_text, encoding="utf-8")

    if captured_log_size is None:
        captured_log_size = len(proxy_slice_text.encode("utf-8"))

    # PR #2285 OWNER fix-delta P1-5: the former bare-int
    # `proxy_captured_log_size` argv entry is now a JSON blob describing a
    # single-fd fstat() identity snapshot (size/dev/ino/mtime_ns).
    proxy_snapshot_stat_json = json.dumps(
        {
            "size": captured_log_size,
            "dev": captured_log_dev,
            "ino": captured_log_ino,
            "mtime_ns": captured_log_mtime_ns,
        }
    )

    args = [
        sys.executable,
        str(script_path),
        str(stdout_path),
        str(proxy_full_log_path),
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
        proxy_snapshot_stat_json,
        agents_json_audit_raw,
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


# ---------------------------------------------------------------------------
# hook-time byte-offset lifecycle window matrix (AC17 corrective iteration,
# 2026-08-22). These tests are the regression guard for the genuine
# implementation gap the 2026-08-22 corrective comment identified: the
# proxy correlation window is now sliced from the SubagentStart/
# SubagentStop hooks' OWN execution-time byte offsets, never from an
# invocation-wide before/after-the-whole-launch.sh-call approximation.
# ---------------------------------------------------------------------------


def _proxy_log_line(*, model, req_id, transport="http"):
    return (
        json.dumps(
            {
                "msg": "codex_upstream_request_started",
                "fields": {"reqId": req_id, "model": model, "transport": transport},
            },
            separators=(",", ":"),
        )
        + "\n"
    )


def test_contamination_before_hook_start_is_excluded_and_fails(tmp_path):
    """A genuine Spark-model request that fired BEFORE the SubagentStart
    hook's own execution-time offset (e.g. leftover traffic from a prior
    turn/agent) must never be readable as this run's evidence. Under the
    superseded invocation-wide approximation this exact contamination shape
    was reachable as a false PASS -- it fell inside the wider
    before/after-the-whole-launch.sh-call window. The hook-time window
    excludes it structurally (this process never opens those bytes), so the
    lifecycle window here (which contains zero Spark requests) must FAIL."""
    contam_line = _proxy_log_line(model=EXPECTED_MODEL, req_id="req-contam")
    noise_line = _proxy_log_line(model=UNRELATED_MODEL, req_id="req-noise")
    full_log = contam_line + noise_line
    start_offset = len(contam_line.encode("utf-8"))
    stop_offset = len(full_log.encode("utf-8"))

    stream = _build_stream(
        resolved_model=EXPECTED_MODEL,
        models_used_field=[EXPECTED_MODEL],
        start_byte_offset=start_offset,
        stop_byte_offset=stop_offset,
    )
    evidence = _run_evidence(
        tmp_path,
        stream_text=stream,
        proxy_slice_text=full_log,
        captured_log_size=len(full_log.encode("utf-8")),
    )

    assert evidence["verdict"]["status"] == "fail"
    assert "no_proxy_spark_model_request_observed_in_lifecycle_window" in evidence["_debug_reasons"]
    assert evidence["proxy"]["request_count"] == 0
    assert all(r.get("req_id") != "req-contam" for r in evidence["proxy"]["requests"])


def test_bounded_window_excludes_requests_outside_hook_time_window(tmp_path):
    """A proxy log with unrelated parent-session traffic both before and
    after the hook-time window, and exactly one genuine Spark request
    inside it, must PASS with request_count == 1 and must never surface the
    out-of-window requests as evidence."""
    pre_line = _proxy_log_line(model=UNRELATED_MODEL, req_id="req-pre")
    spark_line = _proxy_log_line(model=EXPECTED_MODEL, req_id="req-spark")
    post_line = _proxy_log_line(model=UNRELATED_MODEL, req_id="req-post")
    full_log = pre_line + spark_line + post_line
    start_offset = len(pre_line.encode("utf-8"))
    stop_offset = len((pre_line + spark_line).encode("utf-8"))

    stream = _build_stream(
        resolved_model=EXPECTED_MODEL,
        models_used_field=[EXPECTED_MODEL],
        start_byte_offset=start_offset,
        stop_byte_offset=stop_offset,
    )
    evidence = _run_evidence(
        tmp_path,
        stream_text=stream,
        proxy_slice_text=full_log,
        captured_log_size=len(full_log.encode("utf-8")),
    )

    assert evidence["verdict"]["status"] == "pass"
    assert evidence["proxy"]["request_count"] == 1
    assert [r["req_id"] for r in evidence["proxy"]["requests"]] == ["req-spark"]
    assert evidence["proxy"]["lifecycle_window_start_byte_offset"] == start_offset
    assert evidence["proxy"]["lifecycle_window_stop_byte_offset"] == stop_offset


@pytest.mark.parametrize(
    "kwargs,expected_reason_prefix",
    [
        ({"omit_start_byte_offset": True}, "spark_start_offset_missing"),
        ({"omit_stop_byte_offset": True}, "spark_stop_offset_missing"),
        ({"start_byte_offset": "abc"}, "spark_start_offset_not_integer"),
        ({"stop_byte_offset": "xyz"}, "spark_stop_offset_not_integer"),
        ({"start_byte_offset": -5}, "spark_start_offset_negative"),
        ({"stop_byte_offset": -1}, "spark_stop_offset_negative"),
        ({"start_byte_offset": 999, "stop_byte_offset": 1}, "spark_start_offset_after_stop_offset"),
    ],
)
def test_offset_validation_matrix_is_fail_closed(tmp_path, kwargs, expected_reason_prefix):
    stream = _build_stream(
        resolved_model=EXPECTED_MODEL,
        models_used_field=[EXPECTED_MODEL],
        **kwargs,
    )
    evidence = _run_evidence(tmp_path, stream_text=stream)
    assert any(r.startswith(expected_reason_prefix) for r in evidence["_debug_reasons"]), evidence["_debug_reasons"]
    assert evidence["verdict"]["status"] == "fail"


def test_stop_offset_beyond_captured_proxy_log_size_is_fail(tmp_path):
    stream = _build_stream(
        resolved_model=EXPECTED_MODEL,
        models_used_field=[EXPECTED_MODEL],
        start_byte_offset=0,
        stop_byte_offset=500,
    )
    evidence = _run_evidence(tmp_path, stream_text=stream, captured_log_size=10)
    assert any(
        r.startswith("spark_stop_offset_beyond_captured_proxy_log_size") for r in evidence["_debug_reasons"]
    ), evidence["_debug_reasons"]
    assert evidence["verdict"]["status"] == "fail"


def test_valid_hook_time_offsets_default_window_is_not_flagged(tmp_path):
    """The default fixture (single Spark request, window covering the
    whole default proxy log) must remain a clean PASS candidate with no
    offset-related typed reason -- i.e. this corrective iteration's new
    validation must not regress the already-passing default case."""
    stream = _build_stream(resolved_model=EXPECTED_MODEL, models_used_field=[EXPECTED_MODEL])
    evidence = _run_evidence(tmp_path, stream_text=stream)
    assert not any(
        r.startswith("spark_start_offset") or r.startswith("spark_stop_offset")
        for r in evidence["_debug_reasons"]
    )
    assert evidence["verdict"]["status"] == "pass"



# ---------------------------------------------------------------------------
# proxy request cardinality / reqId matrix (PR #2285 OWNER fix-delta P0-2).
# Only "zero matching requests" was previously fail-closed; multiple/
# duplicate/uncorrelated Spark proxy evidence must ALL fail too.
# ---------------------------------------------------------------------------


def test_two_spark_requests_in_window_is_fail(tmp_path):
    """Two distinct Spark HTTP requests inside the lifecycle window (e.g. a
    genuine double-dispatch bug) must never PASS -- only exactly one is ever
    trustworthy evidence of a single Agent delegation."""
    line_a = _proxy_log_line(model=EXPECTED_MODEL, req_id="req-a")
    line_b = _proxy_log_line(model=EXPECTED_MODEL, req_id="req-b")
    full_log = line_a + line_b
    stream = _build_stream(
        resolved_model=EXPECTED_MODEL,
        models_used_field=[EXPECTED_MODEL],
        start_byte_offset=0,
        stop_byte_offset=len(full_log.encode("utf-8")),
    )
    evidence = _run_evidence(tmp_path, stream_text=stream, proxy_slice_text=full_log)
    assert evidence["verdict"]["status"] == "fail"
    assert "spark_proxy_request_cardinality_not_one_2" in evidence["_debug_reasons"]
    assert evidence["proxy"]["request_count"] == 2


def test_two_requests_sharing_same_req_id_is_fail(tmp_path):
    """Two Spark requests that share the SAME reqId (e.g. a retry that was
    never de-duplicated) must still fail on cardinality -- duplicate reqId
    must never be silently collapsed into "one trustworthy request"."""
    line_a = _proxy_log_line(model=EXPECTED_MODEL, req_id="req-dup")
    line_b = _proxy_log_line(model=EXPECTED_MODEL, req_id="req-dup")
    full_log = line_a + line_b
    stream = _build_stream(
        resolved_model=EXPECTED_MODEL,
        models_used_field=[EXPECTED_MODEL],
        start_byte_offset=0,
        stop_byte_offset=len(full_log.encode("utf-8")),
    )
    evidence = _run_evidence(tmp_path, stream_text=stream, proxy_slice_text=full_log)
    assert evidence["verdict"]["status"] == "fail"
    assert "spark_proxy_request_cardinality_not_one_2" in evidence["_debug_reasons"]


def test_single_request_missing_req_id_is_fail(tmp_path):
    """A single Spark request with a missing/empty reqId passes cardinality
    (exactly one) but must still fail: an uncorrelated request is not
    trustworthy evidence."""
    line = json.dumps(
        {
            "msg": "codex_upstream_request_started",
            "fields": {"reqId": "", "model": EXPECTED_MODEL, "transport": "http"},
        },
        separators=(",", ":"),
    ) + "\n"
    stream = _build_stream(
        resolved_model=EXPECTED_MODEL,
        models_used_field=[EXPECTED_MODEL],
        start_byte_offset=0,
        stop_byte_offset=len(line.encode("utf-8")),
    )
    evidence = _run_evidence(tmp_path, stream_text=stream, proxy_slice_text=line)
    assert evidence["verdict"]["status"] == "fail"
    assert evidence["proxy"]["request_count"] == 1
    assert "spark_proxy_request_id_missing" in evidence["_debug_reasons"]


def test_spark_request_immediately_followed_by_retry_shaped_request_is_fail(tmp_path):
    """A genuine Spark request immediately followed by what looks like a
    retry (same model, a different reqId, back-to-back in the log) must
    fail on cardinality just like any other 2-request window -- a retry
    shape is never silently treated as "the same" request."""
    original = _proxy_log_line(model=EXPECTED_MODEL, req_id="req-original")
    retry = _proxy_log_line(model=EXPECTED_MODEL, req_id="req-original-retry-1")
    full_log = original + retry
    stream = _build_stream(
        resolved_model=EXPECTED_MODEL,
        models_used_field=[EXPECTED_MODEL],
        start_byte_offset=0,
        stop_byte_offset=len(full_log.encode("utf-8")),
    )
    evidence = _run_evidence(tmp_path, stream_text=stream, proxy_slice_text=full_log)
    assert evidence["verdict"]["status"] == "fail"
    assert "spark_proxy_request_cardinality_not_one_2" in evidence["_debug_reasons"]


def test_concurrent_session_spark_request_with_same_model_is_fail(tmp_path):
    """A second, unrelated concurrent-session Spark request that happens to
    land inside this run's bounded window (e.g. a raciness in test
    infrastructure or a real concurrent delegation) must fail on
    cardinality -- the bounded hook-time window alone is not sufficient
    isolation without also requiring exactly one match."""
    this_run = _proxy_log_line(model=EXPECTED_MODEL, req_id="req-this-run")
    concurrent = _proxy_log_line(model=EXPECTED_MODEL, req_id="req-concurrent-session")
    full_log = this_run + concurrent
    stream = _build_stream(
        resolved_model=EXPECTED_MODEL,
        models_used_field=[EXPECTED_MODEL],
        start_byte_offset=0,
        stop_byte_offset=len(full_log.encode("utf-8")),
    )
    evidence = _run_evidence(tmp_path, stream_text=stream, proxy_slice_text=full_log)
    assert evidence["verdict"]["status"] == "fail"
    assert "spark_proxy_request_cardinality_not_one_2" in evidence["_debug_reasons"]


def test_exactly_one_spark_request_with_unrelated_traffic_in_same_window_still_passes(tmp_path):
    """Exactly one genuine Spark request PLUS unrelated non-Spark parent
    traffic INSIDE the same bounded window must still PASS -- unrelated
    models are never counted toward Spark's own cardinality (this is
    live-observed reality per the AC5 2026-08-22 corrective note)."""
    spark_line = _proxy_log_line(model=EXPECTED_MODEL, req_id="req-spark-only")
    unrelated_line = _proxy_log_line(model=UNRELATED_MODEL, req_id="req-unrelated")
    full_log = spark_line + unrelated_line
    stream = _build_stream(
        resolved_model=EXPECTED_MODEL,
        models_used_field=[EXPECTED_MODEL],
        start_byte_offset=0,
        stop_byte_offset=len(full_log.encode("utf-8")),
    )
    evidence = _run_evidence(tmp_path, stream_text=stream, proxy_slice_text=full_log)
    assert evidence["verdict"]["status"] == "pass"
    assert evidence["proxy"]["request_count"] == 1
    assert [r["req_id"] for r in evidence["proxy"]["requests"]] == ["req-spark-only"]


# ---------------------------------------------------------------------------
# definition.source real --agents JSON audit verification (PR #2285 OWNER
# fix-delta P0-3). `definition.source` must never be a self-reported
# constant -- only reported as launcher-owned when independently confirmed
# from the real audited JSON.
# ---------------------------------------------------------------------------


def test_definition_source_verified_from_matching_real_audit_json(tmp_path):
    stream = _build_stream(resolved_model=EXPECTED_MODEL, models_used_field=[EXPECTED_MODEL])
    evidence = _run_evidence(tmp_path, stream_text=stream)
    assert evidence["definition"]["source"] == "launcher_owned_agents_json"
    assert "agents_json_audit_missing_or_mismatched" not in evidence["_debug_reasons"]


def test_definition_source_unverified_when_audit_missing(tmp_path):
    stream = _build_stream(resolved_model=EXPECTED_MODEL, models_used_field=[EXPECTED_MODEL])
    evidence = _run_evidence(tmp_path, stream_text=stream, agents_json_audit_raw="")
    assert evidence["definition"]["source"] == "unverified"
    assert "agents_json_audit_missing_or_mismatched" in evidence["_debug_reasons"]
    assert evidence["verdict"]["status"] == "fail"


def test_definition_source_unverified_when_audit_model_mismatched(tmp_path):
    stream = _build_stream(resolved_model=EXPECTED_MODEL, models_used_field=[EXPECTED_MODEL])
    mismatched_audit = json.dumps({AGENT_NAME: {"model": "claude-haiku-4-5"}})
    evidence = _run_evidence(tmp_path, stream_text=stream, agents_json_audit_raw=mismatched_audit)
    assert evidence["definition"]["source"] == "unverified"
    assert "agents_json_audit_missing_or_mismatched" in evidence["_debug_reasons"]
    assert evidence["verdict"]["status"] == "fail"


def test_definition_source_unverified_when_audit_json_malformed(tmp_path):
    stream = _build_stream(resolved_model=EXPECTED_MODEL, models_used_field=[EXPECTED_MODEL])
    evidence = _run_evidence(tmp_path, stream_text=stream, agents_json_audit_raw="{not valid json")
    assert evidence["definition"]["source"] == "unverified"
    assert "agents_json_audit_missing_or_mismatched" in evidence["_debug_reasons"]
    assert evidence["verdict"]["status"] == "fail"


def test_definition_source_unverified_when_audit_agent_name_absent(tmp_path):
    stream = _build_stream(resolved_model=EXPECTED_MODEL, models_used_field=[EXPECTED_MODEL])
    other_agent_audit = json.dumps({"some-other-agent": {"model": EXPECTED_MODEL}})
    evidence = _run_evidence(tmp_path, stream_text=stream, agents_json_audit_raw=other_agent_audit)
    assert evidence["definition"]["source"] == "unverified"
    assert "agents_json_audit_missing_or_mismatched" in evidence["_debug_reasons"]


# ---------------------------------------------------------------------------
# proxy log identity (dev/ino) matrix (PR #2285 OWNER fix-delta P1-5). A
# rotation/truncation/replacement between hook-time and snapshot-time must
# be typed-FAIL detected, never silently misapplied as a stale-generation
# byte offset onto a new-generation file.
# ---------------------------------------------------------------------------


def test_identity_missing_on_start_is_fail(tmp_path):
    stream = _build_stream(
        resolved_model=EXPECTED_MODEL,
        models_used_field=[EXPECTED_MODEL],
        omit_start_identity=True,
    )
    evidence = _run_evidence(tmp_path, stream_text=stream)
    assert "spark_start_identity_missing" in evidence["_debug_reasons"]
    assert evidence["verdict"]["status"] == "fail"


def test_identity_missing_on_stop_is_fail(tmp_path):
    stream = _build_stream(
        resolved_model=EXPECTED_MODEL,
        models_used_field=[EXPECTED_MODEL],
        omit_stop_identity=True,
    )
    evidence = _run_evidence(tmp_path, stream_text=stream)
    assert "spark_stop_identity_missing" in evidence["_debug_reasons"]
    assert evidence["verdict"]["status"] == "fail"


def test_identity_changed_during_window_is_fail(tmp_path):
    """SubagentStart and SubagentStop recorded a DIFFERENT (dev, ino) --
    i.e. the proxy log was rotated/truncated/replaced mid-window -- and the
    window must never be trusted in that case."""
    stream = _build_stream(
        resolved_model=EXPECTED_MODEL,
        models_used_field=[EXPECTED_MODEL],
        start_ino=DEFAULT_INO,
        stop_ino=DEFAULT_INO + 1,
    )
    evidence = _run_evidence(tmp_path, stream_text=stream)
    assert "spark_proxy_log_identity_changed_during_window" in evidence["_debug_reasons"]
    assert evidence["verdict"]["status"] == "fail"


def test_identity_changed_before_snapshot_is_fail(tmp_path):
    """SubagentStart and SubagentStop agree on identity, but the
    post-invocation snapshot's own fstat() identity differs -- rotation
    happened AFTER SubagentStop but BEFORE the snapshot was captured."""
    stream = _build_stream(resolved_model=EXPECTED_MODEL, models_used_field=[EXPECTED_MODEL])
    evidence = _run_evidence(tmp_path, stream_text=stream, captured_log_ino=DEFAULT_INO + 1)
    assert "spark_proxy_log_identity_changed_before_snapshot" in evidence["_debug_reasons"]
    assert evidence["verdict"]["status"] == "fail"


def test_default_identity_is_consistent_and_not_flagged(tmp_path):
    """The default fixture (consistent dev/ino across start/stop/snapshot)
    must never surface any identity-related typed reason -- this
    corrective addition must not regress the already-passing default
    case."""
    stream = _build_stream(resolved_model=EXPECTED_MODEL, models_used_field=[EXPECTED_MODEL])
    evidence = _run_evidence(tmp_path, stream_text=stream)
    assert not any(r.startswith("spark_") and "identity" in r for r in evidence["_debug_reasons"])
    assert evidence["verdict"]["status"] == "pass"
