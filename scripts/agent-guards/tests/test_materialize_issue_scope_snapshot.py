from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import controlled_git_change_exec as controlled  # noqa: E402
from controlled_skill_mutation_exec import _validate_issue_scope_snapshot_materialize_fields  # noqa: E402
from controlled_skill_mutation_policy import is_controlled_skill_mutation_exec_command  # noqa: E402
from materialize_issue_scope_snapshot import (  # noqa: E402
    COMMAND_ID,
    PROVENANCE_SCHEMA,
    expected_output_path,
    expected_provenance_path,
    materialize,
)


def _live_issue(issue_body: str, source_body: str) -> dict:
    return {
        "body": issue_body,
        "updatedAt": "2026-07-19T04:22:25Z",
        "comments": [
            {
                "url": "https://github.com/squne121/loop-protocol/issues/1629#issuecomment-5014250179",
                "body": source_body,
            }
        ],
    }


def test_materializer_binds_live_github_contract_and_worktree(tmp_path: Path, monkeypatch):
    issue_body = "## Allowed Paths\n\n- scripts/agent-guards/example.py\n\n## Stop Conditions\n"
    issue_sha = "sha256:" + hashlib.sha256(issue_body.encode()).hexdigest()
    source_body = f"CONTRACT_REVIEW_RESULT_V1:\n  status: go\n  body_sha256: \"{issue_sha}\"\n"
    monkeypatch.setattr(
        "materialize_issue_scope_snapshot._validate_worktree_binding", lambda root, path, branch: root
    )
    monkeypatch.setattr("materialize_issue_scope_snapshot._default_branch_sha", lambda root, ref: "a" * 40)
    monkeypatch.setattr(
        "materialize_issue_scope_snapshot._load_live_issue",
        lambda *args: _live_issue(issue_body, source_body),
    )

    result = materialize(
        issue_number=1629,
        repo="squne121/loop-protocol",
        contract_snapshot_url="https://github.com/squne121/loop-protocol/issues/1629#issuecomment-5014250179",
        base_ref="main",
        branch_name="worktree-issue-1629-test",
        worktree_path=str(tmp_path),
        output=f"artifacts/1629/issue-metadata/{COMMAND_ID}/issue_scope_snapshot.json",
        project_root=tmp_path,
    )

    snapshot_path = expected_output_path(tmp_path, 1629)
    provenance_path = expected_provenance_path(tmp_path, 1629)
    assert result["status"] == "ok"
    snapshot = json.loads(snapshot_path.read_text())
    provenance = json.loads(provenance_path.read_text())
    assert snapshot["issue_number"] == 1629
    assert snapshot["contract_source_id"] == "5014250179"
    assert provenance["schema"] == PROVENANCE_SCHEMA
    assert provenance["artifact_sha256"] == "sha256:" + hashlib.sha256(snapshot_path.read_bytes()).hexdigest()


def test_materializer_rejects_readback_drift_binding_and_unsafe_output(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("materialize_issue_scope_snapshot._validate_worktree_binding", lambda root, path, branch: root)
    monkeypatch.setattr("materialize_issue_scope_snapshot._default_branch_sha", lambda root, ref: "a" * 40)
    stale_body = "## Allowed Paths\n\n- scripts/agent-guards/example.py\n"
    monkeypatch.setattr(
        "materialize_issue_scope_snapshot._load_live_issue",
        lambda *args: _live_issue(
            stale_body,
            "CONTRACT_REVIEW_RESULT_V1:\n  status: go\n  body_sha256: \"sha256:stale\"\n",
        ),
    )
    import pytest

    with pytest.raises(ValueError, match="artifact_path_binding_mismatch"):
        materialize(
            issue_number=1629,
            repo="squne121/loop-protocol",
            contract_snapshot_url="https://github.com/squne121/loop-protocol/issues/1629#issuecomment-5014250179",
            base_ref="main",
            branch_name="topic",
            worktree_path=str(tmp_path),
            output="tmp/handwritten.json",
            project_root=tmp_path,
        )
    with pytest.raises(ValueError, match="contract_source_drift"):
        materialize(
            issue_number=1629,
            repo="squne121/loop-protocol",
            contract_snapshot_url="https://github.com/squne121/loop-protocol/issues/1629#issuecomment-5014250179",
            base_ref="main",
            branch_name="topic",
            worktree_path=str(tmp_path),
            output=f"artifacts/1629/issue-metadata/{COMMAND_ID}/issue_scope_snapshot.json",
            project_root=tmp_path,
        )
    assert not expected_output_path(tmp_path, 1629).exists()


def test_controlled_executor_rejects_handwritten_snapshot_without_materializer_provenance(tmp_path: Path, monkeypatch):
    snapshot_path = expected_output_path(tmp_path, 1629)
    snapshot_path.parent.mkdir(parents=True)
    snapshot = {
        "issue_number": 1629,
        "repository_full_name": "squne121/loop-protocol",
        "worktree_realpath": str(tmp_path),
        "branch_ref": "refs/heads/topic",
        "base_ref": "main",
        "base_sha": "a" * 40,
    }
    snapshot_path.write_text(json.dumps(snapshot))
    monkeypatch.setattr(controlled, "_git_toplevel", lambda cwd: str(tmp_path))
    assert controlled._validate_materialized_snapshot_provenance(snapshot_path, snapshot, str(tmp_path)) == (
        "materializer_provenance_input_file_missing"
    )
    provenance = {
        "schema": PROVENANCE_SCHEMA,
        "producer": "scripts/agent-guards/materialize_issue_scope_snapshot.py",
        "command_id": COMMAND_ID,
        "repository_full_name": "squne121/loop-protocol",
        "issue_number": 1629,
        "artifact_path": str(snapshot_path.relative_to(tmp_path)),
        "artifact_sha256": "sha256:" + hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        "worktree_realpath": str(tmp_path),
        "branch_ref": "refs/heads/topic",
        "base_ref": "main",
        "base_sha": "a" * 40,
    }
    expected_provenance_path(tmp_path, 1629).write_text(json.dumps(provenance))
    assert controlled._validate_materialized_snapshot_provenance(snapshot_path, snapshot, str(tmp_path)) is None


def test_controlled_materializer_command_rejects_argv_and_binding_mismatch(tmp_path: Path):
    project_root = str(tmp_path)
    command = (
        "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py "
        "--command-id issue_scope_snapshot.materialize --issue-number 1629 "
        "--repo squne121/loop-protocol --input-file "
        "artifacts/1629/issue-metadata/issue_scope_snapshot.materialize/request.json --json"
    )
    assert is_controlled_skill_mutation_exec_command(command, project_root)
    assert not is_controlled_skill_mutation_exec_command(command + " --unknown", project_root)
    assert _validate_issue_scope_snapshot_materialize_fields(
        {
            "repo": "squne121/loop-protocol",
            "contract_snapshot_url": "https://github.com/squne121/loop-protocol/issues/1629#issuecomment-5014250179",
            "base_ref": "main",
            "branch_name": "topic",
            "worktree_path": str(tmp_path),
            "output_path": "wrong.json",
        },
        "squne121/loop-protocol",
    ) == ""
