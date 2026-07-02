#!/usr/bin/env python3
"""
Tests for issue metadata mutation command ids (Issue #1284):
issue_body.update / issue_comment.publish / contract_snapshot.publish.

AC1  test_ac1_contract_snapshot_publish_from_root
AC2  test_ac2_input_file_outside_artifacts_subtree_fails_closed
AC3  test_ac3_binding_violations_rejected_before_mutation
AC4  test_ac4_readback_mismatch_not_success
AC6  test_ac6_tracked_diff_fails_executor
AC8  test_ac8_contract_snapshot_publisher_authority_fixed
AC9  test_ac9_issue_body_update_stale_write_prevented
AC10 test_ac10_per_command_input_schema_enforced
AC11 test_ac11_input_namespace_unified_to_artifacts
AC13 test_ac13_argv_only_shell_false_enforced
AC14 test_ac14_readback_idempotency_no_false_success
AC15 test_ac15_env_binding_optional_but_must_match
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import controlled_skill_mutation_exec as _exec
from controlled_skill_mutation_policy import (
    TRUSTED_REPO,
    COMMAND_ID_ISSUE_BODY_UPDATE,
    COMMAND_ID_ISSUE_COMMENT_PUBLISH,
    COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
)


def _sha(text: str) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture()
def tmp_project(tmp_path):
    executor_dir = tmp_path / "scripts" / "agent-guards"
    executor_dir.mkdir(parents=True)
    pub_dir = tmp_path / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
    pub_dir.mkdir(parents=True)
    (pub_dir / "publish_termination_report.py").write_text("# stub\n")
    (pub_dir / "render_termination_report.py").write_text("# stub\n")
    create_issue_dir = tmp_path / ".claude" / "skills" / "create-issue" / "scripts"
    create_issue_dir.mkdir(parents=True)
    (create_issue_dir / "prose_boundary_policy.py").write_text("# stub\n")
    irl_dir = tmp_path / ".claude" / "skills" / "impl-review-loop" / "scripts"
    irl_dir.mkdir(parents=True)
    (irl_dir / "ensure_contract_snapshot.py").write_text("# stub\n")

    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         f"https://github.com/{TRUSTED_REPO}.git"],
        capture_output=True,
    )
    return tmp_path


def _write_input(tmp_project, issue_number, command_id, name, data):
    d = tmp_project / "artifacts" / str(issue_number) / "issue-metadata" / command_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(json.dumps(data))
    return f"artifacts/{issue_number}/issue-metadata/{command_id}/{name}"


# =============================================================================
# AC10 / AC11: namespace + schema
# =============================================================================

class TestNamespaceAndSchema:
    def test_ac11_input_namespace_unified_to_artifacts(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_input(
            tmp_project, 1284, COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "in.json",
            {"schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1", "issue_number": 1284,
             "comment_body": "hi <!-- m --> ", "marker": "<!-- m -->"},
        )
        assert rel.startswith("artifacts/1284/issue-metadata/issue_comment.publish/")
        canonical, err = _exec._validate_and_resolve_input_file(
            rel, 1284, tmp_project, command_id=COMMAND_ID_ISSUE_COMMENT_PUBLISH
        )
        assert err == ""
        assert canonical is not None

    def test_ac2_input_file_outside_artifacts_subtree_fails_closed(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        # File exists but outside the command-id-specific subtree.
        other_dir = tmp_project / "artifacts" / "1284"
        other_dir.mkdir(parents=True, exist_ok=True)
        (other_dir / "outside.json").write_text("{}")
        canonical, err = _exec._validate_and_resolve_input_file(
            "artifacts/1284/outside.json", 1284, tmp_project,
            command_id=COMMAND_ID_ISSUE_COMMENT_PUBLISH,
        )
        assert canonical is None
        assert "outside_issue_subtree" in err

    def test_ac10_per_command_input_schema_enforced(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_input(
            tmp_project, 1284, COMMAND_ID_ISSUE_BODY_UPDATE, "in.json",
            {"schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1", "issue_number": 1284},
        )
        canonical, _ = _exec._validate_and_resolve_input_file(
            rel, 1284, tmp_project, command_id=COMMAND_ID_ISSUE_BODY_UPDATE
        )
        data, err = _exec._load_and_validate_input_json(canonical, 1284, COMMAND_ID_ISSUE_BODY_UPDATE)
        assert data is None
        assert "input_schema_mismatch" in err


# =============================================================================
# AC3: binding violations
# =============================================================================

class TestBindingViolations:
    def test_ac3_binding_violations_rejected_before_mutation(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main([
            "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "--issue-number", "1284",
            "--input-file", "/etc/passwd",
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2

    def test_wrong_repo_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main([
            "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "--issue-number", "1284",
            "--input-file", "artifacts/1284/issue-metadata/issue_comment.publish/x.json",
            "--repo", "evil/repo",
        ])
        assert rc == 2

    def test_dotdot_traversal_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main([
            "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "--issue-number", "1284",
            "--input-file", "artifacts/1284/issue-metadata/issue_comment.publish/../../../x.json",
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2

    def test_unknown_command_id_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main([
            "--command-id", "unknown.command",
            "--issue-number", "1284",
            "--input-file", "artifacts/1284/issue-metadata/unknown.command/x.json",
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2


# =============================================================================
# AC15: env binding optional-but-matching for new command ids
# =============================================================================

class TestEnvBinding:
    def test_ac15_env_missing_allowed_for_new_command(self):
        err = _exec._check_issue_env_binding(COMMAND_ID_ISSUE_COMMENT_PUBLISH, 1284)
        assert err == ""

    def test_ac15_env_present_and_matching_allowed(self, monkeypatch):
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        err = _exec._check_issue_env_binding(COMMAND_ID_ISSUE_COMMENT_PUBLISH, 1284)
        assert err == ""

    def test_ac15_env_present_and_mismatching_denied(self, monkeypatch):
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "9999")
        err = _exec._check_issue_env_binding(COMMAND_ID_ISSUE_COMMENT_PUBLISH, 1284)
        assert "issue_number_mismatch" in err

    def test_legacy_command_still_mandatory(self, monkeypatch):
        monkeypatch.delenv("LOOP_ISSUE_NUMBER", raising=False)
        err = _exec._check_issue_env_binding("termination_report.publish", 1284)
        assert "loop_issue_number_env_missing" in err


# =============================================================================
# AC9: issue_body.update stale-write prevention
# =============================================================================

class TestIssueBodyUpdate:
    def test_ac9_issue_body_update_stale_write_prevented(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        new_body = "new body text"
        rel = _write_input(
            tmp_project, 1284, COMMAND_ID_ISSUE_BODY_UPDATE, "in.json",
            {
                "schema": "ISSUE_BODY_UPDATE_INPUT_V1",
                "issue_number": 1284,
                "previous_body_sha256": _sha("old body"),
                "previous_updated_at": "2026-01-01T00:00:00Z",
                "new_body": new_body,
                "new_body_sha256": _sha(new_body),
            },
        )
        # Live readback disagrees with previous_body_sha256 → stale, must not mutate.
        with patch.object(_exec, "_fetch_issue_body_and_updated_at",
                           return_value=("CURRENT DIFFERENT BODY", "2026-01-01T00:00:00Z", "")):
            with patch.object(_exec, "_patch_issue_body") as mock_patch:
                rc = _exec.main([
                    "--command-id", COMMAND_ID_ISSUE_BODY_UPDATE,
                    "--issue-number", "1284",
                    "--input-file", rel,
                    "--repo", TRUSTED_REPO,
                ])
        assert rc == 1
        mock_patch.assert_not_called()

    def test_ac9_partial_match_not_success(self, tmp_project, monkeypatch):
        """updatedAt matches but body hash doesn't (or vice versa) → still not success."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        new_body = "new body text"
        rel = _write_input(
            tmp_project, 1284, COMMAND_ID_ISSUE_BODY_UPDATE, "in.json",
            {
                "schema": "ISSUE_BODY_UPDATE_INPUT_V1",
                "issue_number": 1284,
                "previous_body_sha256": _sha("old body"),
                "previous_updated_at": "2026-01-01T00:00:00Z",
                "new_body": new_body,
                "new_body_sha256": _sha(new_body),
            },
        )
        with patch.object(_exec, "_fetch_issue_body_and_updated_at",
                           return_value=("old body", "2026-01-02T99:99:99Z", "")):
            with patch.object(_exec, "_patch_issue_body") as mock_patch:
                rc = _exec.main([
                    "--command-id", COMMAND_ID_ISSUE_BODY_UPDATE,
                    "--issue-number", "1284",
                    "--input-file", rel,
                    "--repo", TRUSTED_REPO,
                ])
        assert rc == 1
        mock_patch.assert_not_called()

    def test_issue_body_update_success_path(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        new_body = "new body text"
        rel = _write_input(
            tmp_project, 1284, COMMAND_ID_ISSUE_BODY_UPDATE, "in.json",
            {
                "schema": "ISSUE_BODY_UPDATE_INPUT_V1",
                "issue_number": 1284,
                "previous_body_sha256": _sha("old body"),
                "previous_updated_at": "2026-01-01T00:00:00Z",
                "new_body": new_body,
                "new_body_sha256": _sha(new_body),
            },
        )
        readbacks = iter([
            ("old body", "2026-01-01T00:00:00Z", ""),
            (new_body, "2026-01-01T00:00:01Z", ""),
        ])
        with patch.object(_exec, "_fetch_issue_body_and_updated_at",
                           side_effect=lambda *a, **k: next(readbacks)):
            with patch.object(_exec, "_patch_issue_body", return_value=""):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    rc = _exec.main([
                        "--command-id", COMMAND_ID_ISSUE_BODY_UPDATE,
                        "--issue-number", "1284",
                        "--input-file", rel,
                        "--repo", TRUSTED_REPO,
                        "--json",
                    ])
        assert rc == 0


# =============================================================================
# AC4/AC14: readback mismatch not success (issue_comment.publish)
# =============================================================================

class TestIssueCommentPublish:
    def test_ac4_readback_mismatch_not_success(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project, 1284, COMMAND_ID_ISSUE_COMMENT_PUBLISH, "in.json",
            {"schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1", "issue_number": 1284,
             "comment_body": "hi <!-- marker-x -->", "marker": "<!-- marker-x -->"},
        )
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(_exec, "_post_gh_comment", return_value=("url", "1", "")):
                    with patch.object(_exec, "_readback_by_marker_literal",
                                       return_value={"error": "marker_not_found"}):
                        rc = _exec.main([
                            "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                            "--issue-number", "1284",
                            "--input-file", rel,
                            "--repo", TRUSTED_REPO,
                        ])
        assert rc == 1

    def test_ac14_duplicate_marker_not_success(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project, 1284, COMMAND_ID_ISSUE_COMMENT_PUBLISH, "in.json",
            {"schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1", "issue_number": 1284,
             "comment_body": "hi <!-- marker-x -->", "marker": "<!-- marker-x -->"},
        )
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(_exec, "_post_gh_comment", return_value=("url", "1", "")):
                    with patch.object(_exec, "_readback_by_marker_literal",
                                       return_value={"error": "marker_found_2_times"}):
                        rc = _exec.main([
                            "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                            "--issue-number", "1284",
                            "--input-file", rel,
                            "--repo", TRUSTED_REPO,
                        ])
        assert rc == 1

    def test_issue_comment_publish_success_path(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project, 1284, COMMAND_ID_ISSUE_COMMENT_PUBLISH, "in.json",
            {"schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1", "issue_number": 1284,
             "comment_body": "hi <!-- marker-x -->", "marker": "<!-- marker-x -->"},
        )
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(_exec, "_post_gh_comment", return_value=("https://ex", "1", "")):
                    with patch.object(_exec, "_readback_by_marker_literal",
                                       return_value={"comment_id": "1", "comment_url": "https://ex",
                                                      "body_sha256": "abc"}):
                        with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                            rc = _exec.main([
                                "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                                "--issue-number", "1284",
                                "--input-file", rel,
                                "--repo", TRUSTED_REPO,
                                "--json",
                            ])
        assert rc == 0


# =============================================================================
# AC6: tracked diff fails executor
# =============================================================================

class TestTrackedDiff:
    def test_ac6_tracked_diff_fails_executor(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project, 1284, COMMAND_ID_ISSUE_COMMENT_PUBLISH, "in.json",
            {"schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1", "issue_number": 1284,
             "comment_body": "hi <!-- marker-x -->", "marker": "<!-- marker-x -->"},
        )
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(_exec, "_post_gh_comment", return_value=("https://ex", "1", "")):
                    with patch.object(_exec, "_readback_by_marker_literal",
                                       return_value={"comment_id": "1", "comment_url": "https://ex",
                                                      "body_sha256": "abc"}):
                        with patch.object(_exec, "_check_no_tracked_changes",
                                           return_value=["M:src/tracked_file.py"]):
                            rc = _exec.main([
                                "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                                "--issue-number", "1284",
                                "--input-file", rel,
                                "--repo", TRUSTED_REPO,
                            ])
        assert rc == 1


# =============================================================================
# AC1/AC8: contract_snapshot.publish
# =============================================================================

class TestContractSnapshotPublish:
    def test_ac1_contract_snapshot_publish_from_root(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        # No LOOP_ISSUE_NUMBER set — simulates root/default-branch execution
        # with no issue-specific worktree/session env (AC1/AC15).
        monkeypatch.delenv("LOOP_ISSUE_NUMBER", raising=False)
        rel = _write_input(
            tmp_project, 1284, COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH, "in.json",
            {"schema": "CONTRACT_SNAPSHOT_PUBLISH_INPUT_V1", "issue_number": 1284},
        )
        pub_result = {
            "status": "ok",
            "contract_snapshot_url": "https://github.com/o/r/issues/1284#issuecomment-1",
            "post_status": "posted",
        }
        fake_proc = type("P", (), {"stdout": json.dumps(pub_result), "stderr": "", "returncode": 0})()
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch("subprocess.run", return_value=fake_proc):
                    with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                        rc = _exec.main([
                            "--command-id", COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
                            "--issue-number", "1284",
                            "--input-file", rel,
                            "--repo", TRUSTED_REPO,
                            "--json",
                        ])
        assert rc == 0

    def test_ac8_contract_snapshot_publisher_authority_fixed(self):
        assert _exec._ENSURE_CONTRACT_SNAPSHOT_REL == (
            ".claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py"
        )

    def test_ac8_publisher_invoked_argv_list_shell_false(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project, 1284, COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH, "in.json",
            {"schema": "CONTRACT_SNAPSHOT_PUBLISH_INPUT_V1", "issue_number": 1284},
        )
        pub_result = {"status": "ok", "contract_snapshot_url": "https://ex", "post_status": "posted"}
        fake_proc = type("P", (), {"stdout": json.dumps(pub_result), "stderr": "", "returncode": 0})()
        captured = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return fake_proc

        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch("subprocess.run", side_effect=_fake_run):
                    with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                        rc = _exec.main([
                            "--command-id", COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
                            "--issue-number", "1284",
                            "--input-file", rel,
                            "--repo", TRUSTED_REPO,
                        ])
        assert rc == 0
        assert isinstance(captured["cmd"], list)
        assert captured["kwargs"].get("shell") is False
        assert str(_exec.PROJECT_ROOT / _exec._ENSURE_CONTRACT_SNAPSHOT_REL) in captured["cmd"]

    def test_contract_snapshot_publish_failure_not_success(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1284")
        rel = _write_input(
            tmp_project, 1284, COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH, "in.json",
            {"schema": "CONTRACT_SNAPSHOT_PUBLISH_INPUT_V1", "issue_number": 1284},
        )
        pub_result = {"status": "human_judgment", "contract_snapshot_url": None}
        fake_proc = type("P", (), {"stdout": json.dumps(pub_result), "stderr": "", "returncode": 0})()
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch("subprocess.run", return_value=fake_proc):
                    rc = _exec.main([
                        "--command-id", COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
                        "--issue-number", "1284",
                        "--input-file", rel,
                        "--repo", TRUSTED_REPO,
                    ])
        assert rc == 1


# =============================================================================
# AC13: argv-only / shell=False enforced at policy layer
# =============================================================================

class TestArgvOnly:
    def test_ac13_argv_only_shell_false_enforced(self, tmp_path):
        from controlled_skill_mutation_policy import is_controlled_skill_mutation_exec_command

        executor_dir = tmp_path / "scripts" / "agent-guards"
        executor_dir.mkdir(parents=True)
        (executor_dir / "controlled_skill_mutation_exec.py").write_text("# stub\n")

        good = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py "
            "--command-id issue_body.update --issue-number 1284 "
            "--input-file artifacts/1284/issue-metadata/issue_body.update/in.json "
            "--repo squne121/loop-protocol"
        )
        assert is_controlled_skill_mutation_exec_command(good, str(tmp_path)) is True

        bash_wrapped = f"bash -c '{good}'"
        assert is_controlled_skill_mutation_exec_command(bash_wrapped, str(tmp_path)) is False

        dup_flags = good + " --command-id issue_body.update"
        assert is_controlled_skill_mutation_exec_command(dup_flags, str(tmp_path)) is False

        eq_form = good.replace("--command-id issue_body.update", "--command-id=issue_body.update")
        assert is_controlled_skill_mutation_exec_command(eq_form, str(tmp_path)) is False
