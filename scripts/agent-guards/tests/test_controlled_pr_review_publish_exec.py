#!/usr/bin/env python3
"""
Tests for the pr_review.publish controlled mutation command id (Issue #1536).

AC1  test_ac1_no_worktree_required
AC2  test_ac2_event_comment_fixed_rejects_alias_and_missing
AC3  test_ac3_stale_head_stops_before_post
AC4  test_ac4_commit_id_binding_and_readback
AC5  test_ac5_repo_host_origin_binding
AC6  test_ac6_verdict_roundtrip_body_hash
AC7  test_ac7_idempotency_retry_no_duplicate
AC9  test_ac9_no_secret_leak_in_diagnostics
"""

from __future__ import annotations

import hashlib
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
    COMMAND_ID_PR_REVIEW_PUBLISH,
)

PR_NUMBER = 1530
HEAD_SHA = "a" * 40
OTHER_SHA = "b" * 40


def _body_and_hash(text: str = "LOOP_VERDICT_V2: APPROVE"):
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pr_review_input(**overrides) -> dict:
    body, body_sha256 = _body_and_hash()
    data = {
        "schema": "PR_REVIEW_PUBLISH_REQUEST_V1",
        "issue_number": PR_NUMBER,
        "repo": TRUSTED_REPO,
        "pr_number": PR_NUMBER,
        "expected_head_sha": HEAD_SHA,
        "event": "COMMENT",
        "body": body,
        "body_sha256": body_sha256,
        "producer_role": "pr-reviewer",
        "idempotency_key": f"{TRUSTED_REPO}:{PR_NUMBER}:{HEAD_SHA}:{body_sha256}",
    }
    data.update(overrides)
    return data


@pytest.fixture()
def tmp_project(tmp_path):
    executor_dir = tmp_path / "scripts" / "agent-guards"
    executor_dir.mkdir(parents=True)
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         f"https://github.com/{TRUSTED_REPO}.git"],
        capture_output=True,
    )
    return tmp_path


def _write_input(tmp_project, name, data):
    d = tmp_project / "artifacts" / str(PR_NUMBER) / "issue-metadata" / COMMAND_ID_PR_REVIEW_PUBLISH
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(json.dumps(data))
    return f"artifacts/{PR_NUMBER}/issue-metadata/{COMMAND_ID_PR_REVIEW_PUBLISH}/{name}"


def _base_patches():
    """Common patches so main() reaches the pr_review.publish dispatch."""
    return (
        patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")),
        patch.object(_exec, "_verify_git_remote_origin", return_value=""),
    )


class TestAC1NoWorktreeRequired:
    def test_ac1_no_worktree_required(self, tmp_project, monkeypatch):
        """Success path runs to completion from a plain tmp_project checkout --
        no issue-specific worktree bootstrap/cd is required anywhere in the
        pr_review.publish path (Option C: reviewer never creates a worktree)."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_input(tmp_project, "in.json", _pr_review_input())
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_find_pr_review_marker_matches", return_value=([], "")):
                with patch.object(_exec, "_fetch_pr_head_sha", return_value=(HEAD_SHA, "")):
                    with patch.object(_exec, "_post_pr_review",
                                       return_value=({"id": 42}, "")):
                        body, body_sha256 = _body_and_hash()
                        marker = _exec._pr_review_marker_str(
                            f"{TRUSTED_REPO}:{PR_NUMBER}:{HEAD_SHA}:{body_sha256}"
                        )
                        with patch.object(_exec, "_readback_pr_review", return_value={
                            "review": {
                                "id": 42, "html_url": "https://ex/review/42",
                                "state": "COMMENTED", "commit_id": HEAD_SHA,
                                "body": f"{body}\n\n{marker}\n",
                            }
                        }):
                            with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                                rc = _exec.main([
                                    "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                                    "--issue-number", str(PR_NUMBER),
                                    "--input-file", rel,
                                    "--repo", TRUSTED_REPO,
                                    "--json",
                                ])
        assert rc == 0


class TestAC2EventFixed:
    @pytest.mark.parametrize("bad_event", ["APPROVE", "REQUEST_CHANGES", "approve",
                                            "comment", "", None])
    def test_ac2_event_comment_fixed_rejects_alias_and_missing(
        self, tmp_project, monkeypatch, bad_event
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        overrides = {"event": bad_event} if bad_event is not None else {}
        data = _pr_review_input(**overrides)
        if bad_event is None:
            del data["event"]
        rel = _write_input(tmp_project, "in.json", data)
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_post_pr_review") as mock_post:
                rc = _exec.main([
                    "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                    "--issue-number", str(PR_NUMBER),
                    "--input-file", rel,
                    "--repo", TRUSTED_REPO,
                ])
        assert rc == 2
        mock_post.assert_not_called()


class TestAC3StaleHead:
    def test_ac3_stale_head_stops_before_post(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_input(tmp_project, "in.json", _pr_review_input())
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_find_pr_review_marker_matches", return_value=([], "")):
                with patch.object(_exec, "_fetch_pr_head_sha", return_value=(OTHER_SHA, "")):
                    with patch.object(_exec, "_post_pr_review") as mock_post:
                        rc = _exec.main([
                            "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                            "--issue-number", str(PR_NUMBER),
                            "--input-file", rel,
                            "--repo", TRUSTED_REPO,
                        ])
        assert rc == 1
        mock_post.assert_not_called()


class TestAC4CommitIdBindingAndReadback:
    def test_ac4_commit_id_binding_and_readback(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_input(tmp_project, "in.json", _pr_review_input())
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_find_pr_review_marker_matches", return_value=([], "")):
                with patch.object(_exec, "_fetch_pr_head_sha", return_value=(HEAD_SHA, "")):
                    with patch.object(_exec, "_post_pr_review") as mock_post:
                        mock_post.return_value = ({"id": 99}, "")
                        with patch.object(_exec, "_readback_pr_review", return_value={
                            # Wrong state -- must not be treated as success.
                            "review": {"id": 99, "html_url": "https://ex/99",
                                       "state": "PENDING", "commit_id": HEAD_SHA, "body": ""}
                        }):
                            rc = _exec.main([
                                "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                                "--issue-number", str(PR_NUMBER),
                                "--input-file", rel,
                                "--repo", TRUSTED_REPO,
                            ])
        assert rc == 1
        # commit_id must have been sent as expected_head_sha.
        assert mock_post.call_args.args[2] == HEAD_SHA

    def test_ac4_commit_id_mismatch_readback_fails(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_input(tmp_project, "in.json", _pr_review_input())
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_find_pr_review_marker_matches", return_value=([], "")):
                with patch.object(_exec, "_fetch_pr_head_sha", return_value=(HEAD_SHA, "")):
                    with patch.object(_exec, "_post_pr_review", return_value=({"id": 99}, "")):
                        with patch.object(_exec, "_readback_pr_review", return_value={
                            "review": {"id": 99, "html_url": "https://ex/99",
                                       "state": "COMMENTED", "commit_id": OTHER_SHA, "body": ""}
                        }):
                            rc = _exec.main([
                                "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                                "--issue-number", str(PR_NUMBER),
                                "--input-file", rel,
                                "--repo", TRUSTED_REPO,
                            ])
        assert rc == 1


class TestAC5RepoHostOriginBinding:
    def test_ac5_repo_host_origin_binding_repo_mismatch_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_input(tmp_project, "in.json", _pr_review_input())
        with patch.object(_exec, "_post_pr_review") as mock_post:
            rc = _exec.main([
                "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                "--issue-number", str(PR_NUMBER),
                "--input-file", rel,
                "--repo", "attacker/other-repo",
            ])
        assert rc == 2
        mock_post.assert_not_called()

    def test_ac5_repo_host_origin_binding_git_remote_origin_mismatch_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_input(tmp_project, "in.json", _pr_review_input())
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):
            with patch.object(_exec, "_verify_git_remote_origin",
                               return_value="git_remote_origin_mismatch: x != y"):
                with patch.object(_exec, "_post_pr_review") as mock_post:
                    rc = _exec.main([
                        "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                        "--issue-number", str(PR_NUMBER),
                        "--input-file", rel,
                        "--repo", TRUSTED_REPO,
                    ])
        assert rc == 2
        mock_post.assert_not_called()

    def test_ac5_repo_host_origin_binding_declared_body_repo_field_mismatch_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_input(
            tmp_project, "in.json", _pr_review_input(repo="squne121/other-repo")
        )
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_post_pr_review") as mock_post:
                rc = _exec.main([
                    "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                    "--issue-number", str(PR_NUMBER),
                    "--input-file", rel,
                    "--repo", TRUSTED_REPO,
                ])
        assert rc == 2
        mock_post.assert_not_called()


class TestAC6VerdictRoundtripBodyHash:
    def test_ac6_verdict_roundtrip_body_hash(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        body, body_sha256 = _body_and_hash(
            "LOOP_VERDICT_V2: APPROVE\nblockers: []\nnote: `backtick` | pipe $(sub)"
        )
        data = _pr_review_input(
            body=body, body_sha256=body_sha256,
            idempotency_key=f"{TRUSTED_REPO}:{PR_NUMBER}:{HEAD_SHA}:{body_sha256}",
        )
        rel = _write_input(tmp_project, "in.json", data)
        marker = _exec._pr_review_marker_str(data["idempotency_key"])
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_find_pr_review_marker_matches", return_value=([], "")):
                with patch.object(_exec, "_fetch_pr_head_sha", return_value=(HEAD_SHA, "")):
                    with patch.object(_exec, "_post_pr_review", return_value=({"id": 7}, "")):
                        with patch.object(_exec, "_readback_pr_review", return_value={
                            "review": {"id": 7, "html_url": "https://ex/7",
                                       "state": "COMMENTED", "commit_id": HEAD_SHA,
                                       "body": f"{body}\n\n{marker}\n"}
                        }):
                            with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                                rc = _exec.main([
                                    "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                                    "--issue-number", str(PR_NUMBER),
                                    "--input-file", rel,
                                    "--repo", TRUSTED_REPO,
                                    "--json",
                                ])
        assert rc == 0


class TestAC7IdempotencyRetryNoDuplicate:
    def test_ac7_idempotency_retry_no_duplicate_conflict_fails_closed(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_input(tmp_project, "in.json", _pr_review_input())
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(
                _exec, "_find_pr_review_marker_matches",
                return_value=([{"id": 1, "state": "COMMENTED", "commit_id": HEAD_SHA},
                                {"id": 2, "state": "COMMENTED", "commit_id": HEAD_SHA}], ""),
            ):
                with patch.object(_exec, "_post_pr_review") as mock_post:
                    rc = _exec.main([
                        "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                        "--issue-number", str(PR_NUMBER),
                        "--input-file", rel,
                        "--repo", TRUSTED_REPO,
                    ])
        assert rc == 1
        mock_post.assert_not_called()

    def test_ac7_idempotency_retry_no_duplicate_existing_marker_no_repost(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_input(tmp_project, "in.json", _pr_review_input())
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(
                _exec, "_find_pr_review_marker_matches",
                return_value=([{"id": 5, "html_url": "https://ex/5",
                                 "state": "COMMENTED", "commit_id": HEAD_SHA}], ""),
            ):
                with patch.object(_exec, "_post_pr_review") as mock_post:
                    rc = _exec.main([
                        "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                        "--issue-number", str(PR_NUMBER),
                        "--input-file", rel,
                        "--repo", TRUSTED_REPO,
                        "--json",
                    ])
        assert rc == 0
        mock_post.assert_not_called()


class TestAC9NoSecretLeak:
    def test_ac9_no_secret_leak_in_diagnostics(self, tmp_project, monkeypatch, capsys):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("GH_TOKEN", "sekrit-token-value")
        rel = _write_input(tmp_project, "in.json", _pr_review_input())
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_find_pr_review_marker_matches", return_value=([], "")):
                with patch.object(_exec, "_fetch_pr_head_sha", return_value=(OTHER_SHA, "")):
                    rc = _exec.main([
                        "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                        "--issue-number", str(PR_NUMBER),
                        "--input-file", rel,
                        "--repo", TRUSTED_REPO,
                        "--json",
                    ])
        assert rc == 1
        captured = capsys.readouterr()
        assert "sekrit-token-value" not in captured.out
        assert "sekrit-token-value" not in captured.err
