"""
test_gfm_table_detection.py

GFM パイプテーブル検出のユニットテスト（Issue #685）

AC1: prose_boundary_policy.iter_markdown_blocks() が valid GFM pipe table を
     BLOCK_KIND_TABLE として返す
AC6: uv run pytest .claude/skills/create-issue/scripts/ -q が全件 PASS
"""

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import prose_boundary_policy as pbp
from prose_boundary_policy import (
    BLOCK_KIND_CODE_FENCE,
    BLOCK_KIND_HUMAN_PROSE,
    BLOCK_KIND_TABLE,
    iter_markdown_blocks,
    split_blocks,
)
from validate_japanese_content import (
    validate_text,
    extract_code_fences,
    split_markdown_blocks,
)


# ===========================================================================
# AC1: BLOCK_KIND_TABLE 定数の存在確認
# ===========================================================================


class TestBlockKindTableExists:
    """AC1: BLOCK_KIND_TABLE 定数が prose_boundary_policy に存在する"""

    def test_block_kind_table_constant_exists(self):
        """GIVEN: prose_boundary_policy モジュール WHEN: BLOCK_KIND_TABLE を参照
        THEN: 'table' という文字列定数が存在する"""
        assert BLOCK_KIND_TABLE == "table"

    def test_block_kind_table_in_all_block_kinds(self):
        """GIVEN: ALL_BLOCK_KINDS WHEN: table を確認
        THEN: 'table' が含まれる"""
        assert BLOCK_KIND_TABLE in pbp.ALL_BLOCK_KINDS

    def test_all_block_kinds_count(self):
        """GIVEN: ALL_BLOCK_KINDS WHEN: 件数確認
        THEN: 10 種類（#685 で table を追加）"""
        assert len(pbp.ALL_BLOCK_KINDS) == 10


# ===========================================================================
# AC1: iter_markdown_blocks が GFM テーブルを BLOCK_KIND_TABLE として返す
# ===========================================================================


class TestIterMarkdownBlocksTableDetection:
    """AC1: iter_markdown_blocks が valid GFM pipe table を BLOCK_KIND_TABLE として返す"""

    def test_simple_two_column_table(self):
        """GIVEN: 2列のシンプルな GFM テーブル WHEN: iter_markdown_blocks
        THEN: BLOCK_KIND_TABLE を返す"""
        text = "| AC | Status |\n|---|---|\n| AC1 | pass |"
        blocks = list(iter_markdown_blocks(text))
        assert len(blocks) == 1
        block_text, block_kind = blocks[0]
        assert block_kind == BLOCK_KIND_TABLE

    def test_four_column_table_safety_claim_matrix(self):
        """GIVEN: Safety Claim Matrix スタイルの 4 列テーブル WHEN: iter_markdown_blocks
        THEN: BLOCK_KIND_TABLE を返す"""
        text = (
            "| AC | Claim | Evidence | Status |\n"
            "|---|---|---|---|\n"
            "| AC1 | foo bar | rg output | pass |\n"
            "| AC6 | baz qux | pytest | pass |"
        )
        blocks = list(iter_markdown_blocks(text))
        assert len(blocks) == 1
        _, kind = blocks[0]
        assert kind == BLOCK_KIND_TABLE

    def test_table_with_alignment_delimiters(self):
        """GIVEN: セル整列（:---、---:、:---:）を持つ GFM テーブル WHEN: iter_markdown_blocks
        THEN: BLOCK_KIND_TABLE を返す"""
        text = "| Left | Center | Right |\n|:---|:---:|---:|\n| a | b | c |"
        blocks = list(iter_markdown_blocks(text))
        assert any(kind == BLOCK_KIND_TABLE for _, kind in blocks)

    def test_table_without_leading_trailing_pipe(self):
        """GIVEN: leading/trailing pipe なしの optional pipe テーブル WHEN: iter_markdown_blocks
        THEN: BLOCK_KIND_TABLE を返す"""
        # GFM: leading/trailing pipe は optional
        text = "AC | Status\n---|---\nAC1 | pass"
        blocks = list(iter_markdown_blocks(text))
        assert any(kind == BLOCK_KIND_TABLE for _, kind in blocks)

    def test_invalid_table_no_delimiter_row(self):
        """GIVEN: デリミタ行なしのテーブル風テキスト WHEN: iter_markdown_blocks
        THEN: BLOCK_KIND_TABLE を返さない（human_prose として扱う）"""
        text = "| AC | Status |\n| AC1 | pass |"
        blocks = list(iter_markdown_blocks(text))
        # デリミタ行がないので table と認識されない
        assert all(kind != BLOCK_KIND_TABLE for _, kind in blocks)

    def test_invalid_table_cell_count_mismatch(self):
        """GIVEN: header/delimiter のセル数が不一致のテーブル WHEN: iter_markdown_blocks
        THEN: BLOCK_KIND_TABLE を返さない"""
        text = "| AC | Status | Extra |\n|---|---|\n| AC1 | pass |"
        blocks = list(iter_markdown_blocks(text))
        # セル数不一致なので table と認識されない
        assert all(kind != BLOCK_KIND_TABLE for _, kind in blocks)

    def test_pipe_inside_fenced_code_block_not_table(self):
        """GIVEN: fenced code block 内のパイプ文字 WHEN: iter_markdown_blocks
        THEN: code_fence として扱われ BLOCK_KIND_TABLE にならない"""
        text = "```\n| col1 | col2 |\n|---|---|\n| a | b |\n```"
        blocks = list(iter_markdown_blocks(text))
        assert any(kind == BLOCK_KIND_CODE_FENCE for _, kind in blocks)
        assert all(kind != BLOCK_KIND_TABLE for _, kind in blocks)

    def test_table_preceded_by_prose(self):
        """GIVEN: Japanese prose の後にテーブル WHEN: iter_markdown_blocks
        THEN: prose と table の 2 ブロックに分割される"""
        text = "これは日本語の説明文です。\n\n| AC | Status |\n|---|---|\n| AC1 | pass |"
        blocks = list(iter_markdown_blocks(text))
        kinds = [kind for _, kind in blocks]
        assert BLOCK_KIND_TABLE in kinds
        assert BLOCK_KIND_HUMAN_PROSE in kinds

    def test_table_followed_by_prose(self):
        """GIVEN: テーブルの後に Japanese prose WHEN: iter_markdown_blocks
        THEN: table と prose の 2 ブロックに分割される"""
        text = "| AC | Status |\n|---|---|\n| AC1 | pass |\n\nさらに説明。"
        blocks = list(iter_markdown_blocks(text))
        kinds = [kind for _, kind in blocks]
        assert BLOCK_KIND_TABLE in kinds
        assert BLOCK_KIND_HUMAN_PROSE in kinds

    def test_table_between_fenced_code_blocks(self):
        """GIVEN: 2つの fenced code block の間にテーブル WHEN: iter_markdown_blocks
        THEN: code_fence, table, code_fence の 3 ブロック"""
        text = (
            "```bash\necho hello\n```\n\n"
            "| AC | Status |\n|---|---|\n| AC1 | pass |\n\n"
            "```yaml\nkey: value\n```"
        )
        blocks = list(iter_markdown_blocks(text))
        kinds = [kind for _, kind in blocks]
        assert BLOCK_KIND_CODE_FENCE in kinds
        assert BLOCK_KIND_TABLE in kinds


# ===========================================================================
# validate_text: テーブルブロックを prose ratio 判定から除外
# ===========================================================================


class TestValidateTextTableExclusion:
    """validate_text() が BLOCK_KIND_TABLE を prose ratio 判定から除外する"""

    def test_table_only_body_passes(self):
        """GIVEN: テーブルのみの PR body WHEN: validate_text
        THEN: passed=True（machine-readable table のみの body は除外）"""
        text = "| AC | Status |\n|---|---|\n| AC1 | pass |\n| AC6 | pass |"
        result = validate_text(text)
        assert result.passed is True

    def test_empty_body_fails(self):
        """GIVEN: 空の PR body WHEN: validate_text
        THEN: passed=False（empty body は引き続き fail）"""
        result = validate_text("")
        assert result.passed is False

    def test_whitespace_only_body_fails(self):
        """GIVEN: 空白のみの PR body WHEN: validate_text
        THEN: passed=False（whitespace-only body は引き続き fail）"""
        result = validate_text("   \n\n  ")
        assert result.passed is False

    def test_japanese_prose_with_table_passes(self):
        """GIVEN: 日本語 prose + テーブルの PR body WHEN: validate_text
        THEN: テーブルが除外され、日本語 prose のみが検査されて passed=True"""
        text = (
            "このプルリクエストは変更を実装します。詳細な変更内容をここに記述します。\n\n"
            "| AC | Claim | Evidence | Status |\n"
            "|---|---|---|---|\n"
            "| AC1 | foo | bar | pass |\n"
            "| AC6 | baz | qux | pass |"
        )
        result = validate_text(text)
        assert result.passed is True

    def test_table_rows_not_counted_in_ratio(self):
        """GIVEN: テーブル行が大量にある PR body + 日本語 prose WHEN: validate_text
        THEN: テーブル行は比率計算に含まれず、日本語 prose の比率が正しく計算される"""
        # 日本語 prose が十分あるが、大量の英語テーブル行が加わっても比率が落ちない
        japanese_prose = "これは日本語の説明文です。プルリクエストの詳細を説明します。"
        table_rows = "\n".join(
            "| AC{} | claim text here | evidence text | pass |".format(i)
            for i in range(20)
        )
        table = "| AC | Claim | Evidence | Status |\n|---|---|---|---|\n" + table_rows
        text = japanese_prose + "\n\n" + table

        result = validate_text(text)
        assert result.passed is True

    def test_safety_claim_matrix_golden_corpus(self):
        """GIVEN: 実際の Safety Claim Matrix スタイルのテーブル WHEN: validate_text
        THEN: テーブルが除外されて、テーブルのみ body は passed=True"""
        # Safety Claim Matrix スタイルのテーブルのみ body
        text = (
            "| AC | Safety Claim | Evidence Type | Evidence Ref | Status |\n"
            "|---|---|---|---|---|\n"
            "| AC1 | BLOCK_KIND_TABLE が定義される | static check | rg -n BLOCK_KIND_TABLE | pass |\n"
            "| AC6 | pytest 全件 PASS | test execution | uv run pytest | pass |\n"
            "| AC7 | regression なし | test execution | uv run pytest | pass |\n"
            "| AC8 | --tb=short PASS | test execution | uv run pytest | pass |"
        )
        result = validate_text(text)
        assert result.passed is True


# ===========================================================================
# extract_code_fences: テーブルブロックが除外される
# ===========================================================================


class TestExtractCodeFencesTableExclusion:
    """extract_code_fences() が BLOCK_KIND_TABLE を除外する"""

    def test_table_excluded_from_prose_text(self):
        """GIVEN: テーブルを含むテキスト WHEN: extract_code_fences
        THEN: テーブル行が prose テキストから除外される"""
        text = "prose text\n\n| AC | Status |\n|---|---|\n| AC1 | pass |"
        prose, removed = extract_code_fences(text)
        # テーブル行はプロセ部分に含まれない
        assert "| AC | Status |" not in prose
        assert "| AC1 | pass |" not in prose

    def test_table_and_code_fence_both_excluded(self):
        """GIVEN: code fence とテーブルの両方を含むテキスト WHEN: extract_code_fences
        THEN: 両方が prose から除外される"""
        text = (
            "前の prose\n\n"
            "```bash\necho hello\n```\n\n"
            "| AC | Status |\n|---|---|\n| AC1 | pass |\n\n"
            "後の prose"
        )
        prose, removed = extract_code_fences(text)
        assert "echo hello" not in prose
        assert "| AC | Status |" not in prose


# ===========================================================================
# split_markdown_blocks: table ブロックの type 確認
# ===========================================================================


class TestSplitMarkdownBlocksTable:
    """split_markdown_blocks() が table ブロックを正しく分類する"""

    def test_table_block_type_is_table(self):
        """GIVEN: GFM テーブルを含むテキスト WHEN: split_markdown_blocks
        THEN: テーブルブロックの type が 'table'"""
        text = "| AC | Status |\n|---|---|\n| AC1 | pass |"
        blocks = split_markdown_blocks(text)
        table_blocks = [b for b in blocks if b.get('type') == 'table']
        assert len(table_blocks) >= 1

    def test_mixed_content_split(self):
        """GIVEN: Japanese prose + table の混在テキスト WHEN: split_markdown_blocks
        THEN: prose と table がそれぞれ正しい type を持つ"""
        text = (
            "これは日本語の説明文です。\n\n"
            "| AC | Status |\n|---|---|\n| AC1 | pass |"
        )
        blocks = split_markdown_blocks(text)
        types = {b['type'] for b in blocks}
        assert 'table' in types


# ===========================================================================
# classify_block_legacy: table -> 'table' マッピング
# ===========================================================================


class TestClassifyBlockLegacyTable:
    """classify_block_legacy() が BLOCK_KIND_TABLE を 'table' にマップする"""

    def test_table_legacy_name(self):
        """GIVEN: GFM テーブルブロック WHEN: classify_block_legacy
        THEN: 'table' を返す（legacy 名）"""
        table_text = "| AC | Status |\n|---|---|\n| AC1 | pass |"
        # classify_block_legacy は block テキストを取るので、
        # まず iter_markdown_blocks でテーブルブロックを取得してから渡す
        blocks = list(iter_markdown_blocks(table_text))
        assert len(blocks) == 1
        block_text, block_kind = blocks[0]
        assert block_kind == BLOCK_KIND_TABLE
        # classify_block_legacy は GFM table を detect しないが、
        # table ブロックが BLOCK_KIND_TABLE として分類されること自体を確認
        legacy = pbp.classify_block_legacy(block_text)
        assert legacy == 'table'


# ===========================================================================
# golden corpus: 実際の PR body に近いシナリオ
# ===========================================================================


class TestGoldenCorpus:
    """実際の PR body に近いシナリオのゴールデンコーパステスト"""

    def test_pr_body_with_japanese_prose_and_safety_table_passes(self):
        """GIVEN: 日本語 prose + Safety Claim Matrix テーブルを含む PR body
        WHEN: validate_text
        THEN: テーブルが除外され、日本語 prose が検査されて PASS"""
        text = (
            "このプルリクエストは `prose_boundary_policy.py` に `BLOCK_KIND_TABLE` を追加し、"
            "GFM パイプテーブルを非 prose として分類します。\n\n"
            "変更内容の詳細をここに記述します。\n\n"
            "| AC | Claim | Evidence | Status |\n"
            "|---|---|---|---|\n"
            "| AC1 | BLOCK_KIND_TABLE が定義される | rg output | pass |\n"
            "| AC6 | pytest 全件 PASS | test output | pass |"
        )
        result = validate_text(text)
        assert result.passed is True

    def test_table_only_body_passes(self):
        """GIVEN: テーブルのみの PR body（見出しなし）WHEN: validate_text
        THEN: machine-readable table のみなので PASS"""
        text = (
            "| AC | Status |\n"
            "|---|---|\n"
            "| AC1 | pass |\n"
            "| AC6 | pass |"
        )
        result = validate_text(text)
        assert result.passed is True

    def test_multiline_table_body_with_japanese_context_passes(self):
        """GIVEN: 複数行のテーブル + 日本語コンテキスト WHEN: validate_text
        THEN: テーブル行が除外されて日本語比率が正しく計算され PASS"""
        text = (
            "このプルリクエストでは以下の変更を行います。\n\n"
            "修正内容は下記のテーブルを参照してください。\n\n"
            "| Field | Value |\n"
            "|---|---|\n"
            "| author | squne121 |\n"
            "| reviewers | review team |\n"
            "| labels | enhancement, phase/implementation |\n"
        )
        result = validate_text(text)
        assert result.passed is True


# ===========================================================================
# B1 fix: テーブル後の block-level 要素がテーブルに含まれない（#685）
# ===========================================================================


class TestBlockLevelStarterTerminatesTable:
    """
    B1 fix (#685): _yield_prose_with_table_splits が行単位で走査し、
    blockquote・ATX heading 等の block-level 要素の開始でテーブルを終了させることを確認する。

    修正前は空行単位のみで分割していたため、テーブル直後の blockquote や heading が
    誤ってテーブルブロック内に取り込まれ、prose として検査されなかった。
    """

    def test_blockquote_after_table_is_not_in_table(self):
        """GIVEN: テーブル直後（空行なし）に blockquote WHEN: iter_markdown_blocks
        THEN: blockquote は table に含まれず BLOCK_KIND_HUMAN_PROSE として yield される"""
        text = (
            "| Claim | Evidence |\n"
            "| --- | --- |\n"
            "| safe | ci green |\n"
            "> This English prose should be checked, but current splitting can hide it as table.\n"
        )
        blocks = list(iter_markdown_blocks(text))
        kinds = [kind for _, kind in blocks]
        # blockquote がある部分は table でなく human_prose として yield される
        assert BLOCK_KIND_TABLE in kinds
        # blockquote 行を含む human_prose ブロックが存在すること
        prose_blocks = [(t, k) for t, k in blocks if k == BLOCK_KIND_HUMAN_PROSE]
        blockquote_in_prose = any("> This English prose" in t for t, _ in prose_blocks)
        assert blockquote_in_prose, "blockquote が prose ブロックに含まれていない"

    def test_atx_heading_after_table_is_not_in_table(self):
        """GIVEN: テーブル直後（空行なし）に ATX heading WHEN: iter_markdown_blocks
        THEN: heading は table に含まれず BLOCK_KIND_HUMAN_PROSE として yield される"""
        text = (
            "| AC | Status |\n"
            "|---|---|\n"
            "| AC1 | pass |\n"
            "## Next Section\n"
        )
        blocks = list(iter_markdown_blocks(text))
        kinds = [kind for _, kind in blocks]
        assert BLOCK_KIND_TABLE in kinds
        prose_blocks = [(t, k) for t, k in blocks if k == BLOCK_KIND_HUMAN_PROSE]
        heading_in_prose = any("## Next Section" in t for t, _ in prose_blocks)
        assert heading_in_prose, "ATX heading が prose ブロックに含まれていない"

    def test_data_row_cell_count_mismatch_allowed_in_table(self):
        """GIVEN: data row のセル数が header と異なるテーブル WHEN: iter_markdown_blocks
        THEN: GFM 仕様でセル数不足・過剰は許容（data row は table に含まれる）"""
        # GFM spec: data rows may have fewer or more cells than the header
        text = (
            "| AC | Claim | Status |\n"
            "|---|---|---|\n"
            "| AC1 | only two cells |\n"          # セル数不足（2 < 3）
            "| AC2 | extra | extra | extra |\n"   # セル数過剰（4 > 3）
        )
        blocks = list(iter_markdown_blocks(text))
        kinds = [kind for _, kind in blocks]
        # data rows はセル数不一致でもテーブルの継続行として扱う
        assert BLOCK_KIND_TABLE in kinds
        # テーブルブロックに data rows が含まれること
        table_blocks = [(t, k) for t, k in blocks if k == BLOCK_KIND_TABLE]
        table_text = ''.join(t for t, _ in table_blocks)
        assert "AC1" in table_text
        assert "AC2" in table_text

    def test_fenced_code_inside_code_fence_not_table(self):
        """GIVEN: fenced code block 内のテーブル風コンテンツ WHEN: iter_markdown_blocks
        THEN: code_fence として扱われ BLOCK_KIND_TABLE にならない（既存挙動の確認）"""
        text = (
            "```\n"
            "| col1 | col2 |\n"
            "|---|---|\n"
            "| a | b |\n"
            "```\n"
        )
        blocks = list(iter_markdown_blocks(text))
        kinds = [kind for _, kind in blocks]
        assert BLOCK_KIND_CODE_FENCE in kinds
        assert BLOCK_KIND_TABLE not in kinds

    def test_escaped_pipe_in_table_detected(self):
        """GIVEN: escaped pipe（\\|）を含むテーブル WHEN: iter_markdown_blocks
        THEN: escaped pipe はセル区切りとして扱われず、テーブルとして正しく検出される"""
        # escaped pipe はセル区切りでないので cell 数の計算に影響しない
        text = (
            "| Header\\|A | Header B |\n"
            "|---|---|\n"
            "| cell \\| data | val |\n"
        )
        blocks = list(iter_markdown_blocks(text))
        kinds = [kind for _, kind in blocks]
        # escaped pipe を含んでいてもテーブルとして認識される
        assert BLOCK_KIND_TABLE in kinds
