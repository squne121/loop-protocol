"""Hermetic tests for OVERLAP_GATE_BYPASS_V1 validation in validate_pr_body.py (Issue #1776)."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
GENERATE_SCRIPT = SCRIPT_DIR / "generate_pr_body.py"
VALIDATE_PR_BODY_SCRIPT = SCRIPT_DIR / "validate_pr_body.py"

BYPASS_TRIGGER_TEXT = (
    "\n\n## Overlap Preflight 自動判定結果の補足説明\n\n"
    "overlap preflight が兄弟 Issue との `C3 parent_child_collision` を検出し "
    "`route: human_review_required` を返したが、本 PR は overlap gate を経由せず "
    "`gh pr create` を直接実行して起票する（バイパス判断）。\n"
)

COMPLETE_BYPASS_RECORD = (
    "\n\n```yaml\n"
    "OVERLAP_GATE_BYPASS_V1:\n"
    "  bypass_reason: \"disjoint code paths within the same Allowed Path file\"\n"
    "  approver: \"squne121\"\n"
    "  independent_verification_basis: \"git diff --name-only cross-check against sibling issue scope\"\n"
    "  bypassed_gate_class: C3\n"
    "  precedent_refs:\n"
    "    - \"#1756\"\n"
    "    - \"#1763\"\n"
    "```\n"
)


def _run_python_script(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _generate_base_body(*, issue: int = 1776, changed_files: list[str] | None = None) -> str:
    changed_files = changed_files or [".claude/skills/open-pr/scripts/validate_pr_body.py"]
    result = _run_python_script(
        GENERATE_SCRIPT,
        "--issue",
        str(issue),
        "--changed-files",
        *changed_files,
        "--draft",
        "true",
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _run_validate_pr_body(body: str, changed_files: list[str], issue: int = 1776) -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False) as body_file:
        body_file.write(body)
        body_path = Path(body_file.name)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt", delete=False) as paths_file:
        paths_file.write("\n".join(changed_files))
        paths_path = Path(paths_file.name)
    try:
        return _run_python_script(
            VALIDATE_PR_BODY_SCRIPT,
            "--body-file",
            str(body_path),
            "--changed-paths-file",
            str(paths_path),
            "--linked-issue",
            str(issue),
        )
    finally:
        body_path.unlink(missing_ok=True)
        paths_path.unlink(missing_ok=True)


def test_no_bypass_trigger_does_not_require_record():
    body = _generate_base_body()
    changed_files = [".claude/skills/open-pr/scripts/validate_pr_body.py"]
    result = _run_validate_pr_body(body, changed_files)
    assert "LP059" not in result.stdout
    assert "E_OVERLAP_GATE_BYPASS" not in result.stdout


def test_missing_bypass_record_fail_closed():
    body = _generate_base_body() + BYPASS_TRIGGER_TEXT
    changed_files = [".claude/skills/open-pr/scripts/validate_pr_body.py"]
    result = _run_validate_pr_body(body, changed_files)
    assert "LP059" in result.stdout
    assert '"status": "fail"' in result.stdout


def test_verified_successor_claim_does_not_allow_unsafe_publication_without_record():
    """#1797 AC6: producer が safe evidence と主張しても、open-pr consumer
    は required record を欠く bypass publication を受け入れない。
    """
    body = _generate_base_body() + (
        "\n\n## Overlap Preflight 自動判定結果の補足説明\n\n"
        "C2a verified native successor predicate により `route: proceed_with_collision_evidence` "
        "と判断したため、overlap gate を経由せず `gh pr create` を直接実行する。\n"
    )
    changed_files = [".claude/skills/open-pr/scripts/validate_pr_body.py"]
    result = _run_validate_pr_body(body, changed_files)
    assert "LP059" in result.stdout
    assert '"status": "fail"' in result.stdout


def test_complete_bypass_record_pass():
    body = _generate_base_body() + BYPASS_TRIGGER_TEXT + COMPLETE_BYPASS_RECORD
    changed_files = [".claude/skills/open-pr/scripts/validate_pr_body.py"]
    result = _run_validate_pr_body(body, changed_files)
    assert "LP059" not in result.stdout
    assert "E_OVERLAP_GATE_BYPASS" not in result.stdout


def test_bypass_record_missing_required_key_fail_closed():
    incomplete_record = (
        "\n\n```yaml\n"
        "OVERLAP_GATE_BYPASS_V1:\n"
        "  bypass_reason: \"disjoint code paths\"\n"
        "  bypassed_gate_class: C3\n"
        "```\n"
    )
    body = _generate_base_body() + BYPASS_TRIGGER_TEXT + incomplete_record
    changed_files = [".claude/skills/open-pr/scripts/validate_pr_body.py"]
    result = _run_validate_pr_body(body, changed_files)
    assert "E_OVERLAP_GATE_BYPASS_SCHEMA_INVALID" in result.stdout


def test_bypass_record_invalid_gate_class_fail_closed():
    bad_record = (
        "\n\n```yaml\n"
        "OVERLAP_GATE_BYPASS_V1:\n"
        "  bypass_reason: \"disjoint code paths\"\n"
        "  approver: \"squne121\"\n"
        "  independent_verification_basis: \"manual diff review\"\n"
        "  bypassed_gate_class: C99\n"
        "```\n"
    )
    body = _generate_base_body() + BYPASS_TRIGGER_TEXT + bad_record
    changed_files = [".claude/skills/open-pr/scripts/validate_pr_body.py"]
    result = _run_validate_pr_body(body, changed_files)
    assert "E_OVERLAP_GATE_BYPASS_SCHEMA_INVALID" in result.stdout
