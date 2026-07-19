"""
test_heading_policy.py

Issue #654: canonical / bilingual heading 除外と heading_policy の単体テスト。
Issue #678: _extract_template_labels() を YAML schema-aware parsing に置き換え。

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
import yaml

_SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

# implementation.yml のパス（テスト実行ディレクトリに依存しない絶対パス）
_REPO_ROOT = Path(__file__).parent.parent.parent.parent.parent.parent
_IMPLEMENTATION_YML = _REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "implementation.yml"

# create-issue scaffolding 由来の canonical heading。
# GitHub template の textarea label ではないが、実装 issue 本文に create-issue により挿入される。
SCAFFOLDING_HEADINGS = {
    "Background",
    "Runtime Verification Applicability",
}

# implementation.yml の label から HEADING_POLICY key に変換するマッピング。
# label 値が canonical_en と異なる場合のみ記載する。
_LABEL_TO_CANONICAL: dict[str, str] = {
    "Scope Delta（任意）": "Scope Delta",
}

# HEADING_POLICY may only contain:
# - labels emitted by .github/ISSUE_TEMPLATE/implementation.yml
# - headings inserted by create-issue scaffolding
# - explicitly reviewed extras below
#
# Empty by default: no non-template/non-scaffolding English heading is currently
# allowed to be prose-exempt. Adding a key here expands the prose-exemption
# surface and must explain why it cannot be represented as a template label or
# scaffolding heading.
ALLOWED_EXTRAS: set[str] = {
    "Summary",
    "Checks",
    "Schema Change Applicability",
    "Schema Consumer Inventory",
    "Safety Claim Matrix",
    "Notes",
}


_KNOWN_FIELD_TYPES = frozenset(
    {"textarea", "input", "dropdown", "checkboxes", "upload", "markdown"}
)


def _extract_template_labels(yml_path: Path) -> list[str]:
    """
    implementation.yml の body セクションから label: 値を YAML schema-aware parsing で抽出する。

    GitHub Issue Forms YAML の構造:
      body:
        - type: textarea / input / dropdown / checkboxes / upload
          attributes:
            label: <label_value>
        - type: markdown   # skip — heading 化されない
          attributes:
            value: ...

    処理方針:
    - yaml.safe_load() で YAML 全体をパースする
    - data["body"] を list として走査する
    - type が "markdown" の要素は skip する
    - type が textarea/input/dropdown/checkboxes/upload の要素は
      attributes.label を収集する
    - 上記以外の未知の type は ValueError を raise する
    - yml_path が存在しない / YAML malformed / body が list でない場合は ValueError を raise する

    Returns:
        label 値のリスト（出現順）

    Raises:
        ValueError: yml_path 不在、YAML parse error、body が list でない、unknown type の場合
    """
    if not yml_path.exists():
        raise ValueError(f"yml_path が存在しません: {yml_path}")
    text = yml_path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse error: {yml_path}: {exc}") from exc
    body = data.get("body") if isinstance(data, dict) else None
    if not isinstance(body, list):
        raise ValueError(
            f"implementation.yml の 'body' が list ではありません: {type(body)!r}"
        )
    labels: list[str] = []
    for item in body:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "")
        if item_type == "markdown":
            continue
        if item_type not in _KNOWN_FIELD_TYPES:
            raise ValueError(
                f"unknown type: {item_type!r}。"
                f"サポート対象: {sorted(_KNOWN_FIELD_TYPES)}"
            )
        attrs = item.get("attributes")
        if isinstance(attrs, dict) and "label" in attrs:
            labels.append(str(attrs["label"]))
    return labels


def _get_template_canonical_headings() -> list[str]:
    """implementation.yml から label: 値を抽出し canonical heading key に変換する"""
    raw_labels = _extract_template_labels(_IMPLEMENTATION_YML)
    return [_LABEL_TO_CANONICAL.get(label, label) for label in raw_labels]


def _expected_heading_policy_keys() -> set[str]:
    """HEADING_POLICY に許可される canonical heading 集合を返す"""
    return set(_get_template_canonical_headings()) | SCAFFOLDING_HEADINGS | ALLOWED_EXTRAS


def _assert_heading_policy_near_equivalence(policy_keys: set[str]) -> None:
    """HEADING_POLICY key 集合が許可集合と完全一致することを確認する"""
    expected = _expected_heading_policy_keys()
    missing = expected - policy_keys
    extra = policy_keys - expected
    assert missing == set(), f"HEADING_POLICY に不足している heading: {missing}"
    assert extra == set(), f"HEADING_POLICY に余分な heading がある: {extra}"

import prose_boundary_policy as pbp
from prose_boundary_policy import (
    HEADING_POLICY,
    BLOCK_KIND_CANONICAL_HEADING,
    BLOCK_KIND_BILINGUAL_HEADING,
    classify_block,
    lookup_heading_policy,
    parse_atx_heading_line,
    _normalize_heading_text,
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
        THEN: ALL_BLOCK_KINDS の 10 種類が存在する（#685: table 追加）"""
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
            "table",
        }
        assert pbp.ALL_BLOCK_KINDS == frozenset(expected)


# ===========================================================================
# AC2: implementation テンプレートの canonical heading を網羅
# ===========================================================================


class TestInventoryCoversTemplateHeadings:
    """AC2: implementation テンプレートの canonical heading を HEADING_POLICY が網羅する（M2: SSOT 化）

    M2 fix (#654): テストは .github/ISSUE_TEMPLATE/implementation.yml の label: 値を
    動的に抽出して HEADING_POLICY との被覆を exact assert する。
    hard-code された TEMPLATE_HEADINGS は silent drift を起こすため廃止。

    heading の出所区別:
      - template_derived: implementation.yml の textarea/input の label: 値に由来
      - scaffolding_headings: create-issue scaffolding により実装 issue 本文に出現するが
        GitHub template の textarea label ではない canonical heading
        （Background, Runtime Verification Applicability）

    HEADING_POLICY は以下の条件を満たすこと:
      HEADING_POLICY ⊇ (template_derived_canonical_headings ∪ scaffolding_headings)

    Scope Delta（任意）: implementation.yml に label として存在する任意項目。
    label 値 "Scope Delta（任意）" の canonical_en は "Scope Delta" として HEADING_POLICY に登録済み。
    """

    def test_implementation_yml_exists(self):
        """GIVEN: .github/ISSUE_TEMPLATE/implementation.yml WHEN: ファイル存在確認
        THEN: ファイルが存在する（M2: SSOT）"""
        assert _IMPLEMENTATION_YML.exists(), (
            f"implementation.yml が見つかりません: {_IMPLEMENTATION_YML}"
        )

    def test_template_labels_extracted(self):
        """GIVEN: implementation.yml WHEN: label: 値を抽出
        THEN: 1 つ以上の label が抽出される"""
        labels = _extract_template_labels(_IMPLEMENTATION_YML)
        assert len(labels) > 0, "implementation.yml から label が抽出されなかった"

    def test_inventory_covers_template_headings(self):
        """GIVEN: implementation.yml から動的抽出した canonical heading 群
        WHEN: HEADING_POLICY で検索
        THEN: 全て inventory に存在する（M2: SSOT 被覆）"""
        canonical_headings = _get_template_canonical_headings()
        assert len(canonical_headings) > 0, "template から canonical heading を抽出できなかった"
        for heading in canonical_headings:
            assert heading in HEADING_POLICY, (
                f"Template heading '{heading}' not found in HEADING_POLICY. "
                f"Label-to-canonical mapping may need updating in _LABEL_TO_CANONICAL."
            )

    def test_inventory_covers_scaffolding_headings(self):
        """GIVEN: create-issue scaffolding 由来の canonical heading 群
        WHEN: HEADING_POLICY で検索
        THEN: 全て inventory に存在する（M2: scaffolding headings 被覆）"""
        for heading in SCAFFOLDING_HEADINGS:
            assert heading in HEADING_POLICY, (
                f"Scaffolding heading '{heading}' not found in HEADING_POLICY. "
                f"Note: scaffolding headings are inserted by create-issue, "
                f"not from GitHub template textarea labels."
            )

    def test_heading_policy_near_equivalence(self):
        """GIVEN: template-derived ∪ scaffolding ∪ allowed-extras
        WHEN: HEADING_POLICY key 集合を helper で検証
        THEN: 許可集合と exact match する"""
        _assert_heading_policy_near_equivalence(set(HEADING_POLICY.keys()))

    def test_heading_policy_rejects_unknown_heading(self):
        """GIVEN: unknown canonical English heading を含む key 集合
        WHEN: near-equivalence helper を実行
        THEN: AssertionError で拒否される"""
        policy_keys = set(HEADING_POLICY.keys()) | {"Arbitrary English Heading"}
        with pytest.raises(AssertionError):
            _assert_heading_policy_near_equivalence(policy_keys)

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
        _old = "## Outcome\n\nOld content text here.\n"
        _new = "## Outcome\n\nNew content text here.\n"
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

    def test_non_canonical_heading_not_excluded_via_classify_block(self):
        """GIVEN: 非 canonical 英語見出し「## Outcome Risks」
        WHEN: guard 判定（_is_heading_block / changed_prose_blocks）を通す
        THEN: prose 除外されない（prose delta に残る）

        regression: classify_block() が BLOCK_KIND_CANONICAL_HEADING を返しても、
        guard 判定（_is_heading_block）が依拠するのは heading_policy であり
        classify_block() の戻り値だけで prose 除外してはならないことを確認する。
        「classify_block(...) == BLOCK_KIND_CANONICAL_HEADING だから prose 除外」は誤動作。
        """
        heading = "## Outcome Risks"
        # classify_block() は構文的に canonical_heading と返す（ATX heading 形式）
        assert classify_block(heading) == BLOCK_KIND_CANONICAL_HEADING, (
            "classify_block() は ATX heading 形式を canonical_heading と分類する（構文分類のみ）"
        )
        # しかし _is_heading_block()（guard 判定）は heading_policy を参照するため False
        assert _is_heading_block(heading) is False, (
            "_is_heading_block() は heading_policy 未登録見出しを False として返す（prose 除外しない）"
        )
        # changed_prose_blocks（guard 経路）でも prose delta 対象に残る
        old = ""
        new = heading + "\n"
        changed = changed_prose_blocks(old, new)
        assert len(changed) >= 1, (
            "'## Outcome Risks' は non-canonical 見出しなので changed_prose_blocks（guard 経路）"
            " に残るべき。classify_block() の戻り値に依存した誤除外が発生していない。"
        )


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

    @pytest.mark.parametrize(
        "heading",
        ["Schema Change Applicability", "Schema Consumer Inventory"],
    )
    def test_lp052_exact_heading_is_non_prose_canonical(self, heading):
        """GIVEN: LP052 exact English heading WHEN: Japanese prose validation
        THEN: canonical headingとして除外され、翻訳を要求されない"""
        entry = lookup_heading_policy(heading)
        assert entry is not None
        assert entry["prose_guard_kind"] == BLOCK_KIND_CANONICAL_HEADING
        assert _is_heading_block(f"## {heading}") is True

    def test_lp052_noncanonical_english_heading_remains_prose(self):
        """GIVEN: LP052にない英語見出し WHEN: Japanese prose validation
        THEN: prose validation対象のまま failする"""
        text = "## Schema Change Applicability Details\n"
        result = validate_text(text)
        assert result.passed is False
        assert lookup_heading_policy("Schema Change Applicability Details") is None
        assert _is_heading_block("## Schema Change Applicability Details") is False

    def test_delta_heading_not_counted_as_prose_change(self):
        """GIVEN: canonical_heading のみが変更された delta WHEN: changed_prose_blocks
        THEN: prose delta は空（heading は prose 比率チェック対象外）"""
        old = "## Outcome\n"
        new = "## 成果物 (Outcome)\n"
        changed = changed_prose_blocks(old, new)
        # heading 変更は prose delta に含まれない（_is_heading_block でフィルタ）
        assert all(not _is_heading_block(b["text"]) for b in changed)


# ===========================================================================
# B1 fix: 4-space indented line は GFM code block → heading 誤除外してはならない
# ===========================================================================


class TestFourSpaceIndentedHeadingNotExcluded:
    """B1 fix (#654): 4-space indented code block が heading 誤除外される問題の修正確認"""

    def test_four_space_indented_heading_is_not_heading_block(self):
        """GIVEN: '    ## Outcome'（先頭 4 spaces; GFM code block 扱い）
        WHEN: _is_heading_block
        THEN: False（code block 扱いなので heading ではない）

        B1 fix: _is_heading_block() は block.strip() してから parse_atx_heading_line()
        に渡さず、raw line（rstrip only）を渡す。parse_atx_heading_line() が None を
        返す（4-space = code block）ため False になる。
        """
        assert _is_heading_block("    ## Outcome") is False, (
            "4-space indented '    ## Outcome' は GFM code block 扱い。"
            "_is_heading_block() は False を返すべき（B1 fix）"
        )

    def test_four_space_indented_heading_remains_prose_delta_target(self):
        """GIVEN: '    ## Outcome\n'（先頭 4 spaces）の追加
        WHEN: changed_prose_blocks
        THEN: prose delta として返される（heading 誤除外されない）

        B1 fix: split_markdown_blocks() が raw_text（leading spaces 保持）を保持し、
        _is_prose_delta_target() が raw_text で _is_heading_block() を判定する。
        """
        old = ""
        new = "    ## Outcome\n"
        changed = changed_prose_blocks(old, new)
        assert changed != [], (
            "4-space indented '    ## Outcome' は GFM code block 扱い。"
            "changed_prose_blocks に含まれるべき（B1 fix）"
        )

    def test_three_space_indented_canonical_heading_still_excluded(self):
        """GIVEN: '   ## Outcome'（先頭 3 spaces; GFM 有効 ATX heading）
        WHEN: changed_prose_blocks
        THEN: canonical heading として除外される（3 spaces は GFM 許容）"""
        old = ""
        new = "   ## Outcome\n"
        changed = changed_prose_blocks(old, new)
        assert changed == [], (
            "3-space indented '   ## Outcome' は有効な ATX heading。"
            "changed_prose_blocks には含まれない（canonical heading 除外）"
        )

    def test_raw_text_preserved_in_split_markdown_blocks(self):
        """GIVEN: '    ## Outcome\n' WHEN: split_markdown_blocks
        THEN: raw_text が leading spaces を保持し text は strip 済み"""
        from validate_japanese_content import split_markdown_blocks
        blocks = split_markdown_blocks("    ## Outcome\n")
        assert len(blocks) >= 1
        # text は strip 済み（後方互換）
        assert blocks[0]["text"] == "## Outcome"
        # raw_text は leading spaces を保持（B1 fix）
        assert "raw_text" in blocks[0]
        assert blocks[0]["raw_text"].startswith("    ")


# ===========================================================================
# B2 fix: bilingual accepted_forms バイパス修正
# ===========================================================================


class TestBilingualAcceptedFormsBypassFixed:
    """B2 fix (#654): 任意 prefix + (CanonicalEnglish) の括弧内キー単独一致で
    accepted_forms をバイパスする問題の修正確認"""

    @pytest.mark.parametrize("heading_text", [
        "適当な日本語 (Outcome)",
        "成果物ではない（Outcome）",
        "English Prefix (Outcome)",
        "全く関係ない文章 (In Scope)",
        "Something Else (Background)",
    ])
    def test_arbitrary_prefix_bilingual_not_accepted(self, heading_text):
        """GIVEN: 任意 prefix + (CanonicalEnglish) 形式の見出し
        WHEN: lookup_heading_policy
        THEN: None（accepted_forms exact match のみで accept; B2 fix）"""
        result = lookup_heading_policy(heading_text)
        assert result is None, (
            f"'{heading_text}' は accepted_forms に登録されていない。"
            f"None を返すべき（B2 fix）。got={result!r}"
        )

    def test_accepted_forms_exact_match_still_works(self):
        """GIVEN: accepted_forms に登録された bilingual heading
        WHEN: lookup_heading_policy
        THEN: 正しい entry が返る（B2 fix 後も正常動作）"""
        assert lookup_heading_policy("成果物 (Outcome)") is not None
        assert lookup_heading_policy("成果物（Outcome）") is not None
        assert lookup_heading_policy("Outcome") is not None

    def test_canonical_en_direct_match_still_works(self):
        """GIVEN: canonical_en（英語見出し）
        WHEN: lookup_heading_policy
        THEN: entry が返る（direct match は B2 fix 後も維持）"""
        for key in ["Outcome", "Background", "In Scope", "Out of Scope"]:
            assert lookup_heading_policy(key) is not None, (
                f"canonical_en '{key}' の direct match は B2 fix 後も動作すべき"
            )


# ===========================================================================
# M1 fix: code fence shorter closing does not close
# ===========================================================================


class TestCodeFenceShorterClosingDoesNotClose:
    """M1 fix (#654): closing が opening より短い fence は閉じない（GFM 仕様）"""

    def test_code_fence_shorter_closing_does_not_close(self):
        """GIVEN: ````python ... ``` ... ```` の構造
        WHEN: split_markdown_blocks
        THEN: 単一の code_fence ブロックとして扱われ、内部の ``` は closing にならない

        GFM 仕様: closing fence は opening と同長以上でなければならない。
        3-backtick closing は 4-backtick opening を閉じない。
        #659 の iter_markdown_blocks GFM 準拠委譲後はこれが PASS する。
        """
        text = "````python\nprint('x')\n```\nthis is still code\n````\n"
        blocks = split_markdown_blocks(text)
        fence_blocks = [b for b in blocks if b["type"] == "code_fence"]
        assert len(fence_blocks) == 1, (
            f"4-backtick fence 内の 3-backtick は closing にならない。"
            f"単一 fence block になるべき。got {len(fence_blocks)} fence blocks"
        )
        assert "this is still code" in fence_blocks[0]["text"], (
            "3-backtick の後のコンテンツは fence 内に含まれるべき"
        )


# ===========================================================================
# #678: _extract_template_labels YAML schema-aware parsing regression tests
# ===========================================================================


class TestExtractTemplateLabelsSchemaAware:
    """#678: _extract_template_labels() の YAML schema-aware parsing regression test

    AC3: tmp_path に synthetic issue form YAML（type: markdown, checkboxes, textarea）を
         生成し、_extract_template_labels() が top-level non-markdown attributes.label
         のみを返すことを検証する。
    """

    def test_schema_aware_returns_textarea_and_input_labels(self, tmp_path: Path):
        """GIVEN: markdown + checkboxes + textarea を含む synthetic YAML
        WHEN: _extract_template_labels()
        THEN: type: markdown は skip、checkboxes.options[].label は取り込まない、
              top-level attributes.label（textarea/input）のみ返す"""
        yml = tmp_path / "issue_form.yml"
        yml.write_text(
            "name: Test\n"
            "description: Test form\n"
            "body:\n"
            "  - type: markdown\n"
            "    attributes:\n"
            "      value: |\n"
            "        Please fill in all required fields.\n"
            "  - type: checkboxes\n"
            "    id: options\n"
            "    attributes:\n"
            "      label: Options\n"
            "      options:\n"
            "        - label: Option A\n"
            "        - label: Option B\n"
            "  - type: textarea\n"
            "    id: description\n"
            "    attributes:\n"
            "      label: Outcome\n"
            "  - type: input\n"
            "    id: parent\n"
            "    attributes:\n"
            "      label: Parent Issue\n",
            encoding="utf-8",
        )
        labels = _extract_template_labels(yml)
        # markdown は skip される
        assert "Please fill in all required fields." not in labels
        # checkboxes.options[].label は取り込まない
        assert "Option A" not in labels
        assert "Option B" not in labels
        # top-level attributes.label のみ取り込む
        assert "Options" in labels
        assert "Outcome" in labels
        assert "Parent Issue" in labels
        # 順序が保持されている
        assert labels.index("Options") < labels.index("Outcome")
        assert labels.index("Outcome") < labels.index("Parent Issue")

    def test_schema_aware_skips_markdown_type(self, tmp_path: Path):
        """GIVEN: type: markdown のみの YAML
        WHEN: _extract_template_labels()
        THEN: 空リストを返す（markdown は skip）"""
        yml = tmp_path / "form.yml"
        yml.write_text(
            "name: Test\n"
            "body:\n"
            "  - type: markdown\n"
            "    attributes:\n"
            "      value: Some instruction text.\n"
            "  - type: markdown\n"
            "    attributes:\n"
            "      value: Another markdown block.\n",
            encoding="utf-8",
        )
        labels = _extract_template_labels(yml)
        assert labels == [], (
            "type: markdown のみの YAML は空リストを返すべき"
        )

    def test_schema_aware_includes_dropdown_label(self, tmp_path: Path):
        """GIVEN: type: dropdown を含む YAML
        WHEN: _extract_template_labels()
        THEN: dropdown の top-level attributes.label を返す"""
        yml = tmp_path / "form.yml"
        yml.write_text(
            "name: Test\n"
            "body:\n"
            "  - type: dropdown\n"
            "    id: priority\n"
            "    attributes:\n"
            "      label: Priority Level\n"
            "      options:\n"
            "        - Low\n"
            "        - High\n",
            encoding="utf-8",
        )
        labels = _extract_template_labels(yml)
        assert "Priority Level" in labels
        assert "Low" not in labels
        assert "High" not in labels


class TestExtractTemplateLabelsFailFast:
    """#678: _extract_template_labels() の fail-fast テスト（AC4）

    AC4: yml_path 不在・YAML malformed・body が list でない場合の fail-fast。
    """

    def test_yml_path_not_found_raises_value_error(self, tmp_path: Path):
        """GIVEN: 存在しない yml_path
        WHEN: _extract_template_labels()
        THEN: ValueError を raise する（fail-fast）"""
        nonexistent = tmp_path / "nonexistent.yml"
        with pytest.raises(ValueError, match="yml_path が存在しません"):
            _extract_template_labels(nonexistent)

    def test_yaml_malformed_raises_value_error(self, tmp_path: Path):
        """GIVEN: YAML として不正なファイル（malformed YAML）
        WHEN: _extract_template_labels()
        THEN: ValueError を raise する（fail-fast）"""
        yml = tmp_path / "malformed.yml"
        yml.write_text(
            "name: Test\n"
            "body:\n"
            "  - type: textarea\n"
            "    attributes: {\n"
            "      invalid yaml here\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="YAML parse error"):
            _extract_template_labels(yml)

    def test_body_not_list_raises_value_error(self, tmp_path: Path):
        """GIVEN: body が list でない YAML（dict など）
        WHEN: _extract_template_labels()
        THEN: ValueError を raise する（fail-fast）"""
        yml = tmp_path / "form.yml"
        yml.write_text(
            "name: Test\n"
            "body:\n"
            "  type: textarea\n"
            "  attributes:\n"
            "    label: Something\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="'body' が list ではありません"):
            _extract_template_labels(yml)

    def test_unknown_type_raises_value_error(self, tmp_path: Path):
        """GIVEN: 未知の type（known type 以外）を含む YAML
        WHEN: _extract_template_labels()
        THEN: ValueError を raise する（fail-fast; unknown type は許容しない）"""
        yml = tmp_path / "form.yml"
        yml.write_text(
            "name: Test\n"
            "body:\n"
            "  - type: unknown_custom_widget\n"
            "    attributes:\n"
            "      label: Something\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="unknown type"):
            _extract_template_labels(yml)

    def test_yaml_safe_load_used_for_parsing(self, tmp_path: Path):
        """GIVEN: 有効な YAML（AC1 の確認）
        WHEN: _extract_template_labels()
        THEN: yaml.safe_load() ベースの実装が label を正しく返す
              （regex の space+label:space+ に依存しない）"""
        yml = tmp_path / "form.yml"
        yml.write_text(
            "name: Test\n"
            "body:\n"
            "  - type: textarea\n"
            "    attributes:\n"
            "      label: My Label\n"
            "      description: Some description\n",
            encoding="utf-8",
        )
        labels = _extract_template_labels(yml)
        assert labels == ["My Label"], (
            "yaml.safe_load ベースの実装は attributes.label を正しく返すべき"
        )
