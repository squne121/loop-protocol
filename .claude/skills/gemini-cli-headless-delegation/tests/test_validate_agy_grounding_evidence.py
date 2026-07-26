"""Hermetic tests for validate_agy_grounding_evidence.py (Issue #1776, AC1-AC3)."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "validate_agy_grounding_evidence.py"

UNSUPPORTED_CLAIM_TEXT = (
    "## Notes for Reviewer\n\n"
    "grounded_research の fan-out 失敗は AGY provider の認証タイムアウトが原因です。"
    "この問題は今回の変更により解消しました。\n"
)

EVIDENCE_BACKED_TEXT = (
    "## Notes for Reviewer\n\n"
    "grounded_research の fan-out 失敗は AGY provider の認証タイムアウトが原因です。"
    "hook ログ `artifacts/agy-fanout/hook_trace.log` の証跡により、"
    "timeout 発生から 30 秒後にリトライが成功していることが確認できます。"
    "この問題は今回の変更により解消しました（sha256:abcdef0123456789 で固定されたコミット時点）。\n"
)

NO_CLAIM_TEXT = (
    "## Summary\n\n"
    "validate_agy_grounding_evidence.py を新規追加し、テストを追加した。\n"
)


def _run_validator(*, diff_text: str | None = None, pr_body_text: str | None = None) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, str(SCRIPT)]
    tmp_paths = []
    try:
        if diff_text is not None:
            f = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".diff", delete=False)
            f.write(diff_text)
            f.close()
            tmp_paths.append(f.name)
            args += ["--diff-file", f.name]
        if pr_body_text is not None:
            f = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False)
            f.write(pr_body_text)
            f.close()
            tmp_paths.append(f.name)
            args += ["--pr-body-file", f.name]
        return subprocess.run(args, capture_output=True, text=True, check=False)
    finally:
        for p in tmp_paths:
            Path(p).unlink(missing_ok=True)


def test_script_exists():
    """AC1: script exists."""
    assert SCRIPT.is_file()


def test_status_field_present_in_json_output():
    """AC1: emits JSON with a status field."""
    result = _run_validator(pr_body_text=NO_CLAIM_TEXT)
    payload = json.loads(result.stdout)
    assert payload["schema"] == "AGY_GROUNDING_EVIDENCE_VERDICT_V1"
    assert "status" in payload


def test_unsupported_claim_fail_closed():
    """AC2: an unsupported causal claim -> status: fail_closed."""
    result = _run_validator(pr_body_text=UNSUPPORTED_CLAIM_TEXT)
    payload = json.loads(result.stdout)
    assert payload["status"] == "fail_closed"
    assert len(payload["unsupported_claims"]) >= 1
    assert result.returncode == 1


def test_evidence_backed_ok():
    """AC3: evidence-backed claims only -> status: ok."""
    result = _run_validator(pr_body_text=EVIDENCE_BACKED_TEXT)
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert len(payload["unsupported_claims"]) == 0
    assert len(payload["evidence_bindings"]) >= 1
    assert result.returncode == 0


def test_no_claims_is_ok():
    """No causal claims at all -> status: ok, nothing flagged."""
    result = _run_validator(pr_body_text=NO_CLAIM_TEXT)
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["unsupported_claims"] == []
    assert payload["evidence_bindings"] == []


def test_diff_file_input_also_scanned():
    """A causal claim embedded in the diff text (not just PR body) is scanned too."""
    result = _run_validator(diff_text=UNSUPPORTED_CLAIM_TEXT)
    payload = json.loads(result.stdout)
    assert payload["status"] == "fail_closed"


def test_requires_at_least_one_input():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
