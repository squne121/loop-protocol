"""
test_guard_ac_vc_alignment.py

guard-issue-body.py の guard_ac_vc_alignment（および main() 内 issue_kind 未確定時の
フォールバック分岐）が「集合一致」（set(ac_numbers) == set(vc_numbers)）で判定する
ことを確認するユニットテスト（#1694 AC2）。

テスト戦略:
- 関数本体（guard_ac_vc_alignment）は importlib で直接呼び出す
- issue_kind 未確定時のフォールバック分岐は main() のコードパスであり、
  subprocess 経由で --issue-kind を渡さず MRC も持たない body を渡すことで到達させる
- 1つの AC に複数 VC コマンドを束ねる正当なパターン（同一 AC 番号が複数回登場）で
  passed: true になることを確認する（関数本体・フォールバック分岐の両方）
- 真の不一致（AC に無い番号が VC にのみ登場、または AC にある番号が VC に無い）では
  passed: false になることを確認する（関数本体・フォールバック分岐の両方）
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# テスト対象モジュールをインポートするために scripts ディレクトリをパスに追加
# ファイル名が guard-issue-body.py（ハイフン）のため importlib で読み込む
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
_MODULE_PATH = _SCRIPTS_DIR / "guard-issue-body.py"
_MODULE_PATH_STR = str(_MODULE_PATH)

_spec = importlib.util.spec_from_file_location("guard_issue_body_ac_vc", _MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

guard_ac_vc_alignment = _mod.guard_ac_vc_alignment


# ---------------------------------------------------------------------------
# template_dir フィクスチャ（implementation 種別のみ、Verification Commands required）
# ---------------------------------------------------------------------------

_IMPLEMENTATION_TEMPLATE = {
    "name": "Implementation",
    "body": [
        {
            "type": "textarea",
            "attributes": {"label": "Machine-Readable Contract"},
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
    ],
}


@pytest.fixture
def template_dir(tmp_path):
    """一時 ISSUE_TEMPLATE ディレクトリを作成し implementation.yml を書き込む。"""
    tmpl_dir = tmp_path / "ISSUE_TEMPLATE"
    tmpl_dir.mkdir()
    path = tmpl_dir / "implementation.yml"
    path.write_text(yaml.dump(_IMPLEMENTATION_TEMPLATE, allow_unicode=True), encoding="utf-8")
    return tmpl_dir


_BUNDLED_VC_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Acceptance Criteria

- [ ] AC7 4つの検証コマンドを束ねる

## Verification Commands

```bash
# AC7
pnpm typecheck
# AC7
pnpm lint
# AC7
pnpm test
# AC7
pnpm build
```
"""

_TRUE_MISMATCH_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Acceptance Criteria

- [ ] AC1 テスト1

## Verification Commands

```bash
# AC1
pnpm test
# AC2
pnpm build
```
"""


# ---------------------------------------------------------------------------
# 関数本体（guard_ac_vc_alignment）のテスト
# ---------------------------------------------------------------------------

class TestGuardAcVcAlignmentSetBased:
    def test_bundled_vc_commands_for_single_ac_passes(self, template_dir):
        """GIVEN 1 つの AC に複数 VC コマンドを束ねる（同一 AC 番号が複数回登場）
        WHEN guard_ac_vc_alignment THEN 集合一致で passed: true"""
        result = guard_ac_vc_alignment(_BUNDLED_VC_BODY, "implementation", template_dir=template_dir)
        assert result["skipped"] is False
        assert result["passed"] is True
        assert result["ac_count"] == 1
        assert result["vc_ac_count"] == 4

    def test_true_mismatch_ac_missing_from_vc_fails(self, template_dir):
        """GIVEN AC に無い番号が VC にのみ登場する真の不一致
        WHEN guard_ac_vc_alignment THEN passed: false"""
        result = guard_ac_vc_alignment(_TRUE_MISMATCH_BODY, "implementation", template_dir=template_dir)
        assert result["skipped"] is False
        assert result["passed"] is False

    def test_set_based_equality_ignores_ordering_and_duplicates(self, template_dir):
        """GIVEN AC 番号集合と VC 番号集合が同じだが並び・重複が異なる
        WHEN guard_ac_vc_alignment THEN set(ac_numbers) == set(vc_numbers) で passed: true"""
        body = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
```

## Acceptance Criteria

- [ ] AC2 二番目
- [ ] AC1 一番目

## Verification Commands

```bash
# AC1
pnpm test
# AC1
pnpm test --watch
# AC2
pnpm build
```
"""
        result = guard_ac_vc_alignment(body, "implementation", template_dir=template_dir)
        assert result["skipped"] is False
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# issue_kind 未確定時のフォールバック分岐のテスト（main() 内、subprocess 経由）
# ---------------------------------------------------------------------------

def _run_guard_issue_body(body_text: str, tmp_path) -> dict:
    """--issue-kind を渡さず MRC も持たない body で main() を実行し、
    issue_kind 未確定時のフォールバック分岐（ac_vc_alignment を含む）に到達させる。"""
    body_file = tmp_path / "body.md"
    body_file.write_text(body_text, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            _MODULE_PATH_STR,
            str(body_file),
            "--format", "json",
        ],
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


class TestGuardAcVcAlignmentFallbackBranch:
    def test_fallback_bundled_vc_commands_for_single_ac_passes(self, tmp_path):
        """GIVEN issue_kind 未確定（MRC なし）で 1 AC に複数 VC コマンドを束ねる body
        WHEN main() のフォールバック分岐 THEN ac_vc_alignment が passed: true を返す"""
        body = """\
## Acceptance Criteria

- [ ] AC7 4つの検証コマンドを束ねる

## Verification Commands

```bash
# AC7
pnpm typecheck
# AC7
pnpm lint
# AC7
pnpm test
# AC7
pnpm build
```
"""
        output = _run_guard_issue_body(body, tmp_path)
        ac_vc_results = [g for g in output["guards"] if g["name"] == "ac_vc_alignment"]
        assert len(ac_vc_results) == 1
        assert ac_vc_results[0]["passed"] is True
        assert ac_vc_results[0]["ac_count"] == 1
        assert ac_vc_results[0]["vc_ac_count"] == 4

    def test_fallback_true_mismatch_fails(self, tmp_path):
        """GIVEN issue_kind 未確定（MRC なし）で AC に無い番号が VC にのみ登場する body
        WHEN main() のフォールバック分岐 THEN ac_vc_alignment が passed: false を返す"""
        body = """\
## Acceptance Criteria

- [ ] AC1 テスト1

## Verification Commands

```bash
# AC1
pnpm test
# AC2
pnpm build
```
"""
        output = _run_guard_issue_body(body, tmp_path)
        ac_vc_results = [g for g in output["guards"] if g["name"] == "ac_vc_alignment"]
        assert len(ac_vc_results) == 1
        assert ac_vc_results[0]["passed"] is False
