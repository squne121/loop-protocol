"""
tests/test_contract_snapshot_author_trust.py

Unit tests for GitHub provenance / trusted publisher identity handling in
contract_review_result_parser.py (#1475).

AC1: comment fetch が user.login / author_association を保持し、parser result の
     provenance として downstream consumer へ渡ることを確認する。
AC2: trusted publisher identity と許可された author_association を満たす
     schema-valid snapshot だけが authoritative candidate として採用され、
     identity/association 欠落を含む untrusted snapshot は無視されることを
     確認する。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_PARSER_PATH = _SCRIPTS_DIR / "contract_review_result_parser.py"

spec = importlib.util.spec_from_file_location(
    "contract_review_result_parser_trust", _PARSER_PATH
)
assert spec is not None and spec.loader is not None
_parser_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_parser_mod)  # type: ignore[union-attr]

fetch_issue_comments = _parser_mod.fetch_issue_comments
parse_contract_review_results = _parser_mod.parse_contract_review_results
find_latest_go = _parser_mod.find_latest_go
is_trusted_snapshot_author = _parser_mod.is_trusted_snapshot_author
TRUSTED_AUTHOR_ASSOCIATIONS = _parser_mod.TRUSTED_AUTHOR_ASSOCIATIONS

_ISSUE_NUMBER = 1475
_REPO = "squne121/loop-protocol"
_ISSUE_URL = f"https://github.com/{_REPO}/issues/{_ISSUE_NUMBER}"


def _go_body(body_sha256: str = "sha256:" + "a" * 64) -> str:
    return f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "2026-07-12T00:00:00Z"
  generated_by: issue-contract-review
  issue_url: {_ISSUE_URL}
  body_sha256: "{body_sha256}"
```
"""


# ---------------------------------------------------------------------------
# AC1: fetch preserves provenance fields
# ---------------------------------------------------------------------------


def test_fetch_and_parser_preserve_trusted_comment_provenance():
    """AC1: comment fetch が user.login / author_association を保持し、
    parser result の provenance として downstream consumer へ渡ることを確認する。"""
    comment = {
        "id": 1,
        "html_url": f"{_ISSUE_URL}#issuecomment-1",
        "created_at": "2026-07-12T00:00:00Z",
        "author": "trusted-owner",
        "author_association": "OWNER",
        "body": _go_body(),
    }
    with patch("subprocess.run") as run_mock:
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = json.dumps(comment)
        run_mock.return_value.stderr = ""
        comments, err = fetch_issue_comments(_ISSUE_NUMBER, _REPO)

    assert err is None
    assert comments[0]["author"] == "trusted-owner"
    assert comments[0]["author_association"] == "OWNER"

    results = parse_contract_review_results(comments, expected_issue_url=_ISSUE_URL)
    assert len(results) == 1
    assert results[0]["author"] == "trusted-owner"
    assert results[0]["author_association"] == "OWNER"
    assert results[0]["is_trusted_author"] is True


def test_untrusted_schema_valid_go_is_ignored():
    """AC2: trusted publisher identity と許可された author_association を満たす
    schema-valid snapshot だけが authoritative candidate として採用され、
    identity/association 欠落を含む untrusted snapshot は無視されることを確認する。"""
    untrusted_comment = {
        "id": 42,
        "html_url": f"{_ISSUE_URL}#issuecomment-42",
        "created_at": "2026-07-12T00:00:00Z",
        "author": "random-outsider",
        "author_association": "NONE",
        "body": _go_body(),
    }
    results = parse_contract_review_results(
        [untrusted_comment], expected_issue_url=_ISSUE_URL
    )
    assert len(results) == 1
    assert results[0]["is_trusted_author"] is False

    # trusted_only=True: untrusted go must not be authoritative.
    assert find_latest_go(results, trusted_only=True) is None
    # trusted_only=False (legacy default): still schema-valid and returned
    # for callers that have not opted into the trust gate.
    assert find_latest_go(results, trusted_only=False) is not None


class TestFetchPreservesProvenance:
    def test_fetch_issue_comments_jq_projection_requests_provenance_fields(self):
        """GIVEN fetch_issue_comments WHEN it builds the gh api call THEN the jq
        projection requests user.login and author_association."""
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = ""
            run_mock.return_value.stderr = ""
            fetch_issue_comments(_ISSUE_NUMBER, _REPO)

        called_cmd = run_mock.call_args[0][0]
        jq_index = called_cmd.index("--jq")
        jq_arg = called_cmd[jq_index + 1]
        assert "author: .user.login" in jq_arg
        assert "author_association" in jq_arg

    def test_fetch_and_parser_preserve_trusted_comment_provenance(self):
        """GIVEN a comment with user.login/author_association WHEN fetched and
        parsed THEN both fetch result and parse result retain provenance."""
        comment = {
            "id": 1,
            "html_url": f"{_ISSUE_URL}#issuecomment-1",
            "created_at": "2026-07-12T00:00:00Z",
            "author": "trusted-owner",
            "author_association": "OWNER",
            "body": _go_body(),
        }
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = json.dumps(comment)
            run_mock.return_value.stderr = ""
            comments, err = fetch_issue_comments(_ISSUE_NUMBER, _REPO)

        assert err is None
        assert comments[0]["author"] == "trusted-owner"
        assert comments[0]["author_association"] == "OWNER"

        results = parse_contract_review_results(comments, expected_issue_url=_ISSUE_URL)
        assert len(results) == 1
        assert results[0]["author"] == "trusted-owner"
        assert results[0]["author_association"] == "OWNER"
        assert results[0]["is_trusted_author"] is True


# ---------------------------------------------------------------------------
# AC2: untrusted schema-valid go is ignored
# ---------------------------------------------------------------------------


class TestUntrustedSchemaValidGoIsIgnored:
    @pytest.mark.parametrize("association", sorted(TRUSTED_AUTHOR_ASSOCIATIONS))
    def test_trusted_associations_are_trusted(self, association):
        assert is_trusted_snapshot_author("some-actor", association) is True

    @pytest.mark.parametrize(
        "author,association",
        [
            ("outsider", "NONE"),
            ("outsider", "CONTRIBUTOR"),
            ("outsider", "FIRST_TIME_CONTRIBUTOR"),
            ("outsider", "FIRST_TIMER"),
            (None, "OWNER"),
            ("", "OWNER"),
            ("outsider", None),
            ("outsider", ""),
            (None, None),
        ],
    )
    def test_untrusted_or_missing_identity_is_untrusted(self, author, association):
        assert is_trusted_snapshot_author(author, association) is False

    def test_untrusted_schema_valid_go_is_ignored(self):
        """GIVEN a schema-valid status:go comment posted by an untrusted
        outsider (author_association: NONE) WHEN find_latest_go is called
        with trusted_only=True THEN the untrusted snapshot is not returned as
        authoritative, even though it is schema-valid."""
        untrusted_comment = {
            "id": 42,
            "html_url": f"{_ISSUE_URL}#issuecomment-42",
            "created_at": "2026-07-12T00:00:00Z",
            "author": "random-outsider",
            "author_association": "NONE",
            "body": _go_body(),
        }
        results = parse_contract_review_results(
            [untrusted_comment], expected_issue_url=_ISSUE_URL
        )
        assert len(results) == 1
        assert results[0]["is_trusted_author"] is False

        # trusted_only=True: untrusted go must not be authoritative.
        assert find_latest_go(results, trusted_only=True) is None
        # trusted_only=False (legacy default): still schema-valid and returned
        # for callers that have not opted into the trust gate.
        assert find_latest_go(results, trusted_only=False) is not None

    def test_missing_provenance_schema_valid_go_is_ignored(self):
        """A schema-valid go comment with no author/author_association at all
        (e.g. legacy fixture, comment object without provenance) must also be
        rejected under trusted_only=True."""
        legacy_comment = {
            "id": 43,
            "html_url": f"{_ISSUE_URL}#issuecomment-43",
            "created_at": "2026-07-12T00:00:00Z",
            "body": _go_body(),
        }
        results = parse_contract_review_results(
            [legacy_comment], expected_issue_url=_ISSUE_URL
        )
        assert len(results) == 1
        assert results[0]["is_trusted_author"] is False
        assert find_latest_go(results, trusted_only=True) is None

    def test_trusted_owner_go_is_returned(self):
        trusted_comment = {
            "id": 44,
            "html_url": f"{_ISSUE_URL}#issuecomment-44",
            "created_at": "2026-07-12T00:00:00Z",
            "author": "repo-owner",
            "author_association": "OWNER",
            "body": _go_body(),
        }
        results = parse_contract_review_results(
            [trusted_comment], expected_issue_url=_ISSUE_URL
        )
        go = find_latest_go(results, trusted_only=True)
        assert go is not None
        assert go["html_url"] == trusted_comment["html_url"]
