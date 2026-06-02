"""
test_validate_japanese_content.py

validate_japanese_content.py の pytest テスト

AC1: スクリプトが存在し、テストが全件 PASS する
AC2: exit 0 = pass / exit 1 = fail を返し、code fence / inline code / URL / CLI コマンドを除外
AC4: prose block 単位で検査し、英語本文 + 日本語 heading のみで FAIL する
AC5: --threshold オプションでデフォルト 0.1
"""

import subprocess
import sys
from pathlib import Path

import pytest

# テスト対象スクリプトのパス
SCRIPT_PATH = Path(__file__).parent.parent / "validate_japanese_content.py"

# validate_japanese_content モジュールを直接インポート
sys.path.insert(0, str(SCRIPT_PATH.parent))
from validate_japanese_content import (
    clean_prose,
    count_japanese_chars,
    extract_code_fences,
    split_into_prose_blocks,
    validate_text,
)


# ============================================================
# Unit tests: extract_code_fences (code_fence 除去)
# ============================================================


class TestExtractCodeFences:
    """AC2: code fence 内コードを除外することを検証"""

    def test_code_fence_is_removed(self):
        """GIVEN: トリプルバッククォートのコードフェンス WHEN: extract THEN: フェンス内容が除去される"""
        text = "前の文章\n```python\nprint('hello')\n```\n後の文章"
        result, removed = extract_code_fences(text)
        assert "print('hello')" not in result
        assert len(removed) == 1

    def test_tilde_code_fence_is_removed(self):
        """GIVEN: チルダフェンス WHEN: extract THEN: フェンス内容が除去される"""
        text = "前の文章\n~~~bash\necho hello\n~~~\n後の文章"
        result, removed = extract_code_fences(text)
        assert "echo hello" not in result
        assert len(removed) == 1

    def test_plain_text_is_preserved(self):
        """GIVEN: コードフェンスなしテキスト WHEN: extract THEN: テキストが保持される"""
        text = "これはプレーンテキストです。"
        result, removed = extract_code_fences(text)
        assert "これはプレーンテキストです" in result
        assert len(removed) == 0

    def test_multiple_code_fences_removed(self):
        """GIVEN: 複数のコードフェンス WHEN: extract THEN: 全て除去される"""
        text = "テキスト1\n```\ncode1\n```\nテキスト2\n```\ncode2\n```\nテキスト3"
        result, removed = extract_code_fences(text)
        assert "code1" not in result
        assert "code2" not in result
        assert len(removed) == 2


# ============================================================
# Unit tests: clean_prose (inline_code / URL 除去)
# ============================================================


class TestCleanProse:
    """AC2: inline code / URL / CLI コマンドを除外することを検証"""

    def test_inline_code_is_removed(self):
        """GIVEN: バッククォートのインラインコード WHEN: clean THEN: 除去される"""
        text = "これは `git commit` を実行するコマンドです。"
        result = clean_prose(text)
        assert "`git commit`" not in result
        assert "git commit" not in result

    def test_url_is_removed(self):
        """GIVEN: https URL WHEN: clean THEN: URL が除去される"""
        text = "詳細は https://example.com/docs を参照してください。"
        result = clean_prose(text)
        assert "https://example.com/docs" not in result

    def test_cli_line_is_removed(self):
        """GIVEN: $ プレフィックスの CLI 行 WHEN: clean THEN: 除去される"""
        text = "以下のコマンドを実行します:\n$ pnpm test\n終了です。"
        result = clean_prose(text)
        assert "pnpm test" not in result

    def test_markdown_link_text_preserved_url_removed(self):
        """GIVEN: Markdown リンク WHEN: clean THEN: テキスト部分が残り URL が除去される"""
        text = "詳細は [ドキュメント](https://example.com/doc) を参照。"
        result = clean_prose(text)
        assert "ドキュメント" in result
        assert "https://example.com/doc" not in result

    def test_japanese_text_preserved(self):
        """GIVEN: 日本語テキスト WHEN: clean THEN: 日本語が保持される"""
        text = "これは日本語のテキストです。"
        result = clean_prose(text)
        assert "日本語" in result


# ============================================================
# Unit tests: split_into_prose_blocks (prose_block 分割)
# ============================================================


class TestSplitIntoProse_blocks:
    """prose_block 単位での分割を検証"""

    def test_empty_lines_create_blocks(self):
        """GIVEN: 空行区切りのテキスト WHEN: split THEN: 複数ブロックに分割される"""
        text = "ブロック1の内容です。\n\nブロック2の内容です。"
        blocks = split_into_prose_blocks(text)
        assert len(blocks) == 2

    def test_single_block(self):
        """GIVEN: 空行のないテキスト WHEN: split THEN: 1ブロックになる"""
        text = "これは単一のブロックです。"
        blocks = split_into_prose_blocks(text)
        assert len(blocks) == 1

    def test_empty_blocks_filtered(self):
        """GIVEN: 複数の空行 WHEN: split THEN: 空ブロックが除去される"""
        text = "ブロック1\n\n\n\nブロック2"
        blocks = split_into_prose_blocks(text)
        assert len(blocks) == 2


# ============================================================
# Integration tests: validate_text
# ============================================================


class TestValidateText:
    """AC2/AC4/AC5: validate_text 関数の統合テスト"""

    def test_japanese_text_passes(self):
        """GIVEN: 日本語の多い prose WHEN: validate THEN: pass する"""
        text = "これは日本語で書かれたドキュメントです。実装の概要を説明します。"
        result = validate_text(text)
        assert result.passed is True

    def test_english_only_text_fails(self):
        """GIVEN: 英語のみの prose WHEN: validate THEN: fail する (AC4)"""
        text = "This is a document written in English. It describes the implementation."
        result = validate_text(text)
        assert result.passed is False

    def test_code_fence_excluded_from_ratio(self):
        """GIVEN: コードフェンス内に英語コードがある WHEN: validate THEN: コードを除外して日本語比率計算 (AC2)"""
        text = "これは日本語の説明です。\n\n```python\nimport sys\nprint('hello world in english')\n```\n\n以上が実装です。"
        result = validate_text(text)
        # コードフェンスが除外されれば日本語比率は高いはず
        assert result.passed is True

    def test_inline_code_excluded(self):
        """GIVEN: インラインコードを含む prose WHEN: validate THEN: インラインコードを除外 (AC2)"""
        text = "このコマンドは `git commit -m` です。これを実行することで変更をコミットできます。"
        result = validate_text(text)
        # インラインコードが除外されて日本語比率が十分あればpass
        assert result.passed is True

    def test_url_excluded(self):
        """GIVEN: URL を含む prose WHEN: validate THEN: URL を除外 (AC2)"""
        text = "詳細は https://github.com/example/repo を参照してください。日本語での説明はこちらです。"
        result = validate_text(text)
        assert result.passed is True

    def test_prose_block_level_check_english_body_japanese_heading_fails(self):
        """GIVEN: 英語本文 + 日本語 heading の prose WHEN: validate THEN: FAIL する (AC4)"""
        # 日本語 heading だけあっても、英語本文の prose block が日本語比率不足でFAILする
        text = (
            "## 概要\n\n"
            "This is the overview section written entirely in English. "
            "The implementation details are described below.\n\n"
            "## 詳細\n\n"
            "This section also contains only English text. "
            "No Japanese characters appear in the main body."
        )
        result = validate_text(text)
        assert result.passed is False
        assert len(result.failed_blocks) > 0

    def test_default_threshold_is_0_1(self):
        """GIVEN: threshold 未指定 WHEN: validate THEN: デフォルト閾値が 0.1 (AC5)"""
        text = "テスト"
        result = validate_text(text)
        assert result.threshold == 0.1

    def test_custom_threshold_respected(self):
        """GIVEN: カスタム threshold WHEN: validate THEN: 指定した閾値を使用 (AC5)"""
        # 日本語比率が少し低いテキスト
        text = "AB テスト CD"
        result_low = validate_text(text, threshold=0.01)
        result_high = validate_text(text, threshold=0.9)
        # 低閾値ならpass、高閾値ならfail
        assert result_low.passed is True
        assert result_high.passed is False

    def test_aggregate_ratio_calculated(self):
        """GIVEN: 日本語テキスト WHEN: validate THEN: aggregate_ratio が計算される (AC2)"""
        text = "日本語テスト"
        result = validate_text(text)
        assert result.aggregate_ratio > 0.0

    def test_empty_text_fails(self):
        """GIVEN: 空テキスト WHEN: validate THEN: fail する"""
        result = validate_text("")
        assert result.passed is False

    def test_prose_blocks_returned(self):
        """GIVEN: 複数の段落 WHEN: validate THEN: prose_blocks リストが返る (AC2)"""
        text = "最初の段落です。日本語で書かれています。\n\n二番目の段落です。こちらも日本語です。"
        result = validate_text(text)
        assert len(result.prose_blocks) >= 1


# ============================================================
# CLI integration tests: exit codes
# ============================================================


class TestCLIExitCodes:
    """AC2: exit 0 = pass / exit 1 = fail の CLI 動作を検証"""

    def run_validator(self, text: str, args: list = None) -> int:
        """バリデーターを subprocess で実行して exit code を返す"""
        cmd = [sys.executable, str(SCRIPT_PATH)]
        if args:
            cmd.extend(args)
        proc = subprocess.run(
            cmd,
            input=text,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
        return proc.returncode

    def test_japanese_text_exit_0(self):
        """GIVEN: 日本語 prose WHEN: CLI 実行 THEN: exit 0 (AC2)"""
        text = "これは日本語で書かれたドキュメントです。実装の概要を説明します。"
        assert self.run_validator(text) == 0

    def test_english_text_exit_1(self):
        """GIVEN: 英語 prose WHEN: CLI 実行 THEN: exit 1 (AC2)"""
        text = "This is a document written in English only. No Japanese content here."
        assert self.run_validator(text) == 1

    def test_custom_threshold_option(self):
        """GIVEN: --threshold オプション WHEN: CLI 実行 THEN: 指定した閾値を使用 (AC5)"""
        # 少しだけ日本語を含むテキスト
        text = "AB テスト CD EF GH"
        # 非常に低い閾値 → pass
        assert self.run_validator(text, ['--threshold', '0.01']) == 0
        # 非常に高い閾値 → fail
        assert self.run_validator(text, ['--threshold', '0.99']) == 1

    def test_default_threshold_is_0_1_via_cli(self):
        """GIVEN: threshold 未指定 WHEN: CLI 実行 THEN: デフォルト 0.1 が適用される (AC5)"""
        # 日本語比率がちょうど 0.1 未満のテキスト
        # 英語のみ → 比率 0.0 < 0.1 → fail
        text = "This text contains no Japanese characters at all and should fail validation."
        # デフォルト 0.1 → fail
        assert self.run_validator(text) == 1
        # 日本語の多いテキスト → デフォルト 0.1 → pass
        text_jp = "これは日本語で書かれたドキュメントです。実装の概要を説明します。"
        assert self.run_validator(text_jp) == 0
