"""Shared-classifier guard regression for Issue #1660."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


GUARDS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(GUARDS))

from controlled_skill_mutation_policy import is_controlled_skill_mutation_exec_command  # noqa: E402
from local_main_branch_guard import REASON_GH_MUTATION, evaluate  # noqa: E402


def test_shared_classifier_allows_executor_and_denies_raw_title_edit(tmp_path):
    executor = tmp_path / "scripts" / "agent-guards" / "controlled_skill_mutation_exec.py"
    executor.parent.mkdir(parents=True)
    executor.write_text("# canonical test stub\n", encoding="utf-8")
    command = (
        "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py "
        "--command-id issue_content.update --issue-number 1660 "
        "--input-file artifacts/1660/issue-metadata/issue_content.update/input.json "
        "--repo squne121/loop-protocol --dry-run"
    )
    assert is_controlled_skill_mutation_exec_command(command, str(tmp_path))
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    (tmp_path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], check=True, capture_output=True)
    old_root = os.environ.get("CLAUDE_PROJECT_DIR")
    try:
        os.environ["CLAUDE_PROJECT_DIR"] = str(tmp_path)
        result = evaluate("gh issue edit 1660 --repo squne121/loop-protocol --title unsafe", str(tmp_path))
    finally:
        if old_root is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old_root
    assert result["status"] == "block"
    assert result["reason_code"] == REASON_GH_MUTATION
