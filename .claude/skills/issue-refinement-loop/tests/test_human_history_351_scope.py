from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[4]
_PUBLISH_SCRIPT = _ROOT / ".claude/skills/issue-refinement-loop/scripts/publish_termination_report.py"


def test_quality_scope_is_limited_to_new_human_history():
    source = _PUBLISH_SCRIPT.read_text()
    assert "existing controlled publisher, not a new" in source
    assert "existing issue_comment.publish" in source
    assert "human-interface-finalizer" not in source
    assert "finalizer SubAgent" not in source
    step = (_ROOT / ".claude/skills/impl-review-loop/steps/context-protocol-and-guardrails.md").read_text()
    assert "human-history" in step


def _human_history_request(*, phase: str = "review-complete", target: int = 1908) -> dict:
    return {
        "identity": {
            "loop_kind": "issue-refinement-loop",
            "phase": phase,
            "source_issue_number": 1908,
            "target_kind": "issue",
            "target_number": target,
            "route_or_termination_reason": "completed",
            "reviewed_ref": "a" * 64,
        },
        "result": "実施内容の記録テストです",
        "evidence_refs": ["https://github.com/squne121/loop-protocol/issues/1908"],
        "recommended_action": "次の判断を実施してください",
        "recommended_reason": "production entrypoint の subprocess 検証のためです",
        "impact_if_unaddressed": "判断根拠が不足します",
    }


def test_production_entrypoint_is_a_real_subprocess_boundary_not_python_c(tmp_path: Path):
    """Issue #1908 fix_delta BLOCKER: publish_termination_report.py must expose
    exactly one production CLI entrypoint that turns a structured
    HUMAN_HISTORY_PUBLISH_REQUEST_V1 request into validation/rendering and an
    attempted call into the existing issue_comment.publish controlled lane --
    proven here via an actual subprocess of the production script itself
    (never `python -c`/ad-hoc bash), with --dry-run so the assertion does not
    depend on live network access or gh auth state.
    """
    request_file = tmp_path / "human_history_request.json"
    request_file.write_text(json.dumps(_human_history_request()), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(_PUBLISH_SCRIPT),
            "--repo", "squne121/loop-protocol",
            "--human-history-request-file", str(request_file),
            "--dry-run",
        ],
        cwd=str(_ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    stdout_lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    assert stdout_lines, proc.stderr
    receipt = json.loads(stdout_lines[-1])
    assert receipt == {"status_detail": "dry_run_ok", "exit_code": 0}


def test_production_entrypoint_rejects_malformed_request_before_any_dispatch(tmp_path: Path):
    """A malformed/incomplete request must fail closed (exit 2, usage error)
    without ever reaching publish_human_history()/the controlled lane."""
    incomplete_file = tmp_path / "incomplete.json"
    incomplete_file.write_text(json.dumps({"identity": _human_history_request()["identity"]}), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(_PUBLISH_SCRIPT),
            "--repo", "squne121/loop-protocol",
            "--human-history-request-file", str(incomplete_file),
            "--dry-run",
        ],
        cwd=str(_ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 2
    assert proc.stdout.strip() == ""

    not_json_file = tmp_path / "not-json.json"
    not_json_file.write_text("not json", encoding="utf-8")
    proc2 = subprocess.run(
        [
            sys.executable,
            str(_PUBLISH_SCRIPT),
            "--repo", "squne121/loop-protocol",
            "--human-history-request-file", str(not_json_file),
            "--dry-run",
        ],
        cwd=str(_ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert proc2.returncode == 2
    assert proc2.stdout.strip() == ""


def test_production_entrypoint_does_not_replace_legacy_plain_body_mode(tmp_path: Path):
    """Legacy plain --issue-number/--repo/--body-file invocation must still be
    required/validated exactly as before; --human-history-request-file is
    additive, not a replacement."""
    proc = subprocess.run(
        [sys.executable, str(_PUBLISH_SCRIPT), "--repo", "squne121/loop-protocol"],
        cwd=str(_ROOT),
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
        input="",
    )
    assert proc.returncode == 2
    assert "--issue-number is required" in proc.stderr
