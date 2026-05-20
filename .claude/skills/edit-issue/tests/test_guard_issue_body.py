"""
test_guard_issue_body.py

guard-issue-body.py のユニットテスト。

テスト戦略:
- tmp_path で一時 ISSUE_TEMPLATE ディレクトリを作る方式を採用
- implementation / research / parent の 3 種別すべてで正常 body が template guard を pass することを確認
- required section を 1 つ欠いた body が fail することを確認
- parent 種別で guard_ac_vc_alignment() が skipped: true を返すことを確認
- PyYAML は yaml.safe_load() のみ使用
"""

import sys
from pathlib import Path

import pytest
import yaml

# テスト対象モジュールをインポートするために scripts ディレクトリをパスに追加
# ファイル名が guard-issue-body.py（ハイフン）のため importlib で読み込む
import importlib.util

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
_MODULE_PATH = _SCRIPTS_DIR / "guard-issue-body.py"

_spec = importlib.util.spec_from_file_location("guard_issue_body", _MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

guard_ac_vc_alignment = _mod.guard_ac_vc_alignment
guard_template = _mod.guard_template
load_required_labels = _mod.load_required_labels
extract_issue_kind_from_body = _mod.extract_issue_kind_from_body


# ---------------------------------------------------------------------------
# ISSUE_TEMPLATE フィクスチャ用の YAML コンテンツ
# ---------------------------------------------------------------------------

_IMPLEMENTATION_TEMPLATE = {
    "name": "Implementation",
    "body": [
        {
            "type": "markdown",
            "attributes": {"value": "説明テキスト"},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Machine-Readable Contract"},
            "validations": {"required": True},
        },
        {
            "type": "input",
            "attributes": {"label": "Parent Issue"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Parent Goal Ref"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Current Validated Scope"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Remaining Parent Gaps"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Outcome"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "In Scope"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Out of Scope"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Acceptance Criteria"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Verification Commands"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Allowed Paths"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Stop Conditions"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Required Skills"},
            "validations": {"required": True},
        },
    ],
}

_RESEARCH_TEMPLATE = {
    "name": "Research",
    "body": [
        {
            "type": "markdown",
            "attributes": {"value": "説明テキスト"},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Machine-Readable Contract"},
            "validations": {"required": True},
        },
        {
            "type": "input",
            "attributes": {"label": "Parent Issue"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Outcome"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "In Scope"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Out of Scope"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Acceptance Criteria"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Verification Commands"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Allowed Paths"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Stop Conditions"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Handoff Contract"},
            "validations": {"required": True},
        },
    ],
}

_PARENT_TEMPLATE = {
    "name": "Parent",
    "body": [
        {
            "type": "markdown",
            "attributes": {"value": "説明テキスト"},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Machine-Readable Contract"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Summary"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Goal"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Desired Destination"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Current Validated Scope"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Decisions Fixed"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Quality Decision Record"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Parent Closure Rule"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Child Issues"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Remaining Parent Gaps"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Phase Handoff Contract"},
            "validations": {"required": True},
        },
        {
            "type": "textarea",
            "attributes": {"label": "Acceptance Criteria"},
            "validations": {"required": True},
        },
        # Notes は required: false なので含めない
    ],
}


@pytest.fixture
def template_dir(tmp_path):
    """一時 ISSUE_TEMPLATE ディレクトリを作成し、3 種別の YAML を書き込む。"""
    tmpl_dir = tmp_path / "ISSUE_TEMPLATE"
    tmpl_dir.mkdir()

    for kind, content in [
        ("implementation", _IMPLEMENTATION_TEMPLATE),
        ("research", _RESEARCH_TEMPLATE),
        ("parent", _PARENT_TEMPLATE),
    ]:
        path = tmpl_dir / f"{kind}.yml"
        path.write_text(yaml.dump(content, allow_unicode=True), encoding="utf-8")

    return tmpl_dir


# ---------------------------------------------------------------------------
# ヘルパー: 各種別の正常 body を生成
# ---------------------------------------------------------------------------

def make_implementation_body() -> str:
    return """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: none
goal_ref: "テスト用"
change_kind: code
```

## Parent Issue

none

## Parent Goal Ref

- Goal: なし
- Desired Destination: N/A

## Current Validated Scope

- テスト実装

## Remaining Parent Gaps

なし

## Outcome

テストが PASS している状態

## In Scope

- テスト実装

## Out of Scope

- 本番実装

## Acceptance Criteria

- [ ] AC1 テストが PASS する
- [ ] AC2 型エラーがない

## Verification Commands

```bash
# AC1
uv run pytest tests/ -v
# AC2
pnpm typecheck
```

## Allowed Paths

- tests/test_example.py

## Stop Conditions

- Allowed Paths 外の変更が必要と判明した場合

## Required Skills

- Python / pytest
"""


def make_research_body() -> str:
    return """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "テスト調査"
change_kind: research-only
```

## Parent Issue

none

## Outcome

調査完了し次の implementation Issue が起票可能な状態

## In Scope

- アルゴリズム候補 3 件の比較

## Out of Scope

- 実装コード

## Acceptance Criteria

- [ ] AC1 比較結果が本 Issue 本文に記載されている
- [ ] AC2 後続 Issue が起票されている

## Verification Commands

```bash
# AC1
rg -n "比較結果" docs/research/example.md
# AC2
gh issue view 999
```

## Allowed Paths

- 読み取り専用

## Stop Conditions

- Allowed Paths 外への書き込みを試みた場合は即停止

## Handoff Contract

- Current Objective
- Next Action
"""


def make_parent_body() -> str:
    return """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: parent
goal_ref: "親 Issue トラッカー"
change_kind: workflow
parent_mode: delivery-rollup
closure_mode: child-complete
```

## Summary

複数 child issue を束ねるトラッカー

## Goal

機能 X の完成

## Desired Destination

機能 X が main ブランチに統合された状態

## Current Validated Scope

- child issue #1、#2、#3

## Decisions Fixed

- 2026-05-20: アーキテクチャを ECS に決定

## Quality Decision Record

- Status: N/A
- Decision Date: 未記録
- Does this prove the parent goal complete?: N/A
- Reason: N/A
- Evidence: N/A
- Next Action: N/A

## Parent Closure Rule

- delivery-rollup: 全 child issue が close されたら close する
- quality-gate: N/A
- routing-map: N/A
- decision-log: N/A

## Child Issues

- [ ] #100 — implementation child

## Remaining Parent Gaps

- [ ] 後続実装

## Phase Handoff Contract

- Desired Destination
- Current Validated Scope

## Acceptance Criteria

- [ ] AC1 全 child issue が close される
"""


# ---------------------------------------------------------------------------
# load_required_labels のテスト
# ---------------------------------------------------------------------------

class TestLoadRequiredLabels:
    def test_implementation_returns_expected_labels(self, template_dir):
        """GIVEN implementation テンプレート WHEN load_required_labels を呼ぶ THEN 必須ラベルが返る"""
        labels = load_required_labels(template_dir, "implementation")
        assert "Outcome" in labels
        assert "In Scope" in labels
        assert "Out of Scope" in labels
        assert "Acceptance Criteria" in labels
        assert "Verification Commands" in labels
        assert "Allowed Paths" in labels
        assert "Stop Conditions" in labels
        # markdown 要素は除外される
        assert len([l for l in labels if l is None]) == 0

    def test_research_returns_expected_labels(self, template_dir):
        """GIVEN research テンプレート WHEN load_required_labels を呼ぶ THEN 必須ラベルが返る"""
        labels = load_required_labels(template_dir, "research")
        assert "Outcome" in labels
        assert "Verification Commands" in labels
        assert "Handoff Contract" in labels

    def test_parent_returns_expected_labels(self, template_dir):
        """GIVEN parent テンプレート WHEN load_required_labels を呼ぶ THEN 必須ラベルが返る"""
        labels = load_required_labels(template_dir, "parent")
        assert "Summary" in labels
        assert "Goal" in labels
        assert "Acceptance Criteria" in labels
        # parent は Verification Commands を持たない
        assert "Verification Commands" not in labels

    def test_missing_template_raises_file_not_found(self, template_dir):
        """GIVEN 存在しない kind WHEN load_required_labels を呼ぶ THEN FileNotFoundError"""
        with pytest.raises(FileNotFoundError, match="ISSUE_TEMPLATE not found"):
            load_required_labels(template_dir, "nonexistent")

    def test_markdown_items_are_excluded(self, template_dir):
        """GIVEN テンプレートに markdown 要素がある WHEN load_required_labels を呼ぶ THEN markdown は除外される"""
        labels = load_required_labels(template_dir, "implementation")
        # markdown 要素の label は None なので結果リストに None が含まれない
        for label in labels:
            assert isinstance(label, str)


# ---------------------------------------------------------------------------
# extract_issue_kind_from_body のテスト
# ---------------------------------------------------------------------------

class TestExtractIssueKindFromBody:
    def test_extracts_implementation_from_mrc_block(self):
        """GIVEN implementation の MRC ブロック WHEN extract_issue_kind_from_body THEN 'implementation' を返す"""
        body = make_implementation_body()
        result = extract_issue_kind_from_body(body)
        assert result == "implementation"

    def test_extracts_research_from_mrc_block(self):
        """GIVEN research の MRC ブロック WHEN extract_issue_kind_from_body THEN 'research' を返す"""
        body = make_research_body()
        result = extract_issue_kind_from_body(body)
        assert result == "research"

    def test_extracts_parent_from_mrc_block(self):
        """GIVEN parent の MRC ブロック WHEN extract_issue_kind_from_body THEN 'parent' を返す"""
        body = make_parent_body()
        result = extract_issue_kind_from_body(body)
        assert result == "parent"

    def test_returns_none_when_no_mrc_block(self):
        """GIVEN MRC ブロックがない本文 WHEN extract_issue_kind_from_body THEN None を返す"""
        body = "# タイトル\n\n本文のみ"
        result = extract_issue_kind_from_body(body)
        assert result is None

    def test_returns_none_when_no_issue_kind_in_mrc(self):
        """GIVEN issue_kind が無い MRC ブロック WHEN extract_issue_kind_from_body THEN None を返す"""
        body = "```yaml\ncontract_schema_version: v1\n```\n"
        result = extract_issue_kind_from_body(body)
        assert result is None


# ---------------------------------------------------------------------------
# guard_template のテスト（#68 AC2/AC3 対応）
# ---------------------------------------------------------------------------

class TestGuardTemplate:
    def test_implementation_valid_body_passes(self, template_dir):
        """GIVEN implementation の正常 body WHEN guard_template THEN passed=True"""
        body = make_implementation_body()
        result = guard_template(body, "implementation", template_dir=template_dir)
        assert result["passed"] is True
        assert result["missing_sections"] == []

    def test_implementation_missing_section_fails(self, template_dir):
        """GIVEN implementation の body から Outcome を削除 WHEN guard_template THEN passed=False"""
        body = make_implementation_body()
        body = body.replace("## Outcome\n", "## DELETED\n")
        result = guard_template(body, "implementation", template_dir=template_dir)
        assert result["passed"] is False
        assert "## Outcome" in result["missing_sections"]

    def test_research_valid_body_passes(self, template_dir):
        """GIVEN research の正常 body WHEN guard_template THEN passed=True"""
        body = make_research_body()
        result = guard_template(body, "research", template_dir=template_dir)
        assert result["passed"] is True
        assert result["missing_sections"] == []

    def test_research_missing_section_fails(self, template_dir):
        """GIVEN research の body から Handoff Contract を削除 WHEN guard_template THEN passed=False"""
        body = make_research_body()
        body = body.replace("## Handoff Contract\n", "## DELETED\n")
        result = guard_template(body, "research", template_dir=template_dir)
        assert result["passed"] is False
        assert "## Handoff Contract" in result["missing_sections"]

    def test_parent_valid_body_passes(self, template_dir):
        """GIVEN parent の正常 body WHEN guard_template THEN passed=True"""
        body = make_parent_body()
        result = guard_template(body, "parent", template_dir=template_dir)
        assert result["passed"] is True
        assert result["missing_sections"] == []

    def test_parent_missing_section_fails(self, template_dir):
        """GIVEN parent の body から Goal を削除 WHEN guard_template THEN passed=False"""
        body = make_parent_body()
        body = body.replace("## Goal\n", "## DELETED\n")
        result = guard_template(body, "parent", template_dir=template_dir)
        assert result["passed"] is False
        assert "## Goal" in result["missing_sections"]

    def test_nonexistent_kind_fails(self, template_dir):
        """GIVEN 存在しない kind WHEN guard_template THEN passed=False かつ error が含まれる"""
        body = make_implementation_body()
        result = guard_template(body, "nonexistent", template_dir=template_dir)
        assert result["passed"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# guard_ac_vc_alignment のテスト（#99 AC1/AC3 対応）
# ---------------------------------------------------------------------------

class TestGuardAcVcAlignment:
    def test_parent_returns_skipped_true(self, template_dir):
        """GIVEN parent 種別 WHEN guard_ac_vc_alignment THEN skipped=True を返す"""
        body = make_parent_body()
        result = guard_ac_vc_alignment(body, "parent", template_dir=template_dir)
        assert result["passed"] is True
        assert result["skipped"] is True
        assert "reason" in result

    def test_implementation_not_skipped_ac_vc_match(self, template_dir):
        """GIVEN implementation 種別 で AC/VC が一致 WHEN guard_ac_vc_alignment THEN passed=True, skipped=False"""
        body = make_implementation_body()
        result = guard_ac_vc_alignment(body, "implementation", template_dir=template_dir)
        assert result["skipped"] is False
        assert result["passed"] is True

    def test_implementation_not_skipped_ac_vc_mismatch(self, template_dir):
        """GIVEN implementation 種別 で AC 2 件・VC に # AC コメントなし WHEN guard_ac_vc_alignment THEN passed=False"""
        body = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Acceptance Criteria

- [ ] AC1 テスト1
- [ ] AC2 テスト2

## Verification Commands

```bash
pnpm test
```
"""
        result = guard_ac_vc_alignment(body, "implementation", template_dir=template_dir)
        assert result["skipped"] is False
        assert result["passed"] is False
        assert result["ac_count"] == 2
        assert result["vc_ac_count"] == 0

    def test_research_not_skipped(self, template_dir):
        """GIVEN research 種別（VC required あり）WHEN guard_ac_vc_alignment THEN skipped=False"""
        body = make_research_body()
        result = guard_ac_vc_alignment(body, "research", template_dir=template_dir)
        assert result["skipped"] is False

    def test_parent_skipped_even_with_ac_vc_content(self, template_dir):
        """GIVEN parent 種別で AC VC コンテンツが存在する WHEN guard_ac_vc_alignment THEN skipped=True"""
        body = make_parent_body() + "\n# AC1\n# AC2\n"
        result = guard_ac_vc_alignment(body, "parent", template_dir=template_dir)
        assert result["passed"] is True
        assert result["skipped"] is True

    def test_implementation_zero_ac_passes(self, template_dir):
        """GIVEN implementation 種別で AC が 0 件 WHEN guard_ac_vc_alignment THEN passed=True（ゼロ AC は許容）"""
        body = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Acceptance Criteria

なし
"""
        result = guard_ac_vc_alignment(body, "implementation", template_dir=template_dir)
        assert result["skipped"] is False
        assert result["passed"] is True
        assert result["ac_count"] == 0
