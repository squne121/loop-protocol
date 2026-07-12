"""
tests/test_contract_snapshot_author_binding.py

AC3: controlled publisher の expected comment ID と remote readback comment ID
     が一致する場合だけ materialized snapshot を成功扱いし、不一致・欠落を
     fail-closed にすることを確認する。
AC4: run_contract_review_once.py / ensure_contract_snapshot.py /
     build_intake_capsule.py の各 snapshot 採用経路で、untrusted author が
     投稿した完全な schema-valid `status: go` を採用しないことを回帰確認する。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_ICR_SCRIPTS_DIR = _HERE.parents[1] / "issue-contract-review" / "scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_ecs_mod = _load("ensure_contract_snapshot_binding", _SCRIPTS_DIR / "ensure_contract_snapshot.py")
_run_once_mod = _load(
    "run_contract_review_once_binding", _ICR_SCRIPTS_DIR / "run_contract_review_once.py"
)
_parser_mod = _load(
    "contract_review_result_parser_binding",
    _ICR_SCRIPTS_DIR / "contract_review_result_parser.py",
)
_capsule_mod = _load("build_intake_capsule_binding", _SCRIPTS_DIR / "build_intake_capsule.py")

_ISSUE_NUMBER = 1475
_REPO = "squne121/loop-protocol"
_ISSUE_URL = f"https://github.com/{_REPO}/issues/{_ISSUE_NUMBER}"
_SAMPLE_BODY = "## Test body for #1475 binding tests"
_SAMPLE_BODY_SHA256 = _ecs_mod.sha256_of(_SAMPLE_BODY)
_SAMPLE_UPDATED_AT = "2026-07-12T00:00:00Z"


def _go_comment(author, author_association, comment_id: int = 5001) -> dict:
    return {
        "id": comment_id,
        "html_url": f"{_ISSUE_URL}#issuecomment-{comment_id}",
        "created_at": "2026-07-12T00:00:00Z",
        "updated_at": "2026-07-12T00:00:00Z",
        "author": author,
        "author_association": author_association,
        "body": f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-07-12T00:00:00Z"
  generated_by: issue-contract-review
  issue_url: {_ISSUE_URL}
  body_sha256: "{_SAMPLE_BODY_SHA256}"
```
""",
    }


def _make_go_review_result() -> dict:
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
                "body_sha256": _SAMPLE_BODY_SHA256,
                "source_provenance": {"source_type": "github_issue_body", "body_file": None},
            },
            "vc_preflight": "pass",
        },
        "vc_preflight_classifications": [],
        "errors": [],
    }


def _mock_parser_mod_no_go() -> MagicMock:
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
    parser_mod = _mock_parser_mod_no_go()
    review_result = _make_go_review_result()

    def fake_post(issue_number, repo, body, timeout=30):
        return (f"{_ISSUE_URL}#issuecomment-9999", _ecs_mod.POST_STATUS_POSTED, None)

    # Mismatched binding → fail-closed, no status: ok, no contract_snapshot_url.
    with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
        with patch.object(
            _ecs_mod, "fetch_issue_snapshot",
            return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
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
                        mismatched_result = _ecs_mod.ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

    assert mismatched_result["status"] == "controlled_publisher_binding_failed"
    assert mismatched_result["contract_snapshot_url"] is None

    # Missing expected_comment_id → fail-closed without any subprocess call.
    bound_ok, reason = _ecs_mod.verify_controlled_publisher_comment_id_binding(
        _ISSUE_NUMBER, _REPO, None
    )
    assert bound_ok is False
    assert reason == "missing_comment_id"

    # Matching binding → status: ok with a non-null contract_snapshot_url.
    with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
        with patch.object(
            _ecs_mod, "fetch_issue_snapshot",
            return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
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
                        matched_result = _ecs_mod.ensure_contract_snapshot(
                            issue_number=_ISSUE_NUMBER,
                            repo=_REPO,
                            mode="auto",
                            do_post=True,
                        )

    assert matched_result["status"] == "ok"
    assert matched_result["contract_snapshot_url"] is not None


def test_all_snapshot_consumers_reject_untrusted_go():
    """AC4: run_contract_review_once.py / ensure_contract_snapshot.py /
    build_intake_capsule.py の各 snapshot 採用経路で、untrusted author が
    投稿した完全な schema-valid `status: go` を採用しないことを確認する。"""
    untrusted = _go_comment(author="random-outsider", author_association="NONE")

    # 1. contract_review_result_parser.py (shared parser, both consumers use it)
    parsed = _parser_mod.parse_contract_review_results(
        [untrusted], expected_issue_url=_ISSUE_URL
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
        go, _err = _run_once_mod.check_existing_go_comment(_ISSUE_NUMBER, _REPO)
    assert go is None

    # 3. ensure_contract_snapshot.py: check-only mode existing-go adoption
    parser_mod = MagicMock()
    parser_mod.fetch_issue_comments.return_value = ([untrusted], None)
    parser_mod.parse_contract_review_results.return_value = parsed
    parser_mod.find_latest_result.return_value = parsed[0]
    parser_mod.find_latest_go.side_effect = (
        lambda results, trusted_only=False: (
            None
            if trusted_only
            else next((r for r in results if r.get("status") == "go"), None)
        )
    )

    with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
        with patch.object(
            _ecs_mod, "fetch_issue_snapshot",
            return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
        ):
            result = _ecs_mod.ensure_contract_snapshot(
                issue_number=_ISSUE_NUMBER, repo=_REPO, mode="check-only"
            )
    assert result["status"] != "ok"

    # 4. build_intake_capsule.py: live comment normalization path
    capsule_results, _counts = _capsule_mod._parse_contract_results(
        [untrusted], _ISSUE_URL
    )
    assert capsule_results[0]["is_trusted_author"] is False
    assert _capsule_mod._find_latest_go(capsule_results) is None


class TestControlledPublisherCommentIdBinding:
    def test_extract_comment_id_from_url(self):
        assert _ecs_mod.extract_comment_id_from_url(f"{_ISSUE_URL}#issuecomment-42") == 42
        assert _ecs_mod.extract_comment_id_from_url("https://example.test/no-anchor") is None
        assert _ecs_mod.extract_comment_id_from_url(None) is None
        assert _ecs_mod.extract_comment_id_from_url("") is None

    def test_binding_verification_missing_id_is_fail_closed(self):
        bound_ok, reason = _ecs_mod.verify_controlled_publisher_comment_id_binding(
            _ISSUE_NUMBER, _REPO, None
        )
        assert bound_ok is False
        assert reason == "missing_comment_id"

    def test_binding_verification_id_mismatch_is_fail_closed(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                {"id": 999, "issue_url": f"https://api.github.com/repos/{_REPO}/issues/{_ISSUE_NUMBER}"}
            )
            bound_ok, reason = _ecs_mod.verify_controlled_publisher_comment_id_binding(
                _ISSUE_NUMBER, _REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_id_mismatch"

    def test_binding_verification_issue_mismatch_is_fail_closed(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                {"id": 1234, "issue_url": f"https://api.github.com/repos/{_REPO}/issues/9999"}
            )
            bound_ok, reason = _ecs_mod.verify_controlled_publisher_comment_id_binding(
                _ISSUE_NUMBER, _REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_issue_mismatch"

    def test_binding_verification_readback_error_is_fail_closed(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 1
            run_mock.return_value.stdout = ""
            bound_ok, reason = _ecs_mod.verify_controlled_publisher_comment_id_binding(
                _ISSUE_NUMBER, _REPO, 1234
            )
        assert bound_ok is False
        assert reason == "binding_readback_error"

    def test_binding_verification_match_succeeds(self):
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(
                {"id": 1234, "issue_url": f"https://api.github.com/repos/{_REPO}/issues/{_ISSUE_NUMBER}"}
            )
            bound_ok, reason = _ecs_mod.verify_controlled_publisher_comment_id_binding(
                _ISSUE_NUMBER, _REPO, 1234
            )
        assert bound_ok is True
        assert reason is None

    def test_materialization_blocked_when_binding_fails(self):
        """GIVEN a successful comment post WHEN the id-binding readback
        mismatches THEN ensure_contract_snapshot fails closed and does not
        report status: ok, even though post_comment itself succeeded."""
        parser_mod = _mock_parser_mod_no_go()
        review_result = _make_go_review_result()

        def fake_post(issue_number, repo, body, timeout=30):
            return (f"{_ISSUE_URL}#issuecomment-9999", _ecs_mod.POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod, "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
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
                            result = _ecs_mod.ensure_contract_snapshot(
                                issue_number=_ISSUE_NUMBER,
                                repo=_REPO,
                                mode="auto",
                                do_post=True,
                            )

        assert result["status"] == "controlled_publisher_binding_failed"
        assert result["contract_snapshot_url"] is None
        assert any("binding" in e for e in result["errors"])

    def test_materialization_succeeds_when_binding_matches(self):
        parser_mod = _mock_parser_mod_no_go()
        review_result = _make_go_review_result()

        def fake_post(issue_number, repo, body, timeout=30):
            return (f"{_ISSUE_URL}#issuecomment-9999", _ecs_mod.POST_STATUS_POSTED, None)

        with patch.object(_ecs_mod, "_import_parser_module", return_value=parser_mod):
            with patch.object(
                _ecs_mod, "fetch_issue_snapshot",
                return_value=(_SAMPLE_BODY, _SAMPLE_UPDATED_AT, None),
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
                            result = _ecs_mod.ensure_contract_snapshot(
                                issue_number=_ISSUE_NUMBER,
                                repo=_REPO,
                                mode="auto",
                                do_post=True,
                            )

        assert result["status"] == "ok"
        assert result["contract_snapshot_url"] == f"{_ISSUE_URL}#issuecomment-9999"


# ---------------------------------------------------------------------------
# AC4: all snapshot consumers reject untrusted go
# ---------------------------------------------------------------------------


class TestAllSnapshotConsumersRejectUntrustedGo:
    def test_contract_review_result_parser_marks_untrusted_go(self):
        untrusted = _go_comment(author="random-outsider", author_association="NONE")
        results = _parser_mod.parse_contract_review_results(
            [untrusted], expected_issue_url=_ISSUE_URL
        )
        assert results[0]["is_trusted_author"] is False
        assert _parser_mod.find_latest_go(results, trusted_only=True) is None
        assert _parser_mod.find_latest_go(results, trusted_only=False) is not None

    def test_run_contract_review_once_check_existing_go_rejects_untrusted(self):
        """GIVEN an untrusted, schema-valid status:go comment WHEN
        run_contract_review_once.check_existing_go_comment runs THEN the
        untrusted snapshot is not adopted as an existing go (dedupe source)."""
        untrusted = _go_comment(author="random-outsider", author_association="NONE")

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
            go, err = _run_once_mod.check_existing_go_comment(_ISSUE_NUMBER, _REPO)

        assert go is None

    def test_run_contract_review_once_check_existing_go_accepts_trusted(self):
        trusted = _go_comment(author="repo-owner", author_association="OWNER")

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if cmd[:2] == ["gh", "api"] and len(cmd) > 3 and "comments" in cmd[3]:
                result.returncode = 0
                result.stdout = json.dumps(trusted) + "\n"
                result.stderr = ""
            elif cmd[:3] == ["gh", "issue", "view"]:
                result.returncode = 0
                result.stdout = json.dumps({"body": _SAMPLE_BODY})
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
                go, err = _run_once_mod.check_existing_go_comment(_ISSUE_NUMBER, _REPO)

        # Trusted + fresh body hash → adopted as an existing go.
        assert go is not None
        assert go["is_trusted_author"] is True

    def test_build_intake_capsule_rejects_untrusted_go(self):
        untrusted = _go_comment(author="random-outsider", author_association="NONE")
        results, _counts = _capsule_mod._parse_contract_results([untrusted], _ISSUE_URL)
        assert results[0]["is_trusted_author"] is False
        assert _capsule_mod._find_latest_go(results) is None

    def test_build_intake_capsule_accepts_trusted_go(self):
        trusted = _go_comment(author="repo-owner", author_association="OWNER")
        results, _counts = _capsule_mod._parse_contract_results([trusted], _ISSUE_URL)
        assert results[0]["is_trusted_author"] is True
        latest_go = _capsule_mod._find_latest_go(results)
        assert latest_go is not None
        assert latest_go["html_url"] == trusted["html_url"]
