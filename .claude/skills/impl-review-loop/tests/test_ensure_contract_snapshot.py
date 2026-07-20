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
import json
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
is_go_base_binding_current = _ecs_mod.is_go_base_binding_current
_real_verify_snapshot_authority_postcondition = (
    _ecs_mod.verify_snapshot_authority_postcondition
)

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
# #1475: default the controlled-publisher comment id binding check to success
# for all tests in this file. AC5 regression scenarios in this file predate
# the binding check and do not exercise real `gh api` subprocess calls; tests
# that specifically exercise the binding check live in
# test_contract_snapshot_author_binding.py and override this fixture locally.
#
# #1537: also default the new two-phase fingerprint materialize steps
# (base_ref/base_sha capture, PATCH) to success for the same reason -- these
# pre-existing regression scenarios exercise the POST flow / precedence
# rules, not the fingerprint mechanism itself. Tests that specifically
# exercise fingerprint materialization override these locally.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _default_trusted_comment_id_binding(monkeypatch):
    monkeypatch.setattr(
        _ecs_mod,
        "verify_controlled_publisher_comment_id_binding",
        lambda *args, **kwargs: (True, None),
    )
    monkeypatch.setattr(
        _ecs_mod,
        "capture_base_ref_and_sha",
        lambda *args, **kwargs: ("main", "a" * 40),
    )
    monkeypatch.setattr(
        _ecs_mod,
        "patch_comment",
        lambda *args, **kwargs: (True, None),
    )
    monkeypatch.setattr(
        _ecs_mod,
        "verify_snapshot_authority_postcondition",
        lambda *args, **kwargs: (True, None),
    )


# ---------------------------------------------------------------------------
# Helpers for mocking
# ---------------------------------------------------------------------------

_ISSUE_NUMBER = 817
_REPO = "squne121/loop-protocol"
_ISSUE_URL = f"https://github.com/{_REPO}/issues/{_ISSUE_NUMBER}"

_SAMPLE_BODY = "## Test Issue Body\nSome content here.\n\n## Allowed Paths\n- tracked.txt\n"
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
            "inner": _fresh_inner(
                _SAMPLE_BODY_SHA256, fingerprint_comment_id=go_comment["id"]
            ),
        })

    mod.parse_contract_review_results.return_value = results
    mod.find_latest_go.return_value = results[0] if results and results[0]["status"] == "go" else None
    mod.find_latest_authoritative_go.return_value = (
        results[0] if results and results[0]["status"] == "go" else None
    )
    mod.find_latest_result.return_value = latest or (results[0] if results else None)
    return mod


def _fresh_inner(
    body_sha256: str,
    *,
    fingerprint_comment_id: int | None = 1001,
    base_ref: str = "main",
    base_sha: str = "a" * 40,
) -> dict:
    inner = {
        "body_sha256": body_sha256,
        "checks": {
            "product_spec_check": {
                "schema": "product_spec_check/v1",
                "applicability": "applicable",
                "decision": "pass",
                "triggers": {},
                "conditions": {},
                "blocked_reasons": [],
                "body_sha256": body_sha256,
                "source_provenance": {
                    "source_type": "github_issue_body",
                    "body_file": None,
                },
            },
            "vc_preflight": {"classifications": []},
        },
    }
    if fingerprint_comment_id is not None:
        inner["expected_contract_fingerprint"] = {
            "issue_number": _ISSUE_NUMBER,
            "contract_source_kind": "issue_comment",
            "contract_source_id": str(fingerprint_comment_id),
            "contract_body_sha256": body_sha256,
            "allowed_paths_normalized_sha256": "b" * 64,
            "base_ref": base_ref,
            "base_sha_at_snapshot": base_sha,
        }
    return inner


def _make_review_result(status: str) -> dict:
    checks = {
        "readiness": "go" if status == "go" else "needs_fix",
        "blockers": "pass" if status == "go" else None,
        "product_spec": "pass" if status == "go" else None,
        "product_spec_check": _fresh_inner(_SAMPLE_BODY_SHA256)["checks"][
            "product_spec_check"
        ] if status == "go" else None,
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

    def test_current_head_runs_producer_even_when_existing_go_is_fresh(self):
        """GIVEN fresh GO WHEN current-head is requested THEN producer evidence is refreshed."""
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT],
            go_comment=_GO_COMMENT,
            latest={
                "comment_id": 1001,
                "html_url": _GO_COMMENT["html_url"],
                "status": "go",
                "created_at": _GO_COMMENT["created_at"],
            },
        )
        full_envelope = {
            "schema": "baseline_vc_preflight/v1",
            "status": "pass",
            "generated_at": "2026-07-12T00:00:00Z",
            "errors": [],
            "source": {"body_sha256": "sha256:" + "c" * 64},
            "results": [],
            "evidence_mode": "current-head",
            "head_sha": "a" * 40,
            "reviewed_head_sha": "a" * 40,
            "head_after_sha": "a" * 40,
            "clean_before": True,
            "clean_after": True,
            "fallback_detected": False,
            "human_review_required": False,
            "stop_condition_triggered": False,
        }
        review_result = _make_review_result("go")
        review_result["vc_evidence"] = full_envelope
        review_result["current_vc_result"] = full_envelope

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "run_contract_review_once",
                    return_value=(review_result, None),
                ) as run_once:
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER,
                        repo=_REPO,
                        mode="auto",
                        evidence_mode="current-head",
                        cwd="/tmp/pr-worktree",
                        reviewed_head_sha="a" * 40,
                    )

        assert run_once.call_count == 1
        assert result["status"] == "ok"
        assert result["source"] == "existing_go"
        assert result["contract_snapshot_url"] == _GO_COMMENT["html_url"]
        assert result["vc_evidence"] == full_envelope
        assert result["current_vc_result"] == full_envelope

        adjudicator_path = _SCRIPTS_DIR / "adjudicate_vc_result.py"
        spec = importlib.util.spec_from_file_location("adjudicate_vc_result_e2e", adjudicator_path)
        assert spec is not None and spec.loader is not None
        adjudicator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adjudicator)  # type: ignore[union-attr]
        adjudication = adjudicator.adjudicate_vc_result(
            contract_snapshot={
                "schema": "CONTRACT_REVIEW_RESULT_V1",
                "checks": {"vc_preflight": {"classifications": []}},
            },
            current_vc_result=result["current_vc_result"],
            diff_summary={"changed_paths": [], "head_sha": "a" * 40},
            allowed_paths=[".claude/skills/**"],
        )
        assert adjudication["source_integrity"]["current_vc_result_present"] is True
        assert adjudication["source_integrity"]["current_vc_result_head_sha"] == "a" * 40

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
        fresh_comment = {**_GO_COMMENT, "id": 1002, "html_url": fresh_go["html_url"]}
        parser_mod.fetch_issue_comments.side_effect = [([], None), ([fresh_comment], None)]

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
        fresh_comment = {**_GO_COMMENT, "id": 1002, "html_url": fresh_go["html_url"]}
        parser_mod.fetch_issue_comments.side_effect = [([], None), ([fresh_comment], None)]
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

    def test_current_body_legacy_go_without_product_spec_payload_materializes_dry_run(self):
        legacy_go = {
            "comment_id": 1001,
            "html_url": _GO_COMMENT["html_url"],
            "created_at": _GO_COMMENT["created_at"],
            "status": "go",
            "inner": {
                "body_sha256": _SAMPLE_BODY_SHA256,
                "checks": {"vc_preflight": {"classifications": []}},
            },
        }
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT])
        parser_mod.parse_contract_review_results.return_value = [legacy_go]
        parser_mod.find_latest_go.return_value = legacy_go
        parser_mod.find_latest_result.return_value = legacy_go

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
        fingerprint = _ecs_mod.compute_expected_contract_fingerprint(
            issue_number=_ISSUE_NUMBER,
            contract_source_id="1",
            contract_body_sha256=_SAMPLE_BODY_SHA256,
            allowed_paths=["tracked.txt"],
            base_ref="main",
            base_sha_at_snapshot="a" * 40,
        )
        body = _ecs_mod._build_contract_review_comment(
            issue_number=_ISSUE_NUMBER,
            repo=_REPO,
            review_result=_make_review_result("go"),
            idempotency_marker="<!-- marker -->",
            body_sha256=_SAMPLE_BODY_SHA256,
            expected_contract_fingerprint=fingerprint,
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
        assert go_result["inner"]["checks"]["product_spec_check"]["schema"] == "product_spec_check/v1"


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


# ---------------------------------------------------------------------------
# Source-bound contract fingerprint (Issue #1537)
# ---------------------------------------------------------------------------

extract_allowed_paths_from_body = _ecs_mod.extract_allowed_paths_from_body
compute_expected_contract_fingerprint = _ecs_mod.compute_expected_contract_fingerprint
capture_base_ref_and_sha = _ecs_mod.capture_base_ref_and_sha
patch_comment = _ecs_mod.patch_comment

_real_parser_mod_for_fp_tests = _ecs_mod._import_parser_module()
parse_contract_review_results = _real_parser_mod_for_fp_tests.parse_contract_review_results
find_latest_go = _real_parser_mod_for_fp_tests.find_latest_go
is_fingerprint_ready_go = _real_parser_mod_for_fp_tests.is_fingerprint_ready_go

_BODY_WITH_ALLOWED_PATHS = """## Outcome

Something.

## Allowed Paths

- `.claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py`
- `.claude/skills/impl-review-loop/tests/test_ensure_contract_snapshot.py`

## Stop Conditions

- N/A
"""


class TestExtractAllowedPathsFromBody:
    def test_extracts_bullet_paths(self):
        paths = extract_allowed_paths_from_body(_BODY_WITH_ALLOWED_PATHS)
        assert paths == [
            ".claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py",
            ".claude/skills/impl-review-loop/tests/test_ensure_contract_snapshot.py",
        ]

    def test_missing_section_returns_empty(self):
        assert extract_allowed_paths_from_body("## Outcome\n\nNo allowed paths here.\n") == []

    def test_empty_body_returns_empty(self):
        assert extract_allowed_paths_from_body("") == []


class TestComputeExpectedContractFingerprint:
    def test_returns_seven_keys(self):
        fp = compute_expected_contract_fingerprint(
            issue_number=_ISSUE_NUMBER,
            contract_source_id="12345",
            contract_body_sha256=_SAMPLE_BODY_SHA256,
            allowed_paths=[".claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py"],
            base_ref="main",
            base_sha_at_snapshot="a" * 40,
        )
        assert set(fp.keys()) == {
            "issue_number",
            "contract_source_kind",
            "contract_source_id",
            "contract_body_sha256",
            "allowed_paths_normalized_sha256",
            "base_ref",
            "base_sha_at_snapshot",
        }
        assert fp["issue_number"] == _ISSUE_NUMBER
        assert fp["contract_source_kind"] == "issue_comment"
        assert fp["contract_source_id"] == "12345"
        assert fp["contract_body_sha256"] == _SAMPLE_BODY_SHA256
        assert fp["base_ref"] == "main"
        assert fp["base_sha_at_snapshot"] == "a" * 40

    def test_allowed_paths_hash_matches_gate_recomputation(self):
        """AC4: the gate must be able to recompute an identical
        allowed_paths_normalized_sha256 from the same Allowed Paths list."""
        import sys as _sys

        gate_scripts_dir = (
            Path(__file__).resolve().parents[4]
            / ".claude"
            / "skills"
            / "pr-review-judge"
            / "scripts"
        )
        if str(gate_scripts_dir) not in _sys.path:
            _sys.path.insert(0, str(gate_scripts_dir))
        from allowed_paths_review_gate import AllowedPathsGateEvaluator  # noqa: E402

        allowed_paths = ["src/**", "docs/foo.md"]
        fp = compute_expected_contract_fingerprint(
            issue_number=_ISSUE_NUMBER,
            contract_source_id="1",
            contract_body_sha256=_SAMPLE_BODY_SHA256,
            allowed_paths=allowed_paths,
            base_ref="main",
            base_sha_at_snapshot="a" * 40,
        )
        evaluator = AllowedPathsGateEvaluator(
            pr_number=1,
            base_ref="main",
            base_sha_at_snapshot="a" * 40,
            current_base_sha="x",
            diff_base_sha="x",
            head_sha="y",
            reviewed_head_sha="y",
            allowed_paths=allowed_paths,
            contract_body_sha256=_SAMPLE_BODY_SHA256,
            contract_source_kind="issue_comment",
            contract_source_id="1",
            expected_contract_fingerprint=None,
            issue_number=_ISSUE_NUMBER,
        )
        assert fp["allowed_paths_normalized_sha256"] == evaluator.compute_allowed_paths_hash()

    def test_no_sha256_prefix_on_allowed_paths_hash(self):
        fp = compute_expected_contract_fingerprint(
            issue_number=_ISSUE_NUMBER,
            contract_source_id="1",
            contract_body_sha256=_SAMPLE_BODY_SHA256,
            allowed_paths=["src/**"],
            base_ref="main",
            base_sha_at_snapshot="a" * 40,
        )
        assert not fp["allowed_paths_normalized_sha256"].startswith("sha256:")


class TestCaptureBaseRefAndSha:
    def test_success_returns_both_values(self):
        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = json.dumps(
                {
                    "data": {
                        "repository": {
                            "defaultBranchRef": {
                                "name": "main",
                                "target": {"oid": "a" * 40},
                            }
                        }
                    }
                }
            )
            return result

        with patch.object(_ecs_mod.subprocess, "run", side_effect=fake_run):
            base_ref, base_sha = capture_base_ref_and_sha(_REPO)

        assert base_ref == "main"
        assert base_sha == "a" * 40

    def test_uses_one_graphql_readback_for_branch_and_tip(self):
        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps(
            {
                "data": {
                    "repository": {
                        "defaultBranchRef": {
                            "name": "main",
                            "target": {"oid": "b" * 40},
                        }
                    }
                }
            }
        )
        with patch.object(_ecs_mod.subprocess, "run", return_value=result) as run:
            assert capture_base_ref_and_sha(_REPO) == ("main", "b" * 40)

        run.assert_called_once()
        cmd = run.call_args.args[0]
        assert cmd[:3] == ["gh", "api", "graphql"]
        assert "defaultBranchRef" in next(arg for arg in cmd if arg.startswith("query="))

    def test_default_branch_failure_returns_none_none(self):
        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            return result

        with patch.object(_ecs_mod.subprocess, "run", side_effect=fake_run):
            base_ref, base_sha = capture_base_ref_and_sha(_REPO)

        assert base_ref is None
        assert base_sha is None

    def test_graphql_payload_without_tip_fails_closed(self):
        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps(
            {"data": {"repository": {"defaultBranchRef": {"name": "main", "target": {}}}}}
        )

        with patch.object(_ecs_mod.subprocess, "run", return_value=result):
            base_ref, base_sha = capture_base_ref_and_sha(_REPO)

        assert base_ref is None
        assert base_sha is None


class TestFingerprintMaterializeEndToEnd:
    """Integration coverage for the two-phase POST+PATCH materialize flow
    (Issue #1537 AC1/AC5): the final PATCHed comment body must embed a
    fingerprint whose contract_source_id equals the real posted comment id.
    """

    def test_post_then_patch_embeds_fingerprint_bound_to_real_comment_id(self, monkeypatch):
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        real_comment_id = 555444
        real_url = f"{_ISSUE_URL}#issuecomment-{real_comment_id}"

        def fake_post(issue_number, repo, body, timeout=30):
            return (real_url, POST_STATUS_POSTED, None)

        patched_calls = []

        def fake_patch(issue_number, repo, comment_id, body, timeout=30):
            patched_calls.append((comment_id, body))
            return (True, None)

        monkeypatch.setattr(_ecs_mod, "capture_base_ref_and_sha", lambda *a, **kw: ("main", "d" * 40))
        monkeypatch.setattr(_ecs_mod, "patch_comment", fake_patch)

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
        assert len(patched_calls) == 1
        patched_comment_id, patched_body = patched_calls[0]
        assert patched_comment_id == real_comment_id

        parsed = parse_contract_review_results(
            [{"id": real_comment_id, "html_url": real_url, "created_at": "now", "body": patched_body}],
            _ISSUE_URL,
        )
        go_entry = find_latest_go(parsed)
        assert go_entry is not None
        fp = go_entry["inner"]["expected_contract_fingerprint"]
        assert fp["contract_source_id"] == str(real_comment_id)
        assert fp["issue_number"] == _ISSUE_NUMBER
        assert fp["base_ref"] == "main"
        assert fp["base_sha_at_snapshot"] == "d" * 40
        assert is_fingerprint_ready_go(go_entry["inner"], real_comment_id, _ISSUE_NUMBER) is True

    def test_base_ref_capture_failure_blocks_materialization(self, monkeypatch):
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        monkeypatch.setattr(_ecs_mod, "capture_base_ref_and_sha", lambda *a, **kw: (None, None))

        posted = []

        def fake_post(issue_number, repo, body, timeout=30):
            posted.append(body)
            return (f"{_ISSUE_URL}#issuecomment-1", POST_STATUS_POSTED, None)

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

        assert result["status"] == "runtime_error"
        assert not posted, "must not post a go when base_ref/base_sha capture fails"

    def test_patch_failure_does_not_report_ok(self, monkeypatch):
        parser_mod = _mock_parser_mod(comments=[], go_comment=None, latest=None)
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

        review_result = _make_review_result("go")
        monkeypatch.setattr(_ecs_mod, "capture_base_ref_and_sha", lambda *a, **kw: ("main", "e" * 40))
        monkeypatch.setattr(_ecs_mod, "patch_comment", lambda *a, **kw: (False, "patch_error"))

        def fake_post(issue_number, repo, body, timeout=30):
            return (f"{_ISSUE_URL}#issuecomment-2", POST_STATUS_POSTED, None)

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

        assert result["status"] == "controlled_publisher_binding_failed"
        assert result["contract_snapshot_url"] is None


class TestExistingGoFingerprintReuseGate:
    """Issue #1537: an existing go lacking a fingerprint must not be reused
    as current -- it must fall through to (re-)materialization."""

    def test_existing_go_without_fingerprint_is_not_reused_in_check_only(self):
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT],
            go_comment=_GO_COMMENT,
            latest={
                "comment_id": 1001,
                "html_url": _GO_COMMENT["html_url"],
                "status": "go",
                "created_at": "2026-06-13T08:00:00Z",
            },
        )
        # Make this candidate legacy-like so real fingerprint-ready logic is
        # exercised rather than the mock's default return value.
        parser_mod.parse_contract_review_results.return_value[0]["inner"].pop(
            "expected_contract_fingerprint"
        )
        real_parser_mod = _ecs_mod._import_parser_module()
        parser_mod.is_fingerprint_ready_go = real_parser_mod.is_fingerprint_ready_go

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(_ecs_mod, "fetch_issue_snapshot", return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None)):
                result = ensure_contract_snapshot(
                    issue_number=_ISSUE_NUMBER,
                    repo=_REPO,
                    mode="check-only",
                )

        assert result["status"] == "human_judgment"
        assert result["source"] == "readiness_blocked"


class TestExistingGoBaseBindingFreshness:
    """#1635: reuse additionally requires the live GitHub base binding."""

    def test_matching_live_base_reuses_existing_go_without_mutation(self):
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT], go_comment=_GO_COMMENT
        )

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "capture_base_ref_and_sha",
                    return_value=("main", "a" * 40),
                ) as capture:
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER,
                        repo=_REPO,
                        mode="check-only",
                    )

        assert result["status"] == "ok"
        assert result["source"] == "existing_go"
        assert capture.call_count == 1

    def test_base_sha_drift_is_not_reused_in_check_only(self):
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT], go_comment=_GO_COMMENT
        )

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "capture_base_ref_and_sha",
                    return_value=("main", "c" * 40),
                ):
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER,
                        repo=_REPO,
                        mode="check-only",
                    )

        assert result["status"] == "human_judgment"
        assert result["contract_snapshot_url"] is None
        assert "existing_go_base_binding_drift" in result["errors"][0]

    def test_base_ref_drift_is_not_reused_in_check_only(self):
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT], go_comment=_GO_COMMENT
        )

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "capture_base_ref_and_sha",
                    return_value=("release", "a" * 40),
                ):
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER,
                        repo=_REPO,
                        mode="check-only",
                    )

        assert result["status"] == "human_judgment"
        assert result["contract_snapshot_url"] is None
        assert "existing_go_base_binding_drift" in result["errors"][0]

    def test_base_readback_failure_is_fail_closed_for_existing_go(self):
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT], go_comment=_GO_COMMENT
        )

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "capture_base_ref_and_sha",
                    return_value=(None, None),
                ):
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER,
                        repo=_REPO,
                        mode="check-only",
                    )

        assert result["status"] == "runtime_error"
        assert result["contract_snapshot_url"] is None
        assert "base_ref_or_base_sha_capture_failed" in result["errors"][0]

    def test_auto_post_materializes_new_go_after_base_sha_drift(self):
        parser_mod = _mock_parser_mod(
            comments=[_GO_COMMENT], go_comment=_GO_COMMENT
        )
        review_result = _make_review_result("go")
        posted_comment_id = 2002
        posted_url = f"{_ISSUE_URL}#issuecomment-{posted_comment_id}"
        patched_bodies = []

        def fake_patch(_issue, _repo, comment_id, body, timeout=30):
            assert comment_id == posted_comment_id
            patched_bodies.append(body)
            return True, None

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "capture_base_ref_and_sha",
                    return_value=("main", "d" * 40),
                ):
                    with patch.object(
                        _ecs_mod,
                        "run_contract_review_once",
                        return_value=(review_result, None),
                    ):
                        with patch.object(
                            _ecs_mod,
                            "post_comment",
                            return_value=(posted_url, POST_STATUS_POSTED, 201),
                        ) as post:
                            with patch.object(_ecs_mod, "patch_comment", side_effect=fake_patch):
                                result = ensure_contract_snapshot(
                                    issue_number=_ISSUE_NUMBER,
                                    repo=_REPO,
                                    mode="auto",
                                    do_post=True,
                                )

        assert result["status"] == "ok"
        assert result["source"] == "materialized_go"
        post.assert_called_once()
        assert '"base_sha_at_snapshot":"' + ("d" * 40) + '"' in patched_bodies[0]


class TestFinalAuthorityPostcondition:
    """P1: no path may expose an ``ok`` snapshot after authority drift."""

    def test_existing_go_base_moves_during_final_readback_returns_stale(self):
        parser_mod = _mock_parser_mod(comments=[_GO_COMMENT], go_comment=_GO_COMMENT)
        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod,
                "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod,
                    "verify_snapshot_authority_postcondition",
                    return_value=(False, "authority_base_binding_drift"),
                ):
                    result = ensure_contract_snapshot(
                        issue_number=_ISSUE_NUMBER, repo=_REPO, mode="check-only"
                    )

        assert result["status"] == "stale_or_conflicting_snapshot"
        assert result["contract_snapshot_url"] is None

    def test_concurrent_go_dedupe_drift_returns_stale(self):
        fresh_go = {
            "comment_id": 1002,
            "html_url": "https://example.test/fresh",
            "created_at": "2026-06-13T09:00:00Z",
            "status": "go",
            "inner": _fresh_inner(_SAMPLE_BODY_SHA256, fingerprint_comment_id=1002),
        }
        parser_mod = _mock_parser_mod(comments=[])
        parser_mod.parse_contract_review_results.side_effect = [[], [fresh_go]]
        parser_mod.find_latest_result.side_effect = [None, fresh_go]
        parser_mod.find_latest_go.side_effect = [None, fresh_go]

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
                        "verify_snapshot_authority_postcondition",
                        return_value=(False, "authority_base_binding_drift"),
                    ):
                        result = ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

        assert result["status"] == "stale_or_conflicting_snapshot"
        assert result["contract_snapshot_url"] is None

    def test_materialized_comment_drift_after_patch_returns_stale(self):
        parser_mod = _mock_parser_mod(comments=[])
        parser_mod.parse_contract_review_results.return_value = []
        parser_mod.find_latest_go.return_value = None
        parser_mod.find_latest_result.return_value = None

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
                            f"{_ISSUE_URL}#issuecomment-1003",
                            POST_STATUS_POSTED,
                            201,
                        ),
                    ):
                        with patch.object(
                            _ecs_mod,
                            "verify_snapshot_authority_postcondition",
                            return_value=(False, "authority_comment_fingerprint_mismatch"),
                        ):
                            result = ensure_contract_snapshot(
                                issue_number=_ISSUE_NUMBER,
                                repo=_REPO,
                                mode="auto",
                                do_post=True,
                            )

        assert result["status"] == "stale_or_conflicting_snapshot"
        assert result["contract_snapshot_url"] is None


class TestAuthorityPostconditionReadback:
    def test_issue_updated_at_drift_is_rejected(self):
        with patch.object(
            _ecs_mod,
            "fetch_issue_snapshot",
            return_value=(_SAMPLE_BODY, "2026-06-13T09:00:00Z", None),
        ):
            ok, reason = _real_verify_snapshot_authority_postcondition(
                issue_number=_ISSUE_NUMBER,
                repo=_REPO,
                expected_body_sha256=_SAMPLE_BODY_SHA256,
                expected_updated_at=_SAMPLE_UPDATED_AT,
                expected_comment_id=1001,
                expected_comment_body_sha256=sha256_of(_GO_COMMENT["body"]),
                expected_fingerprint=_fresh_inner(_SAMPLE_BODY_SHA256)[
                    "expected_contract_fingerprint"
                ],
            )

        assert ok is False
        assert reason == "authority_issue_updated_at_mismatch"

    def test_comment_body_drift_is_rejected_before_authority_return(self):
        parser_mod = MagicMock()
        parser_mod.fetch_issue_comments.return_value = (
            [{**_GO_COMMENT, "body": "tampered"}],
            None,
        )
        with patch.object(
            _ecs_mod,
            "fetch_issue_snapshot",
            return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
        ):
            with patch.object(
                _ecs_mod,
                "capture_base_ref_and_sha",
                return_value=("main", "a" * 40),
            ):
                with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
                    ok, reason = _real_verify_snapshot_authority_postcondition(
                        issue_number=_ISSUE_NUMBER,
                        repo=_REPO,
                        expected_body_sha256=_SAMPLE_BODY_SHA256,
                        expected_updated_at=_SAMPLE_UPDATED_AT,
                        expected_comment_id=1001,
                        expected_comment_body_sha256=sha256_of(_GO_COMMENT["body"]),
                        expected_fingerprint=_fresh_inner(_SAMPLE_BODY_SHA256)[
                            "expected_contract_fingerprint"
                        ],
                    )

        assert ok is False
        assert reason == "authority_comment_body_sha256_mismatch"


# ---------------------------------------------------------------------------
# Migrated from test_contract_snapshot_author_binding.py (Issue #1537 PR #1548
# Blocker 2 remediation): that file was outside Issue #1537's Allowed Paths.
# Its regression coverage (controlled-publisher comment-id binding, AC3/AC4
# untrusted-author rejection across all snapshot consumers) is preserved here
# verbatim, with module-level identifiers renamed (_AB_ / _ab_ prefix) to avoid
# colliding with this file's own module globals (e.g. _ISSUE_NUMBER/_REPO).
# This file's autouse _default_trusted_comment_id_binding fixture (above)
# already defaults capture_base_ref_and_sha / verify_controlled_publisher_
# comment_id_binding / patch_comment to success, matching the behavior the
# migrated tests' local patch.object() overrides rely on.
# ---------------------------------------------------------------------------

# Captured before the autouse _default_trusted_comment_id_binding fixture
# (defined above in this file) monkeypatches this attribute to always
# succeed -- these migrated tests exercise the REAL implementation.
_ab_real_verify_controlled_publisher_comment_id_binding = (
    _ecs_mod.verify_controlled_publisher_comment_id_binding
)

_AB_HERE = Path(__file__).resolve().parent
_AB_SCRIPTS_DIR = _AB_HERE.parent / "scripts"
_AB_ICR_SCRIPTS_DIR = _AB_HERE.parents[1] / "issue-contract-review" / "scripts"


def _ab_load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_run_once_mod = _ab_load(
    "run_contract_review_once_binding", _AB_ICR_SCRIPTS_DIR / "run_contract_review_once.py"
)
_parser_mod = _ab_load(
    "contract_review_result_parser_binding",
    _AB_ICR_SCRIPTS_DIR / "contract_review_result_parser.py",
)
_capsule_mod = _ab_load("build_intake_capsule_binding", _AB_SCRIPTS_DIR / "build_intake_capsule.py")

_AB_ISSUE_NUMBER = 1475
_AB_REPO = "squne121/loop-protocol"
_AB_ISSUE_URL = f"https://github.com/{_AB_REPO}/issues/{_AB_ISSUE_NUMBER}"
_AB_SAMPLE_BODY = "## Test body for #1475 binding tests\n\n## Allowed Paths\n- tracked.txt\n"
_AB_SAMPLE_BODY_SHA256 = _ecs_mod.sha256_of(_AB_SAMPLE_BODY)
_AB_SAMPLE_UPDATED_AT = "2026-07-12T00:00:00Z"

# #1475 fix_delta P1 item 2: the only entry in TRUSTED_CONTRACT_PUBLISHERS.
_AB_TRUSTED_AUTHOR_ID = 63350259
_AB_TRUSTED_LOGIN = "squne121"
_AB_TRUSTED_TYPE = "User"
_AB_TRUSTED_ASSOCIATION = "OWNER"


def _ab_go_comment(
    author,
    author_association,
    comment_id: int = 5001,
    author_id=None,
    author_type=None,
) -> dict:
    fingerprint = _ecs_mod.compute_expected_contract_fingerprint(
        issue_number=_AB_ISSUE_NUMBER,
        contract_source_id=str(comment_id),
        contract_body_sha256=_AB_SAMPLE_BODY_SHA256,
        allowed_paths=["tracked.txt"],
        base_ref="main",
        base_sha_at_snapshot="a" * 40,
    )
    return {
        "id": comment_id,
        "html_url": f"{_AB_ISSUE_URL}#issuecomment-{comment_id}",
        "created_at": "2026-07-12T00:00:00Z",
        "updated_at": "2026-07-12T00:00:00Z",
        "author": author,
        "author_association": author_association,
        "author_id": author_id,
        "author_type": author_type,
        "body": f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-07-12T00:00:00Z"
  generated_by: issue-contract-review
  issue_url: {_AB_ISSUE_URL}
  body_sha256: "{_AB_SAMPLE_BODY_SHA256}"
  expected_contract_fingerprint: {json.dumps(fingerprint)}
```
""",
    }


def _ab_trusted_go_comment(comment_id: int = 5001) -> dict:
    """A go comment authored by the sole allowlisted TRUSTED_CONTRACT_PUBLISHERS entry."""
    return _ab_go_comment(
        author=_AB_TRUSTED_LOGIN,
        author_association=_AB_TRUSTED_ASSOCIATION,
        comment_id=comment_id,
        author_id=_AB_TRUSTED_AUTHOR_ID,
        author_type=_AB_TRUSTED_TYPE,
    )


def _ab_make_go_review_result() -> dict:
    return {
        "schema": "CONTRACT_REVIEW_ONCE_RESULT_V1",
        "status": "go",
        "readiness_status": "go",
        "checks": {
            "readiness": "go",
            "blockers": "pass",
            "product_spec": "pass",
            "product_spec_check": {
                "schema": "product_spec_check/v1",
                "applicability": "not_applicable",
                "decision": "pass",
                "triggers": {},
                "conditions": {},
                "blocked_reasons": [],
                "body_sha256": _AB_SAMPLE_BODY_SHA256,
                "source_provenance": {"source_type": "github_issue_body", "body_file": None},
            },
            "vc_preflight": "pass",
        },
        "vc_preflight_classifications": [],
        "errors": [],
    }


def _ab_mock_parser_mod_no_go() -> MagicMock:
    mod = MagicMock()
    mod.fetch_issue_comments.return_value = ([], None)
    mod.parse_contract_review_results.return_value = []
    mod.find_latest_go.return_value = None
    mod.find_latest_result.return_value = None
    return mod


# ---------------------------------------------------------------------------
# AC3: controlled publisher comment ID binding
# ---------------------------------------------------------------------------


def test_controlled_publisher_comment_id_binding_is_required():
    """AC3: controlled publisher の expected comment ID と remote readback
    comment ID が一致する場合だけ materialized snapshot を成功扱いし、
    不一致・欠落を fail-closed にすることを確認する。"""
    parser_mod = _ab_mock_parser_mod_no_go()
    review_result = _ab_make_go_review_result()

    def fake_post(issue_number, repo, body, timeout=30):
        return (f"{_AB_ISSUE_URL}#issuecomment-9999", _ecs_mod.POST_STATUS_POSTED, None)

    # Mismatched binding → fail-closed, no status: ok, no contract_snapshot_url.
    # #1537: capture_base_ref_and_sha is a two-phase fingerprint materialize
    # step invoked before the binding verify this test exercises; default it
    # to success since this test's concern is the binding check, not
    # fingerprint materialization mechanics (mirrors the "Matching binding"
    # case below).
    with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
        with patch.object(
            _ecs_mod, "fetch_issue_snapshot",
            return_value=(_AB_SAMPLE_BODY, _AB_SAMPLE_UPDATED_AT, None),
        ):
            with patch.object(
                _ecs_mod, "run_contract_review_once", return_value=(review_result, None)
            ):
                with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                    with patch.object(
                        _ecs_mod,
                        "verify_controlled_publisher_comment_id_binding",
                        return_value=(False, "binding_id_mismatch"),
                    ):
                        with patch.object(
                            _ecs_mod,
                            "capture_base_ref_and_sha",
                            return_value=("main", "a" * 40),
                        ):
                            mismatched_result = _ecs_mod.ensure_contract_snapshot(
                                issue_number=_AB_ISSUE_NUMBER,
                                repo=_AB_REPO,
                                mode="auto",
                                do_post=True,
                            )

    assert mismatched_result["status"] == "controlled_publisher_binding_failed"
    assert mismatched_result["contract_snapshot_url"] is None

    # Missing expected_comment_id → fail-closed without any subprocess call.
    bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
        _AB_ISSUE_NUMBER, _AB_REPO, None
    )
    assert bound_ok is False
    assert reason == "missing_comment_id"

    # Matching binding → status: ok with a non-null contract_snapshot_url.
    # #1537: capture_base_ref_and_sha / patch_comment are the two-phase
    # fingerprint materialize steps added after the binding verify this test
    # exercises; default them to success since this test's concern is the
    # binding check, not fingerprint materialization mechanics.
    with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
        with patch.object(
            _ecs_mod, "fetch_issue_snapshot",
            return_value=(_AB_SAMPLE_BODY, _AB_SAMPLE_UPDATED_AT, None),
        ):
            with patch.object(
                _ecs_mod, "run_contract_review_once", return_value=(review_result, None)
            ):
                with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                    with patch.object(
                        _ecs_mod,
                        "verify_controlled_publisher_comment_id_binding",
                        return_value=(True, None),
                    ):
                        with patch.object(
                            _ecs_mod,
                            "capture_base_ref_and_sha",
                            return_value=("main", "a" * 40),
                        ):
                            with patch.object(
                                _ecs_mod, "patch_comment", return_value=(True, None)
                            ):
                                matched_result = _ecs_mod.ensure_contract_snapshot(
                                    issue_number=_AB_ISSUE_NUMBER,
                                    repo=_AB_REPO,
                                    mode="auto",
                                    do_post=True,
                                )

    assert matched_result["status"] == "ok"
    assert matched_result["contract_snapshot_url"] is not None


def test_all_snapshot_consumers_reject_untrusted_go():
    """AC4: run_contract_review_once.py / ensure_contract_snapshot.py /
    build_intake_capsule.py の各 snapshot 採用経路で、untrusted author が
    投稿した完全な schema-valid `status: go` を採用しないことを確認する。"""
    untrusted = _ab_go_comment(author="random-outsider", author_association="NONE")

    # 1. contract_review_result_parser.py (shared parser, both consumers use it)
    parsed = _parser_mod.parse_contract_review_results(
        [untrusted], expected_issue_url=_AB_ISSUE_URL
    )
    assert parsed[0]["is_trusted_author"] is False
    assert _parser_mod.find_latest_go(parsed, trusted_only=True) is None

    # 2. run_contract_review_once.py: check_existing_go_comment dedupe source
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        if cmd[:2] == ["gh", "api"] and len(cmd) > 3 and "comments" in cmd[3]:
            result.returncode = 0
            result.stdout = json.dumps(untrusted) + "\n"
            result.stderr = ""
        else:
            result.returncode = 1
            result.stdout = ""
            result.stderr = "not_needed_for_this_test"
        return result

    with patch("subprocess.run", side_effect=fake_run):
        go, _err = _run_once_mod.check_existing_go_comment(_AB_ISSUE_NUMBER, _AB_REPO)
    assert go is None

    # 3. ensure_contract_snapshot.py: check-only mode existing-go adoption
    parser_mod = MagicMock()
    parser_mod.fetch_issue_comments.return_value = ([untrusted], None)
    parser_mod.parse_contract_review_results.return_value = parsed
    parser_mod.find_latest_result.return_value = parsed[0]
    parser_mod.find_latest_go.side_effect = (
        lambda results, trusted_only=False, **_kwargs: (
            None
            if trusted_only
            else next((r for r in results if r.get("status") == "go"), None)
        )
    )

    with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
        with patch.object(
            _ecs_mod, "fetch_issue_snapshot",
            return_value=(_AB_SAMPLE_BODY, _AB_SAMPLE_UPDATED_AT, None),
        ):
            result = _ecs_mod.ensure_contract_snapshot(
                issue_number=_AB_ISSUE_NUMBER, repo=_AB_REPO, mode="check-only"
            )
    assert result["status"] != "ok"

    # 4. build_intake_capsule.py: live comment normalization path
    capsule_results, _counts = _capsule_mod._parse_contract_results(
        [untrusted], _AB_ISSUE_URL
    )
    assert capsule_results[0]["is_trusted_author"] is False
    assert _capsule_mod._find_latest_go(capsule_results) is None


class TestControlledPublisherCommentIdBinding:
    def test_extract_comment_id_from_url(self):
        assert _ecs_mod.extract_comment_id_from_url(f"{_AB_ISSUE_URL}#issuecomment-42") == 42
        assert _ecs_mod.extract_comment_id_from_url("https://example.test/no-anchor") is None
        assert _ecs_mod.extract_comment_id_from_url(None) is None
        assert _ecs_mod.extract_comment_id_from_url("") is None

    def test_binding_verification_missing_id_is_fail_closed(self):
        bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
            _AB_ISSUE_NUMBER, _AB_REPO, None
        )
        assert bound_ok is False
        assert reason == "missing_comment_id"

    @staticmethod
    def _full_payload(
        comment_id=1234,
        issue_number=_AB_ISSUE_NUMBER,
        html_url=None,
        author_id=_AB_TRUSTED_AUTHOR_ID,
        author_login=_AB_TRUSTED_LOGIN,
        author_type=_AB_TRUSTED_TYPE,
        author_association=_AB_TRUSTED_ASSOCIATION,
        body="unused-body",
    ) -> dict:
        return {
            "id": comment_id,
            "issue_url": f"https://api.github.com/repos/{_AB_REPO}/issues/{issue_number}",
            "html_url": html_url or f"{_AB_ISSUE_URL}#issuecomment-{comment_id}",
            "user": {"id": author_id, "login": author_login, "type": author_type},
            "author_association": author_association,
            "body": body,
        }

    def test_binding_verification_id_mismatch_is_fail_closed(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(self._full_payload(comment_id=999))
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_id_mismatch"

    def test_binding_verification_issue_mismatch_is_fail_closed(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(self._full_payload(issue_number=9999))
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_issue_mismatch"

    def test_binding_verification_readback_error_is_fail_closed(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 1
            run_mock.return_value.stdout = ""
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_readback_error"

    def test_binding_verification_match_succeeds(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(self._full_payload())
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is True
        assert reason is None

    def test_binding_verification_html_url_mismatch_is_fail_closed(self):
        """fix_delta P1 item 3: the direct-GET html_url must match the
        comment id being verified, not just the numeric id field."""
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(html_url=f"{_AB_ISSUE_URL}#issuecomment-9999999")
            )
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_html_url_mismatch"

    def test_binding_verification_untrusted_collaborator_is_fail_closed(self):
        """fix_delta P1 item 2/3: an unauthorized COLLABORATOR posting a
        schema-valid, id/issue-matching comment must still be rejected."""
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(
                    author_id=987654,
                    author_login="some-collaborator",
                    author_type="User",
                    author_association="COLLABORATOR",
                )
            )
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_publisher_untrusted"

    def test_binding_verification_untrusted_member_is_fail_closed(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(
                    author_id=555555,
                    author_login="some-member",
                    author_type="User",
                    author_association="MEMBER",
                )
            )
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_publisher_untrusted"

    def test_binding_verification_correct_login_wrong_id_is_fail_closed(self):
        """The login string alone must never authorize -- only the id match does."""
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(author_id=1, author_login=_AB_TRUSTED_LOGIN)
            )
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_publisher_untrusted"

    def test_binding_verification_correct_id_wrong_login_is_fail_closed(self):
        """A rename/spoofed-login on the correct id must never authorize."""
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(author_id=_AB_TRUSTED_AUTHOR_ID, author_login="not-squne121")
            )
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_publisher_untrusted"

    def test_binding_verification_type_mismatch_is_fail_closed(self):
        """A Bot account impersonating the trusted id/login must be rejected."""
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(author_type="Bot")
            )
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER, _AB_REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_publisher_untrusted"

    def test_binding_verification_body_hash_mismatch_is_fail_closed(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(body="different-body-than-expected")
            )
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER,
                _AB_REPO,
                1234,
                expected_body_sha256=_ecs_mod.sha256_of("expected-body"),
            )
        assert bound_ok is False
        assert reason == "binding_body_hash_mismatch"

    def test_binding_verification_body_hash_match_succeeds(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                self._full_payload(body="expected-body")
            )
            bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
                _AB_ISSUE_NUMBER,
                _AB_REPO,
                1234,
                expected_body_sha256=_ecs_mod.sha256_of("expected-body"),
            )
        assert bound_ok is True
        assert reason is None

    def test_binding_verification_comment_id_bool_is_fail_closed(self):
        bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
            _AB_ISSUE_NUMBER, _AB_REPO, True
        )
        assert bound_ok is False
        assert reason == "missing_comment_id"

    def test_binding_verification_comment_id_string_is_fail_closed(self):
        bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
            _AB_ISSUE_NUMBER, _AB_REPO, "1234"
        )
        assert bound_ok is False
        assert reason == "missing_comment_id"

    def test_binding_verification_comment_id_zero_is_fail_closed(self):
        bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
            _AB_ISSUE_NUMBER, _AB_REPO, 0
        )
        assert bound_ok is False
        assert reason == "missing_comment_id"

    def test_binding_verification_comment_id_negative_is_fail_closed(self):
        bound_ok, reason = _ab_real_verify_controlled_publisher_comment_id_binding(
            _AB_ISSUE_NUMBER, _AB_REPO, -1234
        )
        assert bound_ok is False
        assert reason == "missing_comment_id"

    def test_materialization_blocked_when_binding_fails(self):
        """GIVEN a successful comment post WHEN the id-binding readback
        mismatches THEN ensure_contract_snapshot fails closed and does not
        report status: ok, even though post_comment itself succeeded."""
        parser_mod = _ab_mock_parser_mod_no_go()
        review_result = _ab_make_go_review_result()

        def fake_post(issue_number, repo, body, timeout=30):
            return (f"{_AB_ISSUE_URL}#issuecomment-9999", _ecs_mod.POST_STATUS_POSTED, None)

        # #1537: capture_base_ref_and_sha is a two-phase fingerprint
        # materialize step invoked before the binding verify this test
        # exercises; default it to success since this test's concern is the
        # binding check, not fingerprint materialization mechanics.
        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod, "fetch_issue_snapshot",
                return_value=(_AB_SAMPLE_BODY, _AB_SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod, "run_contract_review_once", return_value=(review_result, None)
                ):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        with patch.object(
                            _ecs_mod,
                            "verify_controlled_publisher_comment_id_binding",
                            return_value=(False, "binding_id_mismatch"),
                        ):
                            with patch.object(
                                _ecs_mod,
                                "capture_base_ref_and_sha",
                                return_value=("main", "a" * 40),
                            ):
                                result = _ecs_mod.ensure_contract_snapshot(
                                    issue_number=_AB_ISSUE_NUMBER,
                                    repo=_AB_REPO,
                                    mode="auto",
                                    do_post=True,
                                )

        assert result["status"] == "controlled_publisher_binding_failed"
        assert result["contract_snapshot_url"] is None
        assert any("binding" in e for e in result["errors"])

    def test_materialization_succeeds_when_binding_matches(self):
        parser_mod = _ab_mock_parser_mod_no_go()
        review_result = _ab_make_go_review_result()

        def fake_post(issue_number, repo, body, timeout=30):
            return (f"{_AB_ISSUE_URL}#issuecomment-9999", _ecs_mod.POST_STATUS_POSTED, None)

        # #1537: default the two-phase fingerprint materialize steps to
        # success; this test's concern is the binding check, not fingerprint
        # materialization mechanics.
        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod, "fetch_issue_snapshot",
                return_value=(_AB_SAMPLE_BODY, _AB_SAMPLE_UPDATED_AT, None),
            ):
                with patch.object(
                    _ecs_mod, "run_contract_review_once", return_value=(review_result, None)
                ):
                    with patch.object(_ecs_mod, "post_comment", side_effect=fake_post):
                        with patch.object(
                            _ecs_mod,
                            "verify_controlled_publisher_comment_id_binding",
                            return_value=(True, None),
                        ):
                            with patch.object(
                                _ecs_mod,
                                "capture_base_ref_and_sha",
                                return_value=("main", "a" * 40),
                            ):
                                with patch.object(
                                    _ecs_mod, "patch_comment", return_value=(True, None)
                                ):
                                    result = _ecs_mod.ensure_contract_snapshot(
                                        issue_number=_AB_ISSUE_NUMBER,
                                        repo=_AB_REPO,
                                        mode="auto",
                                        do_post=True,
                                    )

        assert result["status"] == "ok"
        assert result["contract_snapshot_url"] == f"{_AB_ISSUE_URL}#issuecomment-9999"


# ---------------------------------------------------------------------------
# AC4: all snapshot consumers reject untrusted go
# ---------------------------------------------------------------------------


class TestAllSnapshotConsumersRejectUntrustedGo:
    def test_contract_review_result_parser_marks_untrusted_go(self):
        untrusted = _ab_go_comment(author="random-outsider", author_association="NONE")
        results = _parser_mod.parse_contract_review_results(
            [untrusted], expected_issue_url=_AB_ISSUE_URL
        )
        assert results[0]["is_trusted_author"] is False
        assert _parser_mod.find_latest_go(results, trusted_only=True) is None
        assert _parser_mod.find_latest_go(results, trusted_only=False) is not None

    def test_run_contract_review_once_check_existing_go_rejects_untrusted(self):
        """GIVEN an untrusted, schema-valid status:go comment WHEN
        run_contract_review_once.check_existing_go_comment runs THEN the
        untrusted snapshot is not adopted as an existing go (dedupe source)."""
        untrusted = _ab_go_comment(author="random-outsider", author_association="NONE")

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if cmd[:2] == ["gh", "api"] and len(cmd) > 3 and "comments" in cmd[3]:
                result.returncode = 0
                result.stdout = json.dumps(untrusted) + "\n"
                result.stderr = ""
            else:
                result.returncode = 1
                result.stdout = ""
                result.stderr = "not_needed_for_this_test"
            return result

        with patch("subprocess.run", side_effect=fake_run):
            go, err = _run_once_mod.check_existing_go_comment(_AB_ISSUE_NUMBER, _AB_REPO)

        assert go is None

    def test_run_contract_review_once_check_existing_go_accepts_trusted(self):
        trusted = _ab_trusted_go_comment()

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if cmd[:2] == ["gh", "api"] and len(cmd) > 3 and "comments" in cmd[3]:
                result.returncode = 0
                result.stdout = json.dumps(trusted) + "\n"
                result.stderr = ""
            elif cmd[:3] == ["gh", "issue", "view"]:
                result.returncode = 0
                result.stdout = json.dumps({"body": _AB_SAMPLE_BODY})
                result.stderr = ""
            else:
                result.returncode = 1
                result.stdout = ""
                result.stderr = "not_needed_for_this_test"
            return result

        # is_go_current's fuller freshness contract (vc_preflight classifications,
        # product_spec_check body binding) is exercised in
        # test_ensure_contract_snapshot.py; here we isolate the trust filter by
        # stubbing that unrelated freshness predicate to True.
        with patch("subprocess.run", side_effect=fake_run):
            with patch.object(_run_once_mod, "_is_current_go_snapshot", return_value=True):
                go, err = _run_once_mod.check_existing_go_comment(_AB_ISSUE_NUMBER, _AB_REPO)

        # Trusted + fresh body hash → adopted as an existing go.
        assert go is not None
        assert go["is_trusted_author"] is True

    def test_build_intake_capsule_rejects_untrusted_go(self):
        untrusted = _ab_go_comment(author="random-outsider", author_association="NONE")
        results, _counts = _capsule_mod._parse_contract_results([untrusted], _AB_ISSUE_URL)
        assert results[0]["is_trusted_author"] is False
        assert _capsule_mod._find_latest_go(results) is None

    def test_build_intake_capsule_accepts_trusted_go(self):
        trusted = _ab_trusted_go_comment()
        results, _counts = _capsule_mod._parse_contract_results(
            [trusted], _AB_ISSUE_URL, _AB_ISSUE_NUMBER
        )
        assert results[0]["is_trusted_author"] is True
        latest_go = _capsule_mod._find_latest_go(results)
        assert latest_go is not None
        assert latest_go["html_url"] == trusted["html_url"]

    def test_build_intake_capsule_rejects_unauthorized_collaborator_go(self):
        """fix_delta P1 item 2: association alone (COLLABORATOR) must not
        authorize an account outside TRUSTED_CONTRACT_PUBLISHERS."""
        collaborator = _ab_go_comment(
            author="some-collaborator",
            author_association="COLLABORATOR",
            author_id=999111,
            author_type="User",
        )
        results, _counts = _capsule_mod._parse_contract_results([collaborator], _AB_ISSUE_URL)
        assert results[0]["is_trusted_author"] is False
        assert _capsule_mod._find_latest_go(results) is None

    def test_untrusted_blocked_does_not_preempt_trusted_go_precedence(self):
        """fix_delta P1 item 1 regression: a schema-valid untrusted `blocked`
        posted AFTER a trusted `go` must not take latest-result precedence in
        any of the three consumers."""
        trusted_go = _ab_trusted_go_comment(comment_id=1)
        trusted_go["created_at"] = "2026-07-12T00:00:00Z"
        untrusted_blocked = {
            "id": 2,
            "html_url": f"{_AB_ISSUE_URL}#issuecomment-2",
            "created_at": "2026-07-12T01:00:00Z",
            "author": "outside-actor",
            "author_association": "NONE",
            "author_id": 42424242,
            "author_type": "User",
            "body": f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: blocked
  generated_at: "2026-07-12T01:00:00Z"
  generated_by: issue-contract-review
  issue_url: {_AB_ISSUE_URL}
```
""",
        }
        comments = [trusted_go, untrusted_blocked]

        # 1. shared parser: trusted_only precedence must select the trusted go.
        parsed = _parser_mod.parse_contract_review_results(
            comments, expected_issue_url=_AB_ISSUE_URL
        )
        latest_trusted = _parser_mod.find_latest_result(parsed, trusted_only=True)
        assert latest_trusted is not None
        assert latest_trusted["status"] == "go"

        # 2. build_intake_capsule: same precedence contract.
        capsule_results, _counts = _capsule_mod._parse_contract_results(
            comments, _AB_ISSUE_URL
        )
        capsule_latest = _capsule_mod._find_latest_result(
            capsule_results, trusted_only=True
        )
        assert capsule_latest is not None
        assert capsule_latest["status"] == "go"

    def test_untrusted_go_does_not_preempt_trusted_blocked_precedence(self):
        """The mirror case: an untrusted `go` posted after a trusted
        `blocked` must not be adopted as authoritative."""
        trusted_blocked = {
            "id": 1,
            "html_url": f"{_AB_ISSUE_URL}#issuecomment-1",
            "created_at": "2026-07-12T00:00:00Z",
            "author": _AB_TRUSTED_LOGIN,
            "author_association": _AB_TRUSTED_ASSOCIATION,
            "author_id": _AB_TRUSTED_AUTHOR_ID,
            "author_type": _AB_TRUSTED_TYPE,
            "body": f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: blocked
  generated_at: "2026-07-12T00:00:00Z"
  generated_by: issue-contract-review
  issue_url: {_AB_ISSUE_URL}
```
""",
        }
        untrusted_go = _ab_go_comment(
            author="outside-actor",
            author_association="NONE",
            comment_id=2,
            author_id=42424242,
            author_type="User",
        )
        untrusted_go["created_at"] = "2026-07-12T01:00:00Z"
        comments = [trusted_blocked, untrusted_go]

        parsed = _parser_mod.parse_contract_review_results(
            comments, expected_issue_url=_AB_ISSUE_URL
        )
        latest_trusted = _parser_mod.find_latest_result(parsed, trusted_only=True)
        assert latest_trusted is not None
        assert latest_trusted["status"] == "blocked"
        assert _parser_mod.find_latest_go(parsed, trusted_only=True) is None
