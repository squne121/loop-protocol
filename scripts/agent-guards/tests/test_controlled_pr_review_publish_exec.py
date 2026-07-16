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
                                "submitted_at": "2026-01-01T00:00:00Z",
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
                                       "submitted_at": "2026-01-01T00:00:00Z",
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


class TestAC6TrailingNewlineBodyRegression:
    """Issue #1539 fix_delta regression: _run_pr_review_publish() previously
    called an open-ended `.rstrip("\n")` on the readback body before hashing,
    which ate any trailing newline that was already part of raw_body itself
    (near-universal for file-sourced bodies) and produced a permanent
    postcondition_body_sha256_mismatch even though the review had already
    posted successfully. None of the pre-existing AC6 fixtures used a
    trailing-newline body, so this regression went undetected."""

    def test_ac6_trailing_newline_body_roundtrip_succeeds(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        body, body_sha256 = _body_and_hash(
            "LOOP_VERDICT_V2: APPROVE\nblockers: []\n"
        )
        assert body.endswith("\n")
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
                    with patch.object(_exec, "_post_pr_review", return_value=({"id": 8}, "")):
                        with patch.object(_exec, "_readback_pr_review", return_value={
                            "review": {"id": 8, "html_url": "https://ex/8",
                                       "state": "COMMENTED", "commit_id": HEAD_SHA,
                                       "submitted_at": "2026-01-01T00:00:00Z",
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

    def test_ac6_genuine_body_corruption_still_detected(self, tmp_project, monkeypatch):
        """The fix must not become a no-op: an actually corrupted readback body
        (diverging content, not just a trailing-newline normalization
        difference) must still fail the postcondition check."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        body, body_sha256 = _body_and_hash("LOOP_VERDICT_V2: APPROVE\n")
        data = _pr_review_input(
            body=body, body_sha256=body_sha256,
            idempotency_key=f"{TRUSTED_REPO}:{PR_NUMBER}:{HEAD_SHA}:{body_sha256}",
        )
        rel = _write_input(tmp_project, "in.json", data)
        marker = _exec._pr_review_marker_str(data["idempotency_key"])
        corrupted_body = "LOOP_VERDICT_V2: REQUEST_CHANGES\n"
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_find_pr_review_marker_matches", return_value=([], "")):
                with patch.object(_exec, "_fetch_pr_head_sha", return_value=(HEAD_SHA, "")):
                    with patch.object(_exec, "_post_pr_review", return_value=({"id": 9}, "")):
                        with patch.object(_exec, "_readback_pr_review", return_value={
                            "review": {"id": 9, "html_url": "https://ex/9",
                                       "state": "COMMENTED", "commit_id": HEAD_SHA,
                                       "submitted_at": "2026-01-01T00:00:00Z",
                                       "body": f"{corrupted_body}\n\n{marker}\n"}
                        }):
                            rc = _exec.main([
                                "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                                "--issue-number", str(PR_NUMBER),
                                "--input-file", rel,
                                "--repo", TRUSTED_REPO,
                                "--json",
                            ])
        assert rc == 1


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
        """Issue #1539 fix_delta Blocker 3: idempotent-retry success now requires
        a FRESH single-review readback (not the list entry alone) to pass the
        SAME postcondition validator as the fresh-post path -- body hash, marker
        position, submitted_at, current head, and author identity all re-verified."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        data = _pr_review_input()
        rel = _write_input(tmp_project, "in.json", data)
        marker = _exec._pr_review_marker_str(data["idempotency_key"])
        review = {
            "id": 5, "html_url": "https://ex/5",
            "state": "COMMENTED", "commit_id": HEAD_SHA,
            "submitted_at": "2026-01-01T00:00:00Z",
            "body": f"{data['body']}\n\n{marker}\n",
            "user": {"login": "reviewer-bot"},
        }
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(
                _exec, "_find_pr_review_marker_matches",
                return_value=([{"id": 5, "state": "COMMENTED", "commit_id": HEAD_SHA}], ""),
            ):
                with patch.object(_exec, "_readback_pr_review", return_value={"review": review}):
                    with patch.object(_exec, "_fetch_pr_head_sha", return_value=(HEAD_SHA, "")):
                        with patch.object(
                            _exec, "_fetch_authenticated_login",
                            return_value=("reviewer-bot", ""),
                        ):
                            with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
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

    def _retry_base(self, tmp_project, monkeypatch, review_overrides=None, data_overrides=None):
        """Shared retry-path fixture builder for the Blocker 3 regression tests
        below. Returns (rel, marker, review dict) with a valid baseline that
        each test then mutates to exercise one specific postcondition check."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        data = _pr_review_input(**(data_overrides or {}))
        rel = _write_input(tmp_project, "in.json", data)
        marker = _exec._pr_review_marker_str(data["idempotency_key"])
        review = {
            "id": 5, "html_url": "https://ex/5",
            "state": "COMMENTED", "commit_id": HEAD_SHA,
            "submitted_at": "2026-01-01T00:00:00Z",
            "body": f"{data['body']}\n\n{marker}\n",
            "user": {"login": "reviewer-bot"},
        }
        review.update(review_overrides or {})
        return data, rel, marker, review

    def test_ac7_retry_wrong_body_hash_rejected(self, tmp_project, monkeypatch):
        """Blocker 3 regression: retry readback body diverges from the
        caller-declared body_sha256 -- must fail, not silently accept."""
        data, rel, marker, review = self._retry_base(tmp_project, monkeypatch)
        review["body"] = f"CORRUPTED CONTENT\n\n{marker}\n"
        p1, p2 = self._base()
        with p1, p2:
            with patch.object(
                _exec, "_find_pr_review_marker_matches",
                return_value=([{"id": 5, "state": "COMMENTED", "commit_id": HEAD_SHA}], ""),
            ):
                with patch.object(_exec, "_readback_pr_review", return_value={"review": review}):
                    with patch.object(_exec, "_fetch_pr_head_sha", return_value=(HEAD_SHA, "")):
                        with patch.object(
                            _exec, "_fetch_authenticated_login", return_value=("reviewer-bot", "")
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

    def test_ac7_retry_marker_in_middle_rejected(self, tmp_project, monkeypatch):
        """Blocker 3 regression: marker present as a mid-body substring (not
        the publisher's own trailing marker) must NOT be treated as a match."""
        data, rel, marker, review = self._retry_base(tmp_project, monkeypatch)
        review["body"] = f"{marker}\n\n{data['body']}"
        p1, p2 = self._base()
        with p1, p2:
            with patch.object(
                _exec, "_find_pr_review_marker_matches",
                return_value=([{"id": 5, "state": "COMMENTED", "commit_id": HEAD_SHA}], ""),
            ):
                with patch.object(_exec, "_readback_pr_review", return_value={"review": review}):
                    with patch.object(_exec, "_fetch_pr_head_sha", return_value=(HEAD_SHA, "")):
                        with patch.object(
                            _exec, "_fetch_authenticated_login", return_value=("reviewer-bot", "")
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

    def test_ac7_retry_stale_current_head_rejected(self, tmp_project, monkeypatch):
        """Blocker 3 regression: the existing review matches expected_head_sha,
        but the PR has since moved to a new head -- must not report success."""
        data, rel, marker, review = self._retry_base(tmp_project, monkeypatch)
        p1, p2 = self._base()
        with p1, p2:
            with patch.object(
                _exec, "_find_pr_review_marker_matches",
                return_value=([{"id": 5, "state": "COMMENTED", "commit_id": HEAD_SHA}], ""),
            ):
                with patch.object(_exec, "_readback_pr_review", return_value={"review": review}):
                    with patch.object(_exec, "_fetch_pr_head_sha", return_value=(OTHER_SHA, "")):
                        with patch.object(
                            _exec, "_fetch_authenticated_login", return_value=("reviewer-bot", "")
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

    def test_ac7_retry_wrong_author_rejected(self, tmp_project, monkeypatch):
        """Blocker 3 regression: review author identity does not match the
        currently authenticated gh identity -- must not report success."""
        data, rel, marker, review = self._retry_base(
            tmp_project, monkeypatch, review_overrides={"user": {"login": "someone-else"}}
        )
        p1, p2 = self._base()
        with p1, p2:
            with patch.object(
                _exec, "_find_pr_review_marker_matches",
                return_value=([{"id": 5, "state": "COMMENTED", "commit_id": HEAD_SHA}], ""),
            ):
                with patch.object(_exec, "_readback_pr_review", return_value={"review": review}):
                    with patch.object(_exec, "_fetch_pr_head_sha", return_value=(HEAD_SHA, "")):
                        with patch.object(
                            _exec, "_fetch_authenticated_login", return_value=("reviewer-bot", "")
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

    def test_ac7_retry_tracked_changes_detected_rejected(self, tmp_project, monkeypatch):
        """Blocker 3 regression: tracked-changes postcondition is re-checked on
        retry too, not only on the fresh-post path."""
        data, rel, marker, review = self._retry_base(tmp_project, monkeypatch)
        p1, p2 = self._base()
        with p1, p2:
            with patch.object(
                _exec, "_find_pr_review_marker_matches",
                return_value=([{"id": 5, "state": "COMMENTED", "commit_id": HEAD_SHA}], ""),
            ):
                with patch.object(_exec, "_readback_pr_review", return_value={"review": review}):
                    with patch.object(_exec, "_fetch_pr_head_sha", return_value=(HEAD_SHA, "")):
                        with patch.object(
                            _exec, "_fetch_authenticated_login", return_value=("reviewer-bot", "")
                        ):
                            with patch.object(
                                _exec, "_check_no_tracked_changes", return_value=["dirty.py"]
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

    def _base(self):
        return _base_patches()


class TestHigh1PostPublishStaleHeadTOCTOU:
    """Issue #1539 fix_delta High 1: commit_id binding proves attachment, not
    atomic freshness at POST time. A second head fetch after postcondition
    verification must catch a head that moved between the pre-POST stale
    check and the POST/readback completing."""

    def test_high1_head_moved_after_publish_reports_published_but_stale(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_input(tmp_project, "in.json", _pr_review_input())
        marker = _exec._pr_review_marker_str(
            f"{TRUSTED_REPO}:{PR_NUMBER}:{HEAD_SHA}:"
            + hashlib.sha256(b"LOOP_VERDICT_V2: APPROVE").hexdigest()
        )
        body = "LOOP_VERDICT_V2: APPROVE"
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_find_pr_review_marker_matches", return_value=([], "")):
                # First call (pre-POST stale check) reports fresh; second call
                # (post-publish TOCTOU recheck) reports the head has moved.
                with patch.object(
                    _exec, "_fetch_pr_head_sha",
                    side_effect=[(HEAD_SHA, ""), (OTHER_SHA, "")],
                ):
                    with patch.object(_exec, "_post_pr_review", return_value=({"id": 77}, "")):
                        with patch.object(_exec, "_readback_pr_review", return_value={
                            "review": {
                                "id": 77, "html_url": "https://ex/77",
                                "state": "COMMENTED", "commit_id": HEAD_SHA,
                                "submitted_at": "2026-01-01T00:00:00Z",
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
        # published_but_stale is a non-error failure status (exit 1): the review
        # WAS posted (evidence retained) but must not be reported as success.
        assert rc == 1


class TestBlocker1RenderModeTrustedBridge:
    """Issue #1539 fix_delta Blocker 1: the pr-reviewer SubAgent (no Write/Edit
    tool, no Bash file writes) can never construct/hash/write the
    PR_REVIEW_PUBLISH_REQUEST_V1 JSON itself. Render mode is the trusted
    bridge: a trusted caller supplies only a raw body TEXT file (written by
    the ORCHESTRATOR, not the SubAgent) plus verdict metadata as CLI flags,
    and the executor independently computes/validates everything else. This
    exercises the full chain: verdict-shaped input -> trusted bridge (render
    mode) -> executor -> (mocked) GitHub API, end to end."""

    def _write_body_file(self, tmp_project, text: str) -> str:
        d = tmp_project / "artifacts" / str(PR_NUMBER) / "issue-metadata" / COMMAND_ID_PR_REVIEW_PUBLISH
        d.mkdir(parents=True, exist_ok=True)
        f = d / "review_body.md"
        f.write_text(text, encoding="utf-8")
        return f"artifacts/{PR_NUMBER}/issue-metadata/{COMMAND_ID_PR_REVIEW_PUBLISH}/review_body.md"

    def test_render_mode_end_to_end_success(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        body = (
            "PR review verdict.\n\n"
            "```yaml\n"
            "LOOP_VERDICT_V2:\n"
            "  verdict: APPROVE\n"
            "  merge_ready: true\n"
            "```\n"
        )
        rel = self._write_body_file(tmp_project, body)
        expected_body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        marker_str = _exec._pr_review_marker_str(
            f"{TRUSTED_REPO}:{PR_NUMBER}:{HEAD_SHA}:{expected_body_sha256}"
        )
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_find_pr_review_marker_matches", return_value=([], "")):
                with patch.object(_exec, "_fetch_pr_head_sha", return_value=(HEAD_SHA, "")):
                    with patch.object(_exec, "_post_pr_review", return_value=({"id": 501}, "")):
                        with patch.object(_exec, "_readback_pr_review", return_value={
                            "review": {
                                "id": 501, "html_url": "https://ex/501",
                                "state": "COMMENTED", "commit_id": HEAD_SHA,
                                "submitted_at": "2026-01-01T00:00:00Z",
                                "body": f"{body}\n\n{marker_str}\n",
                            }
                        }):
                            with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                                rc = _exec.main([
                                    "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                                    "--issue-number", str(PR_NUMBER),
                                    "--render-body-file", rel,
                                    "--verdict", "APPROVE",
                                    "--merge-ready",
                                    "--reviewed-head-sha", HEAD_SHA,
                                    "--expected-head-sha", HEAD_SHA,
                                    "--repo", TRUSTED_REPO,
                                    "--json",
                                ])
        assert rc == 0

    def test_render_mode_body_verdict_mismatch_rejected(self, tmp_project, monkeypatch):
        """High 2: declared --verdict must match the body's own embedded
        LOOP_VERDICT_V2.verdict -- they must never be able to diverge."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        body = (
            "```yaml\nLOOP_VERDICT_V2:\n  verdict: REQUEST_CHANGES\n  "
            "merge_ready: false\n```\n"
        )
        rel = self._write_body_file(tmp_project, body)
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(_exec, "_post_pr_review") as mock_post:
                    rc = _exec.main([
                        "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                        "--issue-number", str(PR_NUMBER),
                        "--render-body-file", rel,
                        "--verdict", "APPROVE",
                        "--reviewed-head-sha", HEAD_SHA,
                        "--expected-head-sha", HEAD_SHA,
                        "--repo", TRUSTED_REPO,
                    ])
        assert rc == 2
        mock_post.assert_not_called()

    def test_render_mode_missing_loop_verdict_v2_block_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = self._write_body_file(tmp_project, "just some prose, no fenced block")
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(_exec, "_post_pr_review") as mock_post:
                    rc = _exec.main([
                        "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                        "--issue-number", str(PR_NUMBER),
                        "--render-body-file", rel,
                        "--verdict", "APPROVE",
                        "--reviewed-head-sha", HEAD_SHA,
                        "--expected-head-sha", HEAD_SHA,
                        "--repo", TRUSTED_REPO,
                    ])
        assert rc == 2
        mock_post.assert_not_called()

    def test_render_mode_merge_ready_without_approve_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        body = (
            "```yaml\nLOOP_VERDICT_V2:\n  verdict: REQUEST_CHANGES\n  "
            "merge_ready: true\n```\n"
        )
        rel = self._write_body_file(tmp_project, body)
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(_exec, "_post_pr_review") as mock_post:
                    rc = _exec.main([
                        "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                        "--issue-number", str(PR_NUMBER),
                        "--render-body-file", rel,
                        "--verdict", "REQUEST_CHANGES",
                        "--merge-ready",
                        "--reviewed-head-sha", HEAD_SHA,
                        "--expected-head-sha", HEAD_SHA,
                        "--repo", TRUSTED_REPO,
                    ])
        assert rc == 2
        mock_post.assert_not_called()

    def test_render_mode_reviewed_head_sha_mismatch_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        body = "```yaml\nLOOP_VERDICT_V2:\n  verdict: APPROVE\n  merge_ready: true\n```\n"
        rel = self._write_body_file(tmp_project, body)
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                with patch.object(_exec, "_post_pr_review") as mock_post:
                    rc = _exec.main([
                        "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                        "--issue-number", str(PR_NUMBER),
                        "--render-body-file", rel,
                        "--verdict", "APPROVE",
                        "--merge-ready",
                        "--reviewed-head-sha", OTHER_SHA,
                        "--expected-head-sha", HEAD_SHA,
                        "--repo", TRUSTED_REPO,
                    ])
        assert rc == 2
        mock_post.assert_not_called()

    def test_render_mode_and_input_file_mutually_exclusive(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel_input = _write_input(tmp_project, "in.json", _pr_review_input())
        rel_body = self._write_body_file(tmp_project, "irrelevant")
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                rc = _exec.main([
                    "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                    "--issue-number", str(PR_NUMBER),
                    "--input-file", rel_input,
                    "--render-body-file", rel_body,
                    "--verdict", "APPROVE",
                    "--reviewed-head-sha", HEAD_SHA,
                    "--expected-head-sha", HEAD_SHA,
                    "--repo", TRUSTED_REPO,
                ])
        assert rc == 2

    def test_neither_input_file_nor_render_body_file_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):
            with patch.object(_exec, "_verify_git_remote_origin", return_value=""):
                rc = _exec.main([
                    "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                    "--issue-number", str(PR_NUMBER),
                    "--repo", TRUSTED_REPO,
                ])
        assert rc == 2


class TestHigh2ExactKeySchema:
    """Issue #1539 fix_delta High 2: PR_REVIEW_PUBLISH_REQUEST_V1 rejects any
    key outside the declared schema, and enforces a body size bound."""

    def test_unknown_field_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        data = _pr_review_input()
        data["extra_untrusted_field"] = "sneaky"
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

    def test_body_too_large_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        big_body = "x" * 70000
        big_sha = hashlib.sha256(big_body.encode("utf-8")).hexdigest()
        data = _pr_review_input(
            body=big_body, body_sha256=big_sha,
            idempotency_key=f"{TRUSTED_REPO}:{PR_NUMBER}:{HEAD_SHA}:{big_sha}",
        )
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


class TestBlocker2HostSchemeBinding:
    """Issue #1539 fix_delta Blocker 2: origin verification must be a
    structural host/scheme check, not a bare last-path-segment regex."""

    @pytest.mark.parametrize("url,expected", [
        (f"https://github.com/{TRUSTED_REPO}.git", TRUSTED_REPO),
        (f"https://github.com/{TRUSTED_REPO}", TRUSTED_REPO),
        (f"git@github.com:{TRUSTED_REPO}.git", TRUSTED_REPO),
        (f"ssh://git@github.com/{TRUSTED_REPO}.git", TRUSTED_REPO),
    ])
    def test_trusted_forms_accepted(self, url, expected):
        assert _exec._parse_trusted_github_remote(url) == expected

    @pytest.mark.parametrize("url", [
        f"https://attacker.example/{TRUSTED_REPO}.git",
        f"http://github.com/{TRUSTED_REPO}.git",  # wrong scheme (not https/ssh)
        f"file:///{TRUSTED_REPO}",
        f"ssh://git@attacker.example/{TRUSTED_REPO}.git",
        f"git@attacker.example:{TRUSTED_REPO}.git",
        f"https://github.com.attacker.example/{TRUSTED_REPO}.git",
        f"https://evil@github.com/{TRUSTED_REPO}.git",  # non-git userinfo
        f"https://github.com:8443/{TRUSTED_REPO}.git",  # non-default port
        "not a url at all",
        "",
    ])
    def test_untrusted_forms_rejected(self, url):
        assert _exec._parse_trusted_github_remote(url) is None

    def test_evil_host_rejected_end_to_end(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        subprocess.run(
            ["git", "-C", str(tmp_project), "remote", "set-url", "origin",
             f"https://attacker.example/{TRUSTED_REPO}.git"],
            capture_output=True,
        )
        rel = _write_input(tmp_project, "in.json", _pr_review_input())
        with patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")):
            with patch.object(_exec, "_post_pr_review") as mock_post:
                rc = _exec.main([
                    "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                    "--issue-number", str(PR_NUMBER),
                    "--input-file", rel,
                    "--repo", TRUSTED_REPO,
                ])
        assert rc == 2
        mock_post.assert_not_called()

    def test_gh_subprocess_env_strips_gh_host_and_debug_overrides(self, monkeypatch):
        """Issue #1539 fix_delta Blocker 2: GH_HOST / GH_REPO / GH_CONFIG_DIR /
        GH_DEBUG / DEBUG must never reach the sanitized gh subprocess env, even
        if the parent process (an inherited/attacker-controlled environment)
        sets them."""
        monkeypatch.setenv("GH_HOST", "attacker.example")
        monkeypatch.setenv("GH_REPO", "attacker/other-repo")
        monkeypatch.setenv("GH_CONFIG_DIR", "/tmp/evil-gh-config")
        monkeypatch.setenv("GH_DEBUG", "api")
        monkeypatch.setenv("DEBUG", "1")
        env = _exec._build_pr_review_gh_env()
        for key in ("GH_HOST", "GH_REPO", "GH_CONFIG_DIR", "GH_DEBUG", "DEBUG"):
            assert key not in env

    def test_pr_review_gh_calls_pass_sanitized_env_not_none(self, tmp_project, monkeypatch):
        """Every pr_review.publish gh subprocess call must receive an explicit
        sanitized env (never inherit the parent process env unfiltered)."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_input(tmp_project, "in.json", _pr_review_input())
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_find_pr_review_marker_matches") as mock_find:
                mock_find.return_value = ([], "")
                with patch.object(_exec, "_fetch_pr_head_sha") as mock_head:
                    mock_head.return_value = (OTHER_SHA, "")
                    _exec.main([
                        "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                        "--issue-number", str(PR_NUMBER),
                        "--input-file", rel,
                        "--repo", TRUSTED_REPO,
                    ])
                # env kwarg passed to the marker-list helper (Blocker 2).
                assert mock_find.call_args.kwargs.get("env") is not None
                assert mock_head.call_args.kwargs.get("env") is not None


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
