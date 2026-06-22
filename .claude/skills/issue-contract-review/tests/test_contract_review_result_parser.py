"""
tests/test_contract_review_result_parser.py

Unit tests for contract_review_result_parser.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


# ---------------------------------------------------------------------------
# Import module under test
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_PARSER_PATH = _SCRIPTS_DIR / "contract_review_result_parser.py"

spec = importlib.util.spec_from_file_location("contract_review_result_parser", _PARSER_PATH)
assert spec is not None and spec.loader is not None
_parser_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_parser_mod)  # type: ignore[union-attr]

parse_contract_review_results = _parser_mod.parse_contract_review_results
find_latest_go = _parser_mod.find_latest_go
find_latest_result = _parser_mod.find_latest_result
_extract_yaml_blocks = _parser_mod._extract_yaml_blocks
_parse_simple_yaml_block = _parser_mod._parse_simple_yaml_block
_is_valid_contract_review_result = _parser_mod._is_valid_contract_review_result

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ISSUE_NUMBER = 817
_REPO = "squne121/loop-protocol"
_ISSUE_URL = f"https://github.com/{_REPO}/issues/{_ISSUE_NUMBER}"


def _make_go_comment(
    comment_id: int = 1001,
    created_at: str = "2026-06-13T08:00:00Z",
    issue_url: str = _ISSUE_URL,
) -> dict:
    return {
        "id": comment_id,
        "html_url": f"{_ISSUE_URL}#issuecomment-{comment_id}",
        "created_at": created_at,
        "updated_at": created_at,
        "body": f"""Some preamble.

```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "{created_at}"
  generated_by: issue-contract-review
  issue_url: {issue_url}
```

Some postamble.
""",
    }


def _make_blocked_comment(
    comment_id: int = 1002,
    created_at: str = "2026-06-13T09:00:00Z",
    issue_url: str = _ISSUE_URL,
) -> dict:
    return {
        "id": comment_id,
        "html_url": f"{_ISSUE_URL}#issuecomment-{comment_id}",
        "created_at": created_at,
        "updated_at": created_at,
        "body": f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: blocked
  generated_at: "{created_at}"
  generated_by: issue-contract-review
  issue_url: {issue_url}
```
""",
    }


# ---------------------------------------------------------------------------
# YAML block extraction
# ---------------------------------------------------------------------------


class TestYamlBlockExtraction:
    """Tests for fenced yaml block extraction."""

    def test_extracts_yaml_block(self):
        body = "Preamble\n```yaml\nkey: value\n```\nPostamble"
        blocks = _extract_yaml_blocks(body)
        assert len(blocks) == 1
        assert "key: value" in blocks[0]

    def test_extracts_yml_block(self):
        body = "```yml\nkey: val\n```"
        blocks = _extract_yaml_blocks(body)
        assert len(blocks) == 1

    def test_no_blocks(self):
        body = "No yaml here"
        blocks = _extract_yaml_blocks(body)
        assert blocks == []

    def test_multiple_blocks(self):
        body = "```yaml\na: 1\n```\n```yaml\nb: 2\n```"
        blocks = _extract_yaml_blocks(body)
        assert len(blocks) == 2


# ---------------------------------------------------------------------------
# Simple YAML parser
# ---------------------------------------------------------------------------


class TestSimpleYamlParser:
    """Tests for _parse_simple_yaml_block."""

    def test_flat_key_value(self):
        block = "status: go\ngenerated_by: issue-contract-review\n"
        result = _parse_simple_yaml_block(block)
        assert result["status"] == "go"
        assert result["generated_by"] == "issue-contract-review"

    def test_quoted_values(self):
        block = 'generated_at: "2026-06-13T08:00:00Z"\nissue_url: "https://example.com"\n'
        result = _parse_simple_yaml_block(block)
        assert result["generated_at"] == "2026-06-13T08:00:00Z"
        assert result["issue_url"] == "https://example.com"

    def test_nested_key(self):
        block = "CONTRACT_REVIEW_RESULT_V1:\n  status: go\n  generated_by: issue-contract-review\n"
        result = _parse_simple_yaml_block(block)
        assert "CONTRACT_REVIEW_RESULT_V1" in result
        inner = result["CONTRACT_REVIEW_RESULT_V1"]
        assert inner["status"] == "go"
        assert inner["generated_by"] == "issue-contract-review"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    """Tests for _is_valid_contract_review_result."""

    def _make_block(
        self,
        status: str = "go",
        generated_by: str = "issue-contract-review",
        issue_url: str = _ISSUE_URL,
        generated_at: str = "2026-06-13T08:00:00Z",
    ) -> dict:
        return {
            "CONTRACT_REVIEW_RESULT_V1": {
                "status": status,
                "generated_by": generated_by,
                "issue_url": issue_url,
                "generated_at": generated_at,
            }
        }

    def test_valid_go_block(self):
        block = self._make_block(status="go")
        assert _is_valid_contract_review_result(block, expected_issue_url=_ISSUE_URL) is True

    def test_valid_blocked_block(self):
        block = self._make_block(status="blocked")
        assert _is_valid_contract_review_result(block, expected_issue_url=_ISSUE_URL) is True

    def test_invalid_status_human_judgment(self):
        """status: human_judgment is NOT a valid CONTRACT_REVIEW_RESULT_V1 status."""
        block = self._make_block(status="human_judgment")
        assert _is_valid_contract_review_result(block) is False

    def test_invalid_status_empty(self):
        block = self._make_block(status="")
        assert _is_valid_contract_review_result(block) is False

    def test_wrong_generated_by(self):
        block = self._make_block(generated_by="some-other-tool")
        assert _is_valid_contract_review_result(block) is False

    def test_issue_url_mismatch(self):
        block = self._make_block(issue_url="https://github.com/other/repo/issues/1")
        assert _is_valid_contract_review_result(block, expected_issue_url=_ISSUE_URL) is False

    def test_issue_url_match_no_expected(self):
        """No expected_issue_url → accept any."""
        block = self._make_block()
        assert _is_valid_contract_review_result(block, expected_issue_url=None) is True

    def test_missing_generated_at(self):
        block = {
            "CONTRACT_REVIEW_RESULT_V1": {
                "status": "go",
                "generated_by": "issue-contract-review",
                "issue_url": _ISSUE_URL,
                "generated_at": None,
            }
        }
        assert _is_valid_contract_review_result(block) is False

    def test_no_root_marker(self):
        block = {"OTHER_MARKER": {"status": "go"}}
        assert _is_valid_contract_review_result(block) is False


# ---------------------------------------------------------------------------
# parse_contract_review_results
# ---------------------------------------------------------------------------


class TestParseContractReviewResults:
    """Tests for the main parsing function."""

    def test_parses_go_comment(self):
        comments = [_make_go_comment()]
        results = parse_contract_review_results(comments, expected_issue_url=_ISSUE_URL)
        assert len(results) == 1
        assert results[0]["status"] == "go"
        assert results[0]["html_url"] == comments[0]["html_url"]

    def test_parses_blocked_comment(self):
        comments = [_make_blocked_comment()]
        results = parse_contract_review_results(comments, expected_issue_url=_ISSUE_URL)
        assert len(results) == 1
        assert results[0]["status"] == "blocked"

    def test_skips_comment_without_marker(self):
        comments = [{"id": 1, "html_url": "url", "created_at": "2026-01-01", "body": "No marker"}]
        results = parse_contract_review_results(comments)
        assert len(results) == 0

    def test_skips_comment_wrong_issue_url(self):
        comment = _make_go_comment(issue_url="https://github.com/wrong/repo/issues/999")
        results = parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL)
        assert len(results) == 0

    def test_multiple_comments_parsed(self):
        comments = [_make_go_comment(comment_id=1001), _make_blocked_comment(comment_id=1002)]
        results = parse_contract_review_results(comments, expected_issue_url=_ISSUE_URL)
        assert len(results) == 2

    def test_only_fenced_yaml_blocks_parsed(self):
        """Inline mentions of CONTRACT_REVIEW_RESULT_V1 are not parsed."""
        comment = {
            "id": 1,
            "html_url": "url",
            "created_at": "2026-01-01",
            "body": "See `CONTRACT_REVIEW_RESULT_V1` for details. No fenced block.",
        }
        results = parse_contract_review_results([comment])
        assert len(results) == 0

    def test_review_comment_in_example_code_not_parsed(self):
        """Example code blocks in review comments should not be parsed as valid results."""
        comment = {
            "id": 2,
            "html_url": "url",
            "created_at": "2026-01-01",
            "body": (
                "Here's an example:\n"
                "```yaml\n"
                "# This is an example, not a real result\n"
                "CONTRACT_REVIEW_RESULT_V1:\n"
                "  status: go\n"
                "  generated_by: some-fake-tool\n"
                "  issue_url: https://example.com/1\n"
                "  generated_at: 2026-01-01T00:00:00Z\n"
                "```\n"
            ),
        }
        results = parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL)
        # Wrong generated_by → not valid
        assert len(results) == 0

    def test_human_judgment_status_not_valid(self):
        """
        Comments with status: human_judgment are NOT valid CONTRACT_REVIEW_RESULT_V1.
        This guards against accidentally treating human_judgment as a valid result.
        """
        comment = {
            "id": 99,
            "html_url": "url",
            "created_at": "2026-01-01",
            "body": f"""
```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: human_judgment
  generated_at: "2026-01-01T00:00:00Z"
  generated_by: issue-contract-review
  issue_url: {_ISSUE_URL}
```
""",
        }
        results = parse_contract_review_results([comment], expected_issue_url=_ISSUE_URL)
        assert len(results) == 0, "human_judgment status must not be a valid result"


# ---------------------------------------------------------------------------
# find_latest_go / find_latest_result
# ---------------------------------------------------------------------------


class TestFindLatest:
    """Tests for find_latest_go and find_latest_result."""

    def test_find_latest_go_returns_go(self):
        results = [
            {"status": "go", "created_at": "2026-01-01", "comment_id": 1, "html_url": "url1"},
        ]
        r = find_latest_go(results)
        assert r is not None
        assert r["status"] == "go"

    def test_find_latest_go_no_go_returns_none(self):
        results = [
            {"status": "blocked", "created_at": "2026-01-01", "comment_id": 1, "html_url": "url1"},
        ]
        r = find_latest_go(results)
        assert r is None

    def test_find_latest_go_picks_newest(self):
        results = [
            {"status": "go", "created_at": "2026-01-01T00:00:00Z", "comment_id": 1, "html_url": "url1"},
            {"status": "go", "created_at": "2026-01-02T00:00:00Z", "comment_id": 2, "html_url": "url2"},
        ]
        r = find_latest_go(results)
        assert r["html_url"] == "url2"

    def test_find_latest_result_returns_newest(self):
        results = [
            {"status": "go", "created_at": "2026-01-01T00:00:00Z", "comment_id": 1, "html_url": "url1"},
            {"status": "blocked", "created_at": "2026-01-02T00:00:00Z", "comment_id": 2, "html_url": "url2"},
        ]
        r = find_latest_result(results)
        assert r["status"] == "blocked"
        assert r["html_url"] == "url2"

    def test_find_latest_result_empty(self):
        r = find_latest_result([])
        assert r is None

    def test_find_latest_go_empty(self):
        r = find_latest_go([])
        assert r is None
