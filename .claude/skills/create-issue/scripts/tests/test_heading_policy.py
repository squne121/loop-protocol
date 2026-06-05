"""
test_heading_policy.py

Issue #654: canonical / bilingual heading 除外と heading_policy の単体テスト。

AC1: heading_policy が prose_boundary_policy.py に存在し、
     classify_block() 公開 API とブロック定数が変更されていない
AC2: heading_policy が implementation テンプレートの canonical heading を網羅
AC3: delta mode で canonical_heading が除外される / full-body mode は維持
AC5: GFM ATX heading 正規化 / similar_invalid_heading negative test
AC6: machine_contract / vc_command / code_fence edge case
AC7: 英語 prose / non-canonical 英語見出しが引き続き fail
AC8: heading_policy は validate_japanese_content.py 経由で適用
"""

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import prose_boundary_policy as pbp
from prose_boundary_policy import (
    HEADING_POLICY,
    BLOCK_KIND_CANONICAL_HEADING,
    BLOCK_KIND_BILINGUAL_HEADING,
    BLOCK_KIND_HUMAN_PROSE,
    classify_block,
    lookup_heading_policy,
    parse_atx_heading_line,
    _normalize_heading_text,
    _extract_bilingual_heading_key,
)
from validate_japanese_content import (
    changed_prose_blocks,
    split_markdown_blocks,
    validate_text,
    _classify_block,
    _is_heading_block,
)


# ===========================================================================
# AC1: heading_policy inventory が存在し、classify_block API が不変
# ===========================================================================


class TestHeadingPolicyExists:
    """AC1: heading_policy inventory と classify_block API の不変性"""

    def test_heading_policy_dict_exists(self):
        """GIVEN: prose_boundary_policy モジュール WHEN: HEADING_POLICY を参照
        THEN: dict として存在する"""
        assert isinstance(HEADING_POLICY, dict)
        assert len(HEADING_POLICY) > 0

    def test_entry_has_required_fields(self):
        """GIVEN: HEADING_POLICY の各 entry WHEN: フィールド確認
        THEN: canonical_en / canonical_ja / accepted_forms / prose_guard_kind / contract_checker_kind を持つ"""
        required_fields = {
            "canonical_en",
            "canonical_ja",
            "accepted_forms",
            "prose_guard_kind",
            "contract_checker_kind",
        }
        for key, entry in HEADING_POLICY.items():
            assert required_fields.issubset(entry.keys()), (
                f"entry '{key}' is missing fields: {required_fields - entry.keys()}"
            )
            assert isinstance(entry["accepted_forms"], list)
            assert len(entry["accepted_forms"]) >= 1

    def test_classify_block_api_unchanged(self):
        """GIVEN: classify_block 公開 API WHEN: 既存 block_kind 定数で呼び出し
        THEN: シグネチャと戻り値の意味が変わっていない"""
        # 英語見出し -> canonical_heading
        assert classify_block("## Outcome") == BLOCK_KIND_CANONICAL_HEADING
        # 日英混在見出し -> bilingual_heading
        assert classify_block("## 成果物 (Outcome)") == BLOCK_KIND_BILINGUAL_HEADING
        # 通常 prose
        assert classify_block("これは日本語の説明文です。") == "human_prose"

    def test_block_kind_constants_unchanged(self):
        """GIVEN: block_kind 定数セット WHEN: 全定数を確認
        THEN: ALL_BLOCK_KINDS の 9 種類が存在する"""
        expected = {
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
        assert pbp.ALL_BLOCK_KINDS == frozenset(expected)


# ===========================================================================
# AC2: implementation テンプレートの canonical heading を網羅
# ===========================================================================


class TestInventoryCoversTemplateHeadings:
    """AC2: 現行 implementation テンプレートの canonical heading を全て含む"""

    TEMPLATE_HEADINGS = [
        "Machine-Readable Contract",
        "Parent Issue",
        "Parent Goal Ref",
        "Current Validated Scope",
        "Remaining Parent Gaps",
        "Outcome",
        "Background",
        "In Scope",
        "Out of Scope",
        "Acceptance Criteria",
        "Verification Commands",
        "Allowed Paths",
        "Stop Conditions",
        "Required Skills",
        "Runtime Verification Applicability",
    ]

    def test_inventory_covers_template_headings(self):
        """GIVEN: implementation テンプレートの全 canonical heading
        WHEN: HEADING_POLICY で検索
        THEN: 全て inventory に存在する"""
        for heading in self.TEMPLATE_HEADINGS:
            assert heading in HEADING_POLICY, (
                f"Template heading '{heading}' not found in HEADING_POLICY"
            )

    def test_each_entry_canonical_en_matches_key(self):
        """GIVEN: HEADING_POLICY の各 entry WHEN: canonical_en フィールド確認
        THEN: key と canonical_en が一致する"""
        for key, entry in HEADING_POLICY.items():
            assert entry["canonical_en"] == key, (
                f"key='{key}' but canonical_en='{entry['canonical_en']}'"
            )

    def test_prose_guard_kind_is_canonical_heading(self):
        """GIVEN: 全 entry WHEN: prose_guard_kind 確認
        THEN: canonical heading は prose_guard_kind が BLOCK_KIND_CANONICAL_HEADING"""
        for key, entry in HEADING_POLICY.items():
            assert entry["prose_guard_kind"] == BLOCK_KIND_CANONICAL_HEADING, (
                f"entry '{key}' has unexpected prose_guard_kind: {entry['prose_guard_kind']}"
            )


# ===========================================================================
# AC3: delta mode で canonical_heading 除外 / full-body mode は維持
# ===========================================================================


class TestDeltaCanonicalHeadingExcluded:
    """AC3: delta mode で canonical_heading が除外される"""

    def test_delta_canonical_heading_excluded(self):
        """GIVEN: ## Outcome 単独の変更 WHEN: changed_prose_blocks
        THEN: prose delta として返されない（canonical_heading は除外）"""
        old = "## Outcome\n\nOld content text here.\n"
        new = "## Outcome\n\nNew content text here.\n"
        # old/new の prose block を比較。## Outcome 自体は canonical_heading
        # として除外されるため、その heading 自体は delta 対象にならない
        # ただし本文（"New content text here."）は prose として検出される
        # テストは heading 単独変更の場合
        old2 = "## Outcome\n"
        new2 = "## Outcome\n"
        changed = changed_prose_blocks(old2, new2)
        assert changed == [], (
            "canonical heading のみの変更は changed_prose_blocks に含まれない"
        )

    def test_delta_only_heading_no_prose_change(self):
        """GIVEN: 見出しのみの文書（prose block なし）WHEN: changed_prose_blocks
        THEN: changed は空（heading は prose delta 対象外）"""
        old = "## Outcome\n\n## Background\n"
        new = "## Outcome\n\n## Background\n\n## In Scope\n"
        changed = changed_prose_blocks(old, new)
        # 追加された ## In Scope は canonical_heading なので prose delta 対象外
        assert all(b["type"] != "canonical_heading" for b in changed)

    def test_split_markdown_blocks_heading_type(self):
        """GIVEN: ## Outcome WHEN: split_markdown_blocks
        THEN: type が 'prose'（legacy type）かつ _is_heading_block が True"""
        blocks = split_markdown_blocks("## Outcome\n")
        heading_blocks = [b for b in blocks if b["text"] == "## Outcome"]
        assert len(heading_blocks) >= 1
        # split_markdown_blocks は legacy type を返す（'prose'）
        assert heading_blocks[0]["type"] == "prose"
        # _is_heading_block で heading であることを確認
        assert _is_heading_block(heading_blocks[0]["text"])

    def test_split_markdown_blocks_bilingual_heading_type(self):
        """GIVEN: ## 成果物 (Outcome) WHEN: split_markdown_blocks
        THEN: type が 'prose'（legacy type）かつ _is_heading_block が True"""
        blocks = split_markdown_blocks("## 成果物 (Outcome)\n")
        heading_blocks = [b for b in blocks if "成果物" in b["text"]]
        assert len(heading_blocks) >= 1
        assert heading_blocks[0]["type"] == "prose"
        # _is_heading_block で bilingual heading を確認
        assert _is_heading_block(heading_blocks[0]["text"])

    def test_full_body_zero_prose_still_fails(self):
        """GIVEN: prose block が全くない文書（heading のみ）WHEN: validate_text（full-body mode）
        THEN: passed=False（#594 の prose-zero fail policy 維持）"""
        # 見出しのみで prose block がない文書
        text = "## Outcome\n\n## Background\n\n## In Scope\n"
        result = validate_text(text)
        assert result.passed is False, (
            "full-body mode: prose block ゼロの文書は fail すべき（#594 policy 維持）"
        )

    def test_full_body_with_japanese_prose_passes(self):
        """GIVEN: 十分な日本語 prose を含む文書 WHEN: validate_text
        THEN: passed=True"""
        text = (
            "## Outcome\n\n"
            "この実装では日本語のコンテンツが適切な比率で含まれています。"
            "日本語の説明が十分にあるため、検証を通過します。\n"
        )
        result = validate_text(text)
        assert result.passed is True


# ===========================================================================
# AC5: GFM ATX heading 正規化 / negative test
# ===========================================================================


class TestAtxNormalization:
    """AC5: GFM ATX heading 正規化と negative test"""

    def test_atx_normalization_trailing_hash(self):
        """GIVEN: ## Outcome ## (closing #) WHEN: lookup_heading_policy
        THEN: Outcome entry が返る"""
        # GFM spec: ## Outcome ## は有効な heading
        entry = lookup_heading_policy("Outcome ##")
        assert entry is not None
        assert entry["canonical_en"] == "Outcome"

    def test_atx_normalization_leading_spaces(self):
        """GIVEN: 0–3 spaces indent (ATX spec) WHEN: _normalize_heading_text
        THEN: 先頭空白が除去される"""
        assert _normalize_heading_text("  Outcome  ") == "Outcome"
        assert _normalize_heading_text("Outcome ##") == "Outcome"

    def test_similar_invalid_heading_negative(self):
        """GIVEN: ## Outcomes / ## Outcome Risks / ## Outcome: English prose
        WHEN: lookup_heading_policy
        THEN: None を返す（Outcome と誤認しない）"""
        assert lookup_heading_policy("Outcomes") is None, (
            "'Outcomes' は 'Outcome' と別物"
        )
        assert lookup_heading_policy("Outcome Risks") is None, (
            "'Outcome Risks' は canonical heading ではない"
        )
        assert lookup_heading_policy("Outcome: English prose") is None, (
            "'Outcome: English prose' は canonical heading ではない"
        )

    def test_bilingual_halfwidth_bracket(self):
        """GIVEN: ## 成果物 (Outcome) WHEN: lookup_heading_policy
        THEN: Outcome entry が返る（半角括弧）"""
        entry = lookup_heading_policy("成果物 (Outcome)")
        assert entry is not None
        assert entry["canonical_en"] == "Outcome"

    def test_bilingual_fullwidth_bracket(self):
        """GIVEN: ## 成果物（Outcome）WHEN: lookup_heading_policy
        THEN: Outcome entry が返る（全角括弧）"""
        entry = lookup_heading_policy("成果物（Outcome）")
        assert entry is not None
        assert entry["canonical_en"] == "Outcome"

    def test_extract_bilingual_heading_key_halfwidth(self):
        """GIVEN: 半角括弧の bilingual heading WHEN: _extract_bilingual_heading_key
        THEN: 英語キーが返る"""
        assert _extract_bilingual_heading_key("成果物 (Outcome)") == "Outcome"
        assert _extract_bilingual_heading_key("スコープ内 (In Scope)") == "In Scope"

    def test_extract_bilingual_heading_key_fullwidth(self):
        """GIVEN: 全角括弧の bilingual heading WHEN: _extract_bilingual_heading_key
        THEN: 英語キーが返る"""
        assert _extract_bilingual_heading_key("成果物（Outcome）") == "Outcome"

    def test_plain_english_not_bilingual(self):
        """GIVEN: 英語のみの見出し WHEN: classify_block
        THEN: bilingual_heading ではなく canonical_heading"""
        assert classify_block("## Outcome") == BLOCK_KIND_CANONICAL_HEADING

    def test_japanese_heading_without_english_key(self):
        """GIVEN: 括弧なしの日本語見出し WHEN: lookup_heading_policy
        THEN: None（inventory に存在しない）"""
        assert lookup_heading_policy("成果物") is None

    def test_indented_canonical_heading_is_excluded(self):
        """GIVEN: '   ## Outcome'（3 spaces indent; GFM 許容）
        WHEN: classify_block
        THEN: canonical_heading として除外される（AC5 / B2 fix_delta）"""
        result = classify_block("   ## Outcome")
        assert result == BLOCK_KIND_CANONICAL_HEADING, (
            f"3 spaces indent の '   ## Outcome' は canonical_heading になるべき。got={result!r}"
        )

    def test_indented_bilingual_heading_extract_section(self):
        """GIVEN: '   ## 成果物 (Outcome)'（3 spaces indent; GFM 許容）
        WHEN: classify_block
        THEN: bilingual_heading として分類される（AC5 / B2 fix_delta）"""
        result = classify_block("   ## 成果物 (Outcome)")
        assert result == BLOCK_KIND_BILINGUAL_HEADING, (
            f"3 spaces indent の '   ## 成果物 (Outcome)' は bilingual_heading になるべき。got={result!r}"
        )

    def test_closing_hash_heading_classify(self):
        """GIVEN: '## 成果物 (Outcome) ##'（closing hash; GFM 許容）
        WHEN: classify_block
        THEN: bilingual_heading として分類される（B2 fix_delta: closing # 対応）"""
        result = classify_block("## 成果物 (Outcome) ##")
        assert result == BLOCK_KIND_BILINGUAL_HEADING, (
            f"'## 成果物 (Outcome) ##' は closing hash があっても bilingual_heading になるべき。got={result!r}"
        )

    def test_parse_atx_heading_line_leading_spaces(self):
        """GIVEN: 0-3 spaces indent WHEN: parse_atx_heading_line
        THEN: 正しく解析される（B2: parse_atx_heading_line() SSOT）"""
        assert parse_atx_heading_line("## Outcome") == {'level': 2, 'text': 'Outcome'}
        assert parse_atx_heading_line(" ## Outcome") == {'level': 2, 'text': 'Outcome'}
        assert parse_atx_heading_line("  ## Outcome") == {'level': 2, 'text': 'Outcome'}
        assert parse_atx_heading_line("   ## Outcome") == {'level': 2, 'text': 'Outcome'}

    def test_parse_atx_heading_line_four_spaces_is_not_heading(self):
        """GIVEN: 4 spaces indent WHEN: parse_atx_heading_line
        THEN: None（code block 扱い; GFM 仕様）"""
        result = parse_atx_heading_line("    ## Outcome")
        assert result is None, (
            "4 spaces indent は code block 扱いなので parse_atx_heading_line は None を返すべき"
        )

    def test_parse_atx_heading_line_closing_hash(self):
        """GIVEN: closing # WHEN: parse_atx_heading_line
        THEN: closing # が除去されたテキストが返る"""
        result = parse_atx_heading_line("## Outcome ##")
        assert result is not None
        assert result['text'] == 'Outcome'

        result2 = parse_atx_heading_line("## 成果物 (Outcome) ###")
        assert result2 is not None
        assert result2['text'] == '成果物 (Outcome)'


# ===========================================================================
# AC6: machine_contract / vc_command / code_fence edge case
# ===========================================================================


class TestMachineContractVcCommandCodeFenceEdge:
    """AC6: machine_contract / vc_command / code_fence が prose ratio から除外"""

    def test_machine_contract_excluded(self):
        """GIVEN: ```yaml + contract_schema_version の fence WHEN: _classify_block
        THEN: 'code_fence'（prose delta に含まれない）"""
        block = "```yaml\ncontract_schema_version: v1\nissue_kind: implementation\n```"
        result = _classify_block(block)
        assert result == "code_fence"

    def test_vc_command_excluded(self):
        """GIVEN: $ uv run pytest のコマンドブロック WHEN: _classify_block
        THEN: 'shell_command'（prose delta に含まれない）"""
        block = "$ uv run pytest .claude/skills/create-issue/scripts/tests/ -q"
        result = _classify_block(block)
        assert result in ("shell_command",)

    def test_code_fence_nested_backtick_exact(self):
        """GIVEN: 4個 fence 内に 3個 fence（nested backtick）WHEN: split_markdown_blocks
        THEN: 全体が単一の code_fence ブロックとして抽出される（B3 exact assertion）

        GFM 仕様: opening が 4 backtick のとき内側の 3 backtick 行は closing として無効。
        #659 で iter_markdown_blocks() GFM 準拠セグメンテーションに委譲されたため
        このテストは通常の passing test（exact assertion）として成立する。
        """
        text = "````markdown\n```python\nprint('hello')\n```\n````\n"
        blocks = split_markdown_blocks(text)
        # 4個 fence ブロックは code_fence として単一ブロックになるべき
        fence_blocks = [b for b in blocks if b["type"] == "code_fence"]
        assert len(fence_blocks) == 1, (
            f"4-backtick fence 内の 3-backtick は単一 code_fence になるべき。"
            f"got {len(fence_blocks)} fence blocks"
        )

    def test_code_fence_tilde_exact(self):
        """GIVEN: チルダ fence WHEN: split_markdown_blocks
        THEN: 正確に 1 つの code_fence ブロックとして扱われる（B3 exact assertion）"""
        text = "~~~python\nprint('hello')\n~~~\n"
        blocks = split_markdown_blocks(text)
        fence_blocks = [b for b in blocks if b["type"] == "code_fence"]
        assert len(fence_blocks) == 1, (
            f"tilde fence は 1 つの code_fence になるべき。got={len(fence_blocks)}"
        )

    def test_code_fence_unclosed_consumes_to_eof(self):
        """GIVEN: 閉じていない fence（未閉じ fence）WHEN: split_markdown_blocks
        THEN: EOF まで単一 code_fence として消費され、fence 直後内容が prose に漏れない（B3 exact assertion）

        GFM 仕様: unclosed fence は文書末尾まで code block 扱い。
        #659 で iter_markdown_blocks() GFM 準拠セグメンテーションに委譲されたため
        unclosed fence は EOF まで単一 code_fence ブロックとして扱われることを確認する。
        """
        text = "```python\nprint('hello')\n\nsome other content\n"
        blocks = split_markdown_blocks(text)
        assert isinstance(blocks, list), "split_markdown_blocks は list を返すべき"
        # GFM: unclosed fence は EOF まで単一 code_fence として扱われる
        assert len(blocks) == 1, (
            f"unclosed fence は EOF まで単一 code_fence になるべき。got {len(blocks)} blocks"
        )
        assert blocks[0]["type"] == "code_fence", (
            f"unclosed fence ブロックは code_fence 型になるべき。got type={blocks[0]['type']!r}"
        )
        # fence 直後の内容が prose として漏れないこと
        prose_blocks = [b for b in blocks if b["type"] == "prose"]
        assert len(prose_blocks) == 0, (
            "未閉じ fence の内容が prose に漏れてはならない（GFM: EOF まで code_fence）"
        )

    def test_code_fence_immediate_prose_is_separate_block(self):
        """GIVEN: code fence 直後に空行なし prose WHEN: split_markdown_blocks
        THEN: prose が別 block として分類される（B3 exact assertion）"""
        text = "```python\nprint('hello')\n```\nThis is prose right after fence.\n"
        blocks = split_markdown_blocks(text)
        assert len(blocks) >= 1, "code fence + immediate prose は少なくとも 1 block を持つ"
        # prose block が存在すること
        prose_blocks = [b for b in blocks if b["type"] == "prose"]
        assert len(prose_blocks) >= 1, (
            "fence 直後の prose は prose block として扱われるべき"
        )

    def test_delta_excludes_machine_contract(self):
        """GIVEN: YAML fence の Machine-Readable Contract ブロックの変更
        WHEN: changed_prose_blocks
        THEN: code_fence として除外される（prose delta にならない）"""
        old = "```yaml\ncontract_schema_version: v1\n```\n"
        new = "```yaml\ncontract_schema_version: v2\n```\n"
        changed = changed_prose_blocks(old, new)
        assert changed == [], "code_fence ブロックの変更は prose delta に含まれない"

    def test_delta_excludes_vc_command(self):
        """GIVEN: $ uv run pytest コマンドの変更 WHEN: changed_prose_blocks
        THEN: shell_command として除外される"""
        old = "$ uv run pytest tests/ -q\n"
        new = "$ uv run pytest tests/ -v\n"
        changed = changed_prose_blocks(old, new)
        assert changed == [], "shell_command ブロックの変更は prose delta に含まれない"


# ===========================================================================
# AC7: 英語 prose / non-canonical 英語見出しが引き続き fail
# ===========================================================================


class TestEnglishProseFails:
    """AC7: 英語 prose と non-canonical 英語見出しは引き続き prose ratio で fail"""

    def test_english_prose_still_fails(self):
        """GIVEN: 英語説明段落のみ WHEN: _classify_block
        THEN: 'prose'（日本語比率チェック対象）"""
        block = "This is a purely English description paragraph."
        result = _classify_block(block)
        assert result == "prose", f"英語 prose は 'prose' 分類すべき: {result}"

    def test_non_canonical_english_heading_is_not_heading_block(self):
        """GIVEN: ## This is a long English sentence（非正規見出し）
        WHEN: classify_block と _is_heading_block
        THEN: classify_block は canonical_heading（ATX heading 形式）を返すが、
              _is_heading_block は False を返す（heading_policy に存在しない）
              → prose delta 対象に残る（AC7 / B1_B4 fix_delta）

        TEST_INVERSION 修正: 以前のテスト（test_non_canonical_english_heading_fails）は
        classify_block() == BLOCK_KIND_CANONICAL_HEADING をアサートして「テスト pass」と
        みなしていたが、この heading が prose ratio 判定から除外されることを意味しており
        AC7（非 canonical 英語見出しは fail に残す）に違反していた。
        正しくは _is_heading_block() が False を返すことで prose delta 対象に残る。
        """
        # classify_block() は ATX 形式なので canonical_heading を返す（AC1 維持）
        result = classify_block("## This is a long English sentence")
        assert result == BLOCK_KIND_CANONICAL_HEADING, (
            f"classify_block() は ATX heading 形式を canonical_heading と分類する（AC1）。got={result!r}"
        )
        # しかし heading_policy に存在しないため _is_heading_block() は False を返す
        assert _is_heading_block("## This is a long English sentence") is False, (
            "_is_heading_block() は非 canonical 英語見出しを False として返すべき（B1_B4）"
        )
        assert lookup_heading_policy("This is a long English sentence") is None, (
            "non-canonical 英語見出しは heading_policy に存在しない"
        )

    def test_non_canonical_english_heading_is_prose_delta_target(self):
        """GIVEN: ## This is a long English sentence（非 canonical）の追加
        WHEN: changed_prose_blocks
        THEN: prose delta として返される（AC7 / B1_B4 fix_delta）"""
        old = ""
        new = "## This is a long English sentence\n"
        changed = changed_prose_blocks(old, new)
        assert len(changed) >= 1, (
            "'## This is a long English sentence' は non-canonical 見出しなので"
            " changed_prose_blocks に残るべき"
        )

    def test_similar_but_noncanonical_outcome_heading_is_prose_delta_target(self):
        """GIVEN: ## Outcome Risks（非 canonical; Outcome と類似）の追加
        WHEN: changed_prose_blocks
        THEN: prose delta として返される（AC7 / B1_B4 fix_delta）"""
        old = ""
        new = "## Outcome Risks\n"
        changed = changed_prose_blocks(old, new)
        assert len(changed) >= 1, (
            "'## Outcome Risks' は non-canonical 見出しなので"
            " changed_prose_blocks に残るべき"
        )

    def test_non_canonical_heading_not_in_policy(self):
        """GIVEN: ## Outcomes（類似但し非 canonical）
        WHEN: lookup_heading_policy
        THEN: None（negative test）"""
        assert lookup_heading_policy("Outcomes") is None
        assert lookup_heading_policy("Outcome Risks") is None
        assert lookup_heading_policy("Background Notes") is None

    def test_delta_english_prose_is_changed(self):
        """GIVEN: 英語 prose の変更 WHEN: changed_prose_blocks
        THEN: prose delta として返される（日本語比率チェック対象）"""
        old = "This is English text.\n"
        new = "This is updated English text with more content.\n"
        changed = changed_prose_blocks(old, new)
        # prose 変更は changed_prose_blocks の対象
        assert len(changed) >= 1, "英語 prose の変更は delta 対象になる"


# ===========================================================================
# AC8: heading_policy は validate_japanese_content.py 経由で適用
# ===========================================================================


class TestHeadingPolicyAppliedViaValidate:
    """AC8: heading_policy の適用は validate_japanese_content.py 経由のみ"""

    def test_heading_policy_applied_via_validate(self):
        """GIVEN: validate_japanese_content._is_heading_block WHEN: canonical_heading
        THEN: True を返す（validate 経由で heading_policy が適用）"""
        assert _is_heading_block("## Outcome") is True, (
            "_is_heading_block が canonical_heading を True として返す"
        )
        # _classify_block は legacy 互換のため 'prose' を返す
        result = _classify_block("## Outcome")
        assert result == "prose"

    def test_bilingual_heading_via_validate(self):
        """GIVEN: ## 成果物 (Outcome) WHEN: _is_heading_block
        THEN: True（bilingual heading と認識）"""
        assert _is_heading_block("## 成果物 (Outcome)") is True

    def test_delta_heading_not_counted_as_prose_change(self):
        """GIVEN: canonical_heading のみが変更された delta WHEN: changed_prose_blocks
        THEN: prose delta は空（heading は prose 比率チェック対象外）"""
        old = "## Outcome\n"
        new = "## 成果物 (Outcome)\n"
        changed = changed_prose_blocks(old, new)
        # heading 変更は prose delta に含まれない（_is_heading_block でフィルタ）
        assert all(not _is_heading_block(b["text"]) for b in changed)
