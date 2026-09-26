"""
test_prose_boundary_policy_section_extraction.py

Issue #2782: prose_boundary_policy.py に新設する level-2 canonical section
extraction helper（extract_level2_section() / extract_level2_section_with_bounds()）
の回帰テスト。

section boundary algorithm の単一 authority を prose_boundary_policy.py に
一本化するにあたり、以下の GFM edge case で誤抽出しないことを固定する:

  AC1: GFM closing-hash 見出し（見出し末尾に `##` が付く形式）
  AC2: section 内部の nested 見出し（`### Runtime checks` 等）で途切れない
  AC3: fenced code block 内の `##` で始まる行を section 終端とみなさない
  AC4: return contract の3値区別
       - heading 不在 -> None
       - heading 存在するが本文空 -> ""
       - heading 存在し本文あり -> non-empty str

Runtime Verification Applicability: not_applicable
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from prose_boundary_policy import (  # noqa: E402
    extract_level2_section,
    extract_level2_section_with_bounds,
)


# ---------------------------------------------------------------------------
# AC1: GFM closing-hash heading
# ---------------------------------------------------------------------------


def test_closing_hash_heading_extracts_verification_commands_section():
    """AC1: `## Verification Commands ##`（GFM closing-hash 見出し）でも
    section 本文を正しく抽出する。"""
    body = textwrap.dedent(
        """\
        ## Outcome

        fixture outcome text.

        ## Verification Commands ##

        ```bash
        $ pytest tests/test_foo.py -q
        ```

        ## Allowed Paths

        - foo/bar.py
        """
    )
    section = extract_level2_section(body, "Verification Commands")
    assert section is not None
    assert "pytest tests/test_foo.py" in section
    # closing heading の次のセクション（Allowed Paths）は含まない
    assert "Allowed Paths" not in section


def test_closing_hash_heading_with_extra_trailing_spaces():
    """AC1: closing-hash 見出しの後に trailing space があっても正しく抽出する。"""
    body = "## Verification Commands ##  \n\ncontent line\n\n## Notes\n\nother\n"
    section = extract_level2_section(body, "Verification Commands")
    assert section is not None
    assert "content line" in section
    assert "other" not in section


# ---------------------------------------------------------------------------
# AC2: nested heading does not terminate the section
# ---------------------------------------------------------------------------


def test_nested_heading_does_not_terminate_section():
    """AC2: section 内部の `### Runtime checks` などの nested 見出しでは
    section が途切れず、次の `##` レベル見出しまでの全文を抽出する。"""
    body = textwrap.dedent(
        """\
        ## Verification Commands

        ```bash
        $ pytest tests/test_a.py -q
        ```

        ### Runtime checks

        ```bash
        $ pytest tests/test_b.py -q
        ```

        ## Stop Conditions

        - N/A
        """
    )
    section = extract_level2_section(body, "Verification Commands")
    assert section is not None
    assert "test_a.py" in section
    assert "### Runtime checks" in section
    assert "test_b.py" in section
    assert "Stop Conditions" not in section


def test_nested_heading_with_bounds_end_index_at_next_level2_heading():
    """AC2: extract_level2_section_with_bounds() の end_index は次の level-2
    見出し行を指し、level-3 の nested 見出しでは止まらない。"""
    body = textwrap.dedent(
        """\
        ## Verification Commands

        top content

        ### Nested

        nested content

        ## Notes

        notes content
        """
    )
    result = extract_level2_section_with_bounds(body, "Verification Commands")
    assert result is not None
    text, start_index, end_index = result
    lines = body.splitlines(keepends=True)
    assert lines[end_index].rstrip("\n") == "## Notes"
    assert "nested content" in text


# ---------------------------------------------------------------------------
# AC3: fenced code block 内の擬似 `##` 行は section 終端とみなさない
# ---------------------------------------------------------------------------


def test_fenced_hash_pseudo_heading_inside_bash_block_not_treated_as_terminator():
    """AC3: fenced ```bash``` ブロック内に `##` で始まる行があっても、
    section 終端とみなさず正しく抽出する。"""
    body = textwrap.dedent(
        """\
        ## Verification Commands

        ```bash
        $ echo '## fake heading inside fence'
        $ pytest tests/test_c.py -q
        ```

        ## Allowed Paths

        - baz.py
        """
    )
    section = extract_level2_section(body, "Verification Commands")
    assert section is not None
    assert "## fake heading inside fence" in section
    assert "test_c.py" in section
    assert "Allowed Paths" not in section


def test_fenced_hash_pseudo_heading_inside_unlabeled_fence_not_treated_as_terminator():
    """AC3: unlabeled fence（``` のみ）内の擬似 `##` 行でも section 終端に
    しない（fence 種別を問わず fence 内は見出し候補から除外する）。"""
    body = textwrap.dedent(
        """\
        ## Verification Commands

        ```
        ## this looks like a heading but is inside a fence
        ```

        ## Notes

        after
        """
    )
    section = extract_level2_section(body, "Verification Commands")
    assert section is not None
    assert "this looks like a heading but is inside a fence" in section
    assert "after" not in section


# ---------------------------------------------------------------------------
# AC4: return contract — None / "" / non-empty str の三値区別
# ---------------------------------------------------------------------------


def test_absent_heading_returns_none():
    """AC4: canonical_en に一致する見出しが存在しない場合 None を返す。"""
    body = textwrap.dedent(
        """\
        ## Outcome

        no VC section here.

        ## Allowed Paths

        - foo.py
        """
    )
    assert extract_level2_section(body, "Verification Commands") is None


def test_absent_heading_returns_none_for_empty_body():
    """AC4: 空文字列 body でも None を返す（heading 自体が存在しないため）。"""
    assert extract_level2_section("", "Verification Commands") is None


def test_present_empty_section_returns_empty_string():
    """AC4: heading は存在するが本文が空（次の見出しまで空行のみ）の場合、
    None ではなく "" を返す。"""
    body = "## Verification Commands\n\n\n## Notes\n\nafter\n"
    section = extract_level2_section(body, "Verification Commands")
    assert section == ""
    assert section is not None


def test_present_empty_section_at_end_of_body_returns_empty_string():
    """AC4: heading が body 末尾にあり、本文が完全に空（EOF まで空行のみ）
    の場合も "" を返す。"""
    body = "## Verification Commands\n\n   \n"
    section = extract_level2_section(body, "Verification Commands")
    assert section == ""


def test_present_nonempty_section_returns_content():
    """AC4: heading が存在し本文に content がある場合、non-empty str を返す。"""
    body = "## Verification Commands\n\n```bash\n$ pytest -q\n```\n\n## Notes\n\nafter\n"
    section = extract_level2_section(body, "Verification Commands")
    assert isinstance(section, str)
    assert section != ""
    assert "pytest -q" in section
