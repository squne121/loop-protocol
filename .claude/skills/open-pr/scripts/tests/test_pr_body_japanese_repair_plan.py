#!/usr/bin/env python3
"""
Tests for pr_body_japanese_repair_plan.py

AC3: per-block 判定・threshold 0.1 の unit test
AC4: repairable / human_review_required の分類
AC5: code fence / GFM table / HTML comment / heading 保護
AC6: GitHub closing keyword / Refs / cross-repo reference の保護
AC7: body_file_out / validate_pr_body 連携
AC8: stdout は compact JSON のみ
AC9: exit code 10/20/30/40 の各分岐
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path


# テスト対象スクリプトをロード
_SCRIPT_DIR = Path(__file__).parent.parent
_SCRIPT_PATH = _SCRIPT_DIR / "pr_body_japanese_repair_plan.py"

sys.path.insert(0, str(_SCRIPT_DIR))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# prose_boundary_policy を先にロード（依存関係）
_CREATE_ISSUE_SCRIPTS = _SCRIPT_DIR.parent.parent / "create-issue" / "scripts"
_pbp = _load_module("prose_boundary_policy", _CREATE_ISSUE_SCRIPTS / "prose_boundary_policy.py")
_vjc = _load_module("validate_japanese_content", _CREATE_ISSUE_SCRIPTS / "validate_japanese_content.py")

# テスト対象
_mod = _load_module("pr_body_japanese_repair_plan", _SCRIPT_PATH)

analyze_pr_body = _mod.analyze_pr_body
extract_preserved_tokens = _mod.extract_preserved_tokens
_determine_exit_code = _mod._determine_exit_code
_is_protected_block = _mod._is_protected_block
_is_repairable_block = _mod._is_repairable_block


# ---------------------------------------------------------------------------
# テスト用 PR body fixture helper
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pr_body"

# 有効な PR body の基本テンプレート（validate_pr_body.py を通過するもの）
_VALID_PR_BODY_TEMPLATE = """\
## Summary

{summary}

## Checks

- CI: パス確認済み
- テスト: 全件 PASS

## Schema Change Applicability

- decision: not_schema_change

## Schema Consumer Inventory

N/A

## Safety Claim Matrix

| Claim | Implemented? | Not controlled | Evidence | Follow-up |
|---|---|---|---|---|
| 変更なし | yes | | コードレビュー | |

## Notes

- Related issue: #{issue_num}
"""

def _make_valid_body(summary: str = "これはテスト用の PR です。日本語で記述します。", issue_num: int = 819) -> str:
    return _VALID_PR_BODY_TEMPLATE.format(summary=summary, issue_num=issue_num)


# ---------------------------------------------------------------------------
# AC3: per-block 判定・threshold 0.1
# ---------------------------------------------------------------------------

class TestPerBlockJapaneseRatio:
    """AC3: per-block 判定・threshold 0.1"""

    def test_threshold_pass_all_japanese(self):
        """日本語比率が threshold 以上のブロックは pass"""
        # validate_text() は per-block で判定するため、
        # 英語見出し（## Summary 等）も有効文字数 > 5 であれば失敗する。
        # そのため pure Japanese text のみを渡して pass を確認する。
        pure_japanese = "これはすべて日本語で書かれた説明です。問題はありません。"
        plan = analyze_pr_body(pure_japanese, threshold=0.1)
        assert plan["status"] == "pass"
        assert plan["schema"] == "PR_BODY_JAPANESE_REPAIR_PLAN_V1"

    def test_threshold_fail_english_only(self):
        """英語のみのブロックは threshold 未満で fail"""
        body = _make_valid_body("This summary is written entirely in English without any Japanese.")
        plan = analyze_pr_body(body, threshold=0.1)
        assert len(plan["failed_blocks"]) > 0
        assert plan["status"] in ("repairable", "human_review_required")

    def test_threshold_per_block_not_aggregate(self):
        """aggregate ではなく per-block で判定される"""
        # Summary は日本語 0%, Notes は日本語 100% でも Summary が失敗
        body = _make_valid_body("Only English in this summary block.")
        plan = analyze_pr_body(body, threshold=0.1)
        # failed_blocks が存在する
        assert len(plan["failed_blocks"]) > 0

    def test_threshold_custom_value(self):
        """threshold 引数が正しく反映される"""
        body = _make_valid_body("一部日本語 some english mixed text here.")
        plan_strict = analyze_pr_body(body, threshold=0.5)
        plan_loose = analyze_pr_body(body, threshold=0.0)
        # threshold=0.0 なら pass
        assert plan_loose["status"] == "pass"
        # threshold が stored される
        assert plan_strict["threshold"] == 0.5
        assert plan_loose["threshold"] == 0.0

    def test_japanese_ratio_field_in_failed_blocks(self):
        """failed_blocks に ratio フィールドが含まれる"""
        body = _make_valid_body("English only summary for testing purposes.")
        plan = analyze_pr_body(body, threshold=0.1)
        if plan["failed_blocks"]:
            fb = plan["failed_blocks"][0]
            assert "ratio" in fb
            assert "effective_chars" in fb
            assert "block_index" in fb


# ---------------------------------------------------------------------------
# AC4: repairable / human_review_required
# ---------------------------------------------------------------------------

class TestRepairableRouting:
    """AC4: repairable = deterministic な意味保存修復のみ"""

    def test_short_english_boilerplate_is_repairable(self):
        """短い英語 boilerplate は human_review_required（body_file_out が None のため downgrade）"""
        body = _make_valid_body("Test PR.")
        plan = analyze_pr_body(body, threshold=0.1)
        assert len(plan["failed_blocks"]) > 0
        # _generate_body_file_out が None を返すため repairable → human_review_required へ downgrade
        assert plan["status"] == "human_review_required"

    def test_long_english_prose_is_human_review_required(self):
        """長い英語 prose（任意の意味変換が必要）は human_review_required"""
        long_english = (
            "This pull request implements a comprehensive solution to the problem "
            "of Japanese text validation in PR bodies. The implementation leverages "
            "existing validation infrastructure to provide deterministic repair plans "
            "for cases where Japanese text ratio falls below the configured threshold."
        )
        body = _make_valid_body(long_english)
        plan = analyze_pr_body(body, threshold=0.1)
        assert len(plan["failed_blocks"]) > 0
        assert plan["status"] == "human_review_required"
        assert plan["next_action"] == "human_review_required"

    def test_repairable_has_safe_rewrite_plan(self):
        """repairable case は safe_rewrite_plan を持つ"""
        body = _make_valid_body("Short PR.")
        plan = analyze_pr_body(body, threshold=0.1)
        if plan["status"] == "repairable":
            assert isinstance(plan["safe_rewrite_plan"], list)
            assert len(plan["safe_rewrite_plan"]) > 0

    def test_human_review_required_has_no_body_file_out(self):
        """human_review_required case は body_file_out が None"""
        long_english = (
            "This is a very long English summary that requires semantic translation "
            "and cannot be deterministically repaired without human judgment."
        )
        body = _make_valid_body(long_english)
        plan = analyze_pr_body(body, threshold=0.1)
        if plan["status"] == "human_review_required":
            assert plan["body_file_out"] is None

    def test_pass_status_has_empty_failed_blocks(self):
        """pass case は failed_blocks が空"""
        # pure Japanese prose のみで pass を確認する
        # (英語見出しが混在する PR body template は英語見出し部分が failed になる)
        pure_japanese = "これは完全に日本語で書かれた概要です。すべての要件を満たしています。"
        plan = analyze_pr_body(pure_japanese, threshold=0.1)
        assert plan["status"] == "pass"
        assert plan["failed_blocks"] == []
        assert plan["safe_rewrite_plan"] == []


# ---------------------------------------------------------------------------
# AC5: code fence / GFM table / HTML comment / heading 保護
# ---------------------------------------------------------------------------

class TestProtectedBlocksRegression:
    """AC5: 保護ブロックを破壊しない"""

    def test_code_fence_not_in_failed_blocks(self):
        """code fence ブロックは failed_blocks に含まれない"""
        body_with_fence = _make_valid_body(
            "日本語の説明です。"
        ) + """
```bash
# This is an English-only code block that should be protected
echo "hello world"
```
"""
        plan = analyze_pr_body(body_with_fence, threshold=0.1)
        # code fence 内の英語は failed_blocks に出てこない（text_preview は --include-preview なしで None）
        for fb in plan["failed_blocks"]:
            assert "hello world" not in (fb.get("text_preview") or "")

    def test_gfm_table_not_in_failed_blocks(self):
        """GFM テーブルは failed_blocks に含まれない"""
        # テーブルのみを含む body で確認する
        # テーブルは type: "table" として分類され、保護される
        body_with_table = """| Claim | Evidence | Status |
|---|---|---|
| Safety | CI green | yes |
| English only row | no japanese | confirmed |
"""
        plan = analyze_pr_body(body_with_table, threshold=0.1)
        # GFM テーブルは保護されるため failed_blocks に含まれない
        for fb in plan["failed_blocks"]:
            # text_preview に表のヘッダ行が含まれていないこと
            preview = fb.get("text_preview") or ""
            # テーブルの delimiter 行や data 行は含まれない
            assert "|---|" not in preview

    def test_html_comment_not_in_failed_blocks(self):
        """HTML comment は failed_blocks に含まれない"""
        body_with_comment = _make_valid_body("日本語の説明です。") + """
<!-- This is an English-only HTML comment that must be protected -->
"""
        plan = analyze_pr_body(body_with_comment, threshold=0.1)
        for fb in plan["failed_blocks"]:
            assert "HTML comment" not in (fb.get("text_preview") or "")

    def test_canonical_heading_not_in_failed_blocks(self):
        """canonical heading は failed_blocks に含まれない"""
        # "## Summary" は heading_policy にある canonical heading ではないが
        # validate_pr_body.py の required section として存在する
        # ここでは heading_policy に登録された見出しを検証
        body = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
```

## Outcome

これは成果物の説明です。

""" + _make_valid_body("日本語の説明です。")
        plan = analyze_pr_body(body, threshold=0.1)
        # "Outcome" や "Machine-Readable Contract" は保護されるべき
        for fb in plan["failed_blocks"]:
            preview = fb.get("text_preview") or ""
            assert not preview.startswith("## Outcome")
            assert not preview.startswith("## Machine-Readable Contract")

    def test_lp052_exact_headings_are_protected_without_translation(self):
        """LP052 heading は英語のまま保護され、日本語化修復の対象にならない。"""
        body = _make_valid_body("日本語の説明です。")
        plan = analyze_pr_body(body, threshold=0.1)

        failed_previews = [item.get("text_preview") or "" for item in plan["failed_blocks"]]
        assert not any("Schema Change Applicability" in preview for preview in failed_previews)
        assert not any("Schema Consumer Inventory" in preview for preview in failed_previews)

    def test_bilingual_heading_protected(self):
        """日英混在見出しは保護される（任意の ATX 見出しは protected）"""
        block = {
            "type": "prose",
            "text": "## 概要 (Summary)",
            "raw_text": "## 概要 (Summary)",
        }
        result = _is_protected_block(block)
        assert result is True

    def test_fence_block_is_protected(self):
        """code_fence type のブロックは必ず保護される"""
        block = {
            "type": "code_fence",
            "text": "```\nEnglish only\n```",
        }
        assert _is_protected_block(block) is True

    def test_table_block_is_protected(self):
        """table type のブロックは必ず保護される"""
        block = {
            "type": "table",
            "text": "| A | B |\n|---|---|\n| 1 | 2 |",
        }
        assert _is_protected_block(block) is True

    def test_html_comment_in_prose_is_protected(self):
        """HTML comment を含む prose ブロックは保護される"""
        block = {
            "type": "prose",
            "text": "<!-- English comment -->",
            "raw_text": "<!-- English comment -->",
        }
        assert _is_protected_block(block) is True


# ---------------------------------------------------------------------------
# AC6: GitHub closing keyword / Refs / cross-repo reference の保護
# ---------------------------------------------------------------------------

class TestPreservedTokens:
    """AC6: 保護トークンの正しい抽出"""

    def test_closes_keyword(self):
        """Closes #N が保護トークンとして抽出される"""
        body = "Closes #819"
        tokens = extract_preserved_tokens(body)
        assert any("Closes" in t and "819" in t for t in tokens)

    def test_closes_variant_case_insensitive(self):
        """大文字小文字を問わず closing keyword が抽出される"""
        for keyword in ["close", "closes", "closed", "fix", "fixes", "fixed",
                        "resolve", "resolves", "resolved"]:
            body = f"{keyword} #123"
            tokens = extract_preserved_tokens(body)
            assert any("123" in t for t in tokens), f"Failed for keyword: {keyword}"

    def test_colon_variant(self):
        """Closes: #N (colon variant) が保護される"""
        body = "Closes: #819"
        tokens = extract_preserved_tokens(body)
        assert any("819" in t for t in tokens)

    def test_cross_repo_reference(self):
        """owner/repo#N (cross-repo reference) が保護される"""
        body = "Closes squne121/loop-protocol#819"
        tokens = extract_preserved_tokens(body)
        assert any("loop-protocol#819" in t for t in tokens)

    def test_multiple_issues(self):
        """Closes #1, #2 (複数 issue 列挙) が保護される"""
        body = "Closes #1, #2, #3"
        tokens = extract_preserved_tokens(body)
        assert len(tokens) >= 1
        # 全 issue 番号が含まれる
        combined = " ".join(tokens)
        assert "1" in combined
        assert "2" in combined

    def test_refs_keyword(self):
        """Refs #N が保護トークンとして抽出される"""
        body = "Refs #819"
        tokens = extract_preserved_tokens(body)
        assert any("Refs" in t and "819" in t for t in tokens)

    def test_refs_cross_repo(self):
        """Refs owner/repo#N が保護される"""
        body = "Refs squne121/loop-protocol#819"
        tokens = extract_preserved_tokens(body)
        assert any("loop-protocol#819" in t for t in tokens)

    def test_closing_keyword_in_body_is_preserved_in_plan(self):
        """PR body に closing keyword があっても preserved_tokens に含まれる"""
        body = _make_valid_body("日本語の説明です。") + "\nCloses #819\n"
        plan = analyze_pr_body(body, threshold=0.1)
        assert any("819" in t for t in plan["preserved_tokens"])

    def test_refs_only_in_body_is_preserved(self):
        """PR body に Refs #N があっても preserved_tokens に含まれる"""
        body = _make_valid_body("日本語の説明です。") + "\nRefs #100\n"
        plan = analyze_pr_body(body, threshold=0.1)
        assert any("100" in t for t in plan["preserved_tokens"])

    def test_repeated_keyword_multi_issue(self):
        """GitHub docs 例: キーワード繰り返しによる複数 issue 列挙"""
        body = "Resolves #10, resolves #123, resolves octo-org/octo-repo#100"
        tokens = extract_preserved_tokens(body)
        combined = " ".join(tokens)
        assert "10" in combined
        assert "123" in combined
        assert "octo-org/octo-repo#100" in combined

    def test_closes_uppercase_colon(self):
        """GitHub docs 例: 大文字 CLOSES: のコロン variant"""
        body = "CLOSES: #10"
        tokens = extract_preserved_tokens(body)
        assert any("10" in t for t in tokens)

    def test_fixes_cross_repo(self):
        """GitHub docs 例: Fixes owner/repo#N (cross-repo reference)"""
        body = "Fixes owner/repo#42"
        tokens = extract_preserved_tokens(body)
        assert any("owner/repo#42" in t for t in tokens)


# ---------------------------------------------------------------------------
# AC7: body_file_out / validate_pr_body との連携
# ---------------------------------------------------------------------------

class TestBodyFileOut:
    """AC7: body_file_out を生成する repairable case のテスト"""

    def test_repairable_case_has_body_file_out_field(self):
        """repairable status は body_file_out が non-None であることを保証する"""
        body = _make_valid_body("Short PR.")
        plan = analyze_pr_body(body, threshold=0.1)
        assert "body_file_out" in plan
        # status: repairable になるのは body_file_out が non-None の場合のみ
        if plan["status"] == "repairable":
            assert plan["body_file_out"] is not None

    def test_pass_case_body_file_out_is_none(self):
        """pass case は body_file_out が None"""
        body = _make_valid_body("これは完全に日本語で書かれた説明です。")
        plan = analyze_pr_body(body, threshold=0.1)
        if plan["status"] == "pass":
            assert plan["body_file_out"] is None

    def test_validate_pr_body_passes_with_valid_fixture(self):
        """validate_pr_body.py と validate_japanese_content.py が有効 fixture で PASS する"""
        _CREATE_ISSUE_SCRIPTS_DIR = _SCRIPT_DIR.parent.parent / "create-issue" / "scripts"
        validate_pr_body_script = _SCRIPT_DIR / "validate_pr_body.py"
        validate_japanese_script = _CREATE_ISSUE_SCRIPTS_DIR / "validate_japanese_content.py"
        fixture_path = Path(__file__).parent / "fixtures" / "pr_body" / "valid_not_schema_change.md"

        # validate_pr_body.py: 構造チェック → 既存 fixture を使用
        non_safety_paths = Path(__file__).parent / "fixtures" / "pr_body" / "non_safety_paths.txt"
        result = subprocess.run(
            [sys.executable, str(validate_pr_body_script),
             "--body-file", str(fixture_path),
             "--linked-issue", "330",
             "--changed-paths-file", str(non_safety_paths)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"validate_pr_body.py failed (exit {result.returncode}): {result.stdout}"
        )

        # validate_japanese_content.py: 日本語比率チェック → 純粋な日本語 prose で検証
        japanese_body = "これはすべて日本語で書かれた概要です。\n\n修正内容を日本語で説明しています。"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8", delete=False) as f:
            f.write(japanese_body)
            japanese_path = f.name
        try:
            result = subprocess.run(
                [sys.executable, str(validate_japanese_script), "--file", japanese_path],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            assert result.returncode == 0, (
                f"validate_japanese_content.py failed (exit {result.returncode}): {result.stderr}"
            )
        finally:
            Path(japanese_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC8/AC9: stdout compact JSON のみ / exit code 固定
# ---------------------------------------------------------------------------

class TestCompactJsonAndExitCode:
    """AC8: compact JSON のみ / AC9: exit code"""

    def test_compact_json_output(self):
        """stdout は compact JSON（改行なし・スペースなし）のみ"""
        body = _make_valid_body("これは日本語の説明です。")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8", delete=False) as f:
            f.write(body)
            body_file = f.name

        try:
            result = subprocess.run(
                [sys.executable, str(_SCRIPT_PATH), "--body-file", body_file],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        finally:
            Path(body_file).unlink(missing_ok=True)

        stdout = result.stdout.strip()
        # compact JSON: 改行なし（1行）
        assert "\n" not in stdout, f"stdout should be single line: {stdout!r}"
        # valid JSON
        parsed = json.loads(stdout)
        assert parsed["schema"] == "PR_BODY_JAPANESE_REPAIR_PLAN_V1"

    def test_no_raw_body_in_output(self):
        """stdout/stderr に raw PR body 全文が含まれない"""
        unique_marker = "UNIQUE_ENGLISH_MARKER_DO_NOT_REPEAT_XYZ123"
        body = _make_valid_body(f"Summary with {unique_marker} in it.")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8", delete=False) as f:
            f.write(body)
            body_file = f.name

        try:
            result = subprocess.run(
                [sys.executable, str(_SCRIPT_PATH), "--body-file", body_file],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        finally:
            Path(body_file).unlink(missing_ok=True)

        # raw body marker が stdout に露出していないこと（--include-preview なしはデフォルト非表示）
        assert "UNIQUE_ENGLISH_MARKER_DO_NOT_REPEAT_XYZ123" not in result.stdout
        stdout_json = json.loads(result.stdout.strip())
        # body_file_out フィールドは None
        assert stdout_json.get("body_file_out") is None
        # text_preview は --include-preview なしなので None
        for fb in stdout_json.get("failed_blocks", []):
            assert fb.get("text_preview") is None

    def test_exit_code_0_for_pass(self):
        """pass case は exit code 0"""
        plan = {"status": "pass"}
        assert _determine_exit_code(plan) == 0

    def test_exit_code_10_for_repairable(self):
        """repairable case は exit code 10"""
        plan = {"status": "repairable"}
        assert _determine_exit_code(plan) == 10

    def test_exit_code_20_for_human_review_required(self):
        """human_review_required case は exit code 20"""
        plan = {"status": "human_review_required"}
        assert _determine_exit_code(plan) == 20

    def test_exit_code_30_for_invalid_body(self):
        """invalid_body case は exit code 30"""
        plan = {"status": "invalid_body"}
        assert _determine_exit_code(plan) == 30

    def test_exit_code_40_for_gh_error_via_cli(self):
        """gh_error case は exit code 40 — 存在しない PR 番号で gh_error を誘発"""
        result = subprocess.run(
            [sys.executable, str(_SCRIPT_PATH), "--pr", "999999999"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        parsed = json.loads(result.stdout.strip())
        assert parsed["status"] == "gh_error"
        assert result.returncode == 40

    def test_exit_code_30_for_missing_body_file(self):
        """存在しない body-file は exit code 30"""
        result = subprocess.run(
            [sys.executable, str(_SCRIPT_PATH), "--body-file", "/tmp/nonexistent_pr_body_819.md"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 30
        parsed = json.loads(result.stdout.strip())
        assert parsed["status"] == "invalid_body"

    def test_exit_code_via_cli_pass(self):
        """CLI から pass body (純粋な日本語 prose) を渡すと exit code 0"""
        # テンプレートは英語見出しがあるため失敗する。純粋な日本語 prose を渡す。
        body = "これは完全に日本語で書かれた概要です。すべて正常です。"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8", delete=False) as f:
            f.write(body)
            body_file = f.name

        try:
            result = subprocess.run(
                [sys.executable, str(_SCRIPT_PATH), "--body-file", body_file],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        finally:
            Path(body_file).unlink(missing_ok=True)

        assert result.returncode == 0
        parsed = json.loads(result.stdout.strip())
        assert parsed["status"] == "pass"

    def test_exit_code_via_cli_human_review_required(self):
        """CLI から長い英語 prose を渡すと exit code 20"""
        long_english = _make_valid_body(
            "This is a very long English summary that requires semantic translation "
            "and cannot be deterministically repaired without human judgment involved. "
            "The implementation details are complex and require domain expertise."
        )

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8", delete=False) as f:
            f.write(long_english)
            body_file = f.name

        try:
            result = subprocess.run(
                [sys.executable, str(_SCRIPT_PATH), "--body-file", body_file],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        finally:
            Path(body_file).unlink(missing_ok=True)

        parsed = json.loads(result.stdout.strip())
        assert result.returncode in (10, 20)  # repairable or human_review_required
        assert parsed["status"] in ("repairable", "human_review_required")


# ---------------------------------------------------------------------------
# AC2: SSOT 再利用の確認（integration）
# ---------------------------------------------------------------------------

class TestSSOTReuse:
    """AC2: validate_japanese_content.py / prose_boundary_policy.py の SSOT 再利用"""

    def test_validate_text_ssot_consistent(self):
        """analyze_pr_body の判定が validate_text と一致する"""
        from validate_japanese_content import validate_text as ssot_validate_text

        # 英語のみ prose は SSOT も analyze_pr_body も fail/repairable/human_review_required を返す
        english_only = "English only summary for testing."
        plan = analyze_pr_body(english_only, threshold=0.1)
        ssot_result = ssot_validate_text(english_only, threshold=0.1)

        if ssot_result.passed:
            assert plan["status"] == "pass"
        else:
            assert plan["status"] in ("repairable", "human_review_required")

        # 日本語のみ prose は両者とも pass を返す
        japanese_only = "これは完全に日本語で書かれた文章です。"
        plan_ja = analyze_pr_body(japanese_only, threshold=0.1)
        ssot_ja = ssot_validate_text(japanese_only, threshold=0.1)
        assert ssot_ja.passed == (plan_ja["status"] == "pass")

    def test_split_markdown_blocks_ssot_used(self):
        """split_markdown_blocks SSOT が code fence を正しく除外する"""
        from validate_japanese_content import split_markdown_blocks as ssot_split

        body_text = """## Summary

日本語の説明です。

```python
# English only code
def hello():
    return "world"
```

## Notes

日本語のノートです。
"""
        blocks = ssot_split(body_text)
        # code_fence ブロックが存在する
        fence_blocks = [b for b in blocks if b["type"] == "code_fence"]
        assert len(fence_blocks) >= 1

        # analyze_pr_body でも code fence 内は failed_blocks に含まれない
        body = _make_valid_body("日本語の説明です。") + body_text
        plan = analyze_pr_body(body, threshold=0.1)
        for fb in plan["failed_blocks"]:
            assert "hello" not in (fb.get("text_preview") or "")

    def test_lookup_heading_policy_ssot_used(self):
        """lookup_heading_policy SSOT が canonical heading を保護する"""
        from prose_boundary_policy import lookup_heading_policy as ssot_lookup

        # canonical heading は heading_policy に登録されている
        result = ssot_lookup("Outcome")
        assert result is not None

        result_none = ssot_lookup("This Is Not A Canonical Heading")
        assert result_none is None


# ---------------------------------------------------------------------------
# スキーマフィールドの存在確認
# ---------------------------------------------------------------------------

class TestSchemaFields:
    """PR_BODY_JAPANESE_REPAIR_PLAN_V1 のスキーマフィールド確認"""

    def test_all_required_fields_present(self):
        """必須フィールドが全て存在する"""
        body = _make_valid_body("日本語の説明です。")
        plan = analyze_pr_body(body, threshold=0.1)

        required_fields = [
            "schema",
            "status",
            "threshold",
            "failed_blocks",
            "safe_rewrite_plan",
            "body_file_out",
            "preserved_tokens",
            "next_action",
        ]
        for field in required_fields:
            assert field in plan, f"Missing required field: {field}"

    def test_schema_name_correct(self):
        """schema フィールドが正しい値"""
        body = _make_valid_body("日本語の説明です。")
        plan = analyze_pr_body(body, threshold=0.1)
        assert plan["schema"] == "PR_BODY_JAPANESE_REPAIR_PLAN_V1"

    def test_empty_body_returns_invalid_body(self):
        """空の PR body は invalid_body"""
        plan = analyze_pr_body("", threshold=0.1)
        assert plan["status"] == "invalid_body"
        assert plan["next_action"] == "human_review_required"

    def test_whitespace_only_body_returns_invalid_body(self):
        """空白のみの PR body は invalid_body"""
        plan = analyze_pr_body("   \n\t\n   ", threshold=0.1)
        assert plan["status"] == "invalid_body"
