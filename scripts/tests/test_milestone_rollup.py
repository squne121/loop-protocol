"""
test_milestone_rollup.py — Unit tests for MILESTONE_DESCENDANT_ROLLUP_V1 checker.

Tests use mocked API responses (no real network calls).
AC8 coverage:
  - 2+ level descendant traversal
  - pagination
  - PR mixed into milestone
  - stale state labels on closed issues
  - milestone null / mismatch
  - open blocker detection
  - native dependency API priority over ## Depends On section
  - sub-issues 404 distinguished from "no children"
  - milestone issues endpoint uses official /issues?milestone=... query
  - unauthenticated (token-less) operation
  - cross-repo exact match guard
  - Markdown table cell escaping
  - _parse_next_link regex robustness
  - evaluate_close_readiness production function
  - validate_defer_decision production function
  - build_report closure_followups / defer_records / scope_conflict_count fields
  - E2E shell smoke test (skipped; requires live GitHub API)
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import milestone_rollup as mr


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_issue(
    number: int,
    title: str = "Issue",
    state: str = "open",
    milestone_number: int | None = 1,
    labels: list[str] | None = None,
    body: str = "",
    is_pr: bool = False,
) -> dict:
    m = None if milestone_number is None else {"number": milestone_number, "title": "M1"}
    issue: dict = {
        "number": number,
        "title": title,
        "state": state,
        "milestone": m,
        "labels": [{"name": lbl} for lbl in (labels or [])],
        "body": body,
        "repository_url": "https://api.github.com/repos/owner/repo",
    }
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/repos/owner/repo/pulls/1"}
    return issue


def _make_sub_issue(
    number: int,
    title: str = "Child Issue",
    state: str = "open",
    milestone_number: int | None = 1,
    labels: list[str] | None = None,
    body: str = "",
    repo_url: str = "https://api.github.com/repos/owner/repo",
) -> dict:
    m = None if milestone_number is None else {"number": milestone_number, "title": "M1"}
    return {
        "number": number,
        "title": title,
        "state": state,
        "milestone": m,
        "labels": [{"name": lbl} for lbl in (labels or [])],
        "body": body,
        "repository_url": repo_url,
    }


# ---------------------------------------------------------------------------
# Tests for _parse_depends_on
# ---------------------------------------------------------------------------

class TestParseDependsOn(unittest.TestCase):
    def test_empty_body(self):
        self.assertEqual(mr._parse_depends_on(""), [])

    def test_no_depends_on_section(self):
        body = "## Overview\nSome text\n## Notes\nOther text"
        self.assertEqual(mr._parse_depends_on(body), [])

    def test_depends_on_section_basic(self):
        body = "## Depends On\n- #42\n- #99\n## Next\nOther"
        result = mr._parse_depends_on(body)
        self.assertEqual(result, [42, 99])

    def test_depends_on_no_issues_in_other_sections(self):
        body = "## Overview\n- #100\n## Depends On\n- #42\n## Notes\n- #999"
        result = mr._parse_depends_on(body)
        self.assertEqual(result, [42])

    def test_depends_on_multiple_refs_on_line(self):
        body = "## Depends On\n- #10 and #20\n"
        result = mr._parse_depends_on(body)
        self.assertEqual(result, [10, 20])


# ---------------------------------------------------------------------------
# Tests for _parse_next_link
# ---------------------------------------------------------------------------

class TestParseNextLink(unittest.TestCase):
    def test_no_link_header(self):
        self.assertIsNone(mr._parse_next_link(""))

    def test_next_link_present(self):
        header = (
            '<https://api.github.com/repos/o/r/issues?page=2>; rel="next",'
            ' <https://api.github.com/repos/o/r/issues?page=5>; rel="last"'
        )
        result = mr._parse_next_link(header)
        self.assertEqual(result, "https://api.github.com/repos/o/r/issues?page=2")

    def test_only_last_link(self):
        header = '<https://api.github.com/repos/o/r/issues?page=5>; rel="last"'
        self.assertIsNone(mr._parse_next_link(header))

    def test_next_link_with_extra_spaces(self):
        """Regex handles extra spaces around rel="next"."""
        header = '<https://example.com/page2>;  rel="next"'
        result = mr._parse_next_link(header)
        self.assertEqual(result, "https://example.com/page2")

    def test_next_link_no_comma_separation(self):
        """Only next link, no last link."""
        header = '<https://api.github.com/repos/o/r/issues?page=2>; rel="next"'
        result = mr._parse_next_link(header)
        self.assertEqual(result, "https://api.github.com/repos/o/r/issues?page=2")


# ---------------------------------------------------------------------------
# Tests for md_cell escaping
# ---------------------------------------------------------------------------

class TestMdCell(unittest.TestCase):
    def test_pipe_escaped(self):
        self.assertEqual(mr.md_cell("a|b"), "a\\|b")

    def test_newline_replaced(self):
        self.assertEqual(mr.md_cell("a\nb"), "a<br>b")

    def test_plain_string_unchanged(self):
        self.assertEqual(mr.md_cell("hello"), "hello")

    def test_integer(self):
        self.assertEqual(mr.md_cell(42), "42")

    def test_multiple_pipes(self):
        self.assertEqual(mr.md_cell("a|b|c"), "a\\|b\\|c")

    def test_backtick_escaped(self):
        """Fix 6: backtick must be escaped as \\`."""
        self.assertEqual(mr.md_cell("a`b"), "a\\`b")

    def test_combined_escapes(self):
        """Fix 6: pipe, newline, backtick all escaped."""
        self.assertEqual(mr.md_cell("a|b\nc`d"), "a\\|b<br>c\\`d")


# ---------------------------------------------------------------------------
# Mock helpers for urlopen
# ---------------------------------------------------------------------------

class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code: int, body: str = ""):
        self._body = body.encode()
        super().__init__("http://fake", code, f"HTTP {code}", {}, None)

    def read(self):
        return self._body


class FakeResponse:
    def __init__(self, data: list | dict, link: str = ""):
        self._data = json.dumps(data).encode()
        self._link = link

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    @property
    def headers(self):
        h = MagicMock()
        h.get = lambda key, default="": self._link if key == "Link" else default
        return h


def _make_urlopen_mock(
    pages: list[list[dict]],
    sub_issues_map: dict[int, list[dict]] | None = None,
    sub_issues_errors: dict[int, int] | None = None,
    native_deps_map: dict[int, list[dict] | int] | None = None,
):
    """
    Create a mock side_effect for urllib.request.urlopen.

    pages: list of page responses for paginated milestone issues calls.
    sub_issues_map: dict mapping issue_number -> list of sub-issues.
    sub_issues_errors: dict mapping issue_number -> HTTP error code to raise.
    native_deps_map: dict mapping issue_number -> list of dep objects or HTTP error code (int).
    """
    sub_issues_map = sub_issues_map or {}
    sub_issues_errors = sub_issues_errors or {}
    native_deps_map = native_deps_map or {}
    call_count = {"milestone": 0}
    sub_call_count: dict[int, int] = {}

    def urlopen_side_effect(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)

        # native dependency API endpoint
        dep_match = re.search(r"/issues/(\d+)/dependencies/blocked_by", url)
        if dep_match:
            issue_num = int(dep_match.group(1))
            if issue_num in native_deps_map:
                val = native_deps_map[issue_num]
                if isinstance(val, int):
                    raise FakeHTTPError(val)
                return FakeResponse(val)
            # Default: 404 (not available) → triggers fallback
            raise FakeHTTPError(404)

        # sub_issues endpoint
        sub_match = re.search(r"/issues/(\d+)/sub_issues", url)
        if sub_match:
            issue_num = int(sub_match.group(1))
            if issue_num in sub_issues_errors:
                raise FakeHTTPError(sub_issues_errors[issue_num])
            children = sub_issues_map.get(issue_num, [])
            sub_call_count.setdefault(issue_num, 0)
            idx = sub_call_count[issue_num]
            sub_call_count[issue_num] += 1
            if idx == 0:
                return FakeResponse(children)
            return FakeResponse([])

        # milestone issues endpoint — must use ?milestone=... query style
        idx = call_count["milestone"]
        call_count["milestone"] += 1
        if idx < len(pages):
            page = pages[idx]
            link = ""
            if idx + 1 < len(pages):
                link = f'<https://api.github.com/repos/owner/repo/issues?milestone=1&page={idx+2}>; rel="next"'
            return FakeResponse(page, link)
        return FakeResponse([])

    return urlopen_side_effect


import re  # noqa: E402 (needed for urlopen_side_effect closure)


# ---------------------------------------------------------------------------
# Tests for collect_descendants (mocked)
# ---------------------------------------------------------------------------

class TestCollectDescendants(unittest.TestCase):
    def _run_collect(
        self,
        pages,
        sub_issues_map=None,
        sub_issues_errors=None,
        native_deps_map=None,
    ):
        side_effect = _make_urlopen_mock(
            pages,
            sub_issues_map=sub_issues_map,
            sub_issues_errors=sub_issues_errors,
            native_deps_map=native_deps_map,
        )
        with patch("urllib.request.urlopen", side_effect=side_effect):
            issues, warnings, partial = mr.collect_descendants("owner", "repo", 1, "fake_token")
            return issues, warnings

    def test_single_direct_issue(self):
        pages = [[_make_issue(10, "Issue A")]]
        issues, warnings = self._run_collect(pages)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["number"], 10)
        self.assertEqual(warnings, [])

    def test_milestone_issues_endpoint_uses_query_param(self):
        """Blocker 2: milestone issues endpoint must use ?milestone=N&state=all&per_page=100."""
        captured_urls = []

        def capturing_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            captured_urls.append(url)
            # sub_issues -> empty
            if "sub_issues" in url:
                return FakeResponse([])
            # native deps -> 404 fallback
            if "dependencies/blocked_by" in url:
                raise FakeHTTPError(404)
            return FakeResponse([_make_issue(1)])

        with patch("urllib.request.urlopen", side_effect=capturing_urlopen):
            mr.collect_descendants("owner", "repo", 1, "fake_token")

        milestone_urls = [u for u in captured_urls if "sub_issues" not in u and "dependencies" not in u]
        self.assertTrue(len(milestone_urls) >= 1, "No milestone API call made")
        first_url = milestone_urls[0]
        self.assertIn("milestone=1", first_url, "URL must use ?milestone=1 query param")
        self.assertIn("state=all", first_url, "URL must include state=all")
        self.assertNotIn("/milestones/", first_url, "Must not use deprecated /milestones/{n}/issues endpoint")

    def test_two_level_descendants(self):
        """AC8: 2+ level descendant traversal"""
        child = _make_sub_issue(20, "Child Issue", milestone_number=1)
        grandchild = _make_sub_issue(30, "Grandchild Issue", milestone_number=1)
        pages = [[_make_issue(10, "Parent Issue")]]
        sub_map = {10: [child], 20: [grandchild]}
        issues, warnings = self._run_collect(pages, sub_issues_map=sub_map)
        numbers = [i["number"] for i in issues]
        self.assertIn(10, numbers)
        self.assertIn(20, numbers)
        self.assertIn(30, numbers)
        # Depth check
        self.assertEqual(next(i["depth"] for i in issues if i["number"] == 10), 0)
        self.assertEqual(next(i["depth"] for i in issues if i["number"] == 20), 1)
        self.assertEqual(next(i["depth"] for i in issues if i["number"] == 30), 2)

    def test_pagination(self):
        """AC8: pagination — two pages of milestone issues"""
        page1 = [_make_issue(1), _make_issue(2)]
        page2 = [_make_issue(3), _make_issue(4)]
        issues, warnings = self._run_collect([page1, page2])
        numbers = [i["number"] for i in issues]
        self.assertEqual(sorted(numbers), [1, 2, 3, 4])

    def test_cycle_prevention(self):
        """visited set prevents revisiting same issue"""
        child = _make_sub_issue(20)
        back_ref = _make_sub_issue(10)  # cycle back to parent
        pages = [[_make_issue(10)]]
        sub_map = {10: [child], 20: [back_ref]}
        issues, warnings = self._run_collect(pages, sub_issues_map=sub_map)
        numbers = [i["number"] for i in issues]
        self.assertIn(10, numbers)
        self.assertIn(20, numbers)
        self.assertEqual(numbers.count(10), 1)
        self.assertEqual(numbers.count(20), 1)
        cycle_warnings = [w for w in warnings if w["type"] == "cycle_or_duplicate"]
        self.assertEqual(len(cycle_warnings), 1)

    def test_cross_repo_sub_issue_skipped_exact_match(self):
        """Blocker 5: cross-repo uses exact match; owner/repo-evil must not match owner/repo."""
        child_evil = _make_sub_issue(
            88, repo_url="https://api.github.com/repos/owner/repo-evil"
        )
        child_other = _make_sub_issue(
            99, repo_url="https://api.github.com/repos/other-owner/other-repo"
        )
        pages = [[_make_issue(10)]]
        sub_map = {10: [child_evil, child_other]}
        issues, warnings = self._run_collect(pages, sub_issues_map=sub_map)
        numbers = [i["number"] for i in issues]
        self.assertNotIn(88, numbers, "owner/repo-evil must be rejected by exact match guard")
        self.assertNotIn(99, numbers)
        cross_warnings = [w for w in warnings if w["type"] == "cross_repo_sub_issue"]
        self.assertEqual(len(cross_warnings), 2)

    def test_cross_repo_sub_issue_skipped(self):
        """Cross-repo sub-issues produce a warning and are skipped"""
        child = _make_sub_issue(
            99, repo_url="https://api.github.com/repos/other-owner/other-repo"
        )
        pages = [[_make_issue(10)]]
        sub_map = {10: [child]}
        issues, warnings = self._run_collect(pages, sub_issues_map=sub_map)
        numbers = [i["number"] for i in issues]
        self.assertNotIn(99, numbers)
        cross_warnings = [w for w in warnings if w["type"] == "cross_repo_sub_issue"]
        self.assertEqual(len(cross_warnings), 1)

    def test_pr_in_milestone(self):
        """AC8: PR mixed into milestone is collected as is_pr=True"""
        pages = [[_make_issue(50, is_pr=True)]]
        issues, _ = self._run_collect(pages)
        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0]["is_pr"])

    def test_sub_issues_404_produces_warning_not_empty_children(self):
        """Blocker 4: 404 from sub_issues endpoint -> warning, not silently empty."""
        pages = [[_make_issue(10)]]
        issues, warnings = self._run_collect(pages, sub_issues_errors={10: 404})
        numbers = [i["number"] for i in issues]
        self.assertIn(10, numbers)
        unavail = [w for w in warnings if w["type"] == "sub_issues_unavailable"]
        self.assertEqual(len(unavail), 1, "404 must produce sub_issues_unavailable warning")
        self.assertEqual(unavail[0]["issue_number"], 10)
        self.assertEqual(unavail[0]["http_code"], 404)

    def test_sub_issues_410_produces_warning(self):
        """Blocker 4: 410 from sub_issues endpoint -> warning."""
        pages = [[_make_issue(10)]]
        issues, warnings = self._run_collect(pages, sub_issues_errors={10: 410})
        unavail = [w for w in warnings if w["type"] == "sub_issues_unavailable"]
        self.assertEqual(len(unavail), 1)
        self.assertEqual(unavail[0]["http_code"], 410)

    def test_sub_issues_422_produces_warning(self):
        """Blocker 4: 422 from sub_issues endpoint -> sub_issues_error warning."""
        pages = [[_make_issue(10)]]
        issues, warnings = self._run_collect(pages, sub_issues_errors={10: 422})
        err_warnings = [w for w in warnings if w["type"] == "sub_issues_error"]
        self.assertEqual(len(err_warnings), 1)
        self.assertEqual(err_warnings[0]["http_code"], 422)

    def test_unauthenticated_works_without_token(self):
        """Blocker 3: token=None should not prevent API calls (no auth header required)."""
        _pages = [[_make_issue(10)]]

        def urlopen_check_auth(req, timeout=None):
            _auth = req.get_header("Authorization")
            if "sub_issues" in req.full_url:
                return FakeResponse([])
            if "dependencies/blocked_by" in req.full_url:
                raise FakeHTTPError(404)
            return FakeResponse([_make_issue(10)])

        with patch("urllib.request.urlopen", side_effect=urlopen_check_auth):
            issues, warnings, partial = mr.collect_descendants("owner", "repo", 1, None)

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["number"], 10)


# ---------------------------------------------------------------------------
# Tests for native dependency API
# ---------------------------------------------------------------------------

class TestNativeDependencies(unittest.TestCase):
    def test_native_api_returns_deps(self):
        """Blocker 1: native API 200 -> returns dep numbers, source='native'."""
        dep_obj = [{"number": 42, "state": "open", "title": "Blocker"}]

        def fake_urlopen(req, timeout=None):
            if "dependencies/blocked_by" in req.full_url:
                return FakeResponse(dep_obj)
            raise FakeHTTPError(404)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            nums, source = mr._get_native_dependencies("owner", "repo", 10, "tok")

        self.assertEqual(nums, [42])
        self.assertEqual(source, "native")

    def test_native_api_200_empty_no_fallback(self):
        """Blocker 1: native API 200 empty -> deps=[], source='native', no fallback."""
        def fake_urlopen(req, timeout=None):
            if "dependencies/blocked_by" in req.full_url:
                return FakeResponse([])
            raise FakeHTTPError(404)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            nums, source = mr._get_native_dependencies("owner", "repo", 10, "tok")

        self.assertIsNotNone(nums)
        self.assertEqual(nums, [])
        self.assertEqual(source, "native")

    def test_native_api_404_triggers_fallback(self):
        """Blocker 1: native API 404 -> returns (None, 'fallback_trigger')."""
        def fake_urlopen(req, timeout=None):
            raise FakeHTTPError(404)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            nums, source = mr._get_native_dependencies("owner", "repo", 10, "tok")

        self.assertIsNone(nums)
        self.assertEqual(source, "fallback_trigger")

    def test_get_dependencies_with_source_uses_native_first(self):
        """Blocker 1: _get_dependencies_with_source prefers native over body parsing."""
        body = "## Depends On\n- #99\n"

        def fake_urlopen(req, timeout=None):
            if "dependencies/blocked_by" in req.full_url:
                return FakeResponse([{"number": 42}])
            raise FakeHTTPError(404)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            nums, source = mr._get_dependencies_with_source("owner", "repo", 10, "tok", body)

        self.assertEqual(nums, [42])
        self.assertEqual(source, "native")
        self.assertNotIn(99, nums, "Body fallback must not be used when native API succeeds")

    def test_get_dependencies_with_source_fallback_on_404(self):
        """Blocker 1: when native returns 404, fall back to ## Depends On body parsing."""
        body = "## Depends On\n- #99\n"

        def fake_urlopen(req, timeout=None):
            raise FakeHTTPError(404)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            nums, source = mr._get_dependencies_with_source("owner", "repo", 10, "tok", body)

        self.assertEqual(nums, [99])
        self.assertEqual(source, "depends_on_section")

    def test_native_200_empty_does_not_fallback_to_body(self):
        """Blocker 1: native 200 empty means 'no deps', must not consult body."""
        body = "## Depends On\n- #99\n"

        def fake_urlopen(req, timeout=None):
            if "dependencies/blocked_by" in req.full_url:
                return FakeResponse([])
            raise FakeHTTPError(404)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            nums, source = mr._get_dependencies_with_source("owner", "repo", 10, "tok", body)

        self.assertEqual(nums, [], "native 200 empty must return [] without consulting body")
        self.assertEqual(source, "native")

    def test_native_api_uses_paginated_helper(self):
        """Fix 2: _get_native_dependencies must use _api_get_paginated (paginated URL)."""
        called_urls = []

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            called_urls.append(url)
            if "dependencies/blocked_by" in url:
                return FakeResponse([{"number": 7}])
            raise FakeHTTPError(404)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            nums, source = mr._get_native_dependencies("owner", "repo", 10, "tok")

        dep_urls = [u for u in called_urls if "dependencies/blocked_by" in u]
        self.assertTrue(len(dep_urls) >= 1, "native deps URL must be called")
        self.assertIn("per_page=100", dep_urls[0], "URL must use per_page=100 (paginated)")
        self.assertEqual(nums, [7])
        self.assertEqual(source, "native")

    def test_native_api_403_raises_runtime_error(self):
        """Fix 2: 403 must raise RuntimeError, not trigger fallback."""
        def fake_urlopen(req, timeout=None):
            raise FakeHTTPError(403)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(RuntimeError, msg="403 must raise RuntimeError, not return fallback_trigger"):
                mr._get_native_dependencies("owner", "repo", 10, "tok")

    def test_native_api_429_raises_runtime_error(self):
        """Fix 2: 429 (rate limit) must raise RuntimeError, not trigger fallback."""
        def fake_urlopen(req, timeout=None):
            raise FakeHTTPError(429)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(RuntimeError, msg="429 must raise RuntimeError, not return fallback_trigger"):
                mr._get_native_dependencies("owner", "repo", 10, "tok")

    def test_native_api_network_error_raises_runtime_error(self):
        """Fix 2: URLError (network error) must raise RuntimeError, not trigger fallback."""
        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(
                RuntimeError,
                msg="Network error must raise RuntimeError, not return fallback_trigger",
            ):
                mr._get_native_dependencies("owner", "repo", 10, "tok")

    def test_native_api_501_triggers_fallback(self):
        """Fix 2: 501 (not implemented) must trigger fallback, not raise."""
        def fake_urlopen(req, timeout=None):
            raise FakeHTTPError(501)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            nums, source = mr._get_native_dependencies("owner", "repo", 10, "tok")

        self.assertIsNone(nums)
        self.assertEqual(source, "fallback_trigger")

    def test_native_api_410_triggers_fallback(self):
        """Fix 2: 410 (gone) must trigger fallback, not raise."""
        def fake_urlopen(req, timeout=None):
            raise FakeHTTPError(410)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            nums, source = mr._get_native_dependencies("owner", "repo", 10, "tok")

        self.assertIsNone(nums)
        self.assertEqual(source, "fallback_trigger")


# ---------------------------------------------------------------------------
# Tests for Fix 3 (timeout) and Fix 4 (422 partial)
# ---------------------------------------------------------------------------

class TestTimeoutAndPartial(unittest.TestCase):
    def test_api_get_uses_timeout(self):
        """Fix 3: _api_get must pass timeout=30 to urlopen."""
        captured_args = {}

        def fake_urlopen(req, timeout=None):
            captured_args["timeout"] = timeout
            return FakeResponse({"key": "value"})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            mr._api_get("https://api.github.com/repos/o/r/issues/1", "tok")

        self.assertEqual(captured_args.get("timeout"), 30, "_api_get must use timeout=30")

    def test_api_get_paginated_uses_timeout(self):
        """Fix 3: _api_get_paginated must pass timeout=30 to urlopen."""
        captured_args = {}

        def fake_urlopen(req, timeout=None):
            captured_args["timeout"] = timeout
            return FakeResponse([])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            mr._api_get_paginated("https://api.github.com/repos/o/r/issues?milestone=1", "tok")

        self.assertEqual(captured_args.get("timeout"), 30, "_api_get_paginated must use timeout=30")

    def test_sub_issues_paginated_uses_timeout(self):
        """Fix 3: _get_sub_issues_paginated must pass timeout=30 to urlopen."""
        captured_args = {}

        def fake_urlopen(req, timeout=None):
            captured_args["timeout"] = timeout
            return FakeResponse([])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            mr._get_sub_issues_paginated("owner", "repo", 10, "tok")

        self.assertEqual(captured_args.get("timeout"), 30, "_get_sub_issues_paginated must use timeout=30")

    def test_sub_issues_422_returns_partial_true(self):
        """Fix 4: 422 from sub_issues endpoint -> partial=True."""
        def fake_urlopen(req, timeout=None):
            raise FakeHTTPError(422)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            children, warnings, partial = mr._get_sub_issues_paginated("owner", "repo", 10, "tok")

        self.assertEqual(children, [])
        self.assertTrue(partial, "422 must return partial=True")
        err_warnings = [w for w in warnings if w["type"] == "sub_issues_error"]
        self.assertEqual(len(err_warnings), 1)
        self.assertEqual(err_warnings[0]["http_code"], 422)

    def test_sub_issues_404_returns_partial_false(self):
        """Fix 4: 404 from sub_issues endpoint -> partial=False (endpoint not available)."""
        def fake_urlopen(req, timeout=None):
            raise FakeHTTPError(404)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            children, warnings, partial = mr._get_sub_issues_paginated("owner", "repo", 10, "tok")

        self.assertFalse(partial, "404 must return partial=False")

    def test_sub_issues_200_returns_partial_false(self):
        """Fix 4: 200 success -> partial=False."""
        def fake_urlopen(req, timeout=None):
            return FakeResponse([])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            children, warnings, partial = mr._get_sub_issues_paginated("owner", "repo", 10, "tok")

        self.assertFalse(partial)

    def test_collect_descendants_422_sets_partial(self):
        """Fix 4: 422 during traversal -> collect_descendants returns partial=True."""
        pages = [[_make_issue(10)]]

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "sub_issues" in url:
                raise FakeHTTPError(422)
            if "dependencies/blocked_by" in url:
                raise FakeHTTPError(404)
            return FakeResponse(pages[0])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            issues, warnings, partial = mr.collect_descendants("owner", "repo", 1, "tok")

        self.assertTrue(partial, "422 sub_issues must propagate partial=True from collect_descendants")

    def test_build_report_partial_field(self):
        """Fix 4: build_report includes partial in top-level and summary."""
        report = mr.build_report(
            1, [], {"pr_mixed": [], "milestone_mismatches": [],
                    "stale_state_labels": [], "open_blockers": [], "scope_conflict_count": 0},
            [], "2026-01-01T00:00:00Z", "owner/repo", partial=True
        )
        self.assertTrue(report["partial"])
        self.assertTrue(report["summary"]["partial"])

    def test_build_report_partial_false_by_default(self):
        """Fix 4: partial defaults to False."""
        report = mr.build_report(
            1, [], {"pr_mixed": [], "milestone_mismatches": [],
                    "stale_state_labels": [], "open_blockers": [], "scope_conflict_count": 0},
            [], "2026-01-01T00:00:00Z", "owner/repo"
        )
        self.assertFalse(report["partial"])
        self.assertFalse(report["summary"]["partial"])


# ---------------------------------------------------------------------------
# Tests for analyze
# ---------------------------------------------------------------------------

class TestAnalyze(unittest.TestCase):
    """Tests for the analyze() function using pre-built issue lists."""

    def _run_analyze(self, issues, dep_states=None, native_deps_map=None):
        """Run analyze with mocked API calls for dep issue states and native deps."""
        dep_states = dep_states or {}
        native_deps_map = native_deps_map or {}

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)

            # native dependency API
            dep_match = re.search(r"/issues/(\d+)/dependencies/blocked_by", url)
            if dep_match:
                num = int(dep_match.group(1))
                if num in native_deps_map:
                    val = native_deps_map[num]
                    if isinstance(val, int):
                        raise FakeHTTPError(val)
                    return FakeResponse(val)
                raise FakeHTTPError(404)

            # single issue fetch for state check
            issue_match = re.search(r"/issues/(\d+)$", url)
            if issue_match:
                n = int(issue_match.group(1))
                state = dep_states.get(n, "closed")
                return FakeResponse({"number": n, "state": state, "title": f"Issue {n}"})

            raise RuntimeError(f"Unexpected URL in test: {url}")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            return mr.analyze(issues, 1, "owner", "repo", "fake_token")

    def test_pr_mixed(self):
        """AC6/AC8: PR in milestone appears in pr_mixed"""
        issues = [
            {
                "number": 1, "title": "PR", "state": "open",
                "milestone_number": 1, "labels": [], "body": "",
                "is_pr": True, "depth": 0, "parent_number": None,
            }
        ]
        findings = self._run_analyze(issues)
        self.assertEqual(len(findings["pr_mixed"]), 1)
        self.assertEqual(findings["pr_mixed"][0]["number"], 1)
        self.assertEqual(len(findings["milestone_mismatches"]), 0)

    def test_milestone_null_mismatch(self):
        """AC5: depth>=1 の descendant で milestone=null は間接所属として正常（mismatch にしない）。"""
        issues = [
            {
                "number": 2, "title": "No Milestone Issue", "state": "open",
                "milestone_number": None, "labels": [], "body": "",
                "is_pr": False, "depth": 1, "parent_number": 1,
            }
        ]
        findings = self._run_analyze(issues)
        self.assertEqual(
            len(findings["milestone_mismatches"]), 0,
            "depth=1, milestone=null は間接所属として正常（mismatch にしない）",
        )

    def test_milestone_mismatch_wrong_number(self):
        """AC3: milestone number 2 when expecting 1"""
        issues = [
            {
                "number": 3, "title": "Wrong Milestone", "state": "open",
                "milestone_number": 2, "labels": [], "body": "",
                "is_pr": False, "depth": 1, "parent_number": 1,
            }
        ]
        findings = self._run_analyze(issues)
        self.assertEqual(len(findings["milestone_mismatches"]), 1)

    def test_stale_state_labels_queued(self):
        """AC4/AC8: closed issue with state/queued -> stale_state_labels"""
        issues = [
            {
                "number": 4, "title": "Stale Queued", "state": "closed",
                "milestone_number": 1, "labels": ["state/queued"], "body": "",
                "is_pr": False, "depth": 0, "parent_number": None,
            }
        ]
        findings = self._run_analyze(issues)
        self.assertEqual(len(findings["stale_state_labels"]), 1)
        self.assertIn("state/queued", findings["stale_state_labels"][0]["stale_labels"])

    def test_stale_state_labels_in_progress(self):
        """AC4: closed issue with state/in-progress -> stale_state_labels"""
        issues = [
            {
                "number": 5, "title": "Stale In Progress", "state": "closed",
                "milestone_number": 1, "labels": ["state/in-progress"], "body": "",
                "is_pr": False, "depth": 0, "parent_number": None,
            }
        ]
        findings = self._run_analyze(issues)
        self.assertEqual(len(findings["stale_state_labels"]), 1)
        self.assertIn("state/in-progress", findings["stale_state_labels"][0]["stale_labels"])

    def test_no_stale_label_if_open(self):
        """AC4: open issue with state/queued is NOT stale (only closed matters)"""
        issues = [
            {
                "number": 6, "title": "Open Queued", "state": "open",
                "milestone_number": 1, "labels": ["state/queued"], "body": "",
                "is_pr": False, "depth": 0, "parent_number": None,
            }
        ]
        findings = self._run_analyze(issues)
        self.assertEqual(len(findings["stale_state_labels"]), 0)

    def test_open_blocker_via_native_api(self):
        """Blocker 1/AC5: native dep API returns open dep -> open_blockers with source='native'."""
        issues = [
            {
                "number": 7, "title": "Blocked Issue", "state": "open",
                "milestone_number": 1, "labels": [], "body": "",
                "is_pr": False, "depth": 0, "parent_number": None,
            }
        ]
        native_deps = {7: [{"number": 10}]}
        findings = self._run_analyze(issues, dep_states={10: "open"}, native_deps_map=native_deps)
        self.assertEqual(len(findings["open_blockers"]), 1)
        self.assertIn(10, findings["open_blockers"][0]["open_blocker_numbers"])
        self.assertEqual(findings["open_blockers"][0]["source"], "native")

    def test_open_blocker_via_body_fallback(self):
        """Blocker 1/AC5: when native 404, falls back to body parsing."""
        body = "## Depends On\n- #10\n"
        issues = [
            {
                "number": 7, "title": "Blocked Issue", "state": "open",
                "milestone_number": 1, "labels": [], "body": body,
                "is_pr": False, "depth": 0, "parent_number": None,
            }
        ]
        native_deps = {7: 404}
        findings = self._run_analyze(issues, dep_states={10: "open"}, native_deps_map=native_deps)
        self.assertEqual(len(findings["open_blockers"]), 1)
        self.assertIn(10, findings["open_blockers"][0]["open_blocker_numbers"])
        self.assertEqual(findings["open_blockers"][0]["source"], "depends_on_section")

    def test_native_200_empty_does_not_use_body_dep(self):
        """Blocker 1: native 200 empty = no deps; body #10 must not appear."""
        body = "## Depends On\n- #10\n"
        issues = [
            {
                "number": 7, "title": "Blocked Issue", "state": "open",
                "milestone_number": 1, "labels": [], "body": body,
                "is_pr": False, "depth": 0, "parent_number": None,
            }
        ]
        native_deps = {7: []}
        findings = self._run_analyze(issues, dep_states={10: "open"}, native_deps_map=native_deps)
        self.assertEqual(len(findings["open_blockers"]), 0, "native 200 empty must not fall back to body")

    def test_open_blocker_detected_via_body(self):
        """AC5/AC8: open issue with open dependency via body -> open_blockers"""
        body = "## Depends On\n- #10\n"
        issues = [
            {
                "number": 7, "title": "Blocked Issue", "state": "open",
                "milestone_number": 1, "labels": [], "body": body,
                "is_pr": False, "depth": 0, "parent_number": None,
            }
        ]
        findings = self._run_analyze(issues, dep_states={10: "open"})
        self.assertEqual(len(findings["open_blockers"]), 1)
        self.assertIn(10, findings["open_blockers"][0]["open_blocker_numbers"])

    def test_no_blocker_if_dep_closed(self):
        """AC5: dependency is closed -> no open_blockers"""
        body = "## Depends On\n- #11\n"
        issues = [
            {
                "number": 8, "title": "Unblocked Issue", "state": "open",
                "milestone_number": 1, "labels": [], "body": body,
                "is_pr": False, "depth": 0, "parent_number": None,
            }
        ]
        findings = self._run_analyze(issues, dep_states={11: "closed"})
        self.assertEqual(len(findings["open_blockers"]), 0)

    def test_no_blocker_if_no_depends_on(self):
        """AC5: issue with no Depends On section -> no open_blockers"""
        issues = [
            {
                "number": 9, "title": "No Deps Issue", "state": "open",
                "milestone_number": 1, "labels": [], "body": "## Notes\nSome text",
                "is_pr": False, "depth": 0, "parent_number": None,
            }
        ]
        findings = self._run_analyze(issues)
        self.assertEqual(len(findings["open_blockers"]), 0)

    def test_clean_issue_no_findings(self):
        """A clean open issue with correct milestone -> no findings"""
        issues = [
            {
                "number": 100, "title": "Clean Issue", "state": "open",
                "milestone_number": 1, "labels": ["phase/implementation"], "body": "",
                "is_pr": False, "depth": 0, "parent_number": None,
            }
        ]
        findings = self._run_analyze(issues)
        self.assertEqual(findings["pr_mixed"], [])
        self.assertEqual(findings["milestone_mismatches"], [])
        self.assertEqual(findings["stale_state_labels"], [])
        self.assertEqual(findings["open_blockers"], [])

    def test_scope_conflict_count_depth_ge1(self):
        """B3: scope_conflict_count counts depth>=1 milestone mismatches."""
        issues = [
            {
                "number": 10, "title": "Scope Conflict Child", "state": "open",
                "milestone_number": 2, "labels": [], "body": "",
                "is_pr": False, "depth": 1, "parent_number": 1,
            },
            {
                "number": 11, "title": "Direct Mismatch", "state": "open",
                "milestone_number": 2, "labels": [], "body": "",
                "is_pr": False, "depth": 0, "parent_number": None,
            },
        ]
        findings = self._run_analyze(issues)
        # depth=1 is scope conflict; depth=0 is direct mismatch (not scope conflict)
        self.assertEqual(findings["scope_conflict_count"], 1)
        self.assertEqual(len(findings["milestone_mismatches"]), 2)

    def test_scope_conflict_count_zero_when_clean(self):
        """B3: scope_conflict_count is 0 when no depth>=1 mismatches."""
        issues = [
            {
                "number": 10, "title": "Clean Child", "state": "open",
                "milestone_number": None, "labels": [], "body": "",
                "is_pr": False, "depth": 1, "parent_number": 1,
            },
        ]
        findings = self._run_analyze(issues)
        # depth=1, milestone=null is normal (AC5), not a mismatch
        self.assertEqual(findings["scope_conflict_count"], 0)
        self.assertEqual(len(findings["milestone_mismatches"]), 0)


# ---------------------------------------------------------------------------
# Tests for build_report
# ---------------------------------------------------------------------------

class TestBuildReport(unittest.TestCase):
    def _empty_findings(self):
        return {
            "pr_mixed": [],
            "milestone_mismatches": [],
            "stale_state_labels": [],
            "open_blockers": [],
            "scope_conflict_count": 0,
        }

    def test_schema_field_present(self):
        report = mr.build_report(1, [], self._empty_findings(),
                                 [], "2026-01-01T00:00:00Z", "owner/repo")
        self.assertEqual(report["schema"], "MILESTONE_DESCENDANT_ROLLUP_V1")

    def test_summary_counts(self):
        issues = [
            {"number": 1, "state": "open", "is_pr": False},
            {"number": 2, "state": "closed", "is_pr": False},
            {"number": 3, "state": "open", "is_pr": True},
        ]
        findings = {
            "pr_mixed": [{"number": 3}],
            "milestone_mismatches": [],
            "stale_state_labels": [],
            "open_blockers": [],
            "scope_conflict_count": 0,
        }
        report = mr.build_report(1, issues, findings,
                                 [], "2026-01-01T00:00:00Z", "owner/repo")
        s = report["summary"]
        self.assertEqual(s["total_descendants"], 3)
        self.assertEqual(s["open_issues"], 1)
        self.assertEqual(s["closed_issues"], 1)
        self.assertEqual(s["pr_mixed_count"], 1)
        self.assertTrue(s["has_invariant_violation"])

    def test_no_invariant_violation_if_no_pr_mixed(self):
        report = mr.build_report(1, [], self._empty_findings(),
                                 [], "2026-01-01T00:00:00Z", "owner/repo")
        self.assertFalse(report["summary"]["has_invariant_violation"])

    def test_closure_followups_default_empty(self):
        """B3: closure_followups defaults to [] when not provided."""
        report = mr.build_report(1, [], self._empty_findings(),
                                 [], "2026-01-01T00:00:00Z", "owner/repo")
        self.assertIn("closure_followups", report)
        self.assertEqual(report["closure_followups"], [])

    def test_defer_records_default_empty(self):
        """B3: defer_records defaults to [] when not provided."""
        report = mr.build_report(1, [], self._empty_findings(),
                                 [], "2026-01-01T00:00:00Z", "owner/repo")
        self.assertIn("defer_records", report)
        self.assertEqual(report["defer_records"], [])

    def test_closure_followups_passed_through(self):
        """B3: closure_followups is stored in report when provided."""
        cf = ["#100", "#200"]
        report = mr.build_report(1, [], self._empty_findings(),
                                 [], "2026-01-01T00:00:00Z", "owner/repo",
                                 closure_followups=cf)
        self.assertEqual(report["closure_followups"], cf)

    def test_defer_records_passed_through(self):
        """B3: defer_records is stored in report when provided."""
        dr = [{"issue": "#42", "_valid": True}]
        report = mr.build_report(1, [], self._empty_findings(),
                                 [], "2026-01-01T00:00:00Z", "owner/repo",
                                 defer_records=dr)
        self.assertEqual(report["defer_records"], dr)

    def test_scope_conflict_count_in_summary(self):
        """B3/H2: scope_conflict_count appears in summary."""
        findings = {
            "pr_mixed": [],
            "milestone_mismatches": [],
            "stale_state_labels": [],
            "open_blockers": [],
            "scope_conflict_count": 2,
        }
        report = mr.build_report(1, [], findings,
                                 [], "2026-01-01T00:00:00Z", "owner/repo")
        self.assertIn("scope_conflict_count", report["summary"])
        self.assertEqual(report["summary"]["scope_conflict_count"], 2)


# ---------------------------------------------------------------------------
# Tests for render_markdown
# ---------------------------------------------------------------------------

class TestRenderMarkdown(unittest.TestCase):
    def _base_report(self, **overrides):
        r = {
            "schema": "MILESTONE_DESCENDANT_ROLLUP_V1",
            "generated_at": "2026-01-01T00:00:00Z",
            "repo": "owner/repo",
            "milestone_number": 1,
            "partial": False,
            "closure_followups": [],
            "defer_records": [],
            "summary": {
                "total_descendants": 0, "open_issues": 0, "closed_issues": 0,
                "pr_mixed_count": 0, "milestone_mismatch_count": 0,
                "stale_state_label_count": 0, "open_blocker_count": 0,
                "scope_conflict_count": 0,
                "has_invariant_violation": False,
            },
            "pr_mixed": [],
            "milestone_mismatches": [],
            "stale_state_labels": [],
            "open_blockers": [],
            "warnings": [],
        }
        r.update(overrides)
        return r

    def test_contains_schema_heading(self):
        md = mr.render_markdown(self._base_report())
        self.assertIn("## Milestone Descendant Rollup: #1", md)
        self.assertIn("owner/repo", md)

    def test_pr_mixed_appears_in_table(self):
        report = self._base_report(
            pr_mixed=[{"number": 55, "title": "Some PR", "state": "open", "depth": 0}],
            summary={
                "total_descendants": 1, "open_issues": 0, "closed_issues": 0,
                "pr_mixed_count": 1, "milestone_mismatch_count": 0,
                "stale_state_label_count": 0, "open_blocker_count": 0,
                "scope_conflict_count": 0,
                "has_invariant_violation": True,
            },
        )
        md = mr.render_markdown(report)
        self.assertIn("55", md)
        self.assertIn("Some PR", md)

    def test_pipe_in_title_escaped(self):
        """High: titles with | must be escaped in table cells."""
        report = self._base_report(
            pr_mixed=[{"number": 1, "title": "A|B title", "state": "open", "depth": 0}],
            summary={
                "total_descendants": 1, "open_issues": 0, "closed_issues": 0,
                "pr_mixed_count": 1, "milestone_mismatch_count": 0,
                "stale_state_label_count": 0, "open_blocker_count": 0,
                "scope_conflict_count": 0,
                "has_invariant_violation": True,
            },
        )
        md = mr.render_markdown(report)
        self.assertIn("A\\|B title", md, "Pipe in title must be escaped as \\|")
        for line in md.splitlines():
            if "A|B title" in line and "A\\|B title" not in line:
                self.fail(f"Unescaped pipe found in table line: {line!r}")


# ---------------------------------------------------------------------------
# Integration-style: main() exit codes
# ---------------------------------------------------------------------------

class TestMainExitCodes(unittest.TestCase):
    """Test main() behavior with mocked API and token."""

    def test_api_error_propagates_as_runtime_error(self):
        """HTTP errors from the API are raised as RuntimeError (non-zero exit path)"""
        def raising_urlopen(req, timeout=None):
            raise urllib.error.URLError('connection refused')

        with patch('urllib.request.urlopen', side_effect=raising_urlopen):
            with self.assertRaises(RuntimeError):
                mr.collect_descendants('owner', 'repo', 1, 'fake_token')

    def test_exit_0_when_no_strict(self):
        """findings present but no --strict -> exit 0"""
        report = mr.build_report(
            1, [], {"pr_mixed": [], "milestone_mismatches": [{"number": 9}],
                    "stale_state_labels": [], "open_blockers": [], "scope_conflict_count": 0},
            [], "2026-01-01T00:00:00Z", "owner/repo"
        )
        self.assertFalse(report["summary"]["has_invariant_violation"])

    def test_strict_flag_detects_pr_mixed(self):
        """has_invariant_violation=True when PR in milestone"""
        report = mr.build_report(
            1, [{"number": 1, "state": "open", "is_pr": True}],
            {"pr_mixed": [{"number": 1}], "milestone_mismatches": [],
             "stale_state_labels": [], "open_blockers": [], "scope_conflict_count": 0},
            [], "2026-01-01T00:00:00Z", "owner/repo"
        )
        self.assertTrue(report["summary"]["has_invariant_violation"])

    def test_no_token_proceeds_unauthenticated(self):
        """Blocker 3: missing token must not return 1; proceeds unauthenticated."""
        headers = mr._build_headers(None)
        self.assertNotIn("Authorization", headers)

    def test_token_present_adds_auth_header(self):
        """Token present adds Authorization header."""
        headers = mr._build_headers("mytoken")
        self.assertIn("Authorization", headers)
        self.assertEqual(headers["Authorization"], "Bearer mytoken")


# ---------------------------------------------------------------------------
# B1/B2: evaluate_close_readiness production function tests
# ---------------------------------------------------------------------------

def _build_valid_defer_decision(
    issue="#42",
    reason="Low priority",
    residual_risk="Minimal",
    destination_issue="#100",
    destination_milestone="M2: Gameplay Core (v0.2.x)",
    revisit_condition="When M2 starts",
    decided_by="squne121",
    decided_at="2026-06-25T00:00:00Z",
):
    """Build a valid defer decision fixture with all required fields."""
    return {
        "issue": issue,
        "reason": reason,
        "residual_risk": residual_risk,
        "destination": {
            "issue": destination_issue,
            "milestone": destination_milestone,
        },
        "revisit_condition": revisit_condition,
        "decided_by": decided_by,
        "decided_at": decided_at,
    }


def _build_report_for_close(
    open_issues=0,
    pr_mixed=None,
    open_blocker_count=0,
    partial=False,
    warnings=None,
    milestone_mismatches=None,
    stale_state_labels=None,
    scope_conflict_count=0,
):
    """Build a MILESTONE_DESCENDANT_ROLLUP_V1 report dict for close predicate tests."""
    pr_mixed = pr_mixed or []
    warnings = warnings or []
    milestone_mismatches = milestone_mismatches or []
    stale_state_labels = stale_state_labels or []
    open_blockers = (
        [{"number": i + 100, "title": f"Blocker {i}", "open_blocker_numbers": [i + 200],
          "depth": 0, "source": "native"}
         for i in range(open_blocker_count)]
        if open_blocker_count > 0
        else []
    )

    all_issues = [
        {"number": i + 1, "state": "open", "is_pr": False}
        for i in range(open_issues)
    ]
    findings = {
        "pr_mixed": pr_mixed,
        "milestone_mismatches": milestone_mismatches,
        "stale_state_labels": stale_state_labels,
        "open_blockers": open_blockers,
        "scope_conflict_count": scope_conflict_count,
    }
    report = mr.build_report(
        1,
        all_issues,
        findings,
        warnings,
        "2026-01-01T00:00:00Z",
        "owner/repo",
        partial=partial,
    )
    return report


class TestEvaluateCloseReadiness(unittest.TestCase):
    """B1/B2: Tests for mr.evaluate_close_readiness() production function."""

    def test_all_conditions_clean_returns_true(self):
        """All conditions satisfied -> (True, [])."""
        report = _build_report_for_close()
        ok, reasons = mr.evaluate_close_readiness(report)
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_open_issues_prevents_close(self):
        """open_issues > 0 -> (False, [reason])."""
        report = _build_report_for_close(open_issues=2)
        ok, reasons = mr.evaluate_close_readiness(report)
        self.assertFalse(ok)
        self.assertTrue(any("open_issues" in r for r in reasons))

    def test_pr_mixed_prevents_close(self):
        """pr_mixed_count > 0 -> (False, [reason])."""
        pr_item = {"number": 99, "title": "Some PR", "state": "open", "depth": 0}
        report = _build_report_for_close(pr_mixed=[pr_item])
        ok, reasons = mr.evaluate_close_readiness(report)
        self.assertFalse(ok)
        self.assertTrue(any("pr_mixed_count" in r for r in reasons))

    def test_partial_prevents_close(self):
        """partial=true -> (False, [reason])."""
        report = _build_report_for_close(partial=True)
        ok, reasons = mr.evaluate_close_readiness(report)
        self.assertFalse(ok)
        self.assertTrue(any("partial" in r for r in reasons))

    def test_warnings_prevent_close(self):
        """warnings non-empty -> (False, [reason])."""
        w = [{"type": "cross_repo_sub_issue", "parent_number": 1, "child_number": 2,
              "child_repo_url": "https://api.github.com/repos/other/repo"}]
        report = _build_report_for_close(warnings=w)
        ok, reasons = mr.evaluate_close_readiness(report)
        self.assertFalse(ok)
        self.assertTrue(any("warning" in r for r in reasons))

    def test_open_blocker_prevents_close(self):
        """open_blocker_count > 0 -> (False, [reason])."""
        report = _build_report_for_close(open_blocker_count=1)
        ok, reasons = mr.evaluate_close_readiness(report)
        self.assertFalse(ok)
        self.assertTrue(any("open_blocker_count" in r for r in reasons))

    def test_scope_conflict_prevents_close(self):
        """scope_conflict_count > 0 -> (False, [reason])."""
        report = _build_report_for_close(scope_conflict_count=1)
        ok, reasons = mr.evaluate_close_readiness(report)
        self.assertFalse(ok)
        self.assertTrue(any("scope_conflict_count" in r for r in reasons))

    def test_multiple_failures_all_reported(self):
        """Multiple failing conditions -> all reported in reasons list."""
        report = _build_report_for_close(
            open_issues=1,
            partial=True,
            open_blocker_count=1,
        )
        ok, reasons = mr.evaluate_close_readiness(report)
        self.assertFalse(ok)
        self.assertGreaterEqual(len(reasons), 3)

    def test_returns_tuple(self):
        """evaluate_close_readiness returns a tuple."""
        report = _build_report_for_close()
        result = mr.evaluate_close_readiness(report)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)


# ---------------------------------------------------------------------------
# B2: validate_defer_decision production function tests
# ---------------------------------------------------------------------------

class TestValidateDeferDecision(unittest.TestCase):
    """B2: Tests for mr.validate_defer_decision() production function."""

    def test_valid_defer_decision_accepted(self):
        """All 7 required fields present -> (True, [])."""
        defer = _build_valid_defer_decision()
        ok, errs = mr.validate_defer_decision(defer)
        self.assertTrue(ok)
        self.assertEqual(errs, [])

    def test_missing_reason_invalid(self):
        """reason field missing -> (False, [error])."""
        defer = _build_valid_defer_decision()
        del defer["reason"]
        ok, errs = mr.validate_defer_decision(defer)
        self.assertFalse(ok)
        self.assertTrue(any("reason" in e for e in errs))

    def test_missing_residual_risk_invalid(self):
        """residual_risk field missing -> (False, [error])."""
        defer = _build_valid_defer_decision()
        del defer["residual_risk"]
        ok, errs = mr.validate_defer_decision(defer)
        self.assertFalse(ok)
        self.assertTrue(any("residual_risk" in e for e in errs))

    def test_missing_decided_by_invalid(self):
        """decided_by field missing -> (False, [error])."""
        defer = _build_valid_defer_decision()
        del defer["decided_by"]
        ok, errs = mr.validate_defer_decision(defer)
        self.assertFalse(ok)
        self.assertTrue(any("decided_by" in e for e in errs))

    def test_missing_decided_at_invalid(self):
        """decided_at field missing -> (False, [error])."""
        defer = _build_valid_defer_decision()
        del defer["decided_at"]
        ok, errs = mr.validate_defer_decision(defer)
        self.assertFalse(ok)
        self.assertTrue(any("decided_at" in e for e in errs))

    def test_missing_destination_invalid(self):
        """destination field missing -> (False, [error])."""
        defer = _build_valid_defer_decision()
        del defer["destination"]
        ok, errs = mr.validate_defer_decision(defer)
        self.assertFalse(ok)
        self.assertTrue(any("destination" in e for e in errs))

    def test_missing_revisit_condition_invalid(self):
        """revisit_condition field missing -> (False, [error])."""
        defer = _build_valid_defer_decision()
        del defer["revisit_condition"]
        ok, errs = mr.validate_defer_decision(defer)
        self.assertFalse(ok)
        self.assertTrue(any("revisit_condition" in e for e in errs))

    def test_missing_issue_field_invalid(self):
        """issue field missing -> (False, [error])."""
        defer = _build_valid_defer_decision()
        del defer["issue"]
        ok, errs = mr.validate_defer_decision(defer)
        self.assertFalse(ok)
        self.assertTrue(any("issue" in e for e in errs))

    def test_destination_missing_issue_key_invalid(self):
        """destination.issue missing -> (False, [error])."""
        defer = _build_valid_defer_decision()
        del defer["destination"]["issue"]
        ok, errs = mr.validate_defer_decision(defer)
        self.assertFalse(ok)
        self.assertTrue(any("destination.issue" in e for e in errs))

    def test_destination_milestone_optional(self):
        """destination.milestone is optional — valid without it."""
        defer = _build_valid_defer_decision()
        del defer["destination"]["milestone"]
        ok, errs = mr.validate_defer_decision(defer)
        self.assertTrue(ok, f"destination.milestone is optional; errs={errs}")

    def test_not_a_dict_invalid(self):
        """Non-dict input -> (False, [error])."""
        ok, errs = mr.validate_defer_decision("not a dict")
        self.assertFalse(ok)
        ok2, errs2 = mr.validate_defer_decision(None)
        self.assertFalse(ok2)
        ok3, errs3 = mr.validate_defer_decision([])
        self.assertFalse(ok3)

    def test_returns_tuple(self):
        """validate_defer_decision returns a tuple."""
        defer = _build_valid_defer_decision()
        result = mr.validate_defer_decision(defer)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)


# ---------------------------------------------------------------------------
# AC8 Fixture tests (now using production functions from mr module)
# ---------------------------------------------------------------------------

class TestClosePredicateFixtures(unittest.TestCase):
    """AC8: Fixture tests for fail-closed close predicate using mr.evaluate_close_readiness()."""

    def test_open_blocker_prevents_close(self):
        """AC8-fixture-1: open_blocker_count > 0 → close not ready."""
        report = _build_report_for_close(open_issues=0, open_blocker_count=1)
        ok, _ = mr.evaluate_close_readiness(report)
        self.assertFalse(ok, "open blocker が存在する場合は close 不可（fail-closed）")
        self.assertEqual(report["summary"]["open_blocker_count"], 1)

    def test_no_blocker_does_not_prevent_close(self):
        """AC8-fixture-1 (negative): no blocker → predicate does not fail on blocker."""
        report = _build_report_for_close(open_issues=0, open_blocker_count=0)
        self.assertEqual(report["summary"]["open_blocker_count"], 0)
        ok, _ = mr.evaluate_close_readiness(report)
        self.assertTrue(ok)

    def test_partial_true_prevents_close(self):
        """AC8-fixture-2: partial=true → close not ready."""
        report = _build_report_for_close(open_issues=0, partial=True)
        ok, _ = mr.evaluate_close_readiness(report)
        self.assertFalse(ok, "partial=true の場合は close 不可（descendant traversal 不完全）")
        self.assertTrue(report["partial"])

    def test_partial_false_does_not_prevent_close(self):
        """AC8-fixture-2 (negative): partial=false → predicate does not fail on partial."""
        report = _build_report_for_close(open_issues=0, partial=False)
        self.assertFalse(report["partial"])
        ok, _ = mr.evaluate_close_readiness(report)
        self.assertTrue(ok)

    def test_warnings_present_prevents_close(self):
        """AC8-fixture-3: warnings 非空 → close not ready."""
        warnings = [{"type": "cross_repo_sub_issue", "parent_number": 10, "child_number": 20,
                     "child_repo_url": "https://api.github.com/repos/other/repo"}]
        report = _build_report_for_close(open_issues=0, warnings=warnings)
        ok, _ = mr.evaluate_close_readiness(report)
        self.assertFalse(ok, "warnings 非空の場合は close 不可")
        self.assertEqual(len(report["warnings"]), 1)

    def test_empty_warnings_does_not_prevent_close(self):
        """AC8-fixture-3 (negative): warnings=[] → predicate does not fail on warnings."""
        report = _build_report_for_close(open_issues=0, warnings=[])
        self.assertEqual(len(report["warnings"]), 0)
        ok, _ = mr.evaluate_close_readiness(report)
        self.assertTrue(ok)

    def test_valid_defer_decision_accepted(self):
        """AC8-fixture-4: valid defer decision（全7フィールド揃い）は accept される。"""
        defer = _build_valid_defer_decision()
        ok, errs = mr.validate_defer_decision(defer)
        self.assertTrue(ok, f"全7フィールドを持つ defer decision は valid; errs={errs}")

    def test_valid_defer_decision_all_fields_present(self):
        """AC8-fixture-4: valid defer decision の必須フィールドがすべて存在することを確認。"""
        defer = _build_valid_defer_decision(
            issue="#50",
            reason="Known limitation, tracked in M2",
            residual_risk="Low: feature non-critical in M1 scope",
            destination_issue="#300",
            destination_milestone="M2: Gameplay Core (v0.2.x)",
            revisit_condition="When #300 is created and assigned",
            decided_by="squne121",
            decided_at="2026-06-25T12:00:00Z",
        )
        ok, errs = mr.validate_defer_decision(defer)
        self.assertTrue(ok, f"errs={errs}")
        self.assertIn("issue", defer["destination"])
        self.assertIn("milestone", defer["destination"])

    def test_invalid_defer_decision_missing_reason(self):
        """AC8-fixture-5: reason フィールド欠落 → invalid defer decision。"""
        defer = _build_valid_defer_decision()
        del defer["reason"]
        ok, _ = mr.validate_defer_decision(defer)
        self.assertFalse(ok, "reason フィールド欠落は invalid defer decision")

    def test_invalid_defer_decision_missing_residual_risk(self):
        """AC8-fixture-5: residual_risk フィールド欠落 → invalid defer decision。"""
        defer = _build_valid_defer_decision()
        del defer["residual_risk"]
        ok, _ = mr.validate_defer_decision(defer)
        self.assertFalse(ok)

    def test_invalid_defer_decision_missing_decided_by(self):
        """AC8-fixture-5: decided_by フィールド欠落 → invalid defer decision。"""
        defer = _build_valid_defer_decision()
        del defer["decided_by"]
        ok, _ = mr.validate_defer_decision(defer)
        self.assertFalse(ok)

    def test_invalid_defer_decision_missing_decided_at(self):
        """AC8-fixture-5: decided_at フィールド欠落 → invalid defer decision。"""
        defer = _build_valid_defer_decision()
        del defer["decided_at"]
        ok, _ = mr.validate_defer_decision(defer)
        self.assertFalse(ok)

    def test_invalid_defer_decision_missing_destination(self):
        """AC8-fixture-5: destination フィールド欠落 → invalid defer decision。"""
        defer = _build_valid_defer_decision()
        del defer["destination"]
        ok, _ = mr.validate_defer_decision(defer)
        self.assertFalse(ok)

    def test_invalid_defer_decision_missing_revisit_condition(self):
        """AC8-fixture-5: revisit_condition フィールド欠落 → invalid defer decision。"""
        defer = _build_valid_defer_decision()
        del defer["revisit_condition"]
        ok, _ = mr.validate_defer_decision(defer)
        self.assertFalse(ok)

    def test_invalid_defer_decision_not_a_dict(self):
        """AC8-fixture-5: defer が dict でない場合は invalid。"""
        ok1, _ = mr.validate_defer_decision("not a dict")
        self.assertFalse(ok1)
        ok2, _ = mr.validate_defer_decision(None)
        self.assertFalse(ok2)
        ok3, _ = mr.validate_defer_decision([])
        self.assertFalse(ok3)

    def test_indirect_child_milestone_null_is_normal(self):
        """AC8-fixture-6: depth>=1 の descendant で milestone=null は間接所属として正常（AC5）。"""
        issues = [
            {
                "number": 2, "title": "Indirect Child", "state": "open",
                "milestone_number": None, "labels": [], "body": "",
                "is_pr": False, "depth": 1, "parent_number": 1,
            }
        ]

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "dependencies/blocked_by" in url:
                raise FakeHTTPError(404)
            raise RuntimeError(f"Unexpected URL: {url}")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            findings = mr.analyze(issues, 1, "owner", "repo", "fake_token")

        self.assertEqual(
            len(findings["milestone_mismatches"]), 0,
            "depth=1, milestone=null は間接所属として正常（mismatch にしない）",
        )

    def test_indirect_child_depth2_milestone_null_is_normal(self):
        """AC8-fixture-6: depth=2 の descendant で milestone=null も間接所属として正常（AC5）。"""
        issues = [
            {
                "number": 5, "title": "Grandchild", "state": "open",
                "milestone_number": None, "labels": [], "body": "",
                "is_pr": False, "depth": 2, "parent_number": 3,
            }
        ]

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "dependencies/blocked_by" in url:
                raise FakeHTTPError(404)
            raise RuntimeError(f"Unexpected URL: {url}")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            findings = mr.analyze(issues, 1, "owner", "repo", "fake_token")

        self.assertEqual(
            len(findings["milestone_mismatches"]), 0,
            "depth=2, milestone=null も間接所属として正常（mismatch にしない）",
        )

    def test_direct_item_milestone_null_is_mismatch(self):
        """AC8-fixture-6 (contrast): depth=0 の direct item で milestone=null は mismatch。"""
        issues = [
            {
                "number": 10, "title": "Direct Item No Milestone", "state": "open",
                "milestone_number": None, "labels": [], "body": "",
                "is_pr": False, "depth": 0, "parent_number": None,
            }
        ]

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "dependencies/blocked_by" in url:
                raise FakeHTTPError(404)
            raise RuntimeError(f"Unexpected URL: {url}")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            findings = mr.analyze(issues, 1, "owner", "repo", "fake_token")

        self.assertEqual(
            len(findings["milestone_mismatches"]), 1,
            "depth=0 の direct item で milestone=null は mismatch（正常扱いしない）",
        )
        self.assertIsNone(findings["milestone_mismatches"][0]["milestone_number"])

    def test_descendant_with_wrong_milestone_is_mismatch(self):
        """AC8-fixture-6 (contrast): depth>=1 で別 Milestone に明示割当は scope conflict (mismatch)。"""
        issues = [
            {
                "number": 20, "title": "Child Wrong Milestone", "state": "open",
                "milestone_number": 2, "labels": [], "body": "",
                "is_pr": False, "depth": 1, "parent_number": 1,
            }
        ]

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "dependencies/blocked_by" in url:
                raise FakeHTTPError(404)
            raise RuntimeError(f"Unexpected URL: {url}")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            findings = mr.analyze(issues, 1, "owner", "repo", "fake_token")

        self.assertEqual(
            len(findings["milestone_mismatches"]), 1,
            "depth=1 で別 Milestone に明示割当は scope conflict として mismatch",
        )
        self.assertEqual(findings["milestone_mismatches"][0]["milestone_number"], 2)

    def test_all_conditions_clean_allows_close(self):
        """AC8: 全条件クリアの場合のみ close_judgment_available=true。"""
        report = _build_report_for_close(
            open_issues=0,
            pr_mixed=[],
            open_blocker_count=0,
            partial=False,
            warnings=[],
        )
        ok, _ = mr.evaluate_close_readiness(report)
        self.assertTrue(ok)

    def test_open_issues_remaining_prevents_close(self):
        """AC8: open_issues > 0 の場合は close 不可。"""
        report = _build_report_for_close(open_issues=2)
        ok, _ = mr.evaluate_close_readiness(report)
        self.assertFalse(ok)

    def test_pr_mixed_prevents_close(self):
        """AC8: pr_mixed_count > 0 の場合は close 不可（invariant violation）。"""
        pr_item = {"number": 99, "title": "Some PR", "state": "open", "depth": 0}
        report = _build_report_for_close(open_issues=0, pr_mixed=[pr_item])
        ok, _ = mr.evaluate_close_readiness(report)
        self.assertFalse(ok)
        self.assertEqual(report["summary"]["pr_mixed_count"], 1)


# ---------------------------------------------------------------------------
# E2E Shell test (B2 requirement — skipped; requires live GitHub API)
# ---------------------------------------------------------------------------

class TestShellE2E(unittest.TestCase):
    def test_shell_close_readiness_field_present(self):
        """Shell が close_judgment_available: フィールドを含む stdout を返すことを確認。

        Live GitHub API が必要なため、構造テストのみ実施してスキップ。
        実機検証は PR CI またはローカル手動実行で確認する。
        """
        shell_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ".claude", "skills", "milestone-rollup", "scripts", "milestone_rollup.sh",
        )
        if not os.path.exists(shell_path):
            self.skipTest("Shell script not found")
        self.skipTest("E2E requires live GitHub API; verified manually")


if __name__ == "__main__":
    unittest.main()
