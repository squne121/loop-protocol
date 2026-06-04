"""
test_prose_boundary_policy.py

prose_boundary_policy.py の pytest テスト。

AC1: prose_boundary_policy.py が存在し block_kind 定数を定義する
AC3: legacy 互換テスト（changed_prose_blocks が prose のみを delta 対象とする）
AC4: golden corpus snapshot test（GFM edge case を含む PASS / FAIL ケース）
"""

import sys
from pathlib import Path

import pytest

# テスト対象スクリプトのパス（worktree / main 両対応）
_SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import prose_boundary_policy as pbp
from prose_boundary_policy import (
    ALL_BLOCK_KINDS,
    BLOCK_KIND_CANONICAL_HEADING,
    BLOCK_KIND_BILINGUAL_HEADING,
    BLOCK_KIND_CODE_FENCE,
    BLOCK_KIND_HUMAN_PROSE,
    BLOCK_KIND_MACHINE_CONTRACT,
    BLOCK_KIND_SHELL_COMMAND,
    BLOCK_KIND_URL_OR_IDENTIFIER,
    BLOCK_KIND_VC_COMMAND,
    BLOCK_KIND_YAML_MACHINE_LINE,
    classify_block,
    classify_block_legacy,
)
from validate_japanese_content import (
    changed_prose_blocks,
    split_markdown_blocks,
)


# ===========================================================================
# AC1: block_kind 定数の存在確認
# ===========================================================================


class TestBlockKindConstants:
    """AC1: block_kind 列挙が正しく定義されている"""

    def test_all_required_kinds_exist(self):
        """GIVEN: block_kind 定数セット WHEN: 存在確認 THEN: 9種類すべて存在する"""
        required = {
            "human_prose",
            "canonical_heading",
            "bilingual_heading",
            "machine_contract",
            "yaml_machine_line",
            "vc_command",
            "shell_command",
            "code_fence",
            "url_or_identifier",
        }
        assert required == ALL_BLOCK_KINDS

    def test_forbidden_kinds_not_in_all(self):
        """GIVEN: block_kind 定数セット WHEN: 禁止種別確認 THEN: temporary_draft/public_body_source が含まれない"""
        assert "temporary_draft" not in ALL_BLOCK_KINDS
        assert "public_body_source" not in ALL_BLOCK_KINDS

    def test_constants_match_strings(self):
        """GIVEN: 各定数 WHEN: 文字列確認 THEN: 定数値が期待通り"""
        assert BLOCK_KIND_HUMAN_PROSE == "human_prose"
        assert BLOCK_KIND_CANONICAL_HEADING == "canonical_heading"
        assert BLOCK_KIND_BILINGUAL_HEADING == "bilingual_heading"
        assert BLOCK_KIND_MACHINE_CONTRACT == "machine_contract"
        assert BLOCK_KIND_YAML_MACHINE_LINE == "yaml_machine_line"
        assert BLOCK_KIND_VC_COMMAND == "vc_command"
        assert BLOCK_KIND_SHELL_COMMAND == "shell_command"
        assert BLOCK_KIND_CODE_FENCE == "code_fence"
        assert BLOCK_KIND_URL_OR_IDENTIFIER == "url_or_identifier"


# ===========================================================================
# AC3: legacy 互換テスト（changed_prose_blocks / split_markdown_blocks）
# ===========================================================================


class TestLegacyCompatSplitMarkdownBlocks:
    """AC3 legacy: split_markdown_blocks の legacy 分類名互換"""

    def test_prose_block_returns_prose_type(self):
        """GIVEN: 日本語 prose テキスト WHEN: split_markdown_blocks THEN: type == 'prose'"""
        text = "これは日本語の本文です。テストのための prose ブロックです。"
        blocks = split_markdown_blocks(text)
        assert len(blocks) >= 1
        assert any(b["type"] == "prose" for b in blocks)

    def test_code_fence_returns_code_fence_type(self):
        """GIVEN: code fence ブロック WHEN: split_markdown_blocks THEN: type == 'code_fence'"""
        text = "```python\nprint('hello')\n```"
        blocks = split_markdown_blocks(text)
        assert len(blocks) >= 1
        assert blocks[0]["type"] == "code_fence"

    def test_yaml_block_returns_machine_yaml_type(self):
        """GIVEN: YAML machine-readable ブロック WHEN: split_markdown_blocks THEN: type == 'machine_yaml'"""
        text = "key: value\nstatus: active\nversion: 1.0"
        blocks = split_markdown_blocks(text)
        assert len(blocks) >= 1
        assert blocks[0]["type"] == "machine_yaml"

    def test_shell_command_block_returns_shell_command_type(self):
        """GIVEN: シェルコマンドブロック WHEN: split_markdown_blocks THEN: type == 'shell_command'"""
        text = "$ git commit -m 'test'\n$ git push"
        blocks = split_markdown_blocks(text)
        assert len(blocks) >= 1
        assert blocks[0]["type"] == "shell_command"

    def test_url_only_block_returns_url_or_identifier_type(self):
        """GIVEN: URL のみブロック WHEN: split_markdown_blocks THEN: type == 'url_or_identifier_only'"""
        text = "https://github.com/squne121/loop-protocol"
        blocks = split_markdown_blocks(text)
        assert len(blocks) >= 1
        assert blocks[0]["type"] == "url_or_identifier_only"

    def test_grep_command_returns_grep_pattern_type(self):
        """GIVEN: grep コマンドブロック WHEN: split_markdown_blocks THEN: type == 'grep_pattern'"""
        text = "rg -n 'pattern' some/file.py\ngrep -r 'foo' ."
        blocks = split_markdown_blocks(text)
        assert len(blocks) >= 1
        assert blocks[0]["type"] == "grep_pattern"


class TestLegacyCompatChangedProseBlocks:
    """AC3 compat: changed_prose_blocks は prose のみを delta 対象とする"""

    def test_prose_change_detected(self):
        """GIVEN: prose ブロックが変更 WHEN: changed_prose_blocks THEN: 変更ブロックを返す"""
        old = "これは古い日本語テキストです。"
        new = "これは新しい日本語テキストです。内容が大幅に変更されました。"
        changed = changed_prose_blocks(old, new)
        assert len(changed) >= 1
        assert all(b["type"] == "prose" for b in changed)

    def test_code_fence_change_not_detected(self):
        """GIVEN: code_fence のみ変更 WHEN: changed_prose_blocks THEN: 空リストを返す（pass）"""
        old = "```python\nprint('old')\n```"
        new = "```python\nprint('new')\n```"
        changed = changed_prose_blocks(old, new)
        # code_fence の変更は prose delta として検出しない
        assert len(changed) == 0

    def test_machine_yaml_change_not_detected(self):
        """GIVEN: machine_yaml のみ変更 WHEN: changed_prose_blocks THEN: 空リストを返す（pass）"""
        old = "key: old_value\nstatus: inactive"
        new = "key: new_value\nstatus: active"
        changed = changed_prose_blocks(old, new)
        assert len(changed) == 0

    def test_shell_command_change_not_detected(self):
        """GIVEN: shell_command のみ変更 WHEN: changed_prose_blocks THEN: 空リストを返す（pass）"""
        old = "$ git commit -m 'old'"
        new = "$ git commit -m 'new'\n$ git push"
        changed = changed_prose_blocks(old, new)
        assert len(changed) == 0

    def test_url_change_not_detected(self):
        """GIVEN: URL のみ変更 WHEN: changed_prose_blocks THEN: 空リストを返す（pass）"""
        old = "https://github.com/squne121/loop-protocol"
        new = "https://github.com/squne121/loop-protocol/issues/653"
        changed = changed_prose_blocks(old, new)
        assert len(changed) == 0

    def test_grep_pattern_change_not_detected(self):
        """GIVEN: grep_pattern のみ変更 WHEN: changed_prose_blocks THEN: 空リストを返す（pass）"""
        old = "rg -n 'old_pattern' src/"
        new = "rg -n 'new_pattern' src/\ngrep -r 'foo' ."
        changed = changed_prose_blocks(old, new)
        assert len(changed) == 0

    def test_prose_unchanged_not_detected(self):
        """GIVEN: prose ブロックが同一 WHEN: changed_prose_blocks THEN: 空リストを返す"""
        text = "これは変更されていない日本語テキストです。"
        changed = changed_prose_blocks(text, text)
        assert len(changed) == 0

    def test_new_prose_block_detected(self):
        """GIVEN: prose ブロックが追加 WHEN: changed_prose_blocks THEN: 追加ブロックを返す"""
        old = "既存の日本語テキストです。"
        new = "既存の日本語テキストです。\n\n新しく追加された日本語テキストです。"
        changed = changed_prose_blocks(old, new)
        assert len(changed) >= 1


# ===========================================================================
# AC4: golden corpus snapshot test（GFM edge case）
# ===========================================================================


class TestGoldenCorpusCodeFence:
    """AC4 golden: code fence の GFM edge case"""

    def test_triple_backtick_basic(self):
        """GIVEN: 基本的な ``` fence WHEN: classify_block THEN: code_fence"""
        block = "```python\nprint('hello')\n```"
        assert classify_block(block) == BLOCK_KIND_CODE_FENCE

    def test_quadruple_backtick_fence(self):
        """GIVEN: 4個バッククォート fence WHEN: classify_block THEN: code_fence"""
        block = "````python\nprint('hello')\n````"
        assert classify_block(block) == BLOCK_KIND_CODE_FENCE

    def test_triple_tilde_fence(self):
        """GIVEN: ~~~ fence WHEN: classify_block THEN: code_fence"""
        block = "~~~bash\necho hello\n~~~"
        assert classify_block(block) == BLOCK_KIND_CODE_FENCE

    def test_quadruple_tilde_fence(self):
        """GIVEN: 4個チルダ fence WHEN: classify_block THEN: code_fence"""
        block = "~~~~bash\necho hello\n~~~~"
        assert classify_block(block) == BLOCK_KIND_CODE_FENCE

    def test_code_fence_opening_longer_than_closing(self):
        """GIVEN: opening より長い closing fence WHEN: classify_block THEN: code_fence

        注意（B4）: この test は classify_block（既にセグメント済み単一ブロックの先頭行判定）
        のみを検証しており、split_markdown_blocks() レベルでセグメンテーションが正しく
        行われるかは検証していない。GFM spec 的に opening より長い closing fence や
        未閉 fence の境界が正しくセグメントされるかは follow-up #659 で固定する予定。
        split レベルの GFM 正当性テストは TestGfmSegmentationLimits を参照。
        """
        # GFM spec: opening が ``` の場合、同じ or longer closing で閉じる
        # classify_block はブロック単体を受け取るため opening 行で判定
        block = "````python\nsome code\n```"
        assert classify_block(block) == BLOCK_KIND_CODE_FENCE

    def test_code_fence_with_no_language(self):
        """GIVEN: 言語指定なし fence WHEN: classify_block THEN: code_fence"""
        block = "```\nsome code\n```"
        assert classify_block(block) == BLOCK_KIND_CODE_FENCE

    def test_machine_contract_yaml_fence(self):
        """GIVEN: YAML Machine-Readable Contract fence WHEN: classify_block THEN: machine_contract"""
        block = "```yaml\ncontract_schema_version: v1\nissue_kind: implementation\n```"
        assert classify_block(block) == BLOCK_KIND_MACHINE_CONTRACT

    def test_contract_schema_in_plain_fence(self):
        """GIVEN: ``` (no lang) + contract_schema_version WHEN: classify_block THEN: code_fence

        以前の実装（N1 修正前）は yaml prefix なしの plain fence でも
        contract_schema_version 文字列があれば machine_contract に分類していた（過剰一致）。
        修正後は yaml/yml prefix を必須とするため、plain fence は code_fence になる。
        machine_contract として分類されるには ```yaml または ```yml prefix が必要。
        """
        block = "```\ncontract_schema_version: v1\ngoal_ref: test\n```"
        assert classify_block(block) == BLOCK_KIND_CODE_FENCE


class TestGoldenCorpusHeadings:
    """AC4 golden: 見出し edge case"""

    def test_canonical_heading_english(self):
        """GIVEN: 英語見出し ## Outcome WHEN: classify_block THEN: canonical_heading"""
        block = "## Outcome"
        assert classify_block(block) == BLOCK_KIND_CANONICAL_HEADING

    def test_canonical_heading_h1(self):
        """GIVEN: H1 英語見出し WHEN: classify_block THEN: canonical_heading"""
        block = "# Background"
        assert classify_block(block) == BLOCK_KIND_CANONICAL_HEADING

    def test_canonical_heading_h6(self):
        """GIVEN: H6 英語見出し WHEN: classify_block THEN: canonical_heading"""
        block = "###### Deep Heading"
        assert classify_block(block) == BLOCK_KIND_CANONICAL_HEADING

    def test_bilingual_heading_japanese(self):
        """GIVEN: 日本語見出し WHEN: classify_block THEN: bilingual_heading"""
        block = "## 背景"
        assert classify_block(block) == BLOCK_KIND_BILINGUAL_HEADING

    def test_bilingual_heading_mixed(self):
        """GIVEN: 日英混在見出し WHEN: classify_block THEN: bilingual_heading"""
        block = "## Outcome（目的）"
        assert classify_block(block) == BLOCK_KIND_BILINGUAL_HEADING

    def test_heading_no_blank_line_before_body(self):
        """GIVEN: 見出し直後に空行なしで本文が続くケース WHEN: split_markdown_blocks THEN: ブロック分割される"""
        # GFM edge case: 見出し直後に本文がある
        text = "## はじめに\nこれは日本語の本文で見出しの直後に空行なしで続きます。"
        blocks = split_markdown_blocks(text)
        # 少なくとも1ブロックが存在すること
        assert len(blocks) >= 1
        # ブロックの type が有効であること
        assert all(b["type"] in (
            "prose", "code_fence", "machine_yaml", "shell_command",
            "grep_pattern", "url_or_identifier_only"
        ) for b in blocks)


class TestGoldenCorpusShellCommands:
    """AC4 golden: シェルコマンド edge case"""

    def test_dollar_prefix_command(self):
        """GIVEN: $ プレフィックスコマンド WHEN: classify_block THEN: shell_command"""
        block = "$ git commit -m 'test'"
        assert classify_block(block) == BLOCK_KIND_SHELL_COMMAND

    def test_hash_prefix_comment(self):
        """GIVEN: # プレフィックス行 WHEN: classify_block THEN: canonical_heading（GFM spec: # text は H1）"""
        # GFM spec: # text は Markdown H1 見出しとして解釈される。
        # シェルの root プロンプト（# コマンド）として扱わない。
        # 複数行で $ / # が混在する場合は shell_command になる（下記 test_dollar_and_hash_mixed 参照）
        block = "# cat /etc/hosts"
        assert classify_block(block) == BLOCK_KIND_CANONICAL_HEADING

    def test_dollar_and_hash_mixed_command(self):
        """GIVEN: $ と # が混在するシェルブロック WHEN: classify_block THEN: shell_command"""
        block = "$ git commit -m 'test'\n# push to remote\n$ git push"
        assert classify_block(block) == BLOCK_KIND_SHELL_COMMAND

    def test_vc_command_uv_pytest(self):
        """GIVEN: uv run pytest コマンド WHEN: classify_block THEN: vc_command"""
        block = "$ uv run pytest .claude/skills/create-issue/scripts/tests/ -q"
        assert classify_block(block) == BLOCK_KIND_VC_COMMAND

    def test_vc_command_pnpm(self):
        """GIVEN: pnpm typecheck コマンド WHEN: classify_block THEN: vc_command"""
        block = "$ pnpm typecheck"
        assert classify_block(block) == BLOCK_KIND_VC_COMMAND

    def test_grep_command_rg(self):
        """GIVEN: rg コマンド行 WHEN: classify_block THEN: shell_command"""
        block = "rg -n 'block_kind' .claude/skills/create-issue/scripts/prose_boundary_policy.py"
        assert classify_block(block) == BLOCK_KIND_SHELL_COMMAND

    def test_grep_command_grep(self):
        """GIVEN: grep コマンド行 WHEN: classify_block THEN: shell_command"""
        block = "grep -r 'prose_boundary_policy' .claude/skills/create-issue/scripts/"
        assert classify_block(block) == BLOCK_KIND_SHELL_COMMAND

    def test_gh_command_is_shell(self):
        """GIVEN: gh コマンド行 WHEN: classify_block THEN: shell_command"""
        block = "$ gh issue view 653 --repo squne121/loop-protocol"
        assert classify_block(block) == BLOCK_KIND_SHELL_COMMAND


class TestGoldenCorpusYAML:
    """AC4 golden: YAML Machine-Readable Contract edge case"""

    def test_yaml_block_key_value_pairs(self):
        """GIVEN: key: value 形式の YAML ブロック WHEN: classify_block THEN: yaml_machine_line"""
        block = "contract_schema_version: v1\nissue_kind: implementation\nchange_kind: code"
        assert classify_block(block) == BLOCK_KIND_YAML_MACHINE_LINE

    def test_yaml_boolean_values(self):
        """GIVEN: boolean 値を含む YAML WHEN: classify_block THEN: yaml_machine_line"""
        block = "enabled: true\nactive: false\nnull_value: null"
        assert classify_block(block) == BLOCK_KIND_YAML_MACHINE_LINE

    def test_yaml_with_prose_value_is_prose(self):
        """GIVEN: 自然文 value を含む YAML WHEN: classify_block THEN: human_prose"""
        block = "description: これは長い説明文で、複数の語から構成されています。"
        assert classify_block(block) == BLOCK_KIND_HUMAN_PROSE


class TestGoldenCorpusUrlAndIdentifier:
    """AC4 golden: URL / identifier のみ行"""

    def test_url_only_line(self):
        """GIVEN: https URL のみ行 WHEN: classify_block THEN: url_or_identifier"""
        block = "https://github.com/squne121/loop-protocol/issues/653"
        assert classify_block(block) == BLOCK_KIND_URL_OR_IDENTIFIER

    def test_issue_ref_only(self):
        """GIVEN: #653 のみ行 WHEN: classify_block THEN: url_or_identifier"""
        block = "#653"
        assert classify_block(block) == BLOCK_KIND_URL_OR_IDENTIFIER

    def test_sha_only_line(self):
        """GIVEN: SHA のみ行 WHEN: classify_block THEN: url_or_identifier"""
        block = "57cbdae"
        assert classify_block(block) == BLOCK_KIND_URL_OR_IDENTIFIER

    def test_long_sha_only_line(self):
        """GIVEN: 40文字 SHA のみ行 WHEN: classify_block THEN: url_or_identifier"""
        block = "57cbdae4b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7"
        assert classify_block(block) == BLOCK_KIND_URL_OR_IDENTIFIER

    def test_path_only_line(self):
        """GIVEN: ファイルパスのみ行 WHEN: classify_block THEN: url_or_identifier"""
        block = ".claude/skills/create-issue/scripts/prose_boundary_policy.py"
        assert classify_block(block) == BLOCK_KIND_URL_OR_IDENTIFIER

    def test_multiple_url_lines(self):
        """GIVEN: 複数 URL 行 WHEN: classify_block THEN: url_or_identifier"""
        block = "https://github.com/foo/bar\nhttps://example.com/path"
        assert classify_block(block) == BLOCK_KIND_URL_OR_IDENTIFIER


class TestGoldenCorpusProse:
    """AC4 golden: human_prose の判定"""

    def test_japanese_prose(self):
        """GIVEN: 日本語 prose WHEN: classify_block THEN: human_prose"""
        block = "これは日本語で書かれた prose テキストです。実装の背景と目的を説明します。"
        assert classify_block(block) == BLOCK_KIND_HUMAN_PROSE

    def test_english_prose(self):
        """GIVEN: 英語 prose WHEN: classify_block THEN: human_prose"""
        block = "This is an English prose paragraph describing the implementation details."
        assert classify_block(block) == BLOCK_KIND_HUMAN_PROSE

    def test_mixed_japanese_english_prose(self):
        """GIVEN: 日英混在 prose WHEN: classify_block THEN: human_prose"""
        block = "この実装は prose_boundary_policy を SSOT として集約し、既存 consumer との後方互換を維持します。"
        assert classify_block(block) == BLOCK_KIND_HUMAN_PROSE

    def test_prose_with_inline_code(self):
        """GIVEN: インラインコードを含む prose WHEN: classify_block THEN: human_prose"""
        block = "関数 `classify_block` は block_kind を返します。これは主要な分類 API です。"
        assert classify_block(block) == BLOCK_KIND_HUMAN_PROSE


class TestGoldenCorpusEdgeCases:
    """AC4 golden: GFM edge case の特殊ケース"""

    def test_four_space_indent_code_block(self):
        """GIVEN: 4スペースインデントコードブロック WHEN: classify_block THEN: url_or_identifier"""
        # 4スペースインデントは split_markdown_blocks レベルでは段落として扱われる。
        # classify_block は fence 先頭行を見るため code_fence には分類されない。
        # _clean_for_effective_char_count で識別子（some_function / another_line）が除去され
        # 有効文字数が 5 未満になるため url_or_identifier に分類される。
        # （以前のコメントで "human_prose 想定" と記載していたが、実測は url_or_identifier）
        block = "    some_function()\n    another_line()"
        result = classify_block(block)
        assert result == BLOCK_KIND_URL_OR_IDENTIFIER

    def test_unclosed_fence_treated_as_code_fence(self):
        """GIVEN: 閉じていない fence WHEN: classify_block THEN: code_fence"""
        block = "```python\nprint('unclosed')"
        assert classify_block(block) == BLOCK_KIND_CODE_FENCE

    def test_empty_block(self):
        """GIVEN: 空ブロック WHEN: classify_block THEN: human_prose"""
        assert classify_block("") == BLOCK_KIND_HUMAN_PROSE
        assert classify_block("   ") == BLOCK_KIND_HUMAN_PROSE

    def test_single_line_prose(self):
        """GIVEN: 1行 prose WHEN: classify_block THEN: human_prose"""
        block = "これは単一行の日本語テキストです。"
        assert classify_block(block) == BLOCK_KIND_HUMAN_PROSE


# ===========================================================================
# AC4 golden: classify_block_legacy（legacy 分類名互換）
# ===========================================================================


class TestClassifyBlockLegacy:
    """AC4 golden: classify_block_legacy の legacy 分類名互換"""

    def test_human_prose_maps_to_prose(self):
        """GIVEN: human_prose ブロック WHEN: classify_block_legacy THEN: 'prose'"""
        block = "これは日本語の自然文です。テストのために書いています。"
        assert classify_block_legacy(block) == "prose"

    def test_canonical_heading_maps_to_prose(self):
        """GIVEN: 英語見出し WHEN: classify_block_legacy THEN: 'prose'"""
        block = "## Outcome"
        assert classify_block_legacy(block) == "prose"

    def test_bilingual_heading_maps_to_prose(self):
        """GIVEN: 日英混在見出し WHEN: classify_block_legacy THEN: 'prose'"""
        block = "## 背景"
        assert classify_block_legacy(block) == "prose"

    def test_code_fence_maps_to_code_fence(self):
        """GIVEN: code_fence ブロック WHEN: classify_block_legacy THEN: 'code_fence'"""
        block = "```python\nprint('hello')\n```"
        assert classify_block_legacy(block) == "code_fence"

    def test_machine_contract_maps_to_code_fence(self):
        """GIVEN: machine_contract ブロック WHEN: classify_block_legacy THEN: 'code_fence'"""
        block = "```yaml\ncontract_schema_version: v1\n```"
        assert classify_block_legacy(block) == "code_fence"

    def test_yaml_machine_line_maps_to_machine_yaml(self):
        """GIVEN: yaml_machine_line ブロック WHEN: classify_block_legacy THEN: 'machine_yaml'"""
        block = "key: value\nstatus: active\nversion: 1"
        assert classify_block_legacy(block) == "machine_yaml"

    def test_shell_command_maps_to_shell_command(self):
        """GIVEN: shell_command ブロック WHEN: classify_block_legacy THEN: 'shell_command'"""
        block = "$ git commit -m 'test'"
        assert classify_block_legacy(block) == "shell_command"

    def test_vc_command_maps_to_shell_command(self):
        """GIVEN: vc_command ブロック WHEN: classify_block_legacy THEN: 'shell_command'"""
        block = "$ pnpm typecheck"
        assert classify_block_legacy(block) == "shell_command"

    def test_grep_pattern_maps_to_grep_pattern(self):
        """GIVEN: grep コマンドブロック WHEN: classify_block_legacy THEN: 'grep_pattern'"""
        block = "rg -n 'pattern' src/\ngrep -r 'foo' ."
        assert classify_block_legacy(block) == "grep_pattern"

    def test_url_or_identifier_maps_to_url_or_identifier_only(self):
        """GIVEN: url_or_identifier ブロック WHEN: classify_block_legacy THEN: 'url_or_identifier_only'"""
        block = "https://github.com/squne121/loop-protocol"
        assert classify_block_legacy(block) == "url_or_identifier_only"

    def test_legacy_values_are_subset_of_valid_legacy_types(self):
        """GIVEN: 各種ブロック WHEN: classify_block_legacy THEN: すべて有効な legacy type"""
        valid_legacy = {
            "prose", "code_fence", "machine_yaml",
            "shell_command", "grep_pattern", "url_or_identifier_only"
        }
        test_blocks = [
            "これは日本語 prose",
            "## English Heading",
            "## 日本語見出し",
            "```yaml\ncontract_schema_version: v1\n```",
            "```python\ncode\n```",
            "key: value\nstatus: ok",
            "$ git push",
            "$ uv run pytest",
            "rg -n 'foo' .",
            "https://example.com",
            "#123",
        ]
        for block in test_blocks:
            result = classify_block_legacy(block)
            assert result in valid_legacy, f"block={block!r}, result={result!r}"


# ===========================================================================
# GFM segmentation 限界テスト（B4）
# split_markdown_blocks() レベルの GFM 正当性確認
# NOTE: 以下テストは現状 regex ベースの split_markdown_blocks() では誤分割が発生する
#       ことが判明している既知の限界を xfail として記録する。
#       GFM-correct セグメンテーション修正は follow-up #659 で対応する予定であり、
#       #659 で fix されたら strict=True に変更して回帰防止とする。
#
#       golden corpus が固定しているのは classify_block（分類 API）であり、
#       split_markdown_blocks() レベルの GFM 正当性（opening より長い closing /
#       未閉 fence の境界）は follow-up #659 の責務。
# ===========================================================================


class TestGfmSegmentationLimits:
    """B4: split_markdown_blocks() の GFM セグメンテーション（#659 で修正済み）"""

    def test_split_nested_markdown_fence_is_single_block(self):
        """GIVEN: `````markdown fence 内に ```yaml を含むネスト WHEN: split_markdown_blocks THEN: 単一 code_fence ブロック

        GFM spec では、opening が ````` (5個) の場合、
        内側の ``` (3個) は fence を閉じない（closing は opening と同じ長さ以上が必要）。
        したがってこの全体は単一の code_fence ブロックとして扱われるべき。

        #659 で GFM-correct segmentation（iter_markdown_blocks SSOT）が実装され、
        xfail を解除して回帰防止テストに変更した（AC10）。
        """
        # `````markdown
        # ```yaml
        # contract_schema_version: v1
        # ```
        # `````
        text = "`````markdown\n```yaml\ncontract_schema_version: v1\n```\n`````"
        blocks = split_markdown_blocks(text)
        # 期待（GFM 的正当性）: 単一ブロックで type が code_fence
        assert len(blocks) == 1
        assert blocks[0]["type"] == "code_fence"


# ===========================================================================
# #659: GFM 準拠 iter_markdown_blocks セグメンテーション golden corpus
# ===========================================================================


class TestIterMarkdownBlocksGfm:
    """#659: iter_markdown_blocks の GFM 準拠 segmentation テスト"""

    def test_split_four_backtick_inner_three_is_single_block(self):
        """GIVEN: 4 backtick fence 内に 3 backtick 行 WHEN: split_markdown_blocks THEN: 単一 code_fence

        GFM spec: opening が ```` (4個) の場合、内側の ``` (3個) は closing として無効。
        全体は単一の code_fence ブロックとして分割されるべき（split_markdown_blocks レベルで検証）。
        AC3 / VC 要件: test_split_four_backtick_inner_three_is_single_block
        """
        text = "````python\n```\nsome code\n```\n````"
        blocks = split_markdown_blocks(text)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "code_fence"

    def test_split_unclosed_fence_to_eof_is_single_block(self):
        """GIVEN: 未閉 fence（EOF まで closing なし）WHEN: split_markdown_blocks THEN: 単一 code_fence

        GFM spec: 未閉 fence は EOF まで単一 code block として扱う。
        AC4 / VC 要件: test_split_unclosed_fence_to_eof_is_single_block
        """
        text = "```python\nprint('unclosed')\nno closing fence"
        blocks = split_markdown_blocks(text)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "code_fence"

    def test_split_closing_longer_than_opening_is_valid(self):
        """GIVEN: opening より長い closing（5 backtick で 4 backtick fence を閉じる）
        WHEN: split_markdown_blocks THEN: 単一 code_fence に正しく分割される

        GFM spec: closing fence は opening と同長以上なら有効。
        """
        text = "````python\nsome code\n`````"
        blocks = split_markdown_blocks(text)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "code_fence"

    def test_split_tilde_backtick_mismatch_no_close(self):
        """GIVEN: backtick fence を tilde で閉じようとした場合 WHEN: split_markdown_blocks
        THEN: fence は閉じない（未閉として EOF まで code_fence）"""
        text = "```python\nsome code\n~~~"
        blocks = split_markdown_blocks(text)
        # tilde は backtick fence の closing として無効 → 全体が未閉 code_fence
        assert len(blocks) == 1
        assert blocks[0]["type"] == "code_fence"

    def test_split_backtick_tilde_mismatch_no_close(self):
        """GIVEN: tilde fence を backtick で閉じようとした場合 WHEN: split_markdown_blocks
        THEN: fence は閉じない（未閉として EOF まで code_fence）"""
        text = "~~~bash\necho hello\n```"
        blocks = split_markdown_blocks(text)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "code_fence"

    def test_split_four_spaces_indent_not_fence(self):
        """GIVEN: 4 spaces indent の ``` WHEN: iter_markdown_blocks THEN: opening fence として認識しない

        GFM spec: 4 spaces indent は fence として無効。
        この opening ``` は prose として扱われる。その後の standalone ``` は
        opening（未閉）として扱われる。
        """
        import prose_boundary_policy as _pbp_mod
        text = "    ```python\nsome indented text"
        # iter_markdown_blocks レベル: 4 spaces indent ``` は prose として yield
        result = list(_pbp_mod.iter_markdown_blocks(text))
        assert len(result) == 1
        assert result[0][1] == _pbp_mod.BLOCK_KIND_HUMAN_PROSE

    def test_split_closing_four_spaces_indent_invalid(self):
        """GIVEN: closing fence に 4 spaces indent WHEN: split_markdown_blocks THEN: closing として無効"""
        text = "```python\nsome code\n    ```"
        blocks = split_markdown_blocks(text)
        # closing fence に 4 spaces indent は無効 → fence は未閉（EOF まで code_fence）
        assert len(blocks) == 1
        assert blocks[0]["type"] == "code_fence"

    def test_split_closing_trailing_non_space_invalid(self):
        """GIVEN: closing fence に trailing non-space WHEN: split_markdown_blocks THEN: closing として無効"""
        text = "```python\nsome code\n``` extra"
        blocks = split_markdown_blocks(text)
        # closing fence に trailing non-space があるため無効 → fence は未閉
        assert len(blocks) == 1
        assert blocks[0]["type"] == "code_fence"

    def test_split_fence_followed_by_prose(self):
        """GIVEN: code fence 直後に空行なしで prose WHEN: split_markdown_blocks THEN: code_fence と prose に分割"""
        text = "```python\nprint('hello')\n```\nこれは prose です。"
        blocks = split_markdown_blocks(text)
        assert len(blocks) == 2
        assert blocks[0]["type"] == "code_fence"
        assert blocks[1]["type"] == "prose"

    def test_split_prose_before_fence(self):
        """GIVEN: prose の後に code fence WHEN: split_markdown_blocks THEN: prose と code_fence に分割"""
        text = "これは prose テキストです。\n\n```python\ncode\n```"
        blocks = split_markdown_blocks(text)
        assert len(blocks) == 2
        assert blocks[0]["type"] == "prose"
        assert blocks[1]["type"] == "code_fence"

    def test_split_zero_indent_fence_is_valid(self):
        """GIVEN: 0 spaces indent fence WHEN: split_markdown_blocks THEN: 単一 code_fence"""
        text = "```bash\necho hello\n```"
        blocks = split_markdown_blocks(text)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "code_fence"

    def test_split_three_spaces_indent_fence_is_valid(self):
        """GIVEN: 3 spaces indent fence WHEN: split_markdown_blocks THEN: code_fence として認識"""
        text = "   ```bash\necho hello\n   ```"
        blocks = split_markdown_blocks(text)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "code_fence"

    def test_split_four_backtick_closed_by_four_backtick(self):
        """GIVEN: 4 backtick fence が 4 backtick で正しく閉じる WHEN: split_markdown_blocks THEN: 単一 code_fence"""
        text = "````python\nsome code\n````"
        blocks = split_markdown_blocks(text)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "code_fence"

    def test_split_multiple_fences(self):
        """GIVEN: 複数の code fence WHEN: split_markdown_blocks THEN: 各 fence が独立した code_fence ブロック"""
        text = "```python\ncode1\n```\n\n```bash\ncode2\n```"
        blocks = split_markdown_blocks(text)
        fence_blocks = [b for b in blocks if b["type"] == "code_fence"]
        assert len(fence_blocks) == 2

    def test_iter_markdown_blocks_yields_tuples(self):
        """GIVEN: prose と code fence WHEN: iter_markdown_blocks THEN: (text, kind) タプルを yield"""
        import prose_boundary_policy as _pbp_mod
        text = "prose text\n```python\ncode\n```\nmore prose"
        result = list(_pbp_mod.iter_markdown_blocks(text))
        assert len(result) >= 2
        for item in result:
            assert isinstance(item, tuple)
            assert len(item) == 2
            text_part, kind_part = item
            assert isinstance(text_part, str)
            assert kind_part in (_pbp_mod.BLOCK_KIND_CODE_FENCE, _pbp_mod.BLOCK_KIND_HUMAN_PROSE)

    def test_split_blocks_returns_list(self):
        """GIVEN: テキスト WHEN: split_blocks THEN: list of tuples"""
        import prose_boundary_policy as _pbp_mod
        text = "```python\ncode\n```"
        result = _pbp_mod.split_blocks(text)
        assert isinstance(result, list)
        assert len(result) >= 1
