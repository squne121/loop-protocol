"""
tests/test_contract_snapshot_author_trust.py

Unit tests for GitHub provenance / trusted publisher identity handling in
contract_review_result_parser.py (#1475).

AC1: comment fetch が user.login / user.id / user.type / author_association を
     保持し、parser result の provenance として downstream consumer へ渡ることを
     確認する。
AC2: TRUSTED_CONTRACT_PUBLISHERS の静的 allowlist（user.id をキーとする）に
     完全一致する identity tuple（id, login, type, association）を持つ
     schema-valid snapshot だけが authoritative candidate として採用され、
     association 単独で任意の COLLABORATOR/MEMBER を信頼してしまわないことを
     確認する（fix_delta P1 item 2 の回帰テスト）。
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
find_latest_result = _parser_mod.find_latest_result
filter_authoritative_results = _parser_mod.filter_authoritative_results
is_trusted_snapshot_author = _parser_mod.is_trusted_snapshot_author
TRUSTED_AUTHOR_ASSOCIATIONS = _parser_mod.TRUSTED_AUTHOR_ASSOCIATIONS
TRUSTED_CONTRACT_PUBLISHERS = _parser_mod.TRUSTED_CONTRACT_PUBLISHERS

_ISSUE_NUMBER = 1475
_REPO = "squne121/loop-protocol"
_ISSUE_URL = f"https://github.com/{_REPO}/issues/{_ISSUE_NUMBER}"

# The sole allowlisted publisher, per contract_review_result_parser.py.
_TRUSTED_ID = 63350259
_TRUSTED_LOGIN = "squne121"
_TRUSTED_TYPE = "User"
_TRUSTED_ASSOCIATION = "OWNER"

assert _TRUSTED_ID in TRUSTED_CONTRACT_PUBLISHERS


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


def _blocked_body() -> str:
    return f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: blocked
  generated_at: "2026-07-12T00:00:00Z"
  generated_by: issue-contract-review
  issue_url: {_ISSUE_URL}
```
"""


def _comment(
    comment_id: int,
    body: str,
    *,
    created_at: str = "2026-07-12T00:00:00Z",
    author=None,
    author_association=None,
    author_id=None,
    author_type=None,
) -> dict:
    return {
        "id": comment_id,
        "html_url": f"{_ISSUE_URL}#issuecomment-{comment_id}",
        "created_at": created_at,
        "author": author,
        "author_association": author_association,
        "author_id": author_id,
        "author_type": author_type,
        "body": body,
    }


def _trusted_comment(comment_id: int = 44, created_at: str = "2026-07-12T00:00:00Z") -> dict:
    return _comment(
        comment_id,
        _go_body(),
        created_at=created_at,
        author=_TRUSTED_LOGIN,
        author_association=_TRUSTED_ASSOCIATION,
        author_id=_TRUSTED_ID,
        author_type=_TRUSTED_TYPE,
    )


# ---------------------------------------------------------------------------
# AC1: fetch preserves provenance fields
# ---------------------------------------------------------------------------


def test_fetch_and_parser_preserve_trusted_comment_provenance():
    """AC1: comment fetch が user.login / user.id / user.type /
    author_association を保持し、parser result の provenance として downstream
    consumer へ渡ることを確認する。"""
    comment = {
        "id": 1,
        "html_url": f"{_ISSUE_URL}#issuecomment-1",
        "created_at": "2026-07-12T00:00:00Z",
        "author": _TRUSTED_LOGIN,
        "author_association": _TRUSTED_ASSOCIATION,
        "author_id": _TRUSTED_ID,
        "author_type": _TRUSTED_TYPE,
        "body": _go_body(),
    }
    with patch("subprocess.run") as run_mock:
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = json.dumps(comment)
        run_mock.return_value.stderr = ""
        comments, err = fetch_issue_comments(_ISSUE_NUMBER, _REPO)

    assert err is None
    assert comments[0]["author"] == _TRUSTED_LOGIN
    assert comments[0]["author_association"] == _TRUSTED_ASSOCIATION
    assert comments[0]["author_id"] == _TRUSTED_ID
    assert comments[0]["author_type"] == _TRUSTED_TYPE

    results = parse_contract_review_results(comments, expected_issue_url=_ISSUE_URL)
    assert len(results) == 1
    assert results[0]["author"] == _TRUSTED_LOGIN
    assert results[0]["author_association"] == _TRUSTED_ASSOCIATION
    assert results[0]["author_id"] == _TRUSTED_ID
    assert results[0]["author_type"] == _TRUSTED_TYPE
    assert results[0]["is_trusted_author"] is True


def test_untrusted_schema_valid_go_is_ignored():
    """AC2: identity tuple (id, login, type, association) の完全一致だけが
    authoritative candidate として採用され、association 単独では信頼されない
    ことを確認する。"""
    untrusted_comment = _comment(
        42, _go_body(), author="random-outsider", author_association="NONE"
    )
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
        """GIVEN fetch_issue_comments WHEN it builds the gh api call THEN the
        jq projection requests user.login, user.id, user.type, and
        author_association."""
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = ""
            run_mock.return_value.stderr = ""
            fetch_issue_comments(_ISSUE_NUMBER, _REPO)

        called_cmd = run_mock.call_args[0][0]
        jq_index = called_cmd.index("--jq")
        jq_arg = called_cmd[jq_index + 1]
        assert "author: .user.login" in jq_arg
        assert "author_id: .user.id" in jq_arg
        assert "author_type: .user.type" in jq_arg
        assert "author_association" in jq_arg

    def test_fetch_issue_comments_fails_closed_on_malformed_ndjson_line(self):
        """fix_delta P2 item 4: a single malformed NDJSON line must abort the
        whole fetch with comments_fetch_incomplete rather than silently
        returning a partial comment list."""
        with patch("subprocess.run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = (
                json.dumps({"id": 1, "body": "ok"}) + "\n{not-valid-json\n"
            )
            run_mock.return_value.stderr = ""
            comments, err = fetch_issue_comments(_ISSUE_NUMBER, _REPO)

        assert comments == []
        assert err == "comments_fetch_incomplete"


# ---------------------------------------------------------------------------
# AC2: is_trusted_snapshot_author identity-tuple matrix
# ---------------------------------------------------------------------------


class TestIdentityTupleAllowlist:
    def test_trusted_identity_tuple_is_trusted(self):
        assert (
            is_trusted_snapshot_author(
                _TRUSTED_LOGIN,
                _TRUSTED_ASSOCIATION,
                author_id=_TRUSTED_ID,
                author_type=_TRUSTED_TYPE,
            )
            is True
        )

    @pytest.mark.parametrize("association", sorted(TRUSTED_AUTHOR_ASSOCIATIONS))
    def test_association_alone_no_longer_authorizes_arbitrary_actor(self, association):
        """fix_delta P1 item 2 regression: an arbitrary actor with a trusted
        association string but no matching allowlisted user.id must NOT be
        trusted, even for OWNER/MEMBER/COLLABORATOR."""
        assert is_trusted_snapshot_author("some-actor", association) is False
        assert (
            is_trusted_snapshot_author(
                "some-actor", association, author_id=999999, author_type="User"
            )
            is False
        )

    def test_unauthorized_collaborator_with_id_is_untrusted(self):
        assert (
            is_trusted_snapshot_author(
                "some-collaborator",
                "COLLABORATOR",
                author_id=111222,
                author_type="User",
            )
            is False
        )

    def test_unauthorized_member_with_id_is_untrusted(self):
        assert (
            is_trusted_snapshot_author(
                "some-member", "MEMBER", author_id=333444, author_type="User"
            )
            is False
        )

    def test_correct_login_wrong_id_is_untrusted(self):
        assert (
            is_trusted_snapshot_author(
                _TRUSTED_LOGIN,
                _TRUSTED_ASSOCIATION,
                author_id=1,
                author_type=_TRUSTED_TYPE,
            )
            is False
        )

    def test_correct_id_wrong_login_is_untrusted(self):
        assert (
            is_trusted_snapshot_author(
                "not-squne121",
                _TRUSTED_ASSOCIATION,
                author_id=_TRUSTED_ID,
                author_type=_TRUSTED_TYPE,
            )
            is False
        )

    def test_bot_type_impersonating_trusted_id_login_is_untrusted(self):
        assert (
            is_trusted_snapshot_author(
                _TRUSTED_LOGIN,
                _TRUSTED_ASSOCIATION,
                author_id=_TRUSTED_ID,
                author_type="Bot",
            )
            is False
        )

    def test_trusted_id_with_disallowed_association_is_untrusted(self):
        """Even the allowlisted account is only trusted for its
        allowed_associations set (OWNER); a downgraded association string
        must not be trusted."""
        assert (
            is_trusted_snapshot_author(
                _TRUSTED_LOGIN,
                "COLLABORATOR",
                author_id=_TRUSTED_ID,
                author_type=_TRUSTED_TYPE,
            )
            is False
        )

    @pytest.mark.parametrize(
        "author_id",
        [None, True, False, "63350259", 0, -63350259, 3.5],
    )
    def test_malformed_author_id_types_are_untrusted(self, author_id):
        assert (
            is_trusted_snapshot_author(
                _TRUSTED_LOGIN,
                _TRUSTED_ASSOCIATION,
                author_id=author_id,
                author_type=_TRUSTED_TYPE,
            )
            is False
        )

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


# ---------------------------------------------------------------------------
# AC2: untrusted schema-valid go is ignored / trusted go is returned
# ---------------------------------------------------------------------------


class TestUntrustedSchemaValidGoIsIgnored:
    def test_untrusted_schema_valid_go_is_ignored(self):
        """GIVEN a schema-valid status:go comment posted by an untrusted
        outsider (author_association: NONE) WHEN find_latest_go is called
        with trusted_only=True THEN the untrusted snapshot is not returned as
        authoritative, even though it is schema-valid."""
        untrusted_comment = _comment(
            42, _go_body(), author="random-outsider", author_association="NONE"
        )
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

    def test_unauthorized_collaborator_schema_valid_go_is_ignored(self):
        """fix_delta P1 item 2: a repo COLLABORATOR who is not the
        allowlisted publisher must not have their go treated as authoritative."""
        collaborator_comment = _comment(
            45,
            _go_body(),
            author="some-collaborator",
            author_association="COLLABORATOR",
            author_id=987654,
            author_type="User",
        )
        results = parse_contract_review_results(
            [collaborator_comment], expected_issue_url=_ISSUE_URL
        )
        assert results[0]["is_trusted_author"] is False
        assert find_latest_go(results, trusted_only=True) is None

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
        trusted_comment = _trusted_comment(comment_id=44)
        results = parse_contract_review_results(
            [trusted_comment], expected_issue_url=_ISSUE_URL
        )
        go = find_latest_go(results, trusted_only=True)
        assert go is not None
        assert go["html_url"] == trusted_comment["html_url"]


# ---------------------------------------------------------------------------
# fix_delta P1 item 1: trust filtering must apply BEFORE go/blocked precedence
# ---------------------------------------------------------------------------


class TestTrustFilteringPrecedesGoBlockedPrecedence:
    def test_untrusted_blocked_after_trusted_go_does_not_preempt(self):
        """The exact bug reported in the PR review: an untrusted `blocked`
        posted after a trusted `go` must not become the "latest" result."""
        trusted_go = _trusted_comment(comment_id=1, created_at="2026-07-12T00:00:00Z")
        untrusted_blocked = _comment(
            2,
            _blocked_body(),
            created_at="2026-07-12T01:00:00Z",
            author="outside-actor",
            author_association="NONE",
        )
        results = parse_contract_review_results(
            [trusted_go, untrusted_blocked], expected_issue_url=_ISSUE_URL
        )

        # Unfiltered "latest" (legacy, unsafe) would be the untrusted blocked.
        assert find_latest_result(results, trusted_only=False)["status"] == "blocked"

        # trusted_only=True must select the trusted go instead.
        latest_trusted = find_latest_result(results, trusted_only=True)
        assert latest_trusted is not None
        assert latest_trusted["status"] == "go"
        assert latest_trusted["comment_id"] == 1

    def test_untrusted_go_after_trusted_blocked_does_not_preempt(self):
        """Mirror case: an untrusted `go` posted after a trusted `blocked`
        must not be adopted as authoritative."""
        trusted_blocked = _comment(
            1,
            _blocked_body(),
            created_at="2026-07-12T00:00:00Z",
            author=_TRUSTED_LOGIN,
            author_association=_TRUSTED_ASSOCIATION,
            author_id=_TRUSTED_ID,
            author_type=_TRUSTED_TYPE,
        )
        untrusted_go = _comment(
            2,
            _go_body(),
            created_at="2026-07-12T01:00:00Z",
            author="outside-actor",
            author_association="NONE",
        )
        results = parse_contract_review_results(
            [trusted_blocked, untrusted_go], expected_issue_url=_ISSUE_URL
        )

        latest_trusted = find_latest_result(results, trusted_only=True)
        assert latest_trusted is not None
        assert latest_trusted["status"] == "blocked"
        assert find_latest_go(results, trusted_only=True) is None

    def test_filter_authoritative_results_only_returns_trusted_entries(self):
        trusted_go = _trusted_comment(comment_id=1)
        untrusted_blocked = _comment(
            2, _blocked_body(), author="outside-actor", author_association="NONE"
        )
        results = parse_contract_review_results(
            [trusted_go, untrusted_blocked], expected_issue_url=_ISSUE_URL
        )
        authoritative = filter_authoritative_results(results)
        assert len(authoritative) == 1
        assert authoritative[0]["comment_id"] == 1
