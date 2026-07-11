"""
tests/test_ensure_contract_snapshot.py

Unit tests for ensure_contract_snapshot.py

AC5: ensure_contract_snapshot.py の unit test が PASS する
AC8: CONTRACT_REVIEW_RESULT_V1.status に human_judgment を投稿しないことを unit test で固定する
AC9: issue body が materialization 中に更新された場合、投稿せず exit 50 になる（unit test で確認）
AC10: GitHub comment 投稿は idempotency marker により重複投稿されない（unit test で確認）
AC11: 403/429/422 から盲目的に retry しない（unit test で確認）

B3 invariant: status: ok implies contract_snapshot_url is not None (unit test で確認)
B4 schema: post_status key (not post_result)
B2 atomicity: body_sha256 OR updatedAt 変化 → stale_or_conflicting_snapshot
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
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
is_go_fresh = _ecs_mod.is_go_fresh
is_go_current = _ecs_mod.is_go_current

# Use post_status constants (B4)
POST_STATUS_POSTED = _ecs_mod.POST_STATUS_POSTED
POST_STATUS_DEDUPED = _ecs_mod.POST_STATUS_DEDUPED
POST_STATUS_DRY_RUN = _ecs_mod.POST_STATUS_DRY_RUN
POST_STATUS_PERMISSION_DENIED = _ecs_mod.POST_STATUS_PERMISSION_DENIED
POST_STATUS_RATE_LIMITED = _ecs_mod.POST_STATUS_RATE_LIMITED
POST_STATUS_VALIDATION_FAILED = _ecs_mod.POST_STATUS_VALIDATION_FAILED
POST_STATUS_AMBIGUOUS = _ecs_mod.POST_STATUS_AMBIGUOUS
POST_STATUS_NOT_REQUESTED = _ecs_mod.POST_STATUS_NOT_REQUESTED

# Legacy aliases preserved for backward compat (still exported from module)
POST_RESULT_POSTED = POST_STATUS_POSTED
POST_RESULT_DEDUPED = POST_STATUS_DEDUPED
POST_RESULT_DRY_RUN = POST_STATUS_DRY_RUN
POST_RESULT_PERMISSION_DENIED = POST_STATUS_PERMISSION_DENIED
POST_RESULT_RATE_LIMITED = POST_STATUS_RATE_LIMITED
POST_RESULT_VALIDATION_FAILED = POST_STATUS_VALIDATION_FAILED
POST_RESULT_AMBIGUOUS = POST_STATUS_AMBIGUOUS
POST_RESULT_NOT_REQUESTED = POST_STATUS_NOT_REQUESTED

# ---------------------------------------------------------------------------
# Helpers for mocking
# ---------------------------------------------------------------------------

_ISSUE_NUMBER = 817
_REPO = "squne121/loop-protocol"
_ISSUE_URL = f"https://github.com/{_REPO}/issues/{_ISSUE_NUMBER}"

_SAMPLE_BODY = "## Test Issue Body\nSome content here."
_SAMPLE_BODY_SHA256 = sha256_of(_SAMPLE_BODY)
_SAMPLE_UPDATED_AT = "2026-06-13T08:00:00Z"

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
  body_sha256: "{_SAMPLE_BODY_SHA256}"
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
            "inner": _fresh_inner(_SAMPLE_BODY_SHA256),
        })

    mod.parse_contract_review_results.return_value = results
    mod.find_latest_go.return_value = results[0] if results and results[0]["status"] == "go" else None
    mod.find_latest_result.return_value = latest or (results[0] if results else None)
    return mod


def _fresh_inner(body_sha256: str) -> dict:
    return {
        "body_sha256": body_sha256,
        "checks": {"vc_preflight": {"classifications": []}},
    }


def _make_review_result(status: str) -> dict:
    checks = {
        "readiness": "go" if status == "go" else "needs_fix",
        "blockers": "pass" if status == "go" else None,
        "product_spec": "pass" if status == "go" else None,
        "vc_preflight": "pass" if status == "go" else None,
    }
    return {
        "schema": "CONTRACT_REVIEW_ONCE_RESULT_V1",
        "status": status,
        "readiness_status": "go" if status == "go" else "needs_fix",
        "checks": checks,
        "vc_preflight_classifications": [{"ac": "AC1", "decision": "pass"}],
        "errors": [],
    }


# ---------------------------------------------------------------------------
# Basic tests
# ---------------------------------------------------------------------------


class TestCheckOnlyMode:
    """check-only mode tests."""

    def test_existing_go_matches_current_body_returns_ok(self, monkeypatch):
        """Existing go comment → status: ok, source: existing_go."""
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT],
            go_comment=_GO_COMMENT,
            latest={
                "comment_id": 1001,
                "html_url": _GO_COMMENT["html_url"],
                "status": "go",
                "created_at": "2026-06-13T08:00:00Z"
            },
        )

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        assert result["status"] == "ok"
        assert result["source"] == "existing_go"
        assert result["contract_snapshot_url"] == _GO_COMMENT["html_url"]
        # B3: status: ok implies contract_snapshot_url is not None
        assert result["contract_snapshot_url"] is not None

    def test_no_go_returns_human_judgment(self, monkeypatch):
        """No go comment in check-only mode → human_judgment (not blocked)."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
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
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        assert result["status"] == "blocked_needs_refinement"
        assert result["source"] == "latest_blocked"


class TestFreshGoSnapshots:
    """#1445: existing go is usable only for the current canonical body hash."""

    @pytest.mark.parametrize(
        "body_sha256",
        [
            None,
            "",
            1,
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            " sha256:" + "a" * 64,
            sha256_of("different body"),
        ],
    )
    def test_invalid_or_stale_go_never_returns_existing_go(self, body_sha256):
        stale_go = {
            "comment_id": 1001,
            "html_url": _GO_COMMENT["html_url"],
            "created_at": _GO_COMMENT["created_at"],
            "status": "go",
            "inner": {"body_sha256": body_sha256},
        }
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT])
        parser_mod.parse_contract_review_results.return_value = [stale_go]
        parser_mod.find_latest_go.return_value = stale_go
        parser_mod.find_latest_result.return_value = stale_go

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER, repo=_REPO, mode="check-only"
                )

        assert result["status"] == "human_judgment"
        assert result["source"] == "readiness_blocked"

    def test_stale_go_auto_without_post_materializes_dry_run(self):
        stale_go = {
            "comment_id": 1001,
            "html_url": _GO_COMMENT["html_url"],
            "created_at": _GO_COMMENT["created_at"],
            "status": "go",
            "inner": {"body_sha256": sha256_of("different body")},
        }
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT])
        parser_mod.parse_contract_review_results.return_value = [stale_go]
        parser_mod.find_latest_go.return_value = stale_go
        parser_mod.find_latest_result.return_value = stale_go

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "run_contract_review_once",
                    return_value=(_make_review_result("go"), None),
                ) as review:
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER, repo=_REPO, mode="auto", do_post=False
                    )

        assert result["status"] == "dry_run_would_post"
        review.assert_called_once()

    def test_stale_go_at_post_check_does_not_dedupe(self):
        stale_go = {
            "comment_id": 1001,
            "html_url": _GO_COMMENT["html_url"],
            "created_at": _GO_COMMENT["created_at"],
            "status": "go",
            "inner": {"body_sha256": sha256_of("different body")},
        }
        parser_mod = _mock_parser_mod(comments=[])
        parser_mod.parse_contract_review_results.side_effect = [[], [stale_go]]
        parser_mod.find_latest_result.side_effect = [None, None]
        parser_mod.find_latest_go.side_effect = [None, stale_go]

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "run_contract_review_once",
                    return_value=(_make_review_result("go"), None),
                ):
                    with patch.object(
                        _ecs_mod,
                        "post_comment",
                        return_value=(
                            "https://example.test/comment",
                            POST_STATUS_POSTED,
                            201,
                        ),
                    ) as post:
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER, repo=_REPO, mode="auto", do_post=True
                        )

        assert result["source"] == "materialized_go"
        post.assert_called_once()

    def test_fresh_go_at_post_check_dedupes(self):
        fresh_go = {
            "comment_id": 1002,
            "html_url": "https://example.test/fresh",
            "created_at": "2026-06-13T09:00:00Z",
            "status": "go",
            "inner": _fresh_inner(_SAMPLE_BODY_SHA256),
        }
        parser_mod = _mock_parser_mod(comments=[])
        parser_mod.parse_contract_review_results.side_effect = [[], [fresh_go]]
        parser_mod.find_latest_result.side_effect = [None, fresh_go]
        parser_mod.find_latest_go.side_effect = [None, fresh_go]

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(_make_review_result("go"), None)):
                    with patch.object(_ecs_mod, "post_comment") as post:
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER, repo=_REPO, mode="auto", do_post=True
                        )

        assert result["source"] == "existing_go"
        assert result["post_status"] == POST_STATUS_DEDUPED
        post.assert_not_called()

    def test_updated_at_only_change_with_fresh_go_dedupes(self):
        fresh_go = {
            "comment_id": 1002,
            "html_url": "https://example.test/fresh",
            "created_at": "2026-06-13T09:00:00Z",
            "status": "go",
            "inner": _fresh_inner(_SAMPLE_BODY_SHA256),
        }
        parser_mod = _mock_parser_mod(comments=[])
        parser_mod.parse_contract_review_results.side_effect = [[], [fresh_go]]
        parser_mod.find_latest_result.side_effect = [None, fresh_go]
        parser_mod.find_latest_go.side_effect = [None, fresh_go]
        snapshots = [
            (_SAMPLE_BODY, "one", None),
            (_SAMPLE_BODY, "two", None),
            (_SAMPLE_BODY, "two", None),
        ]

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", side_effect=snapshots):
                with patch.object(
                    _ecs_mod, "run_contract_review_once", return_value=(_make_review_result("go"), None)
                ):
                    with patch.object(_ecs_mod, "post_comment") as post:
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER, repo=_REPO, mode="auto", do_post=True
                        )

        assert result["status"] == "ok"
        assert result["source"] == "existing_go"
        assert result["post_status"] == POST_STATUS_DEDUPED
        post.assert_not_called()

    def test_existing_go_snapshot_change_retries_then_conflicts(self):
        fresh_go = {
            "comment_id": 1001,
            "html_url": _GO_COMMENT["html_url"],
            "created_at": _GO_COMMENT["created_at"],
            "status": "go",
            "inner": _fresh_inner(_SAMPLE_BODY_SHA256),
        }
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT])
        parser_mod.parse_contract_review_results.return_value = [fresh_go]
        parser_mod.find_latest_result.return_value = fresh_go
        parser_mod.find_latest_go.return_value = fresh_go
        snapshots = [
            (_SAMPLE_BODY, "one", None),
            (_SAMPLE_BODY + " changed", "two", None),
            (_SAMPLE_BODY, "three", None),
            (_SAMPLE_BODY + " changed again", "four", None),
        ]

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", side_effect=snapshots):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER, repo=_REPO, mode="check-only"
                )

        assert result["status"] == "stale_or_conflicting_snapshot"

    def test_existing_go_updated_at_change_retries_then_succeeds(self):
        fresh_go = {
            "comment_id": 1001,
            "html_url": _GO_COMMENT["html_url"],
            "created_at": _GO_COMMENT["created_at"],
            "status": "go",
            "inner": _fresh_inner(_SAMPLE_BODY_SHA256),
        }
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT])
        parser_mod.parse_contract_review_results.return_value = [fresh_go]
        parser_mod.find_latest_result.return_value = fresh_go
        parser_mod.find_latest_go.return_value = fresh_go
        snapshots = [
            (_SAMPLE_BODY, "one", None),
            (_SAMPLE_BODY, "two", None),
            (_SAMPLE_BODY, "two", None),
            (_SAMPLE_BODY, "two", None),
        ]

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", side_effect=snapshots):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER, repo=_REPO, mode="check-only"
                )

        assert result["status"] == "ok"
        assert result["source"] == "existing_go"

    def test_hashless_go_auto_materializes_dry_run(self):
        legacy_go = {
            "comment_id": 1001,
            "html_url": _GO_COMMENT["html_url"],
            "created_at": _GO_COMMENT["created_at"],
            "status": "go",
            "inner": {},
        }
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT])
        parser_mod.parse_contract_review_results.return_value = [legacy_go]
        parser_mod.find_latest_result.return_value = legacy_go
        parser_mod.find_latest_go.return_value = legacy_go

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "run_contract_review_once",
                    return_value=(_make_review_result("go"), None),
                ) as review:
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER, repo=_REPO, mode="auto", do_post=False
                    )

        assert result["status"] == "dry_run_would_post"
        review.assert_called_once()

    def test_fresh_go_without_classifications_materializes_dry_run(self):
        incomplete_go = {
            "comment_id": 1001,
            "html_url": _GO_COMMENT["html_url"],
            "created_at": _GO_COMMENT["created_at"],
            "status": "go",
            "inner": {"body_sha256": _SAMPLE_BODY_SHA256},
        }
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT])
        parser_mod.parse_contract_review_results.return_value = [incomplete_go]
        parser_mod.find_latest_result.return_value = incomplete_go
        parser_mod.find_latest_go.return_value = incomplete_go

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "run_contract_review_once",
                    return_value=(_make_review_result("go"), None),
                ) as review:
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER, repo=_REPO, mode="auto", do_post=False
                    )

        assert result["status"] == "dry_run_would_post"
        review.assert_called_once()

    def test_parser_result_with_canonical_body_sha256_is_consumed(self):
        parser_mod = _ecs_mod._import_parser_module()
        results = parser_mod.parse_contract_review_results([_GO_COMMENT], _ISSUE_URL)
        go_result = parser_mod.find_latest_go(results)

        assert go_result is not None
        assert is_go_fresh(go_result, _SAMPLE_BODY_SHA256)
        assert not is_go_current(go_result, _SAMPLE_BODY_SHA256)


class TestContractReviewComment:
    """Materialized snapshots retain VC baseline classifications for consumers."""

    def test_comment_serializes_vc_preflight_classifications(self):
        body = _ecs_mod._build_contract_review_comment(
            issue_number=_ISSUE_NUMBER,
            repo=_REPO,
            review_result=_make_review_result("go"),
            idempotency_marker="<!-- marker -->",
            body_sha256=_SAMPLE_BODY_SHA256,
        )
        parser_mod = _ecs_mod._import_parser_module()
        results = parser_mod.parse_contract_review_results(
            [{"id": 1, "html_url": "https://example.test/1", "created_at": "now", "body": body}],
            _ISSUE_URL,
        )
        go_result = parser_mod.find_latest_go(results)

        assert go_result is not None
        assert is_go_current(go_result, _SAMPLE_BODY_SHA256)
        assert go_result["inner"]["checks"]["vc_preflight"]["classifications"] == [
            {"ac": "AC1", "decision": "pass"}
        ]


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
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
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
        assert result["post_status"] != POST_STATUS_POSTED  # B4: key is post_status

    def test_human_judgment_source_correct(self, monkeypatch):
        """human_judgment source is correctly set."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        review_result = _make_review_result("human_judgment")

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
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

        def fetch_snapshot_side_effect(issue_number, repo, timeout=30):
            body_calls[0] += 1
            if body_calls[0] == 1:
                return (_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)
            else:
                # Body changed on second call
                return (_SAMPLE_BODY + "\n\nBODY CHANGED", _SAMPLE_UPDATED_AT, None)

        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", side_effect=fetch_snapshot_side_effect):
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

    def test_updated_at_changed_returns_exit50(self, monkeypatch):
        """
        B2: updatedAt changes between initial fetch and pre-post re-fetch → stale_or_conflicting_snapshot.
        """
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")

        body_calls = [0]

        def fetch_snapshot_side_effect(issue_number, repo, timeout=30):
            body_calls[0] += 1
            if body_calls[0] == 1:
                return (_SAMPLE_BODY, "2026-06-13T08:00:00Z", None)
            else:
                # updatedAt changed (e.g. someone edited the issue)
                return (_SAMPLE_BODY, "2026-06-13T09:30:00Z", None)

        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", side_effect=fetch_snapshot_side_effect):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert result["status"] == "stale_or_conflicting_snapshot", (
            f"B2: updatedAt mismatch must produce stale_or_conflicting_snapshot, got {result['status']}"
        )
        assert len(posted) == 0, "must not post when updatedAt changed"

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
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
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
        # B3: status: ok implies contract_snapshot_url is not None
        assert result["contract_snapshot_url"] is not None


# ---------------------------------------------------------------------------
# AC10: idempotency marker で重複投稿しない
# ---------------------------------------------------------------------------


class TestIdempotencyMarker:
    """AC10: GitHub comment 投稿は idempotency marker により重複投稿されない。"""

    def test_marker_only_comment_does_not_authorize_existing_go(self, monkeypatch):
        """
        A marker without a parser-valid fresh go result is not snapshot authority.
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
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert len(posted) == 1, "marker-only comment must not suppress materialization"
        assert result["post_status"] == POST_STATUS_POSTED  # B4: post_status
        assert result["idempotency_marker_found"] is False
        assert result["status"] == "ok"
        # B3: status: ok implies contract_snapshot_url is not None
        assert result["contract_snapshot_url"] is not None

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
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert len(posted) == 1, "should post when no idempotency marker"
        assert result["post_status"] == POST_STATUS_POSTED  # B4: post_status
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
            (403, POST_STATUS_PERMISSION_DENIED),
            (429, POST_STATUS_RATE_LIMITED),
            (422, POST_STATUS_VALIDATION_FAILED),
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
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
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
        assert result["post_status"] == expected_code  # B4: post_status
        assert result["http_status"] == http_status
        # Status should be human_judgment for 403/429, etc.
        assert result["status"] == "human_judgment"

    def test_classify_http_error_403(self):
        assert classify_post_http_error(403) == POST_STATUS_PERMISSION_DENIED

    def test_classify_http_error_429(self):
        assert classify_post_http_error(429) == POST_STATUS_RATE_LIMITED

    def test_classify_http_error_422(self):
        assert classify_post_http_error(422) == POST_STATUS_VALIDATION_FAILED

    def test_classify_http_error_unknown(self):
        assert classify_post_http_error(500) == POST_STATUS_AMBIGUOUS

    def test_classify_http_error_503(self):
        assert classify_post_http_error(503) == POST_STATUS_AMBIGUOUS

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
            return (None, POST_STATUS_RATE_LIMITED, 429)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
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
        assert result["post_status"] == POST_STATUS_RATE_LIMITED  # B4

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
            return (None, POST_STATUS_PERMISSION_DENIED, 403)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
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
        assert result["post_status"] == POST_STATUS_PERMISSION_DENIED  # B4


# ---------------------------------------------------------------------------
# B3: status: ok implies contract_snapshot_url is not None
# ---------------------------------------------------------------------------


class TestOkImpliesSnapshotUrl:
    """B3: status: ok must imply contract_snapshot_url is not None."""

    def test_ok_status_has_contract_snapshot_url(self, monkeypatch):
        """Whenever status: ok is returned, contract_snapshot_url must be non-null."""
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT],
            go_comment=_GO_COMMENT,
            latest={
                "comment_id": 1001,
                "html_url": _GO_COMMENT["html_url"],
                "status": "go",
                "created_at": "2026-06-13T08:00:00Z"
            },
        )

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        if result["status"] == "ok":
            assert result["contract_snapshot_url"] is not None, (
                "B3 violation: status: ok must imply contract_snapshot_url is not None"
            )

    def test_posted_ok_has_contract_snapshot_url(self, monkeypatch):
        """After successful post, status: ok with non-null contract_snapshot_url."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")

        def fake_post(issue_number, repo, body, timeout=30):
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert result["status"] == "ok"
        assert result["contract_snapshot_url"] is not None, (
            "B3: status: ok implies contract_snapshot_url must be non-null"
        )


# ---------------------------------------------------------------------------
# Dry-run mode tests (B3: dry-run → status: dry_run_would_post, NOT ok)
# ---------------------------------------------------------------------------


class TestDryRunMode:
    """dry-run mode does not post and does NOT return status: ok (B3)."""

    def test_dry_run_no_post(self, monkeypatch):
        """dry-run mode: no GitHub mutation, status: dry_run_would_post (not ok)."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="dry-run",
                            do_post=True,  # even with --post, dry-run wins
                        )

        assert len(posted) == 0
        assert result["post_status"] == POST_STATUS_DRY_RUN  # B4: post_status
        # B3: dry-run must NOT return status: ok (contract_snapshot_url is None)
        assert result["status"] == "dry_run_would_post", (
            f"B3: dry-run must return dry_run_would_post, not {result['status']}"
        )
        assert result["contract_snapshot_url"] is None

    def test_auto_no_post_flag(self, monkeypatch):
        """auto mode without --post: no GitHub mutation, status: dry_run_would_post."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-9999", POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                with patch.object(_ecs_mod, "run_contract_review_once", return_value=(review_result, None)):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=False,
                        )

        assert len(posted) == 0
        assert result["post_status"] == POST_STATUS_DRY_RUN  # B4: post_status
        # B3: no-post must NOT return status: ok
        assert result["status"] == "dry_run_would_post", (
            f"B3: no-post must return dry_run_would_post, not {result['status']}"
        )
        assert result["contract_snapshot_url"] is None


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
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
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
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(None, None, "gh_timeout")):
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


# ---------------------------------------------------------------------------
# B2: issue_updated_at fields present in result
# ---------------------------------------------------------------------------


class TestAtomicityFields:
    """B2: body_sha256_at_check, issue_updated_at_at_check, comments_digest_at_check present."""

    def test_atomicity_fields_populated(self, monkeypatch):
        """B2 fields are in result after initial snapshot fetch."""
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        assert "body_sha256_at_check" in result
        assert "issue_updated_at_at_check" in result
        assert "comments_digest_at_check" in result
        assert result["body_sha256_at_check"] == _SAMPLE_BODY_SHA256
        assert result["issue_updated_at_at_check"] == _SAMPLE_UPDATED_AT
