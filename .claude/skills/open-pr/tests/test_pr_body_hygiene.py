from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
GENERATE_SCRIPT = SCRIPT_DIR / "generate_pr_body.py"
HYGIENE_SCRIPT = SCRIPT_DIR / "validate_pr_body_hygiene.py"
VALIDATE_PR_BODY_SCRIPT = SCRIPT_DIR / "validate_pr_body.py"
VALIDATE_JAPANESE_SCRIPT = Path(__file__).resolve().parents[2] / (
    "create-issue"
) / "scripts" / "validate_japanese_content.py"
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "pr_body.ja.md"


def _run_python_script(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _generate_body(*, issue: int = 1040, changed_files: list[str] | None = None, draft: bool = True) -> str:
    changed_files = changed_files or [".claude/hooks/foo.py"]
    result = _run_python_script(
        GENERATE_SCRIPT,
        "--issue",
        str(issue),
        "--changed-files",
        *changed_files,
        "--draft",
        "true" if draft else "false",
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _run_validate_pr_body(body: str, changed_files: list[str], issue: int = 1040) -> subprocess.CompletedProcess[str]:
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


def _run_validate_japanese_content(body: str) -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False) as body_file:
        body_file.write(body)
        body_path = Path(body_file.name)
    try:
        return _run_python_script(
            VALIDATE_JAPANESE_SCRIPT,
            "--file",
            str(body_path),
            "--threshold",
            "0.1",
            "--verbose",
        )
    finally:
        body_path.unlink(missing_ok=True)


def _run_hygiene(
    body: str,
    changed_files: list[str],
    *,
    issue: int = 1040,
    draft: bool
) -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False) as body_file:
        body_file.write(body)
        body_path = Path(body_file.name)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt", delete=False) as paths_file:
        paths_file.write("\n".join(changed_files))
        paths_path = Path(paths_file.name)
    try:
        return _run_python_script(
            HYGIENE_SCRIPT,
            "--body-file",
            str(body_path),
            "--changed-paths-file",
            str(paths_path),
            "--linked-issue",
            str(issue),
            "--draft",
            "true" if draft else "false",
        )
    finally:
        body_path.unlink(missing_ok=True)
        paths_path.unlink(missing_ok=True)


def test_generate_pr_body_emits_required_headings():
    body = _generate_body()
    headings = [line for line in body.splitlines() if line.startswith("## ")]
    assert headings == [
        "## Summary",
        "## Checks",
        "## Schema Change Applicability",
        "## Schema Consumer Inventory",
        "## Safety Claim Matrix",
        "## Notes",
    ]


def test_generated_body_passes_validate_pr_body():
    changed_files = [".claude/hooks/foo.py"]
    body = _generate_body(changed_files=changed_files)
    result = _run_validate_pr_body(body, changed_files)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "pass"


def test_generated_body_passes_japanese_content():
    body = _generate_body()
    result = _run_validate_japanese_content(body)
    assert result.returncode == 0, result.stderr
    assert "failed_blocks: 0" in result.stderr


def test_agent_surface_change_requires_safety_matrix():
    body = _generate_body(changed_files=[".claude/hooks/foo.py"])
    safety_section = body.split("## Safety Claim Matrix", 1)[1].split("## Notes", 1)[0]
    assert "N/A\nreason:" not in safety_section
    assert "| `.claude/**` 変更時でも Safety Claim Matrix の空欄や placeholder を残さない |" in safety_section


def test_non_agent_surface_body_emits_not_schema_change():
    body = _generate_body(changed_files=["docs/dev/workflow.md"])
    schema_section = body.split("## Schema Change Applicability", 1)[1].split("## Schema Consumer Inventory", 1)[0]
    assert "- decision: not_schema_change" in schema_section
    assert "- reason: " in schema_section
    assert "入力契約は変更しない" in schema_section


def test_standalone_closes_block_fails_hygiene():
    changed_files = [".claude/hooks/foo.py"]
    body = _generate_body(changed_files=changed_files)
    invalid_body = body.replace(
        "- 本 PR は Issue #1040 を close します（Closes #1040）。",
        "Closes #1040",
    )
    result = _run_hygiene(invalid_body, changed_files, draft=False)
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "fail"
    assert any(error["rule_id"] == "HY001" for error in payload["errors"])


def test_standalone_multiple_closing_keywords_fail_hygiene():
    changed_files = [".claude/hooks/foo.py"]
    body = _generate_body(changed_files=changed_files)
    invalid_body = body.replace(
        "- 本 PR は Issue #1040 を close します（Closes #1040）。",
        "Resolves #10, resolves owner/repo#123",
    )
    result = _run_hygiene(invalid_body, changed_files, draft=False)
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "fail"
    assert any("resolves owner/repo#123" in " ".join(error["minimal_context"]) for error in payload["errors"])


def test_agent_surface_change_requires_concrete_safety_matrix_data_row():
    changed_files = [".claude/hooks/foo.py"]
    body = _generate_body(changed_files=changed_files)
    invalid_body = body.replace(
        (
           "| `.claude/**` 変更時でも Safety Claim Matrix の"
           "空欄や placeholder を残さない | yes | N/A | `generate_pr_body.py --issue"
           " 1040 --changed-files .claude/hooks/foo.py --draft true` | N/A |"
       ),
        "|  | yes | pending external review |  | N/A |",
    )
    result = _run_hygiene(invalid_body, changed_files, draft=False)
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "fail"
    assert any(error["rule_id"] == "HY003" for error in payload["errors"])


def test_draft_true_requires_mark_ready_for_review():
    changed_files = [".claude/hooks/foo.py"]
    body = _generate_body(changed_files=changed_files, draft=True)
    result = _run_hygiene(body, changed_files, draft=True)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "action_required"
    assert payload["merge_ready"] is False
    assert payload["required_auto_actions"][0]["kind"] == "mark_ready_for_review"


def test_draft_true_require_merge_ready_exits_nonzero():
    changed_files = [".claude/hooks/foo.py"]
    body = _generate_body(changed_files=changed_files, draft=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False) as body_file:
        body_file.write(body)
        body_path = Path(body_file.name)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt", delete=False) as paths_file:
        paths_file.write("\n".join(changed_files))
        paths_path = Path(paths_file.name)
    try:
        result = _run_python_script(
            HYGIENE_SCRIPT,
            "--body-file",
            str(body_path),
            "--changed-paths-file",
            str(paths_path),
            "--linked-issue",
            "1040",
            "--draft",
            "true",
            "--require-merge-ready",
        )
    finally:
        body_path.unlink(missing_ok=True)
        paths_path.unlink(missing_ok=True)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "action_required"
    assert payload["merge_ready"] is False


def test_draft_false_and_valid_body_is_merge_ready():
    changed_files = [".claude/hooks/foo.py"]
    body = _generate_body(changed_files=changed_files, draft=False)
    result = _run_hygiene(body, changed_files, draft=False)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "pass"
    assert payload["merge_ready"] is True
    assert payload["required_auto_actions"] == []


def test_template_uses_exact_headings():
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "## Summary" in template
    assert "## Checks" in template
    assert "## Schema Change Applicability" in template
    assert "## Schema Consumer Inventory" in template
    assert "## Safety Claim Matrix" in template
    assert "## Notes" in template
    assert "## 概要" not in template
    assert "## 確認事項" not in template


def test_generated_body_uses_actual_draft_argument_and_no_stale_draft_note():
    body = _generate_body(changed_files=[".claude/hooks/foo.py"], draft=False)
    assert "--draft false" in body
    assert "Draft PR として作成し" not in body
