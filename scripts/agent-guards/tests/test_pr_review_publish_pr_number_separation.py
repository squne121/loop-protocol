#!/usr/bin/env python3
"""
Tests for pr_review.publish render-mode --pr-number / --issue-number
separation (Issue #1822).

AC1  test_render_mode_requires_pr_number
AC2  test_render_mode_issue_number_and_pr_number_distinct
AC3  test_render_mode_idempotency_key_uses_pr_number
AC4  test_render_mode_fail_closed_on_head_mismatch
AC9  test_input_file_mode_rejects_issue_pr_number_mismatch (fix_delta)
"""

from __future__ import annotations

import argparse
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

import controlled_skill_mutation_exec as _exec  # noqa: E402
from controlled_skill_mutation_policy import (  # noqa: E402
    TRUSTED_REPO,
    COMMAND_ID_PR_REVIEW_PUBLISH,
)

ISSUE_NUMBER = 1688
PR_NUMBER = 1818
HEAD_SHA = "a" * 40
OTHER_SHA = "b" * 40

_APPROVE_BODY = (
    "PR review verdict.\n\n"
    "```yaml\n"
    "LOOP_VERDICT_V2:\n"
    "  verdict: APPROVE\n"
    "  merge_ready: true\n"
    "```\n"
)


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


def _write_body_file(tmp_project, issue_number: int, text: str) -> str:
    # Issue #1822: the artifact subtree stays keyed on --issue-number (the
    # linked Issue), never on --pr-number.
    d = tmp_project / "artifacts" / str(issue_number) / "issue-metadata" / COMMAND_ID_PR_REVIEW_PUBLISH
    d.mkdir(parents=True, exist_ok=True)
    f = d / "review_body.md"
    f.write_text(text, encoding="utf-8")
    return f"artifacts/{issue_number}/issue-metadata/{COMMAND_ID_PR_REVIEW_PUBLISH}/review_body.md"


def _base_patches():
    return (
        patch.object(_exec, "_find_gh_bin", return_value=("/bin/gh", "")),
        patch.object(_exec, "_verify_git_remote_origin", return_value=""),
    )


def _render_argv(rel_body: str, *, issue_number: int, pr_number, repo: str = TRUSTED_REPO,
                  verdict: str = "APPROVE", merge_ready: bool = True,
                  reviewed_head_sha: str = HEAD_SHA, expected_head_sha: str = HEAD_SHA):
    argv = [
        "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
        "--issue-number", str(issue_number),
        "--render-body-file", rel_body,
        "--verdict", verdict,
        "--reviewed-head-sha", reviewed_head_sha,
        "--expected-head-sha", expected_head_sha,
        "--repo", repo,
    ]
    if merge_ready:
        argv.append("--merge-ready")
    if pr_number is not None:
        argv.extend(["--pr-number", str(pr_number)])
    return argv


class TestAC1RenderModeRequiresPrNumber:
    """AC1: --pr-number missing / non-integer / zero-or-negative is rejected
    fail-closed before any POST."""

    def test_missing_pr_number_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_body_file(tmp_project, ISSUE_NUMBER, _APPROVE_BODY)
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_post_pr_review") as mock_post:
                rc = _exec.main(_render_argv(rel, issue_number=ISSUE_NUMBER, pr_number=None))
        assert rc == 2
        mock_post.assert_not_called()

    def test_non_integer_pr_number_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_body_file(tmp_project, ISSUE_NUMBER, _APPROVE_BODY)
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_post_pr_review") as mock_post:
                with pytest.raises(SystemExit) as excinfo:
                    _exec.main([
                        "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                        "--issue-number", str(ISSUE_NUMBER),
                        "--render-body-file", rel,
                        "--verdict", "APPROVE",
                        "--merge-ready",
                        "--reviewed-head-sha", HEAD_SHA,
                        "--expected-head-sha", HEAD_SHA,
                        "--repo", TRUSTED_REPO,
                        "--pr-number", "not-an-int",
                    ])
        # argparse rejects the malformed value before main() body runs --
        # exit code is non-zero (fail-closed) and no POST ever happens.
        assert excinfo.value.code != 0
        mock_post.assert_not_called()

    @pytest.mark.parametrize("bad_value", [0, -1, -1818])
    def test_zero_or_negative_pr_number_rejected(self, tmp_project, monkeypatch, bad_value):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_body_file(tmp_project, ISSUE_NUMBER, _APPROVE_BODY)
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_post_pr_review") as mock_post:
                rc = _exec.main(
                    _render_argv(rel, issue_number=ISSUE_NUMBER, pr_number=bad_value)
                )
        assert rc == 2
        mock_post.assert_not_called()


class TestAC2IssueNumberAndPrNumberDistinct:
    """AC2: linked Issue and target PR are independent identifiers -- the
    artifact subtree stays keyed on --issue-number while the GitHub review
    target (marker list / post / idempotency key) uses --pr-number."""

    def test_render_mode_issue_number_and_pr_number_distinct(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        # Body file lives under the Issue's artifact subtree (#1688), not the
        # PR's (#1818) -- confirming the artifact/issue binding is untouched.
        rel = _write_body_file(tmp_project, ISSUE_NUMBER, _APPROVE_BODY)
        expected_body_sha256 = hashlib.sha256(_APPROVE_BODY.encode("utf-8")).hexdigest()
        marker_str = _exec._pr_review_marker_str(
            f"{TRUSTED_REPO}:{PR_NUMBER}:{HEAD_SHA}:{expected_body_sha256}"
        )
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_find_pr_review_marker_matches",
                               return_value=([], "")) as mock_find:
                with patch.object(_exec, "_fetch_pr_head_sha", return_value=(HEAD_SHA, "")):
                    with patch.object(_exec, "_post_pr_review",
                                       return_value=({"id": 900}, "")) as mock_post:
                        with patch.object(_exec, "_readback_pr_review", return_value={
                            "review": {
                                "id": 900, "html_url": "https://ex/900",
                                "state": "COMMENTED", "commit_id": HEAD_SHA,
                                "submitted_at": "2026-01-01T00:00:00Z",
                                "body": f"{_APPROVE_BODY}\n\n{marker_str}\n",
                            }
                        }):
                            with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                                rc = _exec.main(_render_argv(
                                    rel, issue_number=ISSUE_NUMBER, pr_number=PR_NUMBER,
                                ))
        assert rc == 0
        # The marker precheck / post targeted PR #1818, never Issue #1688.
        assert mock_find.call_args.args[1] == PR_NUMBER
        assert mock_post.call_args.args[0] == PR_NUMBER

    def test_render_mode_marker_targets_pr_number_endpoint(self, tmp_project, monkeypatch):
        """Confirms the marker precheck is dispatched against pr_number even
        when issue_number and pr_number numerically collide with an unrelated
        candidate -- i.e. the field is never silently defaulted back to
        issue_number."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_body_file(tmp_project, ISSUE_NUMBER, _APPROVE_BODY)
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_find_pr_review_marker_matches",
                               return_value=([], "")) as mock_find:
                with patch.object(_exec, "_fetch_pr_head_sha", return_value=(HEAD_SHA, "")):
                    with patch.object(_exec, "_post_pr_review",
                                       return_value=({"id": 901}, "")):
                        _exec.main(_render_argv(
                            rel, issue_number=ISSUE_NUMBER, pr_number=PR_NUMBER,
                        ))
        assert mock_find.call_args.args[1] == PR_NUMBER
        assert mock_find.call_args.args[1] != ISSUE_NUMBER


class TestAC3IdempotencyKeyUsesPrNumber:
    """AC3: idempotency_key is built as repo:pr_number:expected_head_sha:body_sha256."""

    def test_render_mode_idempotency_key_uses_pr_number(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_body_file(tmp_project, ISSUE_NUMBER, _APPROVE_BODY)
        args = argparse.Namespace(
            issue_number=ISSUE_NUMBER,
            pr_number=PR_NUMBER,
            repo=TRUSTED_REPO,
            render_body_file=rel,
            verdict="APPROVE",
            reviewed_head_sha=HEAD_SHA,
            expected_head_sha=HEAD_SHA,
            merge_ready=True,
        )
        input_data, err = _exec._render_pr_review_publish_request(args, tmp_project)
        assert err == ""
        expected_body_sha256 = hashlib.sha256(_APPROVE_BODY.encode("utf-8")).hexdigest()
        expected_key = f"{TRUSTED_REPO}:{PR_NUMBER}:{HEAD_SHA}:{expected_body_sha256}"
        assert input_data["idempotency_key"] == expected_key
        assert input_data["pr_number"] == PR_NUMBER
        assert input_data["issue_number"] == ISSUE_NUMBER

    def test_idempotency_key_differs_when_only_pr_number_changes(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_body_file(tmp_project, ISSUE_NUMBER, _APPROVE_BODY)

        def _build(pr_number):
            args = argparse.Namespace(
                issue_number=ISSUE_NUMBER,
                pr_number=pr_number,
                repo=TRUSTED_REPO,
                render_body_file=rel,
                verdict="APPROVE",
                reviewed_head_sha=HEAD_SHA,
                expected_head_sha=HEAD_SHA,
                merge_ready=True,
            )
            data, err = _exec._render_pr_review_publish_request(args, tmp_project)
            assert err == ""
            return data["idempotency_key"]

        key_a = _build(PR_NUMBER)
        key_b = _build(PR_NUMBER + 1)
        assert key_a != key_b


class TestAC4FailClosedOnHeadMismatch:
    """AC4: expected/reviewed HEAD mismatch, body/verdict/merge_ready
    mismatch, and duplicate marker conflicts remain fail-closed with the new
    mandatory --pr-number in render mode."""

    def test_reviewed_expected_head_sha_mismatch_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_body_file(tmp_project, ISSUE_NUMBER, _APPROVE_BODY)
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_post_pr_review") as mock_post:
                rc = _exec.main(_render_argv(
                    rel, issue_number=ISSUE_NUMBER, pr_number=PR_NUMBER,
                    reviewed_head_sha=OTHER_SHA, expected_head_sha=HEAD_SHA,
                ))
        assert rc == 2
        mock_post.assert_not_called()

    def test_body_verdict_mismatch_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        body = (
            "```yaml\nLOOP_VERDICT_V2:\n  verdict: REQUEST_CHANGES\n  "
            "merge_ready: false\n```\n"
        )
        rel = _write_body_file(tmp_project, ISSUE_NUMBER, body)
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_post_pr_review") as mock_post:
                rc = _exec.main(_render_argv(
                    rel, issue_number=ISSUE_NUMBER, pr_number=PR_NUMBER,
                    verdict="APPROVE", merge_ready=False,
                ))
        assert rc == 2
        mock_post.assert_not_called()

    def test_merge_ready_mismatch_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        body = (
            "```yaml\nLOOP_VERDICT_V2:\n  verdict: APPROVE\n  "
            "merge_ready: false\n```\n"
        )
        rel = _write_body_file(tmp_project, ISSUE_NUMBER, body)
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_post_pr_review") as mock_post:
                rc = _exec.main(_render_argv(
                    rel, issue_number=ISSUE_NUMBER, pr_number=PR_NUMBER,
                    verdict="APPROVE", merge_ready=True,
                ))
        assert rc == 2
        mock_post.assert_not_called()

    def test_duplicate_marker_conflict_rejected(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rel = _write_body_file(tmp_project, ISSUE_NUMBER, _APPROVE_BODY)
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(
                _exec, "_find_pr_review_marker_matches",
                return_value=([{"id": 1}, {"id": 2}], ""),
            ):
                with patch.object(_exec, "_post_pr_review") as mock_post:
                    rc = _exec.main(_render_argv(
                        rel, issue_number=ISSUE_NUMBER, pr_number=PR_NUMBER,
                    ))
        assert rc == 1
        mock_post.assert_not_called()



class TestAC9InputFileModeEnforcesIssuePrMatch:
    """AC9 (fix_delta): the legacy --input-file code path must still reject
    pr_number != issue_number -- only render mode treats them as independent
    identifiers. This restores the pre-#1822 behavior for --input-file mode
    while keeping render mode's Issue/PR separation intact."""

    def _write_input_file(self, tmp_project, issue_number: int, data: dict) -> str:
        d = (
            tmp_project / "artifacts" / str(issue_number) / "issue-metadata"
            / COMMAND_ID_PR_REVIEW_PUBLISH
        )
        d.mkdir(parents=True, exist_ok=True)
        p = d / "in.json"
        p.write_text(json.dumps(data))
        return f"artifacts/{issue_number}/issue-metadata/{COMMAND_ID_PR_REVIEW_PUBLISH}/in.json"

    def test_input_file_mode_rejects_issue_pr_number_mismatch(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        body, body_sha256 = _APPROVE_BODY, hashlib.sha256(_APPROVE_BODY.encode("utf-8")).hexdigest()
        data = {
            "schema": "PR_REVIEW_PUBLISH_REQUEST_V1",
            "issue_number": ISSUE_NUMBER,
            "repo": TRUSTED_REPO,
            "pr_number": PR_NUMBER,
            "expected_head_sha": HEAD_SHA,
            "event": "COMMENT",
            "body": body,
            "body_sha256": body_sha256,
            "producer_role": "pr-reviewer",
            "idempotency_key": f"{TRUSTED_REPO}:{PR_NUMBER}:{HEAD_SHA}:{body_sha256}",
        }
        rel = self._write_input_file(tmp_project, ISSUE_NUMBER, data)
        p1, p2 = _base_patches()
        with p1, p2:
            with patch.object(_exec, "_post_pr_review") as mock_post:
                rc = _exec.main([
                    "--command-id", COMMAND_ID_PR_REVIEW_PUBLISH,
                    "--issue-number", str(ISSUE_NUMBER),
                    "--input-file", rel,
                    "--repo", TRUSTED_REPO,
                    "--json",
                ])
        assert rc == 2
        mock_post.assert_not_called()
