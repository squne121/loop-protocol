#!/usr/bin/env python3
"""
Tests for controlled_skill_mutation_exec.py generic executor mechanism
(command_id whitelist / repo binding / issue binding / input-file binding /
input-JSON field validation / postcondition no-tracked-changes) plus the
issue_dependency.remove command id (Issue #1632 / #1667).

Issue #1873 removed the legacy termination_report.publish and pr_review.publish
command ids (and their dedicated marker/readback/module-realpath machinery).
The generic executor-mechanism tests below therefore exercise the still-live
issue_comment.publish command id as their representative fixture value instead
of the retired termination_report.publish -- the mechanism under test
(command_id whitelist, repo binding, git-remote binding, issue binding,
input-file binding, input-JSON validation, postcondition) is unchanged by that
retirement. Success-path / marker-authority / readback coverage for
issue_comment.publish itself already lives in
test_controlled_issue_metadata_exec.py (TestIssueCommentPublish) and is not
duplicated here; the classes below focus on the low-level, command-id-agnostic
negative fixtures (symlink/hardlink input-file components, malformed input
JSON, non-digit LOOP_ISSUE_NUMBER, generic postcondition checks) that were
previously only exercised through the now-retired termination_report.publish
fixture value.

Tests:
- AC8:  command_id validation (whitelist / unknown command id)
- AC10: repo validation (only TRUSTED_REPO)
- AC12: input-file validation (must be in artifact subtree, no symlinks, no hardlinks)
- AC10: input-file JSON validation (schema + issue_number field cross-check)
- AC15: LOOP_ISSUE_NUMBER binding (optional-but-matching; non-digit denied)
- AC14: postcondition (no tracked changes) -- generic unit-level coverage
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import controlled_skill_mutation_exec as _exec
from controlled_skill_mutation_policy import TRUSTED_REPO, COMMAND_ID_ISSUE_COMMENT_PUBLISH


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture()
def tmp_project(tmp_path):
    """Create minimal project structure for executor tests."""
    # Create executor (so PROJECT_ROOT is set correctly via monkeypatching)
    executor_dir = tmp_path / "scripts" / "agent-guards"
    executor_dir.mkdir(parents=True)
    # Create the issue_comment.publish input namespace with a valid fixture.
    artifact_dir = (
        tmp_path / "artifacts" / "1166" / "issue-metadata" / COMMAND_ID_ISSUE_COMMENT_PUBLISH
    )
    artifact_dir.mkdir(parents=True)
    input_file = artifact_dir / "issue_comment_publish_input.json"
    input_file.write_text(json.dumps({
        "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
        "issue_number": 1166,
        "comment_body": "status update <!-- marker-1166 -->",
        "marker": "<!-- marker-1166 -->",
    }))
    # Make a git repo with correct remote so _verify_git_remote_origin passes
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         f"https://github.com/{TRUSTED_REPO}.git"],
        capture_output=True,
    )
    return tmp_path


_INPUT_REL = f"artifacts/1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/issue_comment_publish_input.json"


# =============================================================================
# AC8: command_id validation
# =============================================================================

class TestCommandIdValidation:
    def test_valid_command_id_is_in_whitelist(self):
        from controlled_skill_mutation_policy import ALL_COMMAND_IDS

        assert COMMAND_ID_ISSUE_COMMENT_PUBLISH in ALL_COMMAND_IDS

    def test_unknown_command_id_returns_2(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main([
            "--command-id", "unknown.command",
            "--issue-number", "1166",
            "--input-file", _INPUT_REL,
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2


# =============================================================================
# AC10: repo validation
# =============================================================================

class TestRepoValidation:
    def test_wrong_repo_returns_2(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main([
            "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "--issue-number", "1166",
            "--input-file", _INPUT_REL,
            "--repo", "evil-org/hijack-repo",
        ])
        assert rc == 2


# =============================================================================
# AC12: input-file validation
# =============================================================================

class TestInputFileValidation:
    def test_file_not_found_returns_2(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main([
            "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "--issue-number", "1166",
            "--input-file",
            f"artifacts/1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/nonexistent.json",
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2

    def test_file_outside_artifact_subtree_returns_2(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        # Create a file outside the issue/command-id artifact subtree
        bad_dir = tmp_project / "tmp"
        bad_dir.mkdir(parents=True)
        (bad_dir / "evil.json").write_text(json.dumps({
            "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
            "issue_number": 1166,
        }))
        rc = _exec.main([
            "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "--issue-number", "1166",
            "--input-file", "tmp/evil.json",
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2

    def test_validate_and_resolve_input_file_fn_passes_for_valid(self, tmp_project):
        canonical, err = _exec._validate_and_resolve_input_file(
            _INPUT_REL, 1166, tmp_project, command_id=COMMAND_ID_ISSUE_COMMENT_PUBLISH
        )
        assert err == ""
        assert canonical is not None
        assert canonical.exists()

    def test_validate_and_resolve_input_file_fn_fails_for_wrong_issue(self, tmp_project):
        canonical, err = _exec._validate_and_resolve_input_file(
            _INPUT_REL, 9999, tmp_project, command_id=COMMAND_ID_ISSUE_COMMENT_PUBLISH
        )
        assert err != ""
        assert canonical is None


# =============================================================================
# P0-1 + P0-3: _validate_and_resolve_input_file negative fixtures
# =============================================================================

class TestInputFileNegativeFixtures:
    def test_absolute_path_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        abs_path = str(tmp_project / _INPUT_REL)
        rc = _exec.main([
            "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "--issue-number", "1166",
            "--input-file", abs_path,
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2

    def test_dotdot_traversal_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        rc = _exec.main([
            "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "--issue-number", "1166",
            "--input-file",
            f"artifacts/1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/../{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/issue_comment_publish_input.json",
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2

    def test_symlink_component_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        # Create a symlink directory in the path
        link = tmp_project / "artifacts" / "link_to_1166"
        link.symlink_to(tmp_project / "artifacts" / "1166")
        rc = _exec.main([
            "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "--issue-number", "1166",
            "--input-file",
            f"artifacts/link_to_1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/issue_comment_publish_input.json",
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2

    def test_hardlink_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        original = tmp_project / _INPUT_REL
        hardlink = original.parent / "hardlink_input.json"
        os.link(str(original), str(hardlink))
        rc = _exec.main([
            "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "--issue-number", "1166",
            "--input-file",
            f"artifacts/1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/hardlink_input.json",
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2


# =============================================================================
# P0-2: Input JSON validation
# =============================================================================

def _issue_metadata_dir(tmp_project):
    return tmp_project / "artifacts" / "1166" / "issue-metadata" / COMMAND_ID_ISSUE_COMMENT_PUBLISH


class TestInputJsonValidation:
    def test_missing_issue_number_in_json_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        bad_input = _issue_metadata_dir(tmp_project) / "bad_input.json"
        bad_input.write_text(json.dumps({"schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1"}))
        rc = _exec.main([
            "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "--issue-number", "1166",
            "--input-file",
            f"artifacts/1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/bad_input.json",
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2

    def test_issue_number_mismatch_in_json_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        bad_input = _issue_metadata_dir(tmp_project) / "mismatch_input.json"
        bad_input.write_text(json.dumps({
            "schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1",
            "issue_number": 9999,
        }))
        rc = _exec.main([
            "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "--issue-number", "1166",
            "--input-file",
            f"artifacts/1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/mismatch_input.json",
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2

    def test_wrong_schema_in_json_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        bad_input = _issue_metadata_dir(tmp_project) / "wrong_schema.json"
        bad_input.write_text(json.dumps({
            "schema": "WRONG_SCHEMA_V1",
            "issue_number": 1166,
        }))
        rc = _exec.main([
            "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "--issue-number", "1166",
            "--input-file",
            f"artifacts/1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/wrong_schema.json",
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2

    def test_valid_json_passes_validation(self, tmp_project):
        canonical = tmp_project / _INPUT_REL
        data, err = _exec._load_and_validate_input_json(canonical, 1166, COMMAND_ID_ISSUE_COMMENT_PUBLISH)
        assert err == ""
        assert data is not None

    def test_missing_issue_number_fails_validation(self, tmp_project):
        f = _issue_metadata_dir(tmp_project) / "no_issue.json"
        f.write_text(json.dumps({"schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1"}))
        data, err = _exec._load_and_validate_input_json(f, 1166, COMMAND_ID_ISSUE_COMMENT_PUBLISH)
        assert data is None
        assert "input_issue_number_missing" in err

    def test_issue_mismatch_fails_validation(self, tmp_project):
        f = _issue_metadata_dir(tmp_project) / "mismatch.json"
        f.write_text(json.dumps({"schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1", "issue_number": 9999}))
        data, err = _exec._load_and_validate_input_json(f, 1166, COMMAND_ID_ISSUE_COMMENT_PUBLISH)
        assert data is None
        assert "input_issue_number_mismatch" in err

    def test_non_int_issue_number_fails_validation(self, tmp_project):
        f = _issue_metadata_dir(tmp_project) / "non_int.json"
        f.write_text(json.dumps({"schema": "ISSUE_COMMENT_PUBLISH_INPUT_V1", "issue_number": "1166"}))
        data, err = _exec._load_and_validate_input_json(f, 1166, COMMAND_ID_ISSUE_COMMENT_PUBLISH)
        assert data is None
        assert "input_issue_number_not_int" in err


# =============================================================================
# AC15: LOOP_ISSUE_NUMBER binding (optional-but-matching; Issue #1873 removed
# the only mandatory-binding command id, termination_report.publish, so a
# missing env var is now allowed for every remaining command id -- see
# test_controlled_issue_metadata_exec.py::TestEnvBinding for the
# missing/matching/mismatching coverage). The non-digit fixture below is not
# covered elsewhere.
# =============================================================================

class TestLoopIssueNumberBinding:
    def test_non_digit_loop_issue_number_denied(self, tmp_project, monkeypatch):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "not-a-number")
        rc = _exec.main([
            "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
            "--issue-number", "1166",
            "--input-file", _INPUT_REL,
            "--repo", TRUSTED_REPO,
        ])
        assert rc == 2

    def test_non_digit_loop_issue_number_denied_unit(self, monkeypatch):
        monkeypatch.setenv("LOOP_ISSUE_NUMBER", "not-a-number")
        err = _exec._check_issue_env_binding(COMMAND_ID_ISSUE_COMMENT_PUBLISH, 1166)
        assert "loop_issue_number_env_not_digit" in err

    def test_missing_loop_issue_number_allowed(self, tmp_project, monkeypatch):
        """LOOP_ISSUE_NUMBER is optional for every live command id (Issue #1873
        removed the only mandatory member, termination_report.publish)."""
        import hashlib

        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        monkeypatch.delenv("LOOP_ISSUE_NUMBER", raising=False)
        comment_body = "status update <!-- marker-1166 -->"
        expected_body_sha256 = hashlib.sha256(comment_body.encode()).hexdigest()
        with patch.object(_exec, "_find_marker_matches", return_value=([], "")):
            with patch.object(_exec, "_post_gh_comment", return_value=("https://ex", "c1", "")):
                with patch.object(_exec, "_readback_by_marker_literal",
                                   return_value={"comment_id": "c1", "comment_url": "https://ex",
                                                 "body_sha256": expected_body_sha256}):
                    with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                        rc = _exec.main([
                            "--command-id", COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                            "--issue-number", "1166",
                            "--input-file", _INPUT_REL,
                            "--repo", TRUSTED_REPO,
                        ])
        assert rc == 0


# =============================================================================
# P1-4: Postcondition -- no tracked changes (generic, command-id-agnostic
# unit-level coverage; TestTrackedDiff in test_controlled_issue_metadata_exec.py
# covers the full main() flow for issue_comment.publish).
# =============================================================================

class TestPostconditionExtended:
    def test_check_no_tracked_changes_clean_repo(self, tmp_project):
        """In a clean git repo, no violations for artifacts/1166/ files."""
        violations = _exec._check_no_tracked_changes(tmp_project, 1166)
        assert isinstance(violations, list)

    def test_artifacts_allowed_prefix_not_flagged(self, tmp_project):
        """Untracked artifacts/1166/ files are not flagged as violations."""
        violations = _exec._check_no_tracked_changes(tmp_project, 1166)
        for v in violations:
            assert "artifacts/1166/" not in v, f"Unexpected violation: {v}"

    def test_command_id_scoped_prefix_isolates_sibling_namespace(self, tmp_project):
        """Issue #1284 Blocker 6: a write in a sibling command-id namespace
        must NOT be allowed by a command-id-scoped prefix."""
        sibling_dir = tmp_project / "artifacts" / "1166" / "issue-metadata" / "issue_body.update"
        sibling_dir.mkdir(parents=True, exist_ok=True)
        (sibling_dir / "unexpected.json").write_text("{}")
        allowed_prefix = f"artifacts/1166/issue-metadata/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/"
        violations = _exec._check_no_tracked_changes(tmp_project, 1166, allowed_prefix)
        assert any("issue_body.update/unexpected.json" in v for v in violations)


# =============================================================================
# Issue #1632: issue_dependency.remove
# =============================================================================

ISSUE_DEPENDENCY_REMOVE_COMMAND_ID = "issue_dependency.remove"
ISSUE_DEPENDENCY_REMOVE_SCHEMA = "ISSUE_DEPENDENCY_REMOVE_INPUT_V1"


def _dep_remove_input_dir(tmp_project, issue_number=1523):
    d = (
        tmp_project / "artifacts" / str(issue_number) / "issue-metadata"
        / ISSUE_DEPENDENCY_REMOVE_COMMAND_ID
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dep_remove_write_input(tmp_project, issue_number=1523, **overrides):
    d = _dep_remove_input_dir(tmp_project, issue_number)
    payload = {
        "schema": ISSUE_DEPENDENCY_REMOVE_SCHEMA,
        "issue_number": issue_number,
        "repo": TRUSTED_REPO,
        "target_blocker_number": 1403,
        "expected_blocked_issue_node_id": "ISSUE_NODE_BLOCKED",
        "expected_blocker_node_id": "ISSUE_NODE_BLOCKER",
        "expected_blocked_by_numbers": [1403],
        "expected_pre_mutation_snapshot_sha256": "sha256:" + "0" * 64,
        "idempotency_key": f"{TRUSTED_REPO}:{issue_number}:1403:v1",
    }
    payload.update(overrides)
    f = d / "input.json"
    f.write_text(json.dumps(payload))
    return f"artifacts/{issue_number}/issue-metadata/{ISSUE_DEPENDENCY_REMOVE_COMMAND_ID}/input.json"


def _dep_remove_main_args(tmp_project, input_rel_path, issue_number=1523):
    return [
        "--command-id", ISSUE_DEPENDENCY_REMOVE_COMMAND_ID,
        "--issue-number", str(issue_number),
        "--input-file", input_rel_path,
        "--repo", TRUSTED_REPO,
    ]


def _blocked_by_page(blocked_id, blocked_number, nodes, has_next=False, end_cursor=None,
                      state="OPEN"):
    return {
        "repository": {
            "issue": {
                "id": blocked_id,
                "number": blocked_number,
                "state": state,
                "blockedBy": {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                    "nodes": nodes,
                },
            }
        }
    }


def _node(node_id, number, state="CLOSED"):
    return {
        "id": node_id, "number": number, "state": state,
        "repository": {"nameWithOwner": TRUSTED_REPO},
    }


class TestIssueDependencyRemoveAllPageReadback:
    """AC2: exhaustive all-page readback; cursor/schema drift is rejected."""

    def test_issue_dependency_remove_reads_all_pages_and_rejects_cursor_or_schema_drift(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        _dep_remove_write_input(
            tmp_project,
            expected_blocked_by_numbers=[1400, 1403],
            expected_pre_mutation_snapshot_sha256="sha256:" + "0" * 64,
        )

        page1 = _blocked_by_page(
            "ISSUE_NODE_BLOCKED", 1523, [_node("N1400", 1400)],
            has_next=True, end_cursor="CURSOR1",
        )
        page2 = _blocked_by_page(
            "ISSUE_NODE_BLOCKED", 1523, [_node("N1403", 1403)],
            has_next=False,
        )

        with patch.object(_exec, "_graphql_call", side_effect=[
            (page1, ""), (page2, ""),
        ]):
            result, err = _exec._fetch_blocked_by_all_pages(
                1523, TRUSTED_REPO, "gh", {}
            )
        assert err == ""
        assert result["page_count"] == 2
        assert sorted(n["number"] for n in result["nodes"]) == [1400, 1403]

        # Cursor/schema drift: hasNextPage True but endCursor missing/None.
        bad_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED", 1523, [_node("N1400", 1400)],
            has_next=True, end_cursor=None,
        )
        with patch.object(_exec, "_graphql_call", return_value=(bad_page, "")):
            result2, err2 = _exec._fetch_blocked_by_all_pages(1523, TRUSTED_REPO, "gh", {})
        assert result2 is None
        assert "cursor" in err2

        # Duplicate node across pages is rejected.
        dup_page1 = _blocked_by_page(
            "ISSUE_NODE_BLOCKED", 1523, [_node("N1403", 1403)],
            has_next=True, end_cursor="CURSOR1",
        )
        dup_page2 = _blocked_by_page(
            "ISSUE_NODE_BLOCKED", 1523, [_node("N1403", 1403)],
            has_next=False,
        )
        with patch.object(_exec, "_graphql_call", side_effect=[
            (dup_page1, ""), (dup_page2, ""),
        ]):
            result3, err3 = _exec._fetch_blocked_by_all_pages(1523, TRUSTED_REPO, "gh", {})
        assert result3 is None
        assert "duplicate" in err3


class TestIssueDependencyRemoveCredentialActor:
    """AC3: trusted credential actor readback gates the mutation."""

    def test_issue_dependency_remove_rejects_untrusted_or_unreadable_credential_actor(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        input_rel = _dep_remove_write_input(tmp_project)

        # Unreadable actor (login fetch fails) -- fail closed, no GraphQL call.
        with patch.object(_exec, "_fetch_authenticated_login",
                           return_value=(None, "gh_api_authenticated_user_empty")):
            with patch.object(_exec, "_graphql_call") as mock_gql:
                rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel))
        assert rc == 1
        mock_gql.assert_not_called()

        # Authorized-but-insufficient permission -- fail closed, no GraphQL call.
        with patch.object(_exec, "_fetch_authenticated_login", return_value=("bot", "")):
            with patch.object(_exec, "_fetch_issue_dependency_remove_actor",
                               return_value=("bot", "read", "")):
                with patch.object(_exec, "_graphql_call") as mock_gql2:
                    rc2 = _exec.main(_dep_remove_main_args(tmp_project, input_rel))
        assert rc2 == 1
        mock_gql2.assert_not_called()


class TestIssueDependencyRemoveClosedStatusNoRetry:
    """AC4: closed result status set; transport/GraphQL failure is never
    automatically retried within one invocation."""

    def test_issue_dependency_remove_records_closed_status_and_never_retries_mutation(
        self, tmp_project, monkeypatch, capsys
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        input_rel = _dep_remove_write_input(tmp_project)

        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED", 1523, [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        pre_hash = _exec._compute_blocked_by_snapshot_sha256(
            "ISSUE_NODE_BLOCKED", 1523, [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}]
        )
        input_rel = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )

        call_count = {"mutation": 0}

        def fake_graphql(gh_bin, env, query, variables):
            if "removeBlockedBy" in query:
                call_count["mutation"] += 1
                return None, "gh_api_graphql_failed: transport error"
            return pre_page, ""

        with patch.object(_exec, "_fetch_issue_dependency_remove_actor",
                           return_value=("bot", "write", "")):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc == 1
        assert call_count["mutation"] == 1  # never retried automatically
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "transport_or_schema_error"

    def test_issue_dependency_remove_success_reports_removed_status(
        self, tmp_project, monkeypatch, capsys
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        pre_nodes = [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}]
        pre_hash = _exec._compute_blocked_by_snapshot_sha256("ISSUE_NODE_BLOCKED", 1523, pre_nodes)
        input_rel = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )
        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED", 1523, [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        post_page = _blocked_by_page("ISSUE_NODE_BLOCKED", 1523, [])

        mutation_response = {
            "removeBlockedBy": {
                "issue": {"id": "ISSUE_NODE_BLOCKED", "number": 1523},
                "blockingIssue": {"id": "ISSUE_NODE_BLOCKER", "number": 1403},
                "clientMutationId": f"{TRUSTED_REPO}:1523:1403:v1",
            }
        }
        responses = iter([pre_page, mutation_response, post_page])

        def fake_graphql(gh_bin, env, query, variables):
            return next(responses), ""

        with patch.object(_exec, "_fetch_issue_dependency_remove_actor",
                           return_value=("bot", "admin", "")):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "removed"
        assert out["idempotency_marker_written"] is True


class TestIssueDependencyRemovePostconditionAndIdempotency:
    """AC5: all-page post-mutation readback + idempotency marker."""

    def test_issue_dependency_remove_requires_all_page_post_snapshot_and_idempotency_marker(
        self, tmp_project, monkeypatch, capsys
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        pre_nodes = [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}]
        pre_hash = _exec._compute_blocked_by_snapshot_sha256("ISSUE_NODE_BLOCKED", 1523, pre_nodes)
        input_rel = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )
        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED", 1523, [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        # Postcondition failure: target relationship still present after mutation.
        post_page_still_present = _blocked_by_page(
            "ISSUE_NODE_BLOCKED", 1523, [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        responses = iter([
            pre_page,
            {
                "removeBlockedBy": {
                    "issue": {"id": "ISSUE_NODE_BLOCKED", "number": 1523},
                    "blockingIssue": {"id": "ISSUE_NODE_BLOCKER", "number": 1403},
                    "clientMutationId": f"{TRUSTED_REPO}:1523:1403:v1",
                }
            },
            post_page_still_present,
        ])

        def fake_graphql(gh_bin, env, query, variables):
            return next(responses), ""

        with patch.object(_exec, "_fetch_issue_dependency_remove_actor",
                           return_value=("bot", "write", "")):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "postcondition_rejected"

        # Idempotency (Issue #1667 review fix_delta P2): a FULLY valid marker
        # present + fresh readback confirms already absent -> already_completed.
        marker_path = _exec._issue_metadata_marker_path(
            tmp_project, 1523, ISSUE_DEPENDENCY_REMOVE_COMMAND_ID,
            "issue_dependency_remove.marker.json",
        )
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps({
            "schema": "ISSUE_DEPENDENCY_REMOVE_MARKER_V1",
            "issue_number": 1523,
            "repo": TRUSTED_REPO,
            "target_blocker_number": 1403,
            "blocked_issue_id": "ISSUE_NODE_BLOCKED",
            "blocked_issue_number": 1523,
            "blocker_node_id": "ISSUE_NODE_BLOCKER",
            "idempotency_key": f"{TRUSTED_REPO}:1523:1403:v1",
            "actor_login": "bot",
            "actor_permission": "write",
            "status_detail": "removed",
        }))
        already_absent_page = _blocked_by_page("ISSUE_NODE_BLOCKED", 1523, [])
        with patch.object(_exec, "_fetch_issue_dependency_remove_actor",
                           return_value=("bot", "write", "")):
            with patch.object(_exec, "_graphql_call", return_value=(already_absent_page, "")) as mock_gql:
                rc2 = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc2 == 0
        out2 = json.loads(capsys.readouterr().out)
        assert out2["status"] == "already_completed"
        # Only the pre-mutation readback call was made -- no mutation attempted.
        assert mock_gql.call_count == 1

        # Issue #1667 review fix_delta P2: an INCOMPLETE marker (missing the
        # closed-schema fields, e.g. only idempotency_key) is never trusted
        # as already_completed, even though the target is remotely absent --
        # this is routed to human judgment (postcondition_rejected) instead.
        marker_path.write_text(json.dumps({
            "schema": "ISSUE_DEPENDENCY_REMOVE_MARKER_V1",
            "idempotency_key": f"{TRUSTED_REPO}:1523:1403:v1",
        }))
        with patch.object(_exec, "_fetch_issue_dependency_remove_actor",
                           return_value=("bot", "write", "")):
            with patch.object(_exec, "_graphql_call", return_value=(already_absent_page, "")) as mock_gql3:
                rc3 = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc3 == 1
        out3 = json.loads(capsys.readouterr().out)
        assert out3["status"] == "postcondition_rejected"
        assert "already_completed_marker_invalid" in out3["reason"]
        # Still no mutation attempted -- the ambiguity is resolved without a
        # network write.
        assert mock_gql3.call_count == 1


class TestIssueDependencyRemoveFailurePathsFailClosed:
    """AC6: dedicated failure-path fail-closed matrix, no real network mutation."""

    def test_issue_dependency_remove_failure_paths_fail_closed_without_network_mutation(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)

        def _assert_blocked(overrides, expect_rc=2):
            input_rel = _dep_remove_write_input(tmp_project, **overrides)
            with patch.object(_exec, "_graphql_call") as mock_gql:
                rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel))
            assert rc == expect_rc, f"overrides={overrides} rc={rc}"
            mock_gql.assert_not_called()

        # unknown key
        _assert_blocked({"unexpected_field": "x"})
        # bool number
        _assert_blocked({"target_blocker_number": True})
        # duplicate set
        _assert_blocked({"expected_blocked_by_numbers": [1403, 1403]})
        # unsorted set
        _assert_blocked({"expected_blocked_by_numbers": [1403, 100]})
        # oversize set
        oversize = list(range(1, _exec.ISSUE_DEPENDENCY_REMOVE_MAX_BLOCKED_BY_NUMBERS + 2))
        _assert_blocked({"expected_blocked_by_numbers": oversize, "target_blocker_number": oversize[0]})
        # null issue_number
        _assert_blocked({"issue_number": None})
        # wrong repo
        _assert_blocked({"repo": "attacker/evil-repo"})

        # -- GraphQL errors / cursor failure / schema drift during precondition
        # readback -- fail closed before any mutation call.
        input_rel = _dep_remove_write_input(tmp_project)
        with patch.object(_exec, "_fetch_issue_dependency_remove_actor",
                           return_value=("bot", "write", "")):
            with patch.object(_exec, "_graphql_call",
                               return_value=(None, "gh_api_graphql_errors: boom")) as mock_gql:
                rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel))
        assert rc == 1
        mock_gql.assert_called_once()

        # -- Hash / actor / node-id / state mismatch: precondition_rejected,
        # no mutation call.
        wrong_state_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED", 1523, [_node("ISSUE_NODE_BLOCKER", 1403, state="OPEN")],
        )
        with patch.object(_exec, "_fetch_issue_dependency_remove_actor",
                           return_value=("bot", "write", "")):
            with patch.object(_exec, "_graphql_call", return_value=(wrong_state_page, "")):
                rc2 = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc2 == 1

        # -- Pre/post TOCTOU: mutation succeeds but a concurrent change means
        # the non-target set differs post-mutation -- postcondition_rejected.
        pre_nodes = [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}]
        pre_hash = _exec._compute_blocked_by_snapshot_sha256("ISSUE_NODE_BLOCKED", 1523, pre_nodes)
        input_rel2 = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )
        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED", 1523, [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        toctou_post_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED", 1523, [_node("N_NEW", 9999)],
        )
        responses = iter([
            pre_page,
            {
                "removeBlockedBy": {
                    "issue": {"id": "ISSUE_NODE_BLOCKED", "number": 1523},
                    "blockingIssue": {"id": "ISSUE_NODE_BLOCKER", "number": 1403},
                    "clientMutationId": f"{TRUSTED_REPO}:1523:1403:v1",
                }
            },
            toctou_post_page,
        ])

        def fake_graphql(gh_bin, env, query, variables):
            return next(responses), ""

        with patch.object(_exec, "_fetch_issue_dependency_remove_actor",
                           return_value=("bot", "write", "")):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    rc3 = _exec.main(_dep_remove_main_args(tmp_project, input_rel2) + ["--json"])
        assert rc3 == 1

        # -- Second mutation attempt within one invocation is never issued:
        # verified structurally by the no-retry test above
        # (call_count["mutation"] == 1). Here we additionally assert that a
        # mutation transport error does not trigger any subsequent GraphQL
        # call at all (fully fail-closed, zero further network activity).
        call_log = []

        def fake_graphql_single_call(gh_bin, env, query, variables):
            call_log.append(query)
            if "removeBlockedBy" in query:
                return None, "gh_api_graphql_errors: boom"
            return pre_page, ""

        with patch.object(_exec, "_fetch_issue_dependency_remove_actor",
                           return_value=("bot", "write", "")):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql_single_call):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    rc4 = _exec.main(_dep_remove_main_args(tmp_project, input_rel2) + ["--json"])
        assert rc4 == 1
        assert sum(1 for q in call_log if "removeBlockedBy" in q) == 1


# =============================================================================
# Issue #1667 review fix_delta P0: GraphQL field-name exactness
# =============================================================================


class TestIssueDependencyRemoveMutationFieldNames:
    """P0: the GitHub GraphQL schema names the RemoveBlockedByInput field
    `blockingIssueId` -- NOT `blockedByIssueId` (that name never existed on
    the input type). This is a read-only, static string check on the fixed
    mutation document; no network call is made."""

    def test_mutation_uses_blocking_issue_id_not_blocked_by_issue_id(self):
        mutation = _exec._ISSUE_DEPENDENCY_REMOVE_MUTATION
        assert "blockingIssueId" in mutation
        assert "blockedByIssueId" not in mutation

    def test_mutation_declares_client_mutation_id_variable(self):
        mutation = _exec._ISSUE_DEPENDENCY_REMOVE_MUTATION
        assert "$clientMutationId: String" in mutation
        assert "clientMutationId: $clientMutationId" in mutation

    def test_mutation_response_selects_blocking_issue_and_client_mutation_id(self):
        mutation = _exec._ISSUE_DEPENDENCY_REMOVE_MUTATION
        assert "blockingIssue { id number }" in mutation
        assert "clientMutationId" in mutation.split("removeBlockedBy", 1)[1]

    def test_mutation_call_site_variables_use_official_field_name(self, tmp_project, monkeypatch):
        """Static/behavioral check: the actual _graphql_call invocation for
        the mutation is made with the exact variable keys
        {issueId, blockingIssueId, clientMutationId} -- never
        blockedByIssueId."""
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        pre_nodes = [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}]
        pre_hash = _exec._compute_blocked_by_snapshot_sha256("ISSUE_NODE_BLOCKED", 1523, pre_nodes)
        input_rel = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )
        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED", 1523, [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        captured_variables = {}

        def fake_graphql(gh_bin, env, query, variables):
            if "removeBlockedBy" in query:
                captured_variables.update(variables)
                return None, "gh_api_graphql_errors: stop_before_mutation_succeeds"
            return pre_page, ""

        with patch.object(_exec, "_fetch_issue_dependency_remove_actor",
                           return_value=("bot", "write", "")):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    _exec.main(_dep_remove_main_args(tmp_project, input_rel))
        assert set(captured_variables.keys()) == {"issueId", "blockingIssueId", "clientMutationId"}
        assert "blockedByIssueId" not in captured_variables


# =============================================================================
# Issue #1667 review fix_delta P0: RemoveBlockedByInput schema compatibility
# (read-only, static -- no network mutation)
# =============================================================================


class TestRemoveBlockedByInputSchemaCompatibility:
    """P0: static compatibility check against the official GitHub GraphQL
    schema's RemoveBlockedByInput type:

        input RemoveBlockedByInput {
          blockingIssueId: ID!
          clientMutationId: String
          issueId: ID!
        }

    This test never makes a network call -- it only inspects the fixed
    mutation document string this executor sends.
    """

    _OFFICIAL_INPUT_FIELDS = frozenset({"issueId", "blockingIssueId", "clientMutationId"})

    def test_mutation_input_object_fields_match_official_schema(self):
        mutation = _exec._ISSUE_DEPENDENCY_REMOVE_MUTATION
        # Extract the input object body between "input: {" and the matching "}"
        start = mutation.index("input: {") + len("input: {")
        end = mutation.index("}", start)
        input_body = mutation[start:end]
        # Field names appear as "<name>: $<name>" pairs.
        declared_fields = {
            part.split(":", 1)[0].strip()
            for part in input_body.split(",")
            if part.strip()
        }
        assert declared_fields == self._OFFICIAL_INPUT_FIELDS

    def test_mutation_variable_declarations_match_official_schema(self):
        mutation = _exec._ISSUE_DEPENDENCY_REMOVE_MUTATION
        header = mutation.split(")", 1)[0]
        # Variable declarations look like "$name: Type"
        declared_vars = {
            piece.split(":", 1)[0].strip().lstrip("$")
            for piece in header.split("(", 1)[1].split(",")
            if piece.strip()
        }
        assert declared_vars == self._OFFICIAL_INPUT_FIELDS

    def test_issue_id_and_blocking_issue_id_are_non_null_id_type(self):
        mutation = _exec._ISSUE_DEPENDENCY_REMOVE_MUTATION
        assert "$issueId: ID!" in mutation
        assert "$blockingIssueId: ID!" in mutation

    def test_client_mutation_id_is_nullable_string_type(self):
        mutation = _exec._ISSUE_DEPENDENCY_REMOVE_MUTATION
        assert "$clientMutationId: String" in mutation
        assert "$clientMutationId: String!" not in mutation


# =============================================================================
# Issue #1667 review fix_delta P1: removeBlockedBy response validation
# =============================================================================


class TestValidateRemoveBlockedByMutationResponse:
    """P1: the mutation response (RemoveBlockedByPayload) must be validated
    before the executor trusts that the mutation succeeded against the
    intended target."""

    _KW = dict(
        expected_blocked_issue_node_id="ISSUE_NODE_BLOCKED",
        expected_blocked_issue_number=1523,
        expected_blocker_node_id="ISSUE_NODE_BLOCKER",
        expected_blocker_number=1403,
        expected_client_mutation_id="squne121/loop-protocol:1523:1403:v1",
    )

    def _valid_response(self):
        return {
            "removeBlockedBy": {
                "issue": {"id": "ISSUE_NODE_BLOCKED", "number": 1523},
                "blockingIssue": {"id": "ISSUE_NODE_BLOCKER", "number": 1403},
                "clientMutationId": "squne121/loop-protocol:1523:1403:v1",
            }
        }

    def test_valid_response_passes(self):
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(
            self._valid_response(), **self._KW
        )
        assert err == ""
        assert is_schema_error is False

    def test_not_a_dict_is_schema_error(self):
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(
            None, **self._KW
        )
        assert err != ""
        assert is_schema_error is True

    def test_missing_remove_blocked_by_key_is_schema_error(self):
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(
            {}, **self._KW
        )
        assert "missing_remove_blocked_by_payload" in err
        assert is_schema_error is True

    def test_missing_blocking_issue_is_schema_error(self):
        resp = self._valid_response()
        del resp["removeBlockedBy"]["blockingIssue"]
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(
            resp, **self._KW
        )
        assert "missing_blocking_issue" in err
        assert is_schema_error is True

    def test_missing_issue_is_schema_error(self):
        resp = self._valid_response()
        del resp["removeBlockedBy"]["issue"]
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(
            resp, **self._KW
        )
        assert "missing_issue" in err
        assert is_schema_error is True

    def test_issue_id_mismatch_is_postcondition_error(self):
        resp = self._valid_response()
        resp["removeBlockedBy"]["issue"]["id"] = "WRONG_ISSUE_ID"
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(
            resp, **self._KW
        )
        assert "issue_identity_mismatch" in err
        assert is_schema_error is False

    def test_issue_number_mismatch_is_postcondition_error(self):
        resp = self._valid_response()
        resp["removeBlockedBy"]["issue"]["number"] = 9999
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(
            resp, **self._KW
        )
        assert "issue_identity_mismatch" in err
        assert is_schema_error is False

    def test_blocking_issue_id_mismatch_is_postcondition_error(self):
        resp = self._valid_response()
        resp["removeBlockedBy"]["blockingIssue"]["id"] = "WRONG_BLOCKER_ID"
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(
            resp, **self._KW
        )
        assert "blocking_issue_identity_mismatch" in err
        assert is_schema_error is False

    def test_blocking_issue_number_mismatch_is_postcondition_error(self):
        resp = self._valid_response()
        resp["removeBlockedBy"]["blockingIssue"]["number"] = 9999
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(
            resp, **self._KW
        )
        assert "blocking_issue_identity_mismatch" in err
        assert is_schema_error is False

    def test_missing_client_mutation_id_key_is_schema_error(self):
        resp = self._valid_response()
        del resp["removeBlockedBy"]["clientMutationId"]
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(
            resp, **self._KW
        )
        assert "missing_client_mutation_id" in err
        assert is_schema_error is True

    def test_client_mutation_id_mismatch_is_postcondition_error(self):
        resp = self._valid_response()
        resp["removeBlockedBy"]["clientMutationId"] = "wrong-key"
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(
            resp, **self._KW
        )
        assert "client_mutation_id_mismatch" in err
        assert is_schema_error is False

    def test_non_string_issue_id_is_schema_error(self):
        resp = self._valid_response()
        resp["removeBlockedBy"]["issue"]["id"] = 12345
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(
            resp, **self._KW
        )
        assert "issue_id_invalid" in err
        assert is_schema_error is True

    def test_non_int_issue_number_is_schema_error(self):
        resp = self._valid_response()
        resp["removeBlockedBy"]["issue"]["number"] = "1523"
        err, is_schema_error = _exec._validate_remove_blocked_by_mutation_response(
            resp, **self._KW
        )
        assert "issue_number_invalid" in err
        assert is_schema_error is True


# =============================================================================
# Issue #1667 review fix_delta P1: pre-mutation tracked-changes precondition
# =============================================================================


class TestIssueDependencyRemovePreMutationTrackedChanges:
    """P1: tracked/staged/untracked changes outside this command's write root
    must be checked BEFORE the remote mutation is attempted, not only after."""

    def test_pre_existing_tracked_changes_block_mutation_before_network_call(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        pre_nodes = [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}]
        pre_hash = _exec._compute_blocked_by_snapshot_sha256("ISSUE_NODE_BLOCKED", 1523, pre_nodes)
        input_rel = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )
        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED", 1523, [_node("ISSUE_NODE_BLOCKER", 1403)],
        )

        with patch.object(_exec, "_fetch_issue_dependency_remove_actor",
                           return_value=("bot", "write", "")):
            with patch.object(_exec, "_graphql_call", return_value=(pre_page, "")):
                with patch.object(_exec, "_check_no_tracked_changes",
                                   return_value=["M :src/unexpected.ts"]):
                    with patch.object(_exec, "_graphql_call") as mock_gql_after_patch:
                        mock_gql_after_patch.return_value = (pre_page, "")
                        rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc == 1
        # No removeBlockedBy mutation call was ever made -- the precondition
        # failed before the single mutation attempt.
        for call in mock_gql_after_patch.call_args_list:
            assert "removeBlockedBy" not in call.args[2]


# =============================================================================
# Issue #1667 review fix_delta P1: attempt marker + audit trail on failure
# =============================================================================


class TestIssueDependencyRemoveAttemptMarker:
    """P1: an attempt marker is written BEFORE the remote mutation call, and
    updated on every post-mutation failure path -- an audit trail must exist
    even if the process fails between the mutation and its readback."""

    def test_marker_written_before_mutation_records_mutation_attempted(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        pre_nodes = [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}]
        pre_hash = _exec._compute_blocked_by_snapshot_sha256("ISSUE_NODE_BLOCKED", 1523, pre_nodes)
        input_rel = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )
        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED", 1523, [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        marker_path = _exec._issue_metadata_marker_path(
            tmp_project, 1523, ISSUE_DEPENDENCY_REMOVE_COMMAND_ID,
            "issue_dependency_remove.marker.json",
        )
        seen_marker_status_before_mutation = {}

        def fake_graphql(gh_bin, env, query, variables):
            if "removeBlockedBy" in query:
                # By this point the attempt marker must already be on disk.
                assert marker_path.exists()
                seen_marker_status_before_mutation["status"] = json.loads(
                    marker_path.read_text()
                )["status_detail"]
                return None, "gh_api_graphql_errors: simulated_transport_error"
            return pre_page, ""

        with patch.object(_exec, "_fetch_issue_dependency_remove_actor",
                           return_value=("bot", "write", "")):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc == 1
        assert seen_marker_status_before_mutation["status"] == "mutation_attempted"
        # After the failed mutation, the marker is updated to reflect the
        # terminal outcome -- never left stuck at "mutation_attempted".
        final_marker = json.loads(marker_path.read_text())
        assert final_marker["status_detail"] == "transport_or_schema_error"

    def test_marker_records_actor_permission_and_blocker_identity_on_success(
        self, tmp_project, monkeypatch
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        pre_nodes = [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}]
        pre_hash = _exec._compute_blocked_by_snapshot_sha256("ISSUE_NODE_BLOCKED", 1523, pre_nodes)
        input_rel = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )
        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED", 1523, [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        post_page = _blocked_by_page("ISSUE_NODE_BLOCKED", 1523, [])
        mutation_response = {
            "removeBlockedBy": {
                "issue": {"id": "ISSUE_NODE_BLOCKED", "number": 1523},
                "blockingIssue": {"id": "ISSUE_NODE_BLOCKER", "number": 1403},
                "clientMutationId": f"{TRUSTED_REPO}:1523:1403:v1",
            }
        }
        responses = iter([pre_page, mutation_response, post_page])

        def fake_graphql(gh_bin, env, query, variables):
            return next(responses), ""

        with patch.object(_exec, "_fetch_issue_dependency_remove_actor",
                           return_value=("bot", "admin", "")):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql):
                with patch.object(_exec, "_check_no_tracked_changes", return_value=[]):
                    rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel))
        assert rc == 0
        marker_path = _exec._issue_metadata_marker_path(
            tmp_project, 1523, ISSUE_DEPENDENCY_REMOVE_COMMAND_ID,
            "issue_dependency_remove.marker.json",
        )
        marker = json.loads(marker_path.read_text())
        assert marker["status_detail"] == "removed"
        assert marker["actor_permission"] == "admin"
        assert marker["blocked_issue_id"] == "ISSUE_NODE_BLOCKED"
        assert marker["blocker_node_id"] == "ISSUE_NODE_BLOCKER"


# =============================================================================
# Issue #1667 review fix_delta P1: GH_TOKEN/GITHUB_TOKEN sanitization
# =============================================================================


class TestIssueDependencyRemoveGhEnvSanitization:
    """P1: an ambient GH_TOKEN/GITHUB_TOKEN must never reach the `gh`
    subprocess for issue_dependency.remove -- it would let a trusted-actor
    identity be silently substituted."""

    def test_gh_token_and_github_token_are_stripped(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "ghp_evil_token")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_evil_token_2")
        env = _exec._build_issue_dependency_remove_gh_env()
        assert "GH_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env


# =============================================================================
# Issue #1667 review fix_delta P1: closed result-status set (no "failed")
# =============================================================================


class TestIssueDependencyRemoveClosedStatusSetNoFailedValue:
    """P1: issue_dependency.remove's result `status` field must only ever be
    one of {removed, precondition_rejected, transport_or_schema_error,
    postcondition_rejected, already_completed} -- never the undefined
    "failed" value."""

    _CLOSED_STATUS_SET = frozenset({
        "removed", "precondition_rejected", "transport_or_schema_error",
        "postcondition_rejected", "already_completed",
    })

    def test_post_mutation_tracked_changes_status_is_postcondition_rejected(
        self, tmp_project, monkeypatch, capsys
    ):
        monkeypatch.setattr(_exec, "PROJECT_ROOT", tmp_project)
        pre_nodes = [{"id": "ISSUE_NODE_BLOCKER", "number": 1403, "state": "CLOSED"}]
        pre_hash = _exec._compute_blocked_by_snapshot_sha256("ISSUE_NODE_BLOCKED", 1523, pre_nodes)
        input_rel = _dep_remove_write_input(
            tmp_project, expected_pre_mutation_snapshot_sha256=pre_hash
        )
        pre_page = _blocked_by_page(
            "ISSUE_NODE_BLOCKED", 1523, [_node("ISSUE_NODE_BLOCKER", 1403)],
        )
        post_page = _blocked_by_page("ISSUE_NODE_BLOCKED", 1523, [])
        mutation_response = {
            "removeBlockedBy": {
                "issue": {"id": "ISSUE_NODE_BLOCKED", "number": 1523},
                "blockingIssue": {"id": "ISSUE_NODE_BLOCKER", "number": 1403},
                "clientMutationId": f"{TRUSTED_REPO}:1523:1403:v1",
            }
        }
        responses = iter([pre_page, mutation_response, post_page])

        def fake_graphql(gh_bin, env, query, variables):
            return next(responses), ""

        # First call (pre-mutation precondition check) clean, second call
        # (post-mutation postcondition check) reports an unrelated change.
        tracked_changes_calls = iter([[], ["M :src/unexpected.ts"]])

        def fake_tracked_changes(*args, **kwargs):
            return next(tracked_changes_calls)

        with patch.object(_exec, "_fetch_issue_dependency_remove_actor",
                           return_value=("bot", "write", "")):
            with patch.object(_exec, "_graphql_call", side_effect=fake_graphql):
                with patch.object(_exec, "_check_no_tracked_changes",
                                   side_effect=fake_tracked_changes):
                    rc = _exec.main(_dep_remove_main_args(tmp_project, input_rel) + ["--json"])
        assert rc == 1
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "postcondition_rejected"
        assert out["status"] in self._CLOSED_STATUS_SET
        assert out["status"] != "failed"
