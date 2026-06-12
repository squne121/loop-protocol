"""
tests/test_ensure_contract_snapshot.py

Unit tests for ensure_contract_snapshot.py

AC5: ensure_contract_snapshot.py の unit test が PASS する
AC8: CONTRACT_REVIEW_RESULT_V1.status に human_judgment を投稿しないことを unit test で固定する
AC9: issue body が materialization 中に更新された場合、投稿せず exit 50 になる（unit test で確認）
AC10: GitHub comment 投稿は idempotency marker により重複投稿されない（unit test で確認）
AC11: 403/429/422 から盲目的に retry しない（unit test で確認）
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import ensure_contract_snapshot from worktree path
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_ECS_PATH = _SCRIPTS_DIR / "ensure_contract_snapshot.py"

spec = importlib.util.spec_from_file_location("ensure_contract_snapshot", _ECS_PATH)
assert spec is not None and spec.loader is not None
_ecs_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_ecs_mod)  # type: ignore[union-attr]

ensure_contract_snapshot = _ecs_mod.ensure_contract_snapshot
classify_post_http_error = _ecs_mod.classify_post_http_error
find_idempotency_marker = _ecs_mod.find_idempotency_marker
sha256_of = _ecs_mod.sha256_of
compute_comments_digest = _ecs_mod.compute_comments_digest

POST_RESULT_POSTED = _ecs_mod.POST_RESULT_POSTED
POST_RESULT_DEDUPED = _ecs_mod.POST_RESULT_DEDUPED
POST_RESULT_DRY_RUN = _ecs_mod.POST_RESULT_DRY_RUN
POST_RESULT_PERMISSION_DENIED = _ecs_mod.POST_RESULT_PERMISSION_DENIED
POST_RESULT_RATE_LIMITED = _ecs_mod.POST_RESULT_RATE_LIMITED
POST_RESULT_VALIDATION_FAILED = _ecs_mod.POST_RESULT_VALIDATION_FAILED
POST_RESULT_AMBIGUOUS = _ecs_mod.POST_RESULT_AMBIGUOUS
POST_RESULT_NOT_REQUESTED = _ecs_mod.POST_RESULT_NOT_REQUESTED

# ---------------------------------------------------------------------------
# Helpers for mocking
# ---------------------------------------------------------------------------

_ISSUE_NUMBER = 817
_REPO = "squne121/loop-protocol"
_ISSUE_URL = f"https://github.com/{_REPO}/issues/{_ISSUE_NUMBER}"

_SAMPLE_BODY = "## Test Issue Body\nSome content here."
_SAMPLE_BODY_SHA256 = sha256_of(_SAMPLE_BODY)

_GO_COMMENT = {
    "id": 1001,
    "html_url": f"{_ISSUE_URL}#issuecomment-1001",
    "created_at": "2026-06-13T08:00:00Z",
    "updated_at": "2026-06-13T08:00:00Z",
    "body": f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-06-13T08:00:00Z"
  generated_by: issue-contract-review
  issue_url: {_ISSUE_URL}
```
""",
}

_BLOCKED_COMMENT = {
    "id": 1002,
    "html_url": f"{_ISSUE_URL}#issuecomment-1002",
    "created_at": "2026-06-13T09:00:00Z",
    "updated_at": "2026-06-13T09:00:00Z",
    "body": f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: blocked
  generated_at: "2026-06-13T09:00:00Z"
  generated_by: issue-contract-review
  issue_url: {_ISSUE_URL}
```
""",
}


def _mock_parser_mod(
    comments: list[dict],
    go_comment: dict | None = None,
    latest: dict | None = None,
    fetch_err: str | None = None,
) -> MagicMock:
    """Create a mock parser module."""
    mod = MagicMock()
    mod.fetch_issue_comments.return_value = ([] if fetch_err else comments, fetch_err)

    # Build results from comments
    results = []
    if go_comment:
        results.append({
            "comment_id": go_comment["id"],
            "html_url": go_comment["html_url"],
            "created_at": go_comment["created_at"],
            "status": "go",
        })

    mod.parse_contract_review_results.return_value = results
    mod.find_latest_go.return_value = results[0] if results and results[0]["status"] == "go" else None
    mod.find_latest_result.return_value = latest or (results[0] if results else None)
    return mod


def _make_review_result(status: str) -> dict:
    return {
        "schema": "CONTRACT_REVIEW_ONCE_RESULT_V1",
        "status": status,
        "readiness_status": "go" if status == "go" else "needs_fix",
        "errors": [],
    }


# ---------------------------------------------------------------------------
# Basic tests
# ---------------------------------------------------------------------------


class TestCheckOnlyMode:
    """check-only mode tests."""

    def test_existing_go_returns_ok(self, monkeypatch):
        """Existing go comment → status: ok, source: existing_go."""
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT],
            go_comment=_GO_COMMENT,
            latest={"comment_id": 1001, "html_url": _GO_COMMENT["html_url"], "status": "go", "created_at": "2026-06-13T08:00:00Z"},
        )

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_body", return_value=(_SAMPLE_BODY, None)):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        assert result["status"] == "ok"
        assert result["source"] == "existing_go"
        assert result["contract_snapshot_url"] == _GO_COMMENT["html_url"]

    def test_no_go_returns_human_judgment(self, monkeypatch):
        """No go comment in check-only mode → human_judgment (not blocked)."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_body", return_value=(_SAMPLE_BODY, None)):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        assert result["status"] == "human_judgment"
        assert result["source"] == "readiness_blocked"

    def test_latest_blocked_returns_blocked_needs_refinement(self, monkeypatch):
        """Latest result is blocked → blocked_needs_refinement."""
        latest = {
            "comment_id": 1002,
            "html_url": _BLOCKED_COMMENT["html_url"],
            "status": "blocked",
            "created_at": "2026-06-13T09:00:00Z",
        }
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT, _BLOCKED_COMMENT],
            go_comment=None,
            latest=latest,
        )
        parser_mod.find_latest_result.return_value = latest
        parser_mod.find_latest_go.return_value = None

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_body", return_value=(_SAMPLE_BODY, None)):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        assert result["status"] == "blocked_needs_refinement"
        assert result["source"] == "latest_blocked"


# ---------------------------------------------------------------------------
# AC8: human_judgment は投稿しない
# ---------------------------------------------------------------------------


class TestHumanJudgmentNotPosted:
    """AC8: CONTRACT_REVIEW_RESULT_V1.status に human_judgment を投稿しない。"""

    def test_human_judgment_status_not_posted(self, monkeypatch):
        """
        run_contract_review_once returns human_judgment → no GitHub comment posted,
        ensure_contract_snapshot returns status: human_judgment (not blocked, not posted).
        """
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        review_result = _make_review_result("human_judgment")

        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_RESULT_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_body", return_value=(_SAMPLE_BODY, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        # Must NOT post a comment when human_judgment
        assert len(posted) == 0, "must not post comment for human_judgment"
        assert result["status"] == "human_judgment"
        assert result["post_result"] != POST_RESULT_POSTED

    def test_human_judgment_source_correct(self, monkeypatch):
        """human_judgment source is correctly set."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        review_result = _make_review_result("human_judgment")

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_body", return_value=(_SAMPLE_BODY, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER,
                        repo=_REPO,
                        mode="auto",
                        do_post=False,
                    )

        assert result["status"] == "human_judgment"
        assert result["source"] == "human_judgment"


# ---------------------------------------------------------------------------
# AC9: stale body → exit 50 (stale_or_conflicting_snapshot)
# ---------------------------------------------------------------------------


class TestStaleBodyDetection:
    """AC9: issue body が materialization 中に更新された場合、投稿せず exit 50 になる。"""

    def test_body_changed_between_check_and_post_returns_exit50(self, monkeypatch):
        """
        Body sha256 changes between initial fetch and pre-post re-fetch → stale_or_conflicting_snapshot.
        """
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")

        # Simulate body changing between initial fetch and re-fetch
        body_calls = [0]

        def fetch_body_side_effect(issue_number, repo, timeout=30):
            body_calls[0] += 1
            if body_calls[0] == 1:
                return (_SAMPLE_BODY, None)
            else:
                # Body changed on second call
                return (_SAMPLE_BODY + "\n\nBODY CHANGED", None)

        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_RESULT_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_body", side_effect=fetch_body_side_effect):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert result["status"] == "stale_or_conflicting_snapshot", (
            f"Expected stale_or_conflicting_snapshot, got {result['status']}"
        )
        assert len(posted) == 0, "must not post when body changed (stale)"
        assert result["body_sha256_at_check"] != result["body_sha256_at_post"]

    def test_body_unchanged_proceeds_to_post(self, monkeypatch):
        """Body sha256 unchanged → proceed to post (no stale)."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")

        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_RESULT_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_body", return_value=(_SAMPLE_BODY, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert result["status"] == "ok"
        assert result["source"] == "materialized_go"
        assert len(posted) == 1


# ---------------------------------------------------------------------------
# AC10: idempotency marker で重複投稿しない
# ---------------------------------------------------------------------------


class TestIdempotencyMarker:
    """AC10: GitHub comment 投稿は idempotency marker により重複投稿されない。"""

    def test_idempotency_marker_found_skips_post(self, monkeypatch):
        """
        If idempotency marker already exists in comments, skip posting.
        """
        # Build a comment that has the idempotency marker
        idempotency_marker = _ecs_mod._IDEMPOTENCY_MARKER_TEMPLATE.format(
            issue=_ISSUE_NUMBER,
            body_sha256=_SAMPLE_BODY_SHA256,
        )
        marker_comment = {
            "id": 2001,
            "html_url": f"{_ISSUE_URL}#issuecomment-2001",
            "created_at": "2026-06-13T10:00:00Z",
            "updated_at": "2026-06-13T10:00:00Z",
            "body": f"{idempotency_marker}\n\nSome contract review content.",
        }

        # Parser returns no valid go (marker comment is not CONTRACT_REVIEW_RESULT_V1)
        parser_mod = _mock_parser_mod(
            comments=[marker_comment],
            go_comment=None,
            latest=None,
        )
        parser_mod.fetch_issue_comments.return_value = ([marker_comment], None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")

        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_RESULT_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_body", return_value=(_SAMPLE_BODY, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        # Should be deduped — no additional post
        assert len(posted) == 0, "must not post when idempotency marker exists"
        assert result["post_result"] == POST_RESULT_DEDUPED
        assert result["idempotency_marker_found"] is True
        assert result["status"] == "ok"

    def test_idempotency_marker_not_found_allows_post(self, monkeypatch):
        """If no idempotency marker, posting proceeds normally."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_RESULT_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_body", return_value=(_SAMPLE_BODY, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert len(posted) == 1, "should post when no idempotency marker"
        assert result["post_result"] == POST_RESULT_POSTED
        assert result["idempotency_marker_found"] is False

    def test_idempotency_marker_correct_format(self):
        """Verify idempotency marker format includes required fields."""
        marker = _ecs_mod._IDEMPOTENCY_MARKER_TEMPLATE.format(
            issue=_ISSUE_NUMBER,
            body_sha256=_SAMPLE_BODY_SHA256,
        )
        assert f"issue={_ISSUE_NUMBER}" in marker
        assert _SAMPLE_BODY_SHA256 in marker
        assert "schema=CONTRACT_REVIEW_RESULT_V1" in marker
        assert marker.startswith("<!--")
        assert marker.endswith("-->")


# ---------------------------------------------------------------------------
# AC11: 403/429/422 から盲目的に retry しない
# ---------------------------------------------------------------------------


class TestRateLimitAndPermissionErrors:
    """AC11: 403/429/422 から盲目的に retry しない。"""

    @pytest.mark.parametrize(
        "http_status,expected_code",
        [
            (403, POST_RESULT_PERMISSION_DENIED),
            (429, POST_RESULT_RATE_LIMITED),
            (422, POST_RESULT_VALIDATION_FAILED),
        ],
    )
    def test_http_error_classified_no_retry(self, http_status, expected_code, monkeypatch):
        """403/429/422 → specific error code, no retry attempted."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")

        call_count = [0]

        def fake_post(issue_number, repo, body, timeout=30):
            call_count[0] += 1
            code = classify_post_http_error(http_status)
            return (None, code, http_status)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_body", return_value=(_SAMPLE_BODY, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        # Only ONE attempt — no blind retry
        assert call_count[0] == 1, f"Expected 1 attempt, got {call_count[0]} (blind retry forbidden)"
        assert result["post_result"] == expected_code
        assert result["http_status"] == http_status
        # Status should be human_judgment for 403/429, etc.
        assert result["status"] == "human_judgment"

    def test_classify_http_error_403(self):
        assert classify_post_http_error(403) == POST_RESULT_PERMISSION_DENIED

    def test_classify_http_error_429(self):
        assert classify_post_http_error(429) == POST_RESULT_RATE_LIMITED

    def test_classify_http_error_422(self):
        assert classify_post_http_error(422) == POST_RESULT_VALIDATION_FAILED

    def test_classify_http_error_unknown(self):
        assert classify_post_http_error(500) == POST_RESULT_AMBIGUOUS

    def test_classify_http_error_503(self):
        assert classify_post_http_error(503) == POST_RESULT_AMBIGUOUS

    def test_rate_limit_no_retry_429(self, monkeypatch):
        """429 rate limit → no retry, status: human_judgment."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        call_count = [0]

        def fake_post(issue_number, repo, body, timeout=30):
            call_count[0] += 1
            return (None, POST_RESULT_RATE_LIMITED, 429)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_body", return_value=(_SAMPLE_BODY, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert call_count[0] == 1, "no retry on 429"
        assert result["status"] == "human_judgment"
        assert result["post_result"] == POST_RESULT_RATE_LIMITED

    def test_permission_denied_no_retry_403(self, monkeypatch):
        """403 permission denied → no retry, status: human_judgment."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        call_count = [0]

        def fake_post(issue_number, repo, body, timeout=30):
            call_count[0] += 1
            return (None, POST_RESULT_PERMISSION_DENIED, 403)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_body", return_value=(_SAMPLE_BODY, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert call_count[0] == 1, "no retry on 403"
        assert result["status"] == "human_judgment"
        assert result["post_result"] == POST_RESULT_PERMISSION_DENIED


# ---------------------------------------------------------------------------
# Dry-run mode tests
# ---------------------------------------------------------------------------


class TestDryRunMode:
    """dry-run mode does not post."""

    def test_dry_run_no_post(self, monkeypatch):
        """dry-run mode: ok but no GitHub mutation."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_RESULT_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_body", return_value=(_SAMPLE_BODY, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="dry-run",
                            do_post=True,  # even with --post, dry-run wins
                        )

        assert len(posted) == 0
        assert result["post_result"] == POST_RESULT_DRY_RUN
        assert result["status"] == "ok"

    def test_auto_no_post_flag(self, monkeypatch):
        """auto mode without --post: no GitHub mutation."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_RESULT_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_body", return_value=(_SAMPLE_BODY, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=False,
                        )

        assert len(posted) == 0
        assert result["post_result"] == POST_RESULT_DRY_RUN
        assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# Runtime error propagation
# ---------------------------------------------------------------------------


class TestRuntimeErrorPropagation:
    """runtime_error from run_contract_review_once → propagated."""

    def test_run_contract_review_once_error_propagated(self, monkeypatch):
        """If run_contract_review_once fails, status: runtime_error."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_body", return_value=(_SAMPLE_BODY, None)):
                with patch.object(
                    _ecs_mod, "run_contract_review_once", return_value=(None, "json_parse_error:test")
                ):
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER,
                        repo=_REPO,
                        mode="auto",
                    )

        assert result["status"] == "runtime_error"
        assert any("json_parse_error" in e for e in result["errors"])

    def test_body_fetch_error_returns_runtime_error(self, monkeypatch):
        """Body fetch error → runtime_error."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_body", return_value=(None, "gh_timeout")):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        assert result["status"] == "runtime_error"


# ---------------------------------------------------------------------------
# idempotency marker utility tests
# ---------------------------------------------------------------------------


class TestIdempotencyMarkerUtility:
    """Unit tests for find_idempotency_marker helper."""

    def test_finds_matching_marker(self):
        marker = _ecs_mod._IDEMPOTENCY_MARKER_TEMPLATE.format(
            issue=100,
            body_sha256="sha256:abc123",
        )
        comments = [
            {"id": 1, "html_url": "https://example.com/1", "body": f"Some content\n{marker}\nMore"},
        ]
        result = find_idempotency_marker(comments, 100, "sha256:abc123")
        assert result == "https://example.com/1"

    def test_no_match_returns_none(self):
        comments = [
            {"id": 1, "html_url": "https://example.com/1", "body": "No marker here"},
        ]
        result = find_idempotency_marker(comments, 100, "sha256:abc123")
        assert result is None

    def test_different_sha_no_match(self):
        marker = _ecs_mod._IDEMPOTENCY_MARKER_TEMPLATE.format(
            issue=100,
            body_sha256="sha256:oldvalue",
        )
        comments = [
            {"id": 1, "html_url": "https://example.com/1", "body": marker},
        ]
        result = find_idempotency_marker(comments, 100, "sha256:newvalue")
        assert result is None
