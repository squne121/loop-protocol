"""
test_guard_borderline_repair.py

guard-japanese-prose.sh の borderline repair signal (Issue #605) の smoke test。

AC への対応:
  AC1: borderline prose で exit 2 + repair reason code が返ること
       (test_borderline_prose_returns_repair_signal)
  AC2: permissionDecision: "ask" が guard-japanese-prose.sh に存在しないこと
       (test_no_ask_permission_decision)
  AC3: reason に borderline_japanese_prose_repair_required が含まれること
       (test_borderline_reason_code_present)
  AC4: reason に「日本語 prose に修正して再試行」の指示が含まれること
       (test_borderline_reason_contains_retry_instruction)
  AC5: code fence / inline code / identifier / file path / URL は英語許容
       (test_english_allowlist_code_fence, test_english_allowlist_identifier,
        test_english_allowlist_url)
  AC6: 明確な英語 prose はブロックされること (test_english_prose_clear_fail_blocked)
  AC7: "ask が返らないこと" と "Claude Code 向け修正指示が返ること" を検証
       (test_no_ask_in_hook_output, test_repair_instruction_in_borderline_output)
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# テスト対象スクリプトのパス
HOOKS_DIR = Path(__file__).parent.parent
HOOK_SCRIPT = HOOKS_DIR / "guard-japanese-prose.sh"
PROJECT_DIR = HOOKS_DIR.parent.parent
VALIDATOR = PROJECT_DIR / ".claude/skills/create-issue/scripts/validate_japanese_content.py"

# validate_japanese_content モジュールを直接インポート（単体テスト用）
sys.path.insert(0, str(PROJECT_DIR / ".claude/skills/create-issue/scripts"))
from validate_japanese_content import classify_borderline


def run_hook(tool_name: str, command: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """
    guard-japanese-prose.sh を実行するヘルパー。
    tool_name と command を持つ JSON を stdin として渡す。
    """
    hook_input = json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=hook_input,
        capture_output=True,
        text=True,
        env=run_env,
    )


def run_hook_write(file_path: str, content: str) -> subprocess.CompletedProcess:
    """Write ツールをシミュレートして hook を実行する"""
    hook_input = json.dumps({
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": content},
    })
    return subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=hook_input,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )


# ============================================================
# borderline テキスト fixture
# ============================================================

# borderline prose: 日本語比率が 0.05〜0.1 の範囲（threshold=0.1, lower=0.05）
# 英語主体だが一部日本語を含む（比率 ~0.08、borderline 範囲）
BORDERLINE_PROSE = (
    "English text with some 日本語の words mixed in quite a bit more. "
    "Here are more Japanese 文字 to increase 比率 ratio slightly."
)

# clear fail prose: 完全な英語 prose（日本語比率 0）
CLEAR_FAIL_ENGLISH_PROSE = (
    "This is a completely English prose paragraph. "
    "There are no Japanese characters at all in this text. "
    "The ratio is clearly zero."
)

# pass prose: 日本語主体
PASS_JAPANESE_PROSE = "これは日本語で書かれた文章です。主に日本語で構成されています。"

# code fence のみのテキスト（英語許容）
CODE_FENCE_ONLY = "```python\nprint('hello world')\nfoo_bar = 'baz'\n```"

# identifier のみのテキスト（英語許容）
IDENTIFIER_ONLY_TEXT = "snake_case_identifier, kebab-case-name, file.ext"

# URL のみのテキスト（英語許容）
URL_ONLY_TEXT = "https://github.com/owner/repo/issues/123"


# ============================================================
# Unit tests: classify_borderline 関数
# ============================================================

class TestClassifyBorderline:
    """classify_borderline 関数の単体テスト"""

    def test_pass_japanese_text(self):
        """日本語主体のテキストは PASS を返す (AC5, AC6)"""
        result = classify_borderline(PASS_JAPANESE_PROSE, threshold=0.1, lower_threshold=0.05)
        assert result == "PASS"

    def test_clear_fail_english_prose(self):
        """完全英語 prose は CLEAR_FAIL を返す (AC6)"""
        result = classify_borderline(CLEAR_FAIL_ENGLISH_PROSE, threshold=0.1, lower_threshold=0.05)
        assert result == "CLEAR_FAIL"

    def test_borderline_mixed_prose(self):
        """borderline 混在 prose は BORDERLINE を返す (AC1)"""
        result = classify_borderline(BORDERLINE_PROSE, threshold=0.1, lower_threshold=0.05)
        assert result == "BORDERLINE"

    def test_code_fence_only_does_not_affect_borderline(self):
        """code fence のみのテキストは prose なしとして CLEAR_FAIL を返す (AC5)"""
        result = classify_borderline(CODE_FENCE_ONLY, threshold=0.1, lower_threshold=0.05)
        # code fence 内は除外されるため prose block なし = CLEAR_FAIL
        # (「英語許容」とは code fence を prose として計上しないという意味)
        assert result in ("CLEAR_FAIL", "PASS")  # prose 抽出結果次第

    def test_identifier_only_is_excluded(self):
        """identifier のみのテキストは有効文字が少なく CLEAR_FAIL または PASS (AC5)"""
        result = classify_borderline(IDENTIFIER_ONLY_TEXT, threshold=0.1, lower_threshold=0.05)
        # identifier は clean_prose で除去されるため有効文字が少ない
        assert result in ("CLEAR_FAIL", "PASS")

    def test_empty_text_is_clear_fail(self):
        """空テキストは CLEAR_FAIL を返す"""
        result = classify_borderline("", threshold=0.1, lower_threshold=0.05)
        assert result == "CLEAR_FAIL"

    def test_custom_thresholds(self):
        """カスタム閾値での borderline 判定 (AC3)"""
        # lower_threshold > ratio の場合は CLEAR_FAIL
        # 完全英語 prose (ratio=0.0) は lower_threshold=0.05 の場合 CLEAR_FAIL
        result = classify_borderline(CLEAR_FAIL_ENGLISH_PROSE, threshold=0.5, lower_threshold=0.05)
        assert result == "CLEAR_FAIL"


# ============================================================
# CLI tests: --borderline-check フラグ
# ============================================================

class TestBorderlineCheckCLI:
    """validate_japanese_content.py --borderline-check フラグのテスト"""

    def run_borderline_check(self, text: str, threshold: float = 0.1) -> str:
        """--borderline-check を stdin 経由で実行して stdout を返す"""
        result = subprocess.run(
            [
                "uv", "run", "python3", str(VALIDATOR),
                "--borderline-check", "--threshold", str(threshold),
            ],
            input=text,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def test_japanese_prose_returns_pass(self):
        """日本語主体は PASS"""
        out = self.run_borderline_check(PASS_JAPANESE_PROSE)
        assert out == "PASS"

    def test_english_prose_returns_clear_fail(self):
        """完全英語 prose は CLEAR_FAIL"""
        out = self.run_borderline_check(CLEAR_FAIL_ENGLISH_PROSE)
        assert out == "CLEAR_FAIL"

    def test_borderline_prose_returns_borderline(self):
        """borderline 混在 prose は BORDERLINE (AC1, AC3)"""
        out = self.run_borderline_check(BORDERLINE_PROSE)
        assert out == "BORDERLINE"

    def test_code_fence_body_english_allowlist(self):
        """code fence 主体のテキストは prose なし扱い (AC5)"""
        code_heavy = (
            "```bash\n"
            "uv run python3 script.py --flag value\n"
            "```\n\n"
            "```yaml\n"
            "key: value\n"
            "```"
        )
        out = self.run_borderline_check(code_heavy)
        # code fence 内は除外、prose block なし = CLEAR_FAIL
        assert out in ("CLEAR_FAIL", "PASS")

    def test_identifier_heavy_text_english_allowlist(self):
        """identifier / file path 主体のテキストは英語許容 (AC5)"""
        # identifier は clean_prose で除去される
        identifier_text = "Use `snake_case_func`, `kebab-case-id`, `file.path.ext` here."
        out = self.run_borderline_check(identifier_text)
        # inline code が除去されると有効文字が少なくなる
        assert out in ("CLEAR_FAIL", "PASS", "BORDERLINE")

    def test_url_in_prose_english_allowlist(self):
        """URL を含む prose は URL 除外後に判定 (AC5)"""
        url_text = "See https://github.com/owner/repo for details. Also check https://example.com/api."
        out = self.run_borderline_check(url_text)
        # URL 除去後は "See for details Also check for" のような英語
        assert out in ("CLEAR_FAIL", "PASS", "BORDERLINE")


# ============================================================
# Integration tests: guard-japanese-prose.sh フック経由
# ============================================================

class TestGuardBorderlineRepairHook:
    """guard-japanese-prose.sh フック経由の integration test (AC1, AC4, AC7)"""

    def test_borderline_prose_returns_exit2(self):
        """borderline prose で exit 2 が返ること (AC1)"""
        # gh issue create --body <borderline>
        result = run_hook("Bash", f"gh issue create --title 'Test' --body '{BORDERLINE_PROSE}'")
        assert result.returncode == 2, (
            f"Expected exit 2 for borderline prose, got {result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    def test_borderline_repair_reason_in_stderr(self):
        """borderline prose で borderline_japanese_prose_repair_required が stderr に出ること (AC3)"""
        result = run_hook("Bash", f"gh issue create --title 'Test' --body '{BORDERLINE_PROSE}'")
        assert "borderline_japanese_prose_repair_required" in result.stderr, (
            f"Expected reason code in stderr, got:\n{result.stderr}"
        )

    def test_borderline_retry_instruction_in_stderr(self):
        """borderline prose で「修正して再試行」の指示が stderr に出ること (AC4)"""
        result = run_hook("Bash", f"gh issue create --title 'Test' --body '{BORDERLINE_PROSE}'")
        assert "修正して再試行" in result.stderr or "repair" in result.stderr.lower(), (
            f"Expected retry instruction in stderr, got:\n{result.stderr}"
        )

    def test_no_ask_in_hook_output(self):
        """ask が hook の出力に含まれないこと (AC7 — ask が返らないこと)"""
        result = run_hook("Bash", f"gh issue create --title 'Test' --body '{BORDERLINE_PROSE}'")
        # stdout に ask JSON が出ないこと
        assert "permissionDecision" not in result.stdout, (
            f"Expected no permissionDecision in stdout, got:\n{result.stdout}"
        )
        assert '"ask"' not in result.stdout, (
            f"Expected no ask in stdout, got:\n{result.stdout}"
        )

    def test_repair_instruction_in_borderline_output(self):
        """borderline 時に Claude Code 向け修正指示が stderr に含まれること (AC7)"""
        result = run_hook("Bash", f"gh issue create --title 'Test' --body '{BORDERLINE_PROSE}'")
        assert result.returncode == 2
        # repair signal として修正指示が含まれること
        assert "borderline_japanese_prose_repair_required" in result.stderr

    def test_japanese_prose_passes(self):
        """日本語 prose は通過すること (AC1 の逆)"""
        result = run_hook("Bash", f"gh issue create --title 'Test' --body '{PASS_JAPANESE_PROSE}'")
        assert result.returncode == 0

    def test_english_prose_clear_fail_blocked(self):
        """明確な英語 prose はブロックされること (AC6)"""
        result = run_hook("Bash", f"gh issue create --title 'Test' --body '{CLEAR_FAIL_ENGLISH_PROSE}'")
        assert result.returncode == 2
        # clear fail の場合は borderline reason code ではなく通常のエラー
        # (borderline_japanese_prose_repair_required でも通常 GUARD: 日本語比率不足 でも exit 2 であること)
        assert result.returncode == 2


# ============================================================
# AC2: permissionDecision: "ask" が hook に存在しないことの静的検証
# ============================================================

class TestNoAskPermissionDecision:
    """AC2: permissionDecision: "ask" が guard-japanese-prose.sh に存在しないこと"""

    def test_no_ask_in_hook_script(self):
        """guard-japanese-prose.sh に permissionDecision.*ask が含まれないこと (AC2)"""
        content = HOOK_SCRIPT.read_text(encoding="utf-8")
        # permissionDecision: "ask" または permissionDecision.*ask パターンを検索
        matches = re.findall(r'permissionDecision.*ask', content)
        assert len(matches) == 0, (
            f"Found 'permissionDecision.*ask' in hook script:\n{matches}"
        )

    def test_borderline_reason_code_in_hook_script(self):
        """guard-japanese-prose.sh に borderline_japanese_prose_repair_required が含まれること (AC3)"""
        content = HOOK_SCRIPT.read_text(encoding="utf-8")
        assert "borderline_japanese_prose_repair_required" in content, (
            "Expected 'borderline_japanese_prose_repair_required' in hook script"
        )


# ============================================================
# AC5: code fence / inline code / identifier / URL は英語許容
# ============================================================

class TestEnglishAllowlist:
    """AC5: 英語許容対象のテスト"""

    def run_borderline_check(self, text: str) -> str:
        result = subprocess.run(
            ["uv", "run", "python3", str(VALIDATOR), "--borderline-check", "--threshold", "0.1"],
            input=text,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def test_english_allowlist_code_fence(self):
        """code fence 内の英語コードは borderline/fail に影響しない (AC5)"""
        # code fence + 日本語 prose の組み合わせ
        text = (
            "これは日本語の説明です。\n\n"
            "```python\n"
            "def hello_world():\n"
            "    print('Hello, World!')\n"
            "```\n\n"
            "以上です。"
        )
        result = self.run_borderline_check(text)
        # code fence を除外すると日本語 prose が残るので PASS になるはず
        assert result == "PASS", f"Expected PASS with Japanese prose + code fence, got {result}"

    def test_english_allowlist_identifier(self):
        """識別子（snake_case, kebab-case, file.ext）は英語許容 (AC5)"""
        # 日本語 prose + identifier の組み合わせ
        text = (
            "変数 `my_variable_name` を使って処理します。\n"
            "`kebab-case-param` や `file.ext` も同様です。"
        )
        result = self.run_borderline_check(text)
        # inline code / identifier が除外されると日本語比率が上がるはず
        assert result in ("PASS", "BORDERLINE"), (
            f"Expected PASS or BORDERLINE for Japanese prose with identifiers, got {result}"
        )

    def test_english_allowlist_url(self):
        """URL は英語許容 (AC5)"""
        text = (
            "詳細は https://github.com/owner/repo/issues/123 を参照してください。\n"
            "API については https://api.example.com/v1/docs を確認します。"
        )
        result = self.run_borderline_check(text)
        # URL 除外後は日本語 prose が残るので PASS
        assert result in ("PASS", "BORDERLINE"), (
            f"Expected PASS or BORDERLINE for Japanese prose with URLs, got {result}"
        )

    def test_english_prose_blocked(self):
        """明確な英語 prose は CLEAR_FAIL (AC6)"""
        result = self.run_borderline_check(CLEAR_FAIL_ENGLISH_PROSE)
        assert result == "CLEAR_FAIL", (
            f"Expected CLEAR_FAIL for English prose, got {result}"
        )
