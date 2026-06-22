"""
test_anchor_scope_reframe_preflight.py

AC3: GitHub API author_association OWNER/MEMBER/COLLABORATOR → trusted anchor
AC4: CONTRIBUTOR / NONE / missing metadata / wrong issue / wrong repo /
     multiple anchor URL / malformed schema / quoted marker / fenced-code marker → fail-closed
AC5: trusted anchor generates scope_delta_decision with implementation_go=false
     and required_rerun
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
SCHEMAS_DIR = SKILL_ROOT / "schemas"

sys.path.insert(0, str(SCRIPTS_DIR))

import run_refinement_preflight as preflight

# Production functions under test
_parse_anchor_scope_reframe_body = preflight._parse_anchor_scope_reframe_body
_classify_anchor_scope_reframe = preflight._classify_anchor_scope_reframe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TRUSTED_ASSOCIATIONS = ["OWNER", "MEMBER", "COLLABORATOR"]
UNTRUSTED_ASSOCIATIONS = ["CONTRIBUTOR", "NONE", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER"]

TARGET_REPO = "squne121/loop-protocol"
TARGET_ISSUE = 920

VALID_ANCHOR_PAYLOAD = {
    "schema_version": "ANCHOR_SCOPE_REFRAME_V1",
    "target": {
        "repo": TARGET_REPO,
        "issue_number": TARGET_ISSUE,
    },
    "decision": "approve_scope_delta",
    "allowed_path_deltas": [
        ".claude/skills/issue-refinement-loop/schemas/anchor_scope_reframe_v1.schema.json"
    ],
    "rationale": "Adding anchor schema for scope signal fix.",
    "required_rerun": ["contract_review", "refinement_preflight", "allowed_paths_gate"],
}

VALID_YAML_BLOCK = (
    "```yaml\n"
    + "schema_version: ANCHOR_SCOPE_REFRAME_V1\n"
    + "target:\n"
    + f"  repo: {TARGET_REPO}\n"
    + f"  issue_number: {TARGET_ISSUE}\n"
    + "decision: approve_scope_delta\n"
    + "allowed_path_deltas:\n"
    + "  - .claude/skills/issue-refinement-loop/schemas/anchor_scope_reframe_v1.schema.json\n"
    + "rationale: Testing trusted anchor.\n"
    + "required_rerun:\n"
    + "  - contract_review\n"
    + "  - refinement_preflight\n"
    + "```\n"
)


def _make_comment(
    comment_id: int,
    body: str,
    author_association: str,
    issue_number: int = TARGET_ISSUE,
    repo: str = TARGET_REPO,
) -> dict:
    """Build a GitHub API-like comment dict."""
    return {
        "id": comment_id,
        "body": body,
        "author_association": author_association,
        "user": {"login": "test-user"},
        "issue_url": f"https://api.github.com/repos/{repo}/issues/{issue_number}",
    }


def _make_anchor_url(
    comment_id: int,
    issue_number: int = TARGET_ISSUE,
    repo: str = TARGET_REPO,
) -> str:
    return f"https://github.com/{repo}/issues/{issue_number}#issuecomment-{comment_id}"


def _classify(comment: dict, repo: str = TARGET_REPO, issue_number: int = TARGET_ISSUE) -> dict:
    """Convenience wrapper: call production function with comment body."""
    anchor_url = _make_anchor_url(comment["id"], issue_number=issue_number, repo=repo)
    return _classify_anchor_scope_reframe(
        comment_payload=comment,
        anchor_body=comment.get("body", ""),
        repo=repo,
        issue_number=issue_number,
        anchor_url=anchor_url,
    )


# ---------------------------------------------------------------------------
# AC3: trusted anchor — OWNER / MEMBER / COLLABORATOR
# ---------------------------------------------------------------------------


class TestTrustedAnchor:
    @pytest.mark.parametrize("author_association", TRUSTED_ASSOCIATIONS)
    def test_trusted_anchor_owner_member_collaborator(self, author_association):
        """AC3: OWNER, MEMBER, COLLABORATOR → approved_by_trusted_anchor"""
        comment = _make_comment(
            comment_id=1001,
            body=VALID_YAML_BLOCK,
            author_association=author_association,
        )
        result = _classify(comment)
        assert result["status"] == "approved_by_trusted_anchor", (
            f"Expected approved_by_trusted_anchor for {author_association}, got: {result}"
        )
        assert result["implementation_go"] is False
        assert result["anchor_author_association"] == author_association

    def test_trusted_anchor_has_required_rerun(self):
        """AC3: trusted anchor result includes required_rerun"""
        comment = _make_comment(
            comment_id=1002,
            body=VALID_YAML_BLOCK,
            author_association="OWNER",
        )
        result = _classify(comment)
        assert result["required_rerun"], "trusted anchor must have required_rerun"
        assert "contract_review" in result["required_rerun"]
        assert "refinement_preflight" in result["required_rerun"]

    def test_trusted_anchor_has_allowed_path_deltas(self):
        """AC3: trusted anchor result includes allowed_path_deltas"""
        comment = _make_comment(
            comment_id=1003,
            body=VALID_YAML_BLOCK,
            author_association="COLLABORATOR",
        )
        result = _classify(comment)
        assert "allowed_path_deltas" in result
        assert len(result["allowed_path_deltas"]) > 0

    def test_trusted_anchor_has_anchor_comment_url(self):
        """AC3: scope_delta_decision.anchor_comment_url is set"""
        comment = _make_comment(
            comment_id=1004,
            body=VALID_YAML_BLOCK,
            author_association="MEMBER",
        )
        result = _classify(comment)
        assert result.get("anchor_comment_url"), "anchor_comment_url must be set in scope_delta_decision"

    def test_trusted_anchor_has_anchor_comment_hash(self):
        """AC3: scope_delta_decision.anchor_comment_hash is set (raw body hash)"""
        comment = _make_comment(
            comment_id=1005,
            body=VALID_YAML_BLOCK,
            author_association="OWNER",
        )
        result = _classify(comment)
        assert result.get("anchor_comment_hash"), "anchor_comment_hash must be set"

    def test_parse_anchor_scope_reframe_valid_yaml_block(self):
        """_parse_anchor_scope_reframe_body returns parsed dict for valid block"""
        parsed = _parse_anchor_scope_reframe_body(VALID_YAML_BLOCK)
        assert parsed is not None
        assert parsed["schema_version"] == "ANCHOR_SCOPE_REFRAME_V1"

    def test_parse_anchor_scope_reframe_not_found_returns_none(self):
        """_parse_anchor_scope_reframe_body returns None when no ANCHOR_SCOPE_REFRAME_V1 found"""
        body = "Just a regular comment without any yaml block."
        parsed = _parse_anchor_scope_reframe_body(body)
        assert parsed is None

    def test_parse_anchor_scope_reframe_wrong_schema_version_returns_none(self):
        """_parse_anchor_scope_reframe_body returns None for wrong schema_version"""
        body = (
            "```yaml\n"
            "schema_version: WRONG_SCHEMA_V1\n"
            "```\n"
        )
        parsed = _parse_anchor_scope_reframe_body(body)
        assert parsed is None


# ---------------------------------------------------------------------------
# AC4: fail-closed — untrusted / wrong / malformed
# ---------------------------------------------------------------------------


class TestFailClosed:
    @pytest.mark.parametrize("author_association", UNTRUSTED_ASSOCIATIONS)
    def test_untrusted_author_association_fail_closed(self, author_association):
        """AC4: CONTRIBUTOR, NONE, etc. → fail_closed"""
        comment = _make_comment(
            comment_id=2001,
            body=VALID_YAML_BLOCK,
            author_association=author_association,
        )
        result = _classify(comment)
        assert result["status"] == "fail_closed", (
            f"Expected fail_closed for {author_association}, got: {result}"
        )
        assert result["implementation_go"] is False

    def test_missing_author_association_fail_closed(self):
        """AC4: missing author_association → fail_closed"""
        comment = {
            "id": 2002,
            "body": VALID_YAML_BLOCK,
            "user": {"login": "test-user"},
            "issue_url": f"https://api.github.com/repos/{TARGET_REPO}/issues/{TARGET_ISSUE}",
        }
        result = _classify(comment)
        assert result["status"] == "fail_closed"

    def test_wrong_repo_fail_closed(self):
        """AC4: target.repo mismatch → fail_closed"""
        comment = _make_comment(
            comment_id=2003,
            body=(
                "```yaml\n"
                "schema_version: ANCHOR_SCOPE_REFRAME_V1\n"
                "target:\n"
                f"  repo: other-owner/other-repo\n"
                f"  issue_number: {TARGET_ISSUE}\n"
                "decision: approve_scope_delta\n"
                "allowed_path_deltas:\n"
                "  - .claude/skills/test.md\n"
                "rationale: Wrong repo test.\n"
                "required_rerun:\n"
                "  - contract_review\n"
                "```\n"
            ),
            author_association="OWNER",
        )
        result = _classify(comment)
        assert result["status"] == "fail_closed"

    def test_wrong_issue_number_fail_closed(self):
        """AC4: target.issue_number mismatch → fail_closed"""
        comment = _make_comment(
            comment_id=2004,
            body=(
                "```yaml\n"
                "schema_version: ANCHOR_SCOPE_REFRAME_V1\n"
                "target:\n"
                f"  repo: {TARGET_REPO}\n"
                "  issue_number: 99999\n"
                "decision: approve_scope_delta\n"
                "allowed_path_deltas:\n"
                "  - .claude/skills/test.md\n"
                "rationale: Wrong issue test.\n"
                "required_rerun:\n"
                "  - contract_review\n"
                "```\n"
            ),
            author_association="OWNER",
        )
        result = _classify(comment)
        assert result["status"] == "fail_closed"

    def test_no_yaml_block_fail_closed(self):
        """AC4: comment with no ANCHOR_SCOPE_REFRAME_V1 yaml → fail_closed"""
        comment = _make_comment(
            comment_id=2005,
            body="Just a plain comment without schema block.",
            author_association="OWNER",
        )
        result = _classify(comment)
        assert result["status"] == "fail_closed"
        assert result["reason"] == "no_anchor_scope_reframe_v1_payload"

    def test_quoted_yaml_block_fail_closed(self):
        """AC4: blockquote-embedded yaml block → fail_closed (not canonical)"""
        quoted_body = "> ```yaml\n> schema_version: ANCHOR_SCOPE_REFRAME_V1\n> ```\n"
        comment = _make_comment(
            comment_id=2006,
            body=quoted_body,
            author_association="OWNER",
        )
        result = _classify(comment)
        assert result["status"] == "fail_closed"

    def test_malformed_yaml_fail_closed(self):
        """AC4: malformed YAML → fail_closed (parse returns None)"""
        comment = _make_comment(
            comment_id=2007,
            body="```yaml\n: invalid: [yaml: content\n```\n",
            author_association="OWNER",
        )
        result = _classify(comment)
        assert result["status"] == "fail_closed"

    def test_implementation_go_always_false(self):
        """AC4: implementation_go is always false even for fail_closed"""
        comment = _make_comment(
            comment_id=2008,
            body=VALID_YAML_BLOCK,
            author_association="NONE",
        )
        result = _classify(comment)
        assert result["implementation_go"] is False

    def test_multiple_anchor_urls_not_supported(self):
        """AC4: multiple anchor URLs → preflight fail_closed (batch validation)"""
        # This is tested at the preflight batch level via _validate_anchor_comments_batch
        urls = [
            _make_anchor_url(3001),
            _make_anchor_url(3002),
        ]
        # _validate_anchor_comments_batch returns blockers for multiple URLs
        sorted_urls, blockers = preflight._validate_anchor_comments_batch(
            urls,
            repo=TARGET_REPO,
            issue_number=TARGET_ISSUE,
        )
        assert blockers, "Expected blockers for multiple anchor URLs, got none"


# ---------------------------------------------------------------------------
# AC5: scope_delta_decision structure
# ---------------------------------------------------------------------------


class TestScopeDeltaDecision:
    def test_approved_trusted_anchor_scope_delta_decision(self):
        """AC5: trusted anchor generates scope_delta_decision with approved_by_trusted_anchor"""
        comment = _make_comment(
            comment_id=4001,
            body=VALID_YAML_BLOCK,
            author_association="OWNER",
        )
        result = _classify(comment)
        assert result["status"] == "approved_by_trusted_anchor"
        assert result["implementation_go"] is False
        assert len(result["required_rerun"]) >= 1

    def test_scope_delta_has_implementation_go_false(self):
        """AC5: implementation_go is always False (scope expansion auto-approval prohibited)"""
        for assoc in TRUSTED_ASSOCIATIONS:
            comment = _make_comment(
                comment_id=4002,
                body=VALID_YAML_BLOCK,
                author_association=assoc,
            )
            result = _classify(comment)
            assert result["implementation_go"] is False, f"implementation_go must be False for {assoc}"

    def test_scope_delta_required_rerun_non_empty(self):
        """AC5: required_rerun is non-empty for trusted anchor"""
        comment = _make_comment(
            comment_id=4003,
            body=VALID_YAML_BLOCK,
            author_association="MEMBER",
        )
        result = _classify(comment)
        assert result["status"] == "approved_by_trusted_anchor"
        assert len(result["required_rerun"]) >= 1

    def test_fail_closed_required_rerun_empty(self):
        """AC5: fail_closed result has empty required_rerun"""
        comment = _make_comment(
            comment_id=4004,
            body=VALID_YAML_BLOCK,
            author_association="CONTRIBUTOR",
        )
        result = _classify(comment)
        assert result["status"] == "fail_closed"
        assert result["required_rerun"] == []
